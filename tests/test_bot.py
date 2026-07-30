import unittest
from unittest.mock import patch

from wheelsparser import active_report, bot, registry


class ChannelCommandTests(unittest.TestCase):
    def test_add_channel_validates_and_saves_channel(self):
        with patch.object(registry, "CHANNELS", ["existing"]), \
             patch.object(bot, "check_channel_preview", return_value="ok") as check, \
             patch.object(registry, "save_channels_file") as save, \
             patch.object(bot, "bot_send") as send:
            bot.handle_command("1", "/add @newchannel")
            self.assertIn("newchannel", registry.CHANNELS)
            self.assertIn("добавлен", send.call_args.args[1])

        check.assert_called_once_with("newchannel")
        save.assert_called_once_with()

    def test_add_channel_rejects_channel_without_web_preview(self):
        with patch.object(registry, "CHANNELS", ["existing"]), \
             patch.object(bot, "check_channel_preview", return_value="no_preview"), \
             patch.object(registry, "save_channels_file") as save, \
             patch.object(bot, "bot_send") as send:
            bot.handle_command("1", "/add @silent")
            self.assertNotIn("silent", registry.CHANNELS)
            self.assertIn("Не добавлен", send.call_args.args[1])

        save.assert_not_called()

    def test_remove_channel_removes_and_saves_channel(self):
        with patch.object(registry, "CHANNELS", ["demo", "other"]), \
             patch.object(registry, "save_channels_file") as save, \
             patch.object(bot, "bot_send") as send:
            bot.handle_command("1", "/remove @demo")
            self.assertEqual(registry.CHANNELS, ["other"])
            self.assertIn("удалён", send.call_args.args[1])

        save.assert_called_once_with()


class KeywordCommandTests(unittest.TestCase):
    def test_addword_adds_keyword_case_insensitively(self):
        with patch.object(registry, "KEYWORDS", ["колесо"]), \
             patch.object(registry, "save_keywords_file") as save, \
             patch.object(bot, "bot_send") as send:
            bot.handle_command("1", "/addword Фрибет")
            self.assertEqual(registry.KEYWORDS, ["колесо", "Фрибет"])
            self.assertIn("добавлено", send.call_args.args[1])

        save.assert_called_once_with()

    def test_addword_rejects_one_sided_wildcard(self):
        with patch.object(registry, "KEYWORDS", ["колесо"]), \
             patch.object(registry, "save_keywords_file") as save, \
             patch.object(bot, "bot_send") as send:
            bot.handle_command("1", "/addword колесо*")
            self.assertEqual(registry.KEYWORDS, ["колесо"])
            self.assertIn("Звёздочки", send.call_args.args[1])

        save.assert_not_called()

    def test_removeword_removes_existing_keyword_case_insensitively(self):
        with patch.object(registry, "KEYWORDS", ["Колесо", "фрибет"]), \
             patch.object(registry, "save_keywords_file") as save, \
             patch.object(bot, "bot_send") as send:
            bot.handle_command("1", "/removeword колесо")
            self.assertEqual(registry.KEYWORDS, ["фрибет"])
            self.assertIn("удалено", send.call_args.args[1])

        save.assert_called_once_with()


class TwitchCommandTests(unittest.TestCase):
    def test_addtwitch_accepts_channel_url_and_signals_reload(self):
        registry.TWITCH_RELOAD.clear()
        with patch.object(registry, "TWITCH_CHANNELS", []), \
             patch.object(registry, "save_twitch_channels_file") as save, \
             patch.object(bot, "bot_send") as send:
            bot.handle_command("1", "/addtwitch https://twitch.tv/Demo/")
            self.assertEqual(registry.TWITCH_CHANNELS, ["demo"])
            self.assertIn("добавлен", send.call_args.args[1])

        self.assertTrue(registry.TWITCH_RELOAD.is_set())
        registry.TWITCH_RELOAD.clear()
        save.assert_called_once_with()


class ActiveReportFormatTests(unittest.TestCase):
    def test_referral_wheel_is_marked_in_active_list(self):
        item = {
            "url": "https://betboom.ru/freestream/one",
            "found_at": "2026-07-30T20:40:31+03:00",
            "channel": "demo",
            "referral": True,
        }
        line = active_report.format_active_item(item, 1)
        self.assertIn("рефералов", line)

    def test_regular_wheel_has_no_referral_mark(self):
        item = {
            "url": "https://betboom.ru/freestream/one",
            "found_at": "2026-07-30T20:40:31+03:00",
            "channel": "demo",
        }
        self.assertNotIn("реф", active_report.format_active_item(item, 1))


class RemoveWheelCommandTests(unittest.TestCase):
    def test_removes_wheel_by_number_from_last_active_answer(self):
        active_report.remember_active_numbers(
            [(1, {"url": "https://betboom.ru/freestream/one/?utm=x"})]
        )
        self.addCleanup(active_report.forget_active_numbers)

        with patch.object(bot, "mark_wheel_removed", return_value=True) as mark, \
             patch.object(bot, "bot_send") as send:
            bot.handle_command("1", "/removewheel 1")

        mark.assert_called_once_with("https://betboom.ru/freestream/one")
        self.assertIn("Колесо удалено", send.call_args.args[1])

    def test_reports_missing_number_without_touching_state(self):
        active_report.forget_active_numbers()
        with patch.object(bot, "mark_wheel_removed") as mark, \
             patch.object(bot, "bot_send") as send:
            bot.handle_command("1", "/removewheel 7")

        mark.assert_not_called()
        self.assertIn("Сначала вызовите /active", send.call_args.args[1])

    def test_accepts_direct_link(self):
        with patch.object(bot, "mark_wheel_removed", return_value=True) as mark, \
             patch.object(bot, "bot_send"):
            bot.handle_command(
                "1", "/removewheel https://www.betboom.ru/freestream/two/"
            )

        mark.assert_called_once_with("https://betboom.ru/freestream/two")


class DispatchTests(unittest.TestCase):
    def test_unknown_command_is_ignored(self):
        with patch.object(bot, "bot_send") as send:
            bot.handle_command("1", "/unknown thing")
        send.assert_not_called()

    def test_command_with_bot_suffix_is_recognized(self):
        with patch.object(bot, "bot_send") as send:
            bot.handle_command("1", "/help@WheelsParserBot")
        send.assert_called_once()


if __name__ == "__main__":
    unittest.main()
