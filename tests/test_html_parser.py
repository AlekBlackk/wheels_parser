import unittest
from unittest.mock import Mock, patch

import requests
from bs4 import BeautifulSoup

import betboom_web_parser as parser


class UrlParsingTests(unittest.TestCase):
    def test_finds_urls_in_text_and_html_and_deduplicates_normalized_values(self):
        html = BeautifulSoup(
            """
            <div>
                Текст https://www.betboom.ru/freestream/demo#post.
                <a href="https://betboom.ru/freestream/demo#fragment">колесо</a>
                <a href="https://betboom.ru/freestream/other">второе</a>
            </div>
            """,
            "html.parser",
        )

        urls = parser.find_urls(html, html.get_text(" ", strip=True))

        self.assertEqual(
            urls,
            [
                "https://betboom.ru/freestream/demo",
                "https://betboom.ru/freestream/other",
            ],
        )

    def test_ignores_non_freestream_links_and_trailing_punctuation(self):
        html = BeautifulSoup(
            "<div>https://betboom.ru/other, https://example.com/freestream/a; "
            "https://betboom.ru/freestream/valid!</div>",
            "html.parser",
        )

        self.assertEqual(
            parser.find_urls(html, html.get_text(" ", strip=True)),
            ["https://betboom.ru/freestream/valid"],
        )


class ChannelHtmlParsingTests(unittest.TestCase):
    def test_fetch_channel_extracts_message_data(self):
        response = Mock(status_code=200)
        response.raise_for_status.return_value = None
        response.text = """
        <div class="tgme_widget_message_wrap">
          <div class="tgme_widget_message" data-post="demo/42">
            <div class="tgme_widget_message_text">
              Новое колесо: <a href="https://betboom.ru/freestream/abc">ссылка</a>
            </div>
          </div>
        </div>
        """

        with patch.object(parser.SESSION, "get", return_value=response) as get:
            messages = parser.fetch_channel("demo")

        get.assert_called_once_with("https://t.me/s/demo", timeout=parser.REQUEST_TIMEOUT)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["id"], "demo/42")
        self.assertEqual(messages[0]["message_url"], "https://t.me/demo/42")
        self.assertEqual(messages[0]["urls"], ["https://betboom.ru/freestream/abc"])
        self.assertIn("Новое колесо", messages[0]["text"])

    def test_fetch_channel_returns_none_for_not_found(self):
        response = Mock(status_code=404)

        with patch.object(parser.SESSION, "get", return_value=response):
            self.assertIsNone(parser.fetch_channel("missing"))

    def test_fetch_channel_returns_none_for_http_error(self):
        response = Mock(status_code=500)
        response.raise_for_status.side_effect = requests.HTTPError("server error")

        with patch.object(parser.SESSION, "get", return_value=response):
            self.assertIsNone(parser.fetch_channel("broken"))

    def test_fetch_channel_returns_none_for_network_error(self):
        with patch.object(
            parser.SESSION,
            "get",
            side_effect=requests.Timeout("connection timed out"),
        ):
            self.assertIsNone(parser.fetch_channel("offline"))


class CommandTests(unittest.TestCase):
    def test_add_channel_validates_and_saves_channel(self):
        with patch.object(parser, "CHANNELS", ["existing"]), \
             patch.object(parser, "check_channel_preview", return_value="ok") as check, \
             patch.object(parser, "save_channels_file") as save, \
             patch.object(parser, "bot_send") as send:
            parser.handle_command("1", "/add @newchannel")
            self.assertIn("newchannel", parser.CHANNELS)
            self.assertIn("добавлен", send.call_args.args[1])

        check.assert_called_once_with("newchannel")
        save.assert_called_once_with()

    def test_remove_channel_removes_and_saves_channel(self):
        with patch.object(parser, "CHANNELS", ["demo", "other"]), \
             patch.object(parser, "save_channels_file") as save, \
             patch.object(parser, "bot_send") as send:
            parser.handle_command("1", "/remove @demo")
            self.assertEqual(parser.CHANNELS, ["other"])
            self.assertIn("удалён", send.call_args.args[1])

        save.assert_called_once_with()

    def test_addword_adds_keyword_case_insensitively(self):
        with patch.object(parser, "KEYWORDS", ["колесо"]), \
             patch.object(parser, "save_keywords_file") as save, \
             patch.object(parser, "bot_send") as send:
            parser.handle_command("1", "/addword Фрибет")
            self.assertEqual(parser.KEYWORDS, ["колесо", "Фрибет"])
            self.assertIn("добавлено", send.call_args.args[1])

        save.assert_called_once_with()

    def test_removeword_removes_existing_keyword_case_insensitively(self):
        with patch.object(parser, "KEYWORDS", ["Колесо", "фрибет"]), \
             patch.object(parser, "save_keywords_file") as save, \
             patch.object(parser, "bot_send") as send:
            parser.handle_command("1", "/removeword колесо")
            self.assertEqual(parser.KEYWORDS, ["фрибет"])
            self.assertIn("удалено", send.call_args.args[1])

        save.assert_called_once_with()


class ProcessCycleDeduplicationTests(unittest.TestCase):
    def test_same_url_from_two_new_messages_is_saved_once(self):
        first_message = {
            "id": "demo/2",
            "text": "колесо",
            "preview_html": "колесо",
            "urls": ["https://betboom.ru/freestream/same"],
            "message_url": "https://t.me/demo/2",
        }
        second_message = {**first_message, "id": "demo/3", "message_url": "https://t.me/demo/3"}
        seen = {"demo": set()}
        results = []

        with patch.object(parser, "CHANNELS", ["demo"]), \
             patch.object(parser, "fetch_channel", side_effect=[[first_message], [second_message]]), \
             patch.object(parser, "send_telegram_notification", return_value=True):
            parser.process_cycle(seen, results, baseline=True)
            parser.process_cycle(seen, results, baseline=False)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["url"], "https://betboom.ru/freestream/same")


if __name__ == "__main__":
    unittest.main()
