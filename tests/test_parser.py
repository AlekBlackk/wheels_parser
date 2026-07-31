import unittest
from datetime import timedelta
from unittest.mock import Mock, patch

import requests

from tests.dbfixture import entries_since, use_temp_db
from wheelsparser import alerts, config, db, parser, registry, urls


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
        self._start(patch.object(parser, "precheck_wheel", return_value=("active", False, "")))
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

    def test_post_text_is_used_as_referral_signal_for_single_link(self):
        message = make_message(
            "demo/1", "Колесо для рефов 🔥", ["https://betboom.ru/freestream/a"]
        )
        with patch.object(
            parser, "precheck_wheel", return_value=("active", True, "")
        ) as precheck:
            entries = self.process(message, {})

        self.assertEqual(precheck.call_args.kwargs["post_text"], "Колесо для рефов 🔥")
        self.assertTrue(entries[0]["referral"])

    def test_post_text_is_not_used_when_post_has_several_links(self):
        # «для рефов» относится к одному из колёс — к какому, неизвестно,
        # поэтому текст поста как сигнал не используется.
        message = make_message(
            "demo/1",
            "Колесо для рефов 🔥",
            [
                "https://betboom.ru/freestream/a",
                "https://betboom.ru/freestream/b",
            ],
        )
        with patch.object(
            parser, "precheck_wheel", return_value=("active", False, "")
        ) as precheck:
            self.process(message, {})

        self.assertEqual(precheck.call_args.kwargs["post_text"], "")

    def test_expired_wheel_is_not_notified(self):
        message = make_message("demo/1", "колесо", ["https://betboom.ru/freestream/a"])
        with patch.object(parser, "precheck_wheel", return_value=("expired", False, "")):
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

    def test_failed_keyword_notification_is_recorded_for_retry(self):
        # Пост обрабатывается по хэшу один раз: без записи в истории
        # находка по ключевому слову терялась бы навсегда.
        with patch.object(registry, "KEYWORDS", ["колесо"]), \
             patch.object(parser, "send_keyword_notification", return_value=False):
            entries = self.process(make_message("demo/1", "будет колесо", []), {})

        self.assertEqual(len(entries), 1)
        self.assertFalse(entries[0]["notified"])
        self.assertEqual(entries[0]["keywords"], ["колесо"])
        # Без url: это не колесо, и в /wheels, /status, /active запись не идёт.
        self.assertNotIn("url", entries[0])

    def test_delivered_keyword_notification_is_marked_notified(self):
        with patch.object(registry, "KEYWORDS", ["колесо"]), \
             patch.object(parser, "send_keyword_notification", return_value=True):
            entries = self.process(make_message("demo/1", "будет колесо", []), {})

        self.assertTrue(entries[0]["notified"])


class RetryFailedNotificationsTests(unittest.TestCase):
    """Пост обрабатывается по хэшу один раз — сбой отправки без ретрая
    терял бы находку навсегда, поэтому retry_failed_notifications должен
    подбирать из базы записи с notified=0 на следующих циклах."""

    def _start(self, patcher):
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    def setUp(self):
        use_temp_db(self)
        self._start(patch.object(parser, "notifications_enabled", return_value=True))
        self.now = parser.now_msk()

    def store(self, url="https://betboom.ru/freestream/a", notified=False,
              age_minutes=1, **overrides):
        """Кладёт запись в базу и возвращает её (с проставленным id)."""
        entry = {
            "url": url,
            "found_at": (self.now - timedelta(minutes=age_minutes)).isoformat(
                timespec="seconds"
            ),
            "channel": "demo",
            "notified": notified,
        }
        entry.update(overrides)
        db.insert_entries([entry])
        return entry

    def pending_urls(self):
        window = self.now - timedelta(minutes=config.NOTIFY_RETRY_WINDOW_MINUTES)
        return [item.get("url") for item in db.pending_retry(window, 100)]

    def test_retries_and_marks_notified_on_success(self):
        send = self._start(
            patch.object(parser, "send_telegram_notification", return_value=True)
        )
        self.store()

        retried = parser.retry_failed_notifications(self.now)

        self.assertEqual(retried, 1)
        send.assert_called_once()

    def test_successful_retry_is_persisted_and_not_repeated(self):
        # Без записи в базу следующий цикл отправил бы то же уведомление снова.
        self._start(
            patch.object(parser, "send_telegram_notification", return_value=True)
        )
        self.store()

        parser.retry_failed_notifications(self.now)

        self.assertEqual(self.pending_urls(), [])

    def test_already_notified_entries_are_skipped(self):
        send = self._start(patch.object(parser, "send_telegram_notification"))
        self.store(notified=True)

        retried = parser.retry_failed_notifications(self.now)

        self.assertEqual(retried, 0)
        send.assert_not_called()

    def test_entries_older_than_window_are_not_retried(self):
        send = self._start(patch.object(parser, "send_telegram_notification"))
        self.store(age_minutes=config.NOTIFY_RETRY_WINDOW_MINUTES + 1)

        retried = parser.retry_failed_notifications(self.now)

        self.assertEqual(retried, 0)
        send.assert_not_called()

    def test_stays_pending_when_retry_also_fails(self):
        self._start(patch.object(parser, "send_telegram_notification", return_value=False))
        self.store()

        parser.retry_failed_notifications(self.now)

        self.assertEqual(self.pending_urls(), ["https://betboom.ru/freestream/a"])

    def test_entries_with_unknown_delivery_are_not_retried(self):
        # Telegram мог принять сообщение и не донести ответ — повтор
        # такой отправки рассылает дубликат.
        send = self._start(patch.object(parser, "send_telegram_notification"))
        self.store(delivery_unknown=True)

        retried = parser.retry_failed_notifications(self.now)

        self.assertEqual(retried, 0)
        send.assert_not_called()

    def test_unknown_delivery_during_retry_stops_further_attempts(self):
        # Отправка могла дойти, а ответ — нет: флаг должен попасть в базу,
        # иначе следующий цикл продублирует сообщение.
        def mark_unknown(entry, *_args, **_kwargs):
            entry["delivery_unknown"] = True
            return False

        self._start(
            patch.object(parser, "send_telegram_notification", side_effect=mark_unknown)
        )
        self.store()

        parser.retry_failed_notifications(self.now)

        self.assertEqual(self.pending_urls(), [])

    def test_respects_max_per_cycle_limit(self):
        send = self._start(
            patch.object(parser, "send_telegram_notification", return_value=True)
        )
        for index in range(config.NOTIFY_RETRY_MAX_PER_CYCLE + 3):
            self.store(url=f"https://betboom.ru/freestream/{index}")

        retried = parser.retry_failed_notifications(self.now)

        self.assertEqual(retried, config.NOTIFY_RETRY_MAX_PER_CYCLE)
        self.assertEqual(send.call_count, config.NOTIFY_RETRY_MAX_PER_CYCLE)

    def test_retries_keyword_notification_without_url(self):
        # У находок по ключевым словам url нет — ретрай узнаёт их по
        # keywords и шлёт своим отправителем.
        keyword_send = self._start(
            patch.object(parser, "send_keyword_notification", return_value=True)
        )
        link_send = self._start(patch.object(parser, "send_telegram_notification"))
        self.store(
            url="",
            keywords=["колесо"],
            message_url="https://t.me/demo/1",
        )

        retried = parser.retry_failed_notifications(self.now)

        self.assertEqual(retried, 1)
        self.assertEqual(self.pending_urls(), [])
        keyword_send.assert_called_once()
        link_send.assert_not_called()

    def test_entries_without_url_and_keywords_are_skipped(self):
        keyword_send = self._start(patch.object(parser, "send_keyword_notification"))
        link_send = self._start(patch.object(parser, "send_telegram_notification"))
        self.store(url="")

        retried = parser.retry_failed_notifications(self.now)

        self.assertEqual(retried, 0)
        keyword_send.assert_not_called()
        link_send.assert_not_called()

    def test_noop_when_notifications_disabled(self):
        with patch.object(parser, "notifications_enabled", return_value=False):
            send = self._start(patch.object(parser, "send_telegram_notification"))
            self.store()

            retried = parser.retry_failed_notifications(self.now)

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
        use_temp_db(self)
        seen: dict[str, dict[str, str]] = {}
        with patch.dict(parser.CHANNEL_EMPTY_STREAK, clear=True), \
             patch.object(registry, "CHANNELS", ["a", "b"]), \
             patch.object(parser, "fetch_channel", return_value=[]), \
             patch.object(parser, "save_seen"):
            parser.process_cycle(seen, baseline=True)

            self.assertEqual(parser.CHANNEL_EMPTY_STREAK, {"a": 1, "b": 1})

    def test_process_cycle_does_not_count_unreachable_channel_as_empty(self):
        use_temp_db(self)
        seen: dict[str, dict[str, str]] = {}
        with patch.dict(parser.CHANNEL_EMPTY_STREAK, clear=True), \
             patch.object(registry, "CHANNELS", ["a"]), \
             patch.object(parser, "fetch_channel", return_value=None), \
             patch.object(parser, "save_seen"):
            parser.process_cycle(seen, baseline=True)

            self.assertEqual(parser.CHANNEL_EMPTY_STREAK, {})


class ProcessCycleTests(unittest.TestCase):
    def setUp(self):
        use_temp_db(self)
        self.now = parser.now_msk()

    def stored(self):
        return entries_since(self.now - timedelta(hours=1))

    def test_found_wheel_is_written_to_the_database(self):
        message = make_message(
            "demo/1", "колесо", ["https://betboom.ru/freestream/new"]
        )

        with patch.dict(alerts.LAST_URL_ALERT, clear=True), \
             patch.object(registry, "CHANNELS", ["demo"]), \
             patch.object(parser, "precheck_wheel", return_value=("active", False, "")), \
             patch.object(parser, "fetch_channel", return_value=[message]), \
             patch.object(parser, "send_telegram_notification", return_value=True), \
             patch.object(parser, "save_seen"):
            # Непустой seen: у канала уже есть история, значит «тихий»
            # первый цикл ему не положен и уведомление уходит сразу.
            parser.process_cycle({"demo": {"demo/0": "hash"}}, baseline=False)

        (entry,) = self.stored()
        self.assertEqual(entry["url"], "https://betboom.ru/freestream/new")
        self.assertEqual(entry["channel"], "demo")
        self.assertTrue(entry["notified"])

    def test_same_url_from_two_new_messages_is_saved_once(self):
        first = make_message("demo/2", "колесо", ["https://betboom.ru/freestream/same"])
        second = make_message("demo/3", "колесо", ["https://betboom.ru/freestream/same"])
        seen: dict[str, dict[str, str]] = {"demo": {}}

        with patch.dict(alerts.LAST_URL_ALERT, clear=True), \
             patch.object(registry, "CHANNELS", ["demo"]), \
             patch.object(parser, "precheck_wheel", return_value=("active", False, "")), \
             patch.object(parser, "fetch_channel", side_effect=[[first], [second]]), \
             patch.object(parser, "send_telegram_notification", return_value=True), \
             patch.object(parser, "save_seen"):
            parser.process_cycle(seen, baseline=True)
            parser.process_cycle(seen, baseline=False)

        stored = self.stored()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["url"], "https://betboom.ru/freestream/same")

    def test_cooldown_survives_restart_because_history_is_in_the_database(self):
        # Кулдаун восстанавливается запросом к базе, а не из списка в
        # памяти: после рестарта та же ссылка не должна уйти повторно.
        message = make_message(
            "demo/9", "колесо", ["https://betboom.ru/freestream/again"]
        )
        db.insert_entries([{
            "url": "https://betboom.ru/freestream/again",
            "found_at": (self.now - timedelta(minutes=1)).isoformat(timespec="seconds"),
            "channel": "demo",
            "notified": True,
        }])

        with patch.dict(alerts.LAST_URL_ALERT, clear=True), \
             patch.object(registry, "CHANNELS", ["demo"]), \
             patch.object(parser, "precheck_wheel", return_value=("active", False, "")), \
             patch.object(parser, "fetch_channel", return_value=[message]), \
             patch.object(parser, "send_telegram_notification", return_value=True) as send, \
             patch.object(parser, "save_seen"):
            parser.process_cycle({"demo": {}}, baseline=False)

        send.assert_not_called()
        self.assertEqual(len(self.stored()), 1)

    def test_twitch_findings_from_the_queue_are_stored(self):
        parser.TWITCH_NEW_ENTRIES.put({
            "url": "https://betboom.ru/freestream/twitch",
            "found_at": self.now.isoformat(timespec="seconds"),
            "channel": "streamer",
            "source": "twitch",
            "notified": True,
        })

        with patch.object(registry, "CHANNELS", []), \
             patch.object(parser, "save_seen"):
            parser.process_cycle({}, baseline=False)

        (entry,) = self.stored()
        self.assertEqual(entry["source"], "twitch")

    def test_cycle_trims_history_to_max_results(self):
        for index in range(3):
            db.insert_entries([{
                "url": f"https://betboom.ru/freestream/{index}",
                "found_at": (self.now - timedelta(minutes=10 - index)).isoformat(
                    timespec="seconds"
                ),
                "channel": "demo",
                "notified": True,
            }])
        # Обрезка выполняется в циклах с находкой: новая приходит из Twitch.
        parser.TWITCH_NEW_ENTRIES.put({
            "url": "https://betboom.ru/freestream/fresh",
            "found_at": self.now.isoformat(timespec="seconds"),
            "channel": "streamer",
            "source": "twitch",
            "notified": True,
        })

        with patch.object(registry, "CHANNELS", []), \
             patch.object(parser, "MAX_RESULTS", 2), \
             patch.object(parser, "save_seen"):
            parser.process_cycle({}, baseline=False)

        self.assertEqual(
            [entry["url"] for entry in self.stored()],
            ["https://betboom.ru/freestream/2", "https://betboom.ru/freestream/fresh"],
        )


if __name__ == "__main__":
    unittest.main()
