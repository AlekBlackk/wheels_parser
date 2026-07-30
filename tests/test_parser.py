import unittest
from datetime import timedelta
from unittest.mock import Mock, patch

import requests

from wheelsparser import alerts, config, parser, registry, urls


def make_message(message_id, text, links):
    """Сообщение в том виде, в каком его отдаёт fetch_channel."""
    return {
        "id": message_id,
        "text": text,
        "preview_html": text,
        "urls": links,
        "hash": urls.message_content_hash(text, links),
        "legacy_hash": urls.message_content_hash(text, links),
        "message_url": f"https://t.me/{message_id}",
    }


class FetchChannelTests(unittest.TestCase):
    def test_extracts_message_data(self):
        response = Mock(status_code=200)
        response.raise_for_status.return_value = None
        # Именно content: fetch_channel читает байты, чтобы BeautifulSoup
        # сам определил кодировку страницы.
        response.content = """
        <div class="tgme_widget_message_wrap">
          <div class="tgme_widget_message" data-post="demo/42">
            <div class="tgme_widget_message_text">
              Новое колесо: <a href="https://betboom.ru/freestream/abc">ссылка</a>
            </div>
          </div>
        </div>
        """.encode()

        with patch.object(parser.PARSER_SESSION, "get", return_value=response) as get:
            messages = parser.fetch_channel("demo")

        get.assert_called_once_with(
            "https://t.me/s/demo", timeout=config.REQUEST_TIMEOUT
        )
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["id"], "demo/42")
        self.assertEqual(messages[0]["message_url"], "https://t.me/demo/42")
        self.assertEqual(messages[0]["urls"], ["https://betboom.ru/freestream/abc"])
        self.assertIn("Новое колесо", messages[0]["text"])

    def test_returns_none_for_not_found(self):
        response = Mock(status_code=404)
        with patch.object(parser.PARSER_SESSION, "get", return_value=response):
            self.assertIsNone(parser.fetch_channel("missing"))

    def test_returns_none_for_http_error(self):
        response = Mock(status_code=500)
        response.raise_for_status.side_effect = requests.HTTPError("server error")
        with patch.object(parser.PARSER_SESSION, "get", return_value=response):
            self.assertIsNone(parser.fetch_channel("broken"))

    def test_returns_none_for_network_error(self):
        with patch.object(
            parser.PARSER_SESSION,
            "get",
            side_effect=requests.Timeout("connection timed out"),
        ):
            self.assertIsNone(parser.fetch_channel("offline"))


class ProcessMessageTests(unittest.TestCase):
    """process_message без сети: precheck и отправка замоканы."""

    def _start(self, patcher):
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    def setUp(self):
        # Кулдаун глобален для процесса — изолируем тесты друг от друга.
        self._start(patch.dict(alerts.LAST_URL_ALERT, clear=True))
        self._start(patch.object(parser, "precheck_wheel", return_value=("active", False)))
        self.single = self._start(
            patch.object(parser, "send_telegram_notification", return_value=True)
        )
        self.multi = self._start(
            patch.object(parser, "send_multi_telegram_notification", return_value=True)
        )
        self.now = parser.now_msk()

    def process(self, message, channel_seen, baseline=False):
        return parser.process_message(
            message, "demo", channel_seen, baseline, self.now, {}
        )

    def test_new_message_with_link_is_notified_once(self):
        message = make_message("demo/1", "колесо", ["https://betboom.ru/freestream/a"])
        channel_seen = {}

        first = self.process(message, channel_seen)
        second = self.process(message, channel_seen)

        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.single.assert_called_once()

    def test_baseline_cycle_records_hash_without_notifying(self):
        message = make_message("demo/1", "колесо", ["https://betboom.ru/freestream/a"])
        channel_seen = {}

        self.assertEqual(self.process(message, channel_seen, baseline=True), [])
        self.single.assert_not_called()
        self.assertEqual(channel_seen["demo/1"], message["hash"])

    def test_edited_message_with_new_link_is_notified_as_edit(self):
        original = make_message("demo/1", "колесо", ["https://betboom.ru/freestream/a"])
        edited = make_message("demo/1", "колесо", ["https://betboom.ru/freestream/b"])
        channel_seen = {}

        self.process(original, channel_seen)
        entries = self.process(edited, channel_seen)

        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0]["edited"])

    def test_legacy_hash_match_is_not_treated_as_edit(self):
        message = make_message("demo/1", "колесо", ["https://betboom.ru/freestream/a"])
        # Состояние, записанное старой версией парсера: хэш из legacy-формата.
        channel_seen = {"demo/1": message["legacy_hash"]}
        message["hash"] = "0" * 16

        self.assertEqual(self.process(message, channel_seen), [])
        self.single.assert_not_called()

    def test_two_links_in_one_post_produce_single_multi_notification(self):
        message = make_message(
            "demo/1",
            "колесо",
            [
                "https://betboom.ru/freestream/a",
                "https://betboom.ru/freestream/b",
            ],
        )

        entries = self.process(message, {})

        self.assertEqual(len(entries), 2)
        self.multi.assert_called_once()
        self.single.assert_not_called()

    def test_expired_wheel_is_not_notified(self):
        message = make_message("demo/1", "колесо", ["https://betboom.ru/freestream/a"])
        with patch.object(parser, "precheck_wheel", return_value=("expired", False)):
            self.assertEqual(self.process(message, {}), [])
        self.single.assert_not_called()

    def test_keywords_are_checked_only_for_messages_without_links(self):
        with patch.object(registry, "KEYWORDS", ["колесо"]), \
             patch.object(parser, "send_keyword_notification") as notify:
            self.process(make_message("demo/1", "будет колесо", []), {})
            notify.assert_called_once()

            notify.reset_mock()
            self.process(
                make_message("demo/2", "колесо", ["https://betboom.ru/freestream/a"]),
                {},
            )
            notify.assert_not_called()


class RetryFailedNotificationsTests(unittest.TestCase):
    """Пост обрабатывается по хэшу один раз — сбой отправки без ретрая
    терял бы находку навсегда, поэтому retry_failed_notifications должен
    подбирать записи с notified=False на следующих циклах."""

    def _start(self, patcher):
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    def setUp(self):
        self._start(patch.object(parser, "notifications_enabled", return_value=True))
        self.now = parser.now_msk()

    def entry(self, url="https://betboom.ru/freestream/a", notified=False, age_minutes=1):
        found_at = (self.now - timedelta(minutes=age_minutes)).isoformat(timespec="seconds")
        return {"url": url, "found_at": found_at, "channel": "demo", "notified": notified}

    def test_retries_and_marks_notified_on_success(self):
        send = self._start(
            patch.object(parser, "send_telegram_notification", return_value=True)
        )
        results = [self.entry()]

        retried = parser.retry_failed_notifications(results, self.now)

        self.assertEqual(retried, 1)
        self.assertTrue(results[0]["notified"])
        send.assert_called_once()

    def test_already_notified_entries_are_skipped(self):
        send = self._start(patch.object(parser, "send_telegram_notification"))
        results = [self.entry(notified=True)]

        retried = parser.retry_failed_notifications(results, self.now)

        self.assertEqual(retried, 0)
        send.assert_not_called()

    def test_entries_older_than_window_are_not_retried(self):
        send = self._start(patch.object(parser, "send_telegram_notification"))
        results = [self.entry(age_minutes=config.NOTIFY_RETRY_WINDOW_MINUTES + 1)]

        retried = parser.retry_failed_notifications(results, self.now)

        self.assertEqual(retried, 0)
        send.assert_not_called()

    def test_stays_false_when_retry_also_fails(self):
        self._start(patch.object(parser, "send_telegram_notification", return_value=False))
        results = [self.entry()]

        parser.retry_failed_notifications(results, self.now)

        self.assertFalse(results[0]["notified"])

    def test_entries_with_unknown_delivery_are_not_retried(self):
        # Telegram мог принять сообщение и не донести ответ — повтор
        # такой отправки рассылает дубликат.
        send = self._start(patch.object(parser, "send_telegram_notification"))
        entry = self.entry()
        entry["delivery_unknown"] = True
        results = [entry]

        retried = parser.retry_failed_notifications(results, self.now)

        self.assertEqual(retried, 0)
        send.assert_not_called()

    def test_respects_max_per_cycle_limit(self):
        send = self._start(
            patch.object(parser, "send_telegram_notification", return_value=True)
        )
        results = [
            self.entry(url=f"https://betboom.ru/freestream/{i}")
            for i in range(config.NOTIFY_RETRY_MAX_PER_CYCLE + 3)
        ]

        retried = parser.retry_failed_notifications(results, self.now)

        self.assertEqual(retried, config.NOTIFY_RETRY_MAX_PER_CYCLE)
        self.assertEqual(send.call_count, config.NOTIFY_RETRY_MAX_PER_CYCLE)

    def test_noop_when_notifications_disabled(self):
        with patch.object(parser, "notifications_enabled", return_value=False):
            send = self._start(patch.object(parser, "send_telegram_notification"))
            results = [self.entry()]

            retried = parser.retry_failed_notifications(results, self.now)

        self.assertEqual(retried, 0)
        send.assert_not_called()


class EmptyChannelDetectionTests(unittest.TestCase):
    """Страница канала отдалась, но постов в ней нет.

    Единственный отказ, который иначе не виден: канал засчитывается
    успешным, в логе «каналов N/N», новых ссылок ноль — и так до тех пор,
    пока кто-нибудь не заметит, что колёса перестали приходить.
    """

    def _start(self, patcher):
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    def setUp(self):
        self._start(patch.dict(parser.CHANNEL_EMPTY_STREAK, clear=True))
        self._start(patch.object(parser, "CHANNEL_EMPTY_ALERTED", set()))
        self._start(patch.object(parser, "LAYOUT_ALERTED", False))
        self._start(patch.object(parser, "CHANNEL_EMPTY_THRESHOLD", 3))
        self.notify = self._start(patch.object(parser, "send_service_notification"))

    def run_cycles(self, checked, failed, empty, times=1):
        for _ in range(times):
            parser.update_channel_empty_streaks(checked, failed, empty)
            parser.report_empty_channels(checked, failed)

    def test_streak_grows_and_alerts_only_at_threshold(self):
        with patch.object(registry, "CHANNELS", ["a", "b"]):
            self.run_cycles(["a", "b"], [], ["a"], times=2)
            self.notify.assert_not_called()

            self.run_cycles(["a", "b"], [], ["a"])

        self.notify.assert_called_once()
        self.assertIn("@a", self.notify.call_args.args[0])

    def test_alert_is_sent_once_per_series(self):
        with patch.object(registry, "CHANNELS", ["a", "b"]):
            self.run_cycles(["a", "b"], [], ["a"], times=5)

        self.notify.assert_called_once()

    def test_posts_reset_streak_and_allow_new_alert(self):
        with patch.object(registry, "CHANNELS", ["a", "b"]):
            self.run_cycles(["a", "b"], [], ["a"], times=3)
            self.assertEqual(self.notify.call_count, 1)

            self.run_cycles(["a", "b"], [], [])  # канал снова отдал посты
            self.assertEqual(parser.CHANNEL_EMPTY_STREAK.get("a", 0), 0)

            self.run_cycles(["a", "b"], [], ["a"], times=3)

        self.assertEqual(self.notify.call_count, 2)

    def test_unreachable_channel_is_not_counted_as_empty(self):
        """Недоступность — забота fail-streak, смешивать счётчики нельзя."""
        with patch.object(registry, "CHANNELS", ["a", "b"]):
            self.run_cycles(["a", "b"], ["a"], [], times=5)

        self.notify.assert_not_called()
        self.assertNotIn("a", parser.CHANNEL_EMPTY_STREAK)

    def test_all_channels_empty_reports_layout_change_once(self):
        with patch.object(registry, "CHANNELS", ["a", "b", "c"]):
            self.run_cycles(["a", "b", "c"], [], ["a", "b", "c"], times=4)

        # Одно сообщение про вёрстку, а не по одному на каждый канал.
        self.notify.assert_called_once()
        message = self.notify.call_args.args[0]
        self.assertIn("вёрстка t.me/s", message)

    def test_layout_alert_repeats_after_recovery(self):
        with patch.object(registry, "CHANNELS", ["a", "b"]):
            self.run_cycles(["a", "b"], [], ["a", "b"], times=3)
            self.assertEqual(self.notify.call_count, 1)

            self.run_cycles(["a", "b"], [], [])  # разбор починился
            self.run_cycles(["a", "b"], [], ["a", "b"], times=3)

        self.assertEqual(self.notify.call_count, 2)

    def test_single_channel_setup_reports_channel_not_layout(self):
        """С одним каналом «сломалась вёрстка» и «канал опустел» неразличимы."""
        with patch.object(registry, "CHANNELS", ["a"]):
            self.run_cycles(["a"], [], ["a"], times=3)

        self.notify.assert_called_once()
        self.assertIn("@a", self.notify.call_args.args[0])

    def test_streaks_of_removed_channels_are_dropped(self):
        with patch.object(registry, "CHANNELS", ["a", "b"]):
            self.run_cycles(["a", "b"], [], ["a"], times=2)
        with patch.object(registry, "CHANNELS", ["b"]):  # /remove a
            self.run_cycles(["b"], [], [])

        self.assertNotIn("a", parser.CHANNEL_EMPTY_STREAK)

    def test_process_cycle_counts_channel_without_posts_as_empty(self):
        seen: dict[str, dict[str, str]] = {}
        with patch.dict(parser.CHANNEL_EMPTY_STREAK, clear=True), \
             patch.object(registry, "CHANNELS", ["a", "b"]), \
             patch.object(parser, "fetch_channel", return_value=[]), \
             patch.object(parser, "save_seen"), \
             patch.object(parser, "save_results"):
            parser.process_cycle(seen, [], baseline=True)

            self.assertEqual(parser.CHANNEL_EMPTY_STREAK, {"a": 1, "b": 1})

    def test_process_cycle_does_not_count_unreachable_channel_as_empty(self):
        seen: dict[str, dict[str, str]] = {}
        with patch.dict(parser.CHANNEL_EMPTY_STREAK, clear=True), \
             patch.object(registry, "CHANNELS", ["a"]), \
             patch.object(parser, "fetch_channel", return_value=None), \
             patch.object(parser, "save_seen"), \
             patch.object(parser, "save_results"):
            parser.process_cycle(seen, [], baseline=True)

            self.assertEqual(parser.CHANNEL_EMPTY_STREAK, {})


class ProcessCycleTests(unittest.TestCase):
    def test_same_url_from_two_new_messages_is_saved_once(self):
        first = make_message("demo/2", "колесо", ["https://betboom.ru/freestream/same"])
        second = make_message("demo/3", "колесо", ["https://betboom.ru/freestream/same"])
        seen: dict[str, dict[str, str]] = {"demo": {}}
        results: list[dict] = []

        with patch.dict(alerts.LAST_URL_ALERT, clear=True), \
             patch.object(registry, "CHANNELS", ["demo"]), \
             patch.object(parser, "precheck_wheel", return_value=("active", False)), \
             patch.object(parser, "fetch_channel", side_effect=[[first], [second]]), \
             patch.object(parser, "send_telegram_notification", return_value=True), \
             patch.object(parser, "save_seen"), \
             patch.object(parser, "save_results"):
            parser.process_cycle(seen, results, baseline=True)
            parser.process_cycle(seen, results, baseline=False)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["url"], "https://betboom.ru/freestream/same")


if __name__ == "__main__":
    unittest.main()
