"""Прямые тесты чистых функций «данные → текст» (wheelsparser.reports).

Команды бота проверяются через bot.*, но report-функции — самостоятельный
слой без сети, и их граничные случаи (пустые списки, экранирование,
twitch-подпись) дешевле закрыть здесь.
"""

import unittest
from unittest.mock import patch

from tests.dbfixture import use_temp_db
from wheelsparser import db, reports
from wheelsparser.timeutils import now_msk


class ChannelLabelTests(unittest.TestCase):
    def test_telegram_channel_gets_at_prefix(self):
        self.assertEqual(reports.channel_label({"channel": "demo"}), "@demo")

    def test_twitch_channel_gets_domain_prefix(self):
        label = reports.channel_label({"channel": "streamer", "source": "twitch"})
        self.assertEqual(label, "twitch.tv/streamer")

    def test_channel_name_is_html_escaped(self):
        self.assertIn("&lt;", reports.channel_label({"channel": "<b>"}))


class ListTextTests(unittest.TestCase):
    def test_channels_text_empty_hint(self):
        self.assertIn("/add", reports.channels_text([]))

    def test_channels_text_lists_and_counts(self):
        text = reports.channels_text(["one", "two"])
        self.assertIn("(2)", text)
        self.assertIn("@one", text)

    def test_words_text_empty_hint(self):
        self.assertIn("/addword", reports.words_text([]))

    def test_words_text_escapes_keyword(self):
        self.assertIn("&amp;", reports.words_text(["a&b"]))

    def test_twitch_text_empty_hint(self):
        self.assertIn("/addtwitch", reports.twitch_text([]))

    def test_twitch_text_lists_channels(self):
        self.assertIn("twitch.tv/streamer", reports.twitch_text(["streamer"]))


class HelpTextTests(unittest.TestCase):
    def test_help_mentions_core_commands_and_counters(self):
        with (
            patch.object(reports.registry, "channels_snapshot", return_value=["a"]),
            patch.object(reports.registry, "twitch_channels_snapshot", return_value=[]),
            patch.object(reports.registry, "keywords_snapshot", return_value=["k1", "k2"]),
        ):
            text = reports.help_text()
        self.assertIn("/active", text)
        self.assertIn("Каналов под мониторингом: 1", text)
        self.assertIn("Ключевых слов: 2", text)


class DbBackedReportTests(unittest.TestCase):
    def setUp(self):
        use_temp_db(self)

    def _store(self, url: str, *, channel: str = "demo", source: str = "telegram") -> None:
        entry = db.make_wheel_entry(
            url=url, channel=channel, source=source, found_at=now_msk(), notified=True
        )
        db.insert_entries([entry])

    def test_status_without_history(self):
        self.assertIn("пока нет", reports.status_text())

    def test_status_shows_last_url_and_source(self):
        self._store("https://betboom.ru/freestream/x", channel="chan")
        text = reports.status_text()
        self.assertIn("https://betboom.ru/freestream/x", text)
        self.assertIn("@chan", text)

    def test_recent_wheels_newest_first(self):
        self._store("https://betboom.ru/freestream/old")
        self._store("https://betboom.ru/freestream/new")
        urls = [item["url"] for item in reports.recent_wheels(minutes=60)]
        self.assertEqual(urls[0], "https://betboom.ru/freestream/new")

    def test_wheels_for_active_uses_injected_removed_set(self):
        self._store("https://betboom.ru/freestream/gone")
        removed = {"https://betboom.ru/freestream/gone"}
        self.assertEqual(reports.wheels_for_active(lambda: removed), [])


if __name__ == "__main__":
    unittest.main()
