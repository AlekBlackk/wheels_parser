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


class UndoStoreTests(unittest.TestCase):
    def setUp(self):
        menu.forget_deletions()
        self.addCleanup(menu.forget_deletions)

    def test_pop_within_window_returns_value_once(self):
        menu.remember_deletion("channel", "demo")
        self.assertEqual(menu.pop_deletion("channel"), "demo")
        self.assertIsNone(menu.pop_deletion("channel"))

    def test_pop_after_window_returns_none(self):
        with patch.object(menu.time, "time", return_value=1000.0):
            menu.remember_deletion("channel", "demo")
        with patch.object(menu.time, "time", return_value=1000.0 + menu.UNDO_WINDOW_SECONDS + 1):
            self.assertIsNone(menu.pop_deletion("channel"))

    def test_new_deletion_overwrites_previous_slot_in_same_category(self):
        menu.remember_deletion("channel", "first")
        menu.remember_deletion("channel", "second")
        self.assertEqual(menu.pop_deletion("channel"), "second")

    def test_categories_are_independent(self):
        menu.remember_deletion("channel", "demo")
        self.assertIsNone(menu.pop_deletion("twitch"))
        self.assertEqual(menu.pop_deletion("channel"), "demo")


class WithUndoTests(unittest.TestCase):
    def test_inserts_undo_row_before_last_row(self):
        base = menu._kb([[{"text": "x", "callback_data": "y"}], [menu.BACK_BUTTON]])
        combined = menu._with_undo(base, "undo:channel")
        rows = combined["inline_keyboard"]
        self.assertEqual(rows[0][0]["callback_data"], "y")
        self.assertEqual(rows[1][0]["callback_data"], "undo:channel")
        self.assertEqual(rows[2][0], menu.BACK_BUTTON)


class NavigationCallbackTests(unittest.TestCase):
    def test_m_root_edits_message_and_answers(self):
        with patch.object(menu, "edit_message_text") as edit, \
             patch.object(menu, "answer_callback_query") as answer:
            handled = menu.handle_callback("1", 55, "cb1", "m:root")
        self.assertTrue(handled)
        answer.assert_called_once_with("cb1")
        edit.assert_called_once_with("1", 55, menu.root_text(), menu.root_menu_keyboard())

    def test_m_channels_shows_channel_list(self):
        with patch.object(registry, "CHANNELS", ["demo"]), \
             patch.object(menu, "edit_message_text") as edit, \
             patch.object(menu, "answer_callback_query"):
            menu.handle_callback("1", 55, "cb1", "m:channels")
            edit.assert_called_once_with(
                "1", 55, menu.channels_section_text(), menu.channels_list_keyboard()
            )

    def test_unknown_callback_is_not_handled_and_stays_silent(self):
        with patch.object(menu, "edit_message_text") as edit, \
             patch.object(menu, "answer_callback_query") as answer:
            handled = menu.handle_callback("1", 55, "cb1", "m:do_wheels")
        self.assertFalse(handled)
        edit.assert_not_called()
        answer.assert_not_called()


class RemoveChannelCallbackTests(unittest.TestCase):
    def setUp(self):
        menu.forget_deletions()
        self.addCleanup(menu.forget_deletions)

    def test_removes_channel_saves_and_offers_undo(self):
        with patch.object(registry, "CHANNELS", ["demo", "other"]), \
             patch.object(registry, "save_channels_file") as save, \
             patch.object(menu, "edit_message_text") as edit, \
             patch.object(menu, "answer_callback_query") as answer:
            handled = menu.handle_callback("1", 55, "cb1", "ch:rm:demo")

            self.assertTrue(handled)
            self.assertEqual(registry.CHANNELS, ["other"])
            save.assert_called_once_with()
            answer.assert_called_once()
            self.assertIn("demo", answer.call_args.args[1])
            edited_keyboard = edit.call_args.args[3]
            callbacks = [b["callback_data"] for row in edited_keyboard["inline_keyboard"] for b in row]
            self.assertIn("undo:channel", callbacks)
            self.assertEqual(menu.pop_deletion("channel"), "demo")

    def test_removing_already_gone_channel_does_not_touch_undo_slot(self):
        with patch.object(registry, "CHANNELS", ["other"]), \
             patch.object(registry, "save_channels_file") as save, \
             patch.object(menu, "edit_message_text"), \
             patch.object(menu, "answer_callback_query"):
            menu.handle_callback("1", 55, "cb1", "ch:rm:demo")
        save.assert_not_called()
        self.assertIsNone(menu.pop_deletion("channel"))

    def test_undo_channel_restores_and_saves(self):
        menu.remember_deletion("channel", "demo")
        with patch.object(registry, "CHANNELS", ["other"]), \
             patch.object(registry, "save_channels_file") as save, \
             patch.object(menu, "edit_message_text"), \
             patch.object(menu, "answer_callback_query") as answer:
            handled = menu.handle_callback("1", 55, "cb1", "undo:channel")

            self.assertTrue(handled)
            self.assertIn("demo", registry.CHANNELS)
            save.assert_called_once_with()
            self.assertIn("demo", answer.call_args.args[1])

    def test_undo_channel_after_window_shows_alert_and_does_not_restore(self):
        with patch.object(registry, "CHANNELS", []), \
             patch.object(registry, "save_channels_file") as save, \
             patch.object(menu, "edit_message_text"), \
             patch.object(menu, "answer_callback_query") as answer:
            menu.handle_callback("1", 55, "cb1", "undo:channel")

            save.assert_not_called()
            self.assertEqual(registry.CHANNELS, [])
            self.assertTrue(answer.call_args.kwargs.get("show_alert"))


class RemoveTwitchCallbackTests(unittest.TestCase):
    def setUp(self):
        menu.forget_deletions()
        self.addCleanup(menu.forget_deletions)
        registry.TWITCH_RELOAD.clear()
        self.addCleanup(registry.TWITCH_RELOAD.clear)

    def test_removes_twitch_channel_signals_reload_and_offers_undo(self):
        with patch.object(registry, "TWITCH_CHANNELS", ["streamer"]), \
             patch.object(registry, "save_twitch_channels_file") as save, \
             patch.object(menu, "edit_message_text"), \
             patch.object(menu, "answer_callback_query"):
            menu.handle_callback("1", 55, "cb1", "tw:rm:streamer")

            self.assertEqual(registry.TWITCH_CHANNELS, [])
            save.assert_called_once_with()
            self.assertTrue(registry.TWITCH_RELOAD.is_set())
            self.assertEqual(menu.pop_deletion("twitch"), "streamer")

    def test_undo_twitch_restores_and_signals_reload(self):
        menu.remember_deletion("twitch", "streamer")
        with patch.object(registry, "TWITCH_CHANNELS", []), \
             patch.object(registry, "save_twitch_channels_file") as save, \
             patch.object(menu, "edit_message_text"), \
             patch.object(menu, "answer_callback_query"):
            menu.handle_callback("1", 55, "cb1", "undo:twitch")

            self.assertEqual(registry.TWITCH_CHANNELS, ["streamer"])
            save.assert_called_once_with()
            self.assertTrue(registry.TWITCH_RELOAD.is_set())


class RemoveWordCallbackTests(unittest.TestCase):
    def setUp(self):
        menu.forget_deletions()
        self.addCleanup(menu.forget_deletions)

    def test_non_integer_index_answers_callback_instead_of_hanging(self):
        with patch.object(registry, "KEYWORDS", ["колесо"]), \
             patch.object(registry, "save_keywords_file") as save, \
             patch.object(menu, "edit_message_text") as edit, \
             patch.object(menu, "answer_callback_query") as answer:
            handled = menu.handle_callback("1", 55, "cb1", "wd:rm:abc")

            self.assertTrue(handled)
            answer.assert_called_once()
            self.assertTrue(answer.call_args.kwargs.get("show_alert"))
            edit.assert_not_called()
            save.assert_not_called()
            self.assertEqual(registry.KEYWORDS, ["колесо"])

    def test_removes_word_by_index_and_offers_undo(self):
        with patch.object(registry, "KEYWORDS", ["колесо", "фрибет"]), \
             patch.object(registry, "save_keywords_file") as save, \
             patch.object(menu, "edit_message_text"), \
             patch.object(menu, "answer_callback_query") as answer:
            handled = menu.handle_callback("1", 55, "cb1", "wd:rm:0")

            self.assertTrue(handled)
            self.assertEqual(registry.KEYWORDS, ["фрибет"])
            save.assert_called_once_with()
            self.assertIn("колесо", answer.call_args.args[1])
            self.assertEqual(menu.pop_deletion("word"), "колесо")

    def test_out_of_range_index_does_not_crash_and_refreshes_list(self):
        with patch.object(registry, "KEYWORDS", ["колесо"]), \
             patch.object(registry, "save_keywords_file") as save, \
             patch.object(menu, "edit_message_text") as edit, \
             patch.object(menu, "answer_callback_query") as answer:
            menu.handle_callback("1", 55, "cb1", "wd:rm:5")

            save.assert_not_called()
            self.assertEqual(registry.KEYWORDS, ["колесо"])
            self.assertTrue(answer.call_args.kwargs.get("show_alert"))
            edit.assert_called_once_with("1", 55, menu.words_section_text(), menu.words_list_keyboard())

    def test_undo_word_restores_and_saves(self):
        menu.remember_deletion("word", "колесо")
        with patch.object(registry, "KEYWORDS", ["фрибет"]), \
             patch.object(registry, "save_keywords_file") as save, \
             patch.object(menu, "edit_message_text"), \
             patch.object(menu, "answer_callback_query") as answer:
            menu.handle_callback("1", 55, "cb1", "undo:word")

            self.assertIn("колесо", registry.KEYWORDS)
            save.assert_called_once_with()
            self.assertIn("колесо", answer.call_args.args[1])

    def test_undo_word_does_not_duplicate_if_already_present(self):
        menu.remember_deletion("word", "колесо")
        with patch.object(registry, "KEYWORDS", ["колесо"]), \
             patch.object(registry, "save_keywords_file") as save, \
             patch.object(menu, "edit_message_text"), \
             patch.object(menu, "answer_callback_query"):
            menu.handle_callback("1", 55, "cb1", "undo:word")

            self.assertEqual(registry.KEYWORDS, ["колесо"])
            save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
