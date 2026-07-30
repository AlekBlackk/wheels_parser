import unittest
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
        self._start(patch.object(parser, "precheck_wheel_status", return_value="active"))
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
        with patch.object(parser, "precheck_wheel_status", return_value="expired"):
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


class ProcessCycleTests(unittest.TestCase):
    def test_same_url_from_two_new_messages_is_saved_once(self):
        first = make_message("demo/2", "колесо", ["https://betboom.ru/freestream/same"])
        second = make_message("demo/3", "колесо", ["https://betboom.ru/freestream/same"])
        seen: dict[str, dict[str, str]] = {"demo": {}}
        results: list[dict] = []

        with patch.dict(alerts.LAST_URL_ALERT, clear=True), \
             patch.object(registry, "CHANNELS", ["demo"]), \
             patch.object(parser, "precheck_wheel_status", return_value="active"), \
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
