import unittest
from unittest.mock import patch

from wheelsparser import menu, registry


class RootKeyboardTests(unittest.TestCase):
    def test_root_open_keyboard_has_single_menu_button(self):
        keyboard = menu.root_open_keyboard()
        self.assertEqual(
            keyboard, {"inline_keyboard": [[{"text": "☰ Меню", "callback_data": "m:root"}]]}
        )

    def test_root_menu_keyboard_has_four_sections(self):
        keyboard = menu.root_menu_keyboard()
        callbacks = [
            button["callback_data"]
            for row in keyboard["inline_keyboard"]
            for button in row
        ]
        self.assertEqual(
            callbacks, ["m:wheels", "m:channels", "m:twitch", "m:words"]
        )


class WheelsSectionTests(unittest.TestCase):
    def test_wheels_section_keyboard_has_four_actions_and_back(self):
        keyboard = menu.wheels_section_keyboard()
        callbacks = [
            button["callback_data"]
            for row in keyboard["inline_keyboard"]
            for button in row
        ]
        self.assertEqual(
            callbacks,
            ["m:do_wheels", "m:do_active", "m:do_status", "m:do_top", "m:root"],
        )


class ChannelsListKeyboardTests(unittest.TestCase):
    def test_lists_every_channel_with_remove_callback(self):
        with patch.object(registry, "CHANNELS", ["one", "two"]):
            keyboard = menu.channels_list_keyboard()
        rows = keyboard["inline_keyboard"]
        self.assertEqual(rows[0][0]["callback_data"], "ch:rm:one")
        self.assertEqual(rows[1][0]["callback_data"], "ch:rm:two")
        self.assertEqual(rows[-1][0]["callback_data"], "m:root")

    def test_empty_list_still_has_back_button(self):
        with patch.object(registry, "CHANNELS", []):
            keyboard = menu.channels_list_keyboard()
        self.assertEqual(keyboard["inline_keyboard"], [[menu.BACK_BUTTON]])

    def test_section_text_mentions_count(self):
        with patch.object(registry, "CHANNELS", ["one", "two"]):
            self.assertIn("2", menu.channels_section_text())

    def test_section_text_for_empty_list_points_to_add_command(self):
        with patch.object(registry, "CHANNELS", []):
            self.assertIn("/add", menu.channels_section_text())


class TwitchListKeyboardTests(unittest.TestCase):
    def test_lists_every_channel_with_remove_callback(self):
        with patch.object(registry, "TWITCH_CHANNELS", ["streamer"]):
            keyboard = menu.twitch_list_keyboard()
        self.assertEqual(
            keyboard["inline_keyboard"][0][0]["callback_data"], "tw:rm:streamer"
        )

    def test_section_text_for_empty_list_points_to_add_command(self):
        with patch.object(registry, "TWITCH_CHANNELS", []):
            self.assertIn("/addtwitch", menu.twitch_section_text())


class WordsListKeyboardTests(unittest.TestCase):
    def test_lists_every_word_with_index_based_remove_callback(self):
        with patch.object(registry, "KEYWORDS", ["колесо", "фрибет"]):
            keyboard = menu.words_list_keyboard()
        self.assertEqual(keyboard["inline_keyboard"][0][0]["callback_data"], "wd:rm:0")
        self.assertEqual(keyboard["inline_keyboard"][1][0]["callback_data"], "wd:rm:1")

    def test_section_text_for_empty_list_points_to_add_command(self):
        with patch.object(registry, "KEYWORDS", []):
            self.assertIn("/addword", menu.words_section_text())


class WheelRemovalKeyboardTests(unittest.TestCase):
    def test_builds_one_row_per_entry_with_rmw_callback(self):
        keyboard = menu.wheel_removal_keyboard([(1, "❌ 10:00 @demo"), (2, "❌ 10:05 @demo")])
        self.assertEqual(
            keyboard,
            {
                "inline_keyboard": [
                    [{"text": "❌ 10:00 @demo", "callback_data": "rmw:1"}],
                    [{"text": "❌ 10:05 @demo", "callback_data": "rmw:2"}],
                ]
            },
        )


if __name__ == "__main__":
    unittest.main()
