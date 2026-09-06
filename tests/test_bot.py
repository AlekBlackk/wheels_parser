import threading
import unittest
from datetime import timedelta
from unittest.mock import Mock, patch

from tests.dbfixture import use_temp_db
from wheelsparser import active_report, bot, db, registry
from wheelsparser.timeutils import now_msk


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

    def test_deadline_is_shown_with_time_left(self):
        ends_at = (now_msk() + timedelta(minutes=12, seconds=30)).isoformat(timespec="seconds")
        item = {
            "url": "https://betboom.ru/freestream/one",
            "found_at": "2026-07-30T20:40:31+03:00",
            "channel": "demo",
            "ends_at": ends_at,
        }
        line = active_report.format_active_item(item, 1)
        self.assertIn("осталось 12 мин", line)

    def test_wheel_without_deadline_has_no_deadline_mark(self):
        item = {
            "url": "https://betboom.ru/freestream/one",
            "found_at": "2026-07-30T20:40:31+03:00",
            "channel": "demo",
            "ends_at": "",
        }
        self.assertNotIn("осталось", active_report.format_active_item(item, 1))

    def test_format_active_result_returns_text_and_keyboard(self):
        items = [{
            "url": "https://betboom.ru/freestream/one",
            "found_at": "2026-07-30T20:40:31+03:00",
            "channel": "demo",
        }]
        text, keyboard = active_report.format_active_result(items, total=1)
        self.assertIn("Активные колёса", text)
        self.assertEqual(
            keyboard,
            {
                "inline_keyboard": [
                    [{"text": "❌ 1", "callback_data": "rmw:1"}],
                    [{"text": "☰ Меню", "callback_data": "m:root"}],
                ]
            },
        )

    def test_format_active_result_keyboard_is_menu_only_when_nothing_to_remove(self):
        text, keyboard = active_report.format_active_result([], total=2, unknown_count=0)
        self.assertIn("активных не найдено", text)
        self.assertEqual(
            keyboard, {"inline_keyboard": [[{"text": "☰ Меню", "callback_data": "m:root"}]]}
        )

    def test_format_active_result_keyboard_is_menu_only_on_check_failure(self):
        text, keyboard = active_report.format_active_result(None, total=0)
        self.assertIn("Не удалось проверить", text)
        self.assertEqual(
            keyboard, {"inline_keyboard": [[{"text": "☰ Меню", "callback_data": "m:root"}]]}
        )


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

    def test_unicode_digit_lookalike_is_rejected_not_crashed(self):
        # "²" проходит str.isdigit(), но int("²") бросает ValueError —
        # без isascii()-проверки команда упала бы необработанным исключением
        # вместо обычной подсказки об ошибке.
        with patch.object(bot, "mark_wheel_removed") as mark, \
             patch.object(bot, "bot_send") as send:
            bot.handle_command("1", "/removewheel ²")

        mark.assert_not_called()
        self.assertIn("Укажите номер колеса", send.call_args.args[1])


class HistoryReportTests(unittest.TestCase):
    """Отчёты бота читают историю запросами к базе, а не всем файлом."""

    def setUp(self):
        use_temp_db(self)
        self.now = now_msk()

    def store(self, url="https://betboom.ru/freestream/a", minutes_ago=1, **overrides):
        entry = {
            "url": url,
            "found_at": (self.now - timedelta(minutes=minutes_ago)).isoformat(
                timespec="seconds"
            ),
            "channel": "demo",
            "notified": True,
        }
        entry.update(overrides)
        db.insert_entries([entry])
        return entry

    def test_recent_wheels_keeps_only_the_window_newest_first(self):
        self.store(url="https://betboom.ru/freestream/old", minutes_ago=9)
        self.store(url="https://betboom.ru/freestream/new", minutes_ago=1)
        self.store(url="https://betboom.ru/freestream/ancient", minutes_ago=60)

        wheels = bot.recent_wheels(minutes=10)

        self.assertEqual(
            [item["url"] for item in wheels],
            ["https://betboom.ru/freestream/new", "https://betboom.ru/freestream/old"],
        )

    def test_recent_wheels_skips_keyword_records(self):
        db.insert_entries([{
            "found_at": self.now.isoformat(timespec="seconds"),
            "channel": "demo",
            "keywords": ["колесо"],
        }])

        self.assertEqual(bot.recent_wheels(minutes=10), [])

    def test_wheels_command_marks_referral_wheel(self):
        self.store(url="https://betboom.ru/freestream/ref", referral=True)
        self.store(url="https://betboom.ru/freestream/plain")

        with patch.object(bot, "bot_send") as send:
            bot.cmd_wheels("1", "")
        text = send.call_args.args[1]

        # Метка стоит в заголовке своего колеса — строкой выше его ссылки.
        lines = text.splitlines()
        header = {
            url_line.rsplit("/", 1)[-1]: lines[lines.index(url_line) - 1]
            for url_line in lines
            if url_line.startswith("https://")
        }
        self.assertIn("для рефералов", header["ref"])
        self.assertNotIn("реф", header["plain"])

    def test_status_reports_totals_and_last_wheel(self):
        self.store(url="https://betboom.ru/freestream/first", minutes_ago=30)
        self.store(url="https://betboom.ru/freestream/last", minutes_ago=1)

        text = bot.status_text()

        self.assertIn("всего: 2", text)
        self.assertIn("https://betboom.ru/freestream/last", text)
        self.assertIn("@demo", text)

    def test_status_without_wheels_says_so(self):
        self.assertIn("пока нет", bot.status_text())

    def test_active_deduplicates_by_canonical_url(self):
        self.store(url="https://betboom.ru/freestream/one?utm_source=tg", minutes_ago=5)
        self.store(url="https://www.betboom.ru/freestream/one/", minutes_ago=1)

        items = bot.wheels_for_active()

        self.assertEqual(len(items), 1)

    def test_active_skips_manually_removed_wheels(self):
        self.store(url="https://betboom.ru/freestream/gone")

        with patch.object(
            bot, "removed_wheels_today", return_value={"https://betboom.ru/freestream/gone"}
        ):
            self.assertEqual(bot.wheels_for_active(), [])

    def test_top_ranks_channels_by_number_of_wheels(self):
        self.store(url="https://betboom.ru/freestream/1", channel="often")
        self.store(url="https://betboom.ru/freestream/2", channel="often")
        self.store(url="https://betboom.ru/freestream/3", channel="rare")

        text = bot.top_text(days=30)

        self.assertLess(text.index("often"), text.index("rare"))
        self.assertIn("@often", text)

    def test_top_marks_twitch_channels(self):
        self.store(url="https://betboom.ru/freestream/1", channel="streamer",
                   source="twitch")

        self.assertIn("twitch.tv/streamer", bot.top_text(days=30))

    def test_top_without_history_says_so(self):
        self.assertIn("пока нет", bot.top_text(days=30))

    def test_top_command_accepts_period_in_days(self):
        self.store(url="https://betboom.ru/freestream/1", minutes_ago=60 * 24 * 5)

        with patch.object(bot, "bot_send") as send:
            bot.handle_command("1", "/top 3")
            recent = send.call_args.args[1]
            bot.handle_command("1", "/top 30")
            older = send.call_args.args[1]

        self.assertIn("пока нет", recent)
        self.assertIn("@demo", older)

    def test_top_rejects_unicode_digit_lookalike_without_crashing(self):
        # "²" проходит str.isdigit(), но int("²") бросает ValueError —
        # без isascii()-проверки команда упала бы необработанным исключением
        # вместо обычной подсказки об ошибке.
        with patch.object(bot, "bot_send") as send:
            bot.handle_command("1", "/top ²")

        self.assertIn("Укажите период в днях", send.call_args.args[1])

    def test_wheels_command_attaches_removal_keyboard_and_shares_numbering(self):
        from wheelsparser import active_report
        self.store(url="https://betboom.ru/freestream/only")
        self.addCleanup(active_report.forget_active_numbers)

        with patch.object(bot, "bot_send") as send:
            bot.cmd_wheels("1", "")

        keyboard = send.call_args.kwargs["reply_markup"]
        self.assertEqual(len(keyboard["inline_keyboard"]), 2)
        self.assertEqual(keyboard["inline_keyboard"][0][0]["callback_data"], "rmw:1")
        self.assertEqual(keyboard["inline_keyboard"][-1][0]["callback_data"], "m:root")
        url, known = active_report.lookup_active_number(1)
        self.assertEqual(url, "https://betboom.ru/freestream/only")
        self.assertEqual(known, 1)


class DispatchTests(unittest.TestCase):
    def test_unknown_command_is_ignored(self):
        with patch.object(bot, "bot_send") as send:
            bot.handle_command("1", "/unknown thing")
        send.assert_not_called()

    def test_command_with_bot_suffix_is_recognized(self):
        with patch.object(bot, "bot_send") as send:
            bot.handle_command("1", "/help@WheelsParserBot")
        send.assert_called_once()


class MenuCommandTests(unittest.TestCase):
    def test_menu_command_sends_root_keyboard(self):
        from wheelsparser import menu
        with patch.object(bot, "bot_send") as send:
            bot.handle_command("1", "/menu")
        send.assert_called_once_with("1", menu.root_text(), reply_markup=menu.root_menu_keyboard())

    def test_start_includes_menu_button(self):
        from wheelsparser import menu
        with patch.object(bot, "bot_send") as send:
            bot.handle_command("1", "/start")
        self.assertEqual(send.call_args.kwargs["reply_markup"], menu.root_open_keyboard())

    def test_help_includes_menu_button(self):
        from wheelsparser import menu
        with patch.object(bot, "bot_send") as send:
            bot.handle_command("1", "/help")
        self.assertEqual(send.call_args.kwargs["reply_markup"], menu.root_open_keyboard())


class CallbackDispatchTests(unittest.TestCase):
    def test_do_wheels_runs_cmd_wheels_and_answers(self):
        with patch.object(bot, "cmd_wheels") as cmd, \
             patch.object(bot, "answer_callback_query") as answer:
            bot.handle_callback("1", 55, "cb1", "m:do_wheels")
        cmd.assert_called_once_with("1", "")
        answer.assert_called_once_with("cb1")

    def test_do_active_runs_cmd_active(self):
        with patch.object(bot, "cmd_active") as cmd, \
             patch.object(bot, "answer_callback_query"):
            bot.handle_callback("1", 55, "cb1", "m:do_active")
        cmd.assert_called_once_with("1", "")

    def test_do_status_runs_cmd_status(self):
        with patch.object(bot, "cmd_status") as cmd, \
             patch.object(bot, "answer_callback_query"):
            bot.handle_callback("1", 55, "cb1", "m:do_status")
        cmd.assert_called_once_with("1", "")

    def test_do_top_runs_cmd_top(self):
        with patch.object(bot, "cmd_top") as cmd, \
             patch.object(bot, "answer_callback_query"):
            bot.handle_callback("1", 55, "cb1", "m:do_top")
        cmd.assert_called_once_with("1", "")

    def test_unrecognized_data_falls_through_to_menu(self):
        from wheelsparser import menu
        with patch.object(menu, "handle_callback", return_value=True) as delegate:
            bot.handle_callback("1", 55, "cb1", "m:root")
        delegate.assert_called_once_with("1", 55, "cb1", "m:root")


class SuggestedChannelButtonTests(unittest.TestCase):
    """Кнопка «➕ Добавить» под предложением первоисточника (см.
    parser.suggest_forward_source). Добавление обязано пройти ту же
    проверку ленты t.me/s, что и /add: иначе в мониторинг попадёт канал,
    посты которого парсер прочитать не сможет."""

    def test_adds_channel_after_preview_check(self):
        with patch.object(registry, "CHANNELS", ["existing"]), \
             patch.object(bot, "check_channel_preview", return_value="ok") as check, \
             patch.object(registry, "save_channels_file") as save, \
             patch.object(bot, "answer_callback_query") as answer, \
             patch.object(bot, "bot_send"):
            bot.handle_callback("1", 55, "cb1", "ch:add:origchannel")
            self.assertIn("origchannel", registry.CHANNELS)

        check.assert_called_once_with("origchannel")
        save.assert_called_once_with()
        self.assertIn("добавлен", answer.call_args.args[1])

    def test_channel_without_web_preview_is_not_added(self):
        with patch.object(registry, "CHANNELS", ["existing"]), \
             patch.object(bot, "check_channel_preview", return_value="no_preview"), \
             patch.object(registry, "save_channels_file") as save, \
             patch.object(bot, "answer_callback_query") as answer, \
             patch.object(bot, "bot_send"):
            bot.handle_callback("1", 55, "cb1", "ch:add:origchannel")

        save.assert_not_called()
        self.assertNotIn("origchannel", registry.CHANNELS)
        self.assertTrue(answer.call_args.kwargs.get("show_alert"))

    def test_missing_channel_is_not_added(self):
        with patch.object(registry, "CHANNELS", ["existing"]), \
             patch.object(bot, "check_channel_preview", return_value="not_found"), \
             patch.object(registry, "save_channels_file") as save, \
             patch.object(bot, "answer_callback_query"), \
             patch.object(bot, "bot_send"):
            bot.handle_callback("1", 55, "cb1", "ch:add:origchannel")

        save.assert_not_called()
        self.assertNotIn("origchannel", registry.CHANNELS)

    def test_already_monitored_channel_is_not_added_twice(self):
        with patch.object(registry, "CHANNELS", ["origchannel"]), \
             patch.object(bot, "check_channel_preview") as check, \
             patch.object(registry, "save_channels_file") as save, \
             patch.object(bot, "answer_callback_query") as answer:
            bot.handle_callback("1", 55, "cb1", "ch:add:origchannel")
            self.assertEqual(registry.CHANNELS, ["origchannel"])

        # До сетевой проверки дело доходить не должно.
        check.assert_not_called()
        save.assert_not_called()
        self.assertIn("уже в списке", answer.call_args.args[1])

    def test_invalid_username_in_callback_is_rejected(self):
        # callback_data — не доверенный ввод: сообщение с кнопкой могло
        # быть переслано и отредактировано.
        with patch.object(registry, "CHANNELS", ["existing"]), \
             patch.object(bot, "check_channel_preview") as check, \
             patch.object(registry, "save_channels_file") as save, \
             patch.object(bot, "answer_callback_query") as answer:
            bot.handle_callback("1", 55, "cb1", "ch:add:../../etc/passwd")
            self.assertEqual(registry.CHANNELS, ["existing"])

        check.assert_not_called()
        save.assert_not_called()
        self.assertTrue(answer.call_args.kwargs.get("show_alert"))


class CallbackQueryLoopTests(unittest.TestCase):
    """update с callback_query обрабатывается в bot_loop наравне с message."""

    def test_bot_loop_dispatches_trusted_callback_and_advances_offset(self):
        update = {
            "update_id": 10,
            "callback_query": {
                "id": "cb1",
                "data": "m:root",
                "message": {"message_id": 77, "chat": {"id": 42}},
            },
        }
        response = Mock(status_code=200)
        response.raise_for_status.return_value = None
        response.json.return_value = {"result": [update]}
        session = Mock()
        session.get.return_value = response
        session.post.return_value = response

        with patch.object(bot, "BOT_SESSION", session), \
             patch.object(bot, "TELEGRAM_CHAT_ID", "42"), \
             patch.object(bot, "load_bot_offset", return_value=0), \
             patch.object(bot, "save_bot_offset") as save_offset, \
             patch.object(bot, "handle_callback") as handle, \
             patch.object(bot.STOP_EVENT, "is_set", side_effect=[False, True]):
            bot.bot_loop()

        handle.assert_called_once_with("42", 77, "cb1", "m:root")
        save_offset.assert_called_once_with(11)

    def test_bot_loop_ignores_callback_from_untrusted_chat(self):
        update = {
            "update_id": 10,
            "callback_query": {
                "id": "cb1",
                "data": "m:root",
                "message": {"message_id": 77, "chat": {"id": 999}},
            },
        }
        response = Mock(status_code=200)
        response.raise_for_status.return_value = None
        response.json.return_value = {"result": [update]}
        session = Mock()
        session.get.return_value = response
        session.post.return_value = response

        with patch.object(bot, "BOT_SESSION", session), \
             patch.object(bot, "TELEGRAM_CHAT_ID", "42"), \
             patch.object(bot, "load_bot_offset", return_value=0), \
             patch.object(bot, "save_bot_offset") as save_offset, \
             patch.object(bot, "handle_callback") as handle, \
             patch.object(bot.STOP_EVENT, "is_set", side_effect=[False, True]):
            bot.bot_loop()

        handle.assert_not_called()
        # offset всё равно продвигается, чтобы бэклог не повторялся:
        save_offset.assert_called_once_with(11)


class AuthorizedSenderTests(unittest.TestCase):
    """Кто имеет право слать команды и жать кнопки бота."""

    def test_wrong_chat_is_rejected(self):
        with patch.object(bot, "TELEGRAM_CHAT_ID", "42"), \
             patch.object(bot, "TELEGRAM_ADMIN_ID", ""):
            self.assertFalse(bot.is_authorized_sender("999", "42"))

    def test_private_chat_matches_sender(self):
        with patch.object(bot, "TELEGRAM_CHAT_ID", "42"), \
             patch.object(bot, "TELEGRAM_ADMIN_ID", ""):
            self.assertTrue(bot.is_authorized_sender("42", "42"))
            self.assertFalse(bot.is_authorized_sender("42", "7"))

    def test_group_chat_without_admin_id_accepts_any_member(self):
        with patch.object(bot, "TELEGRAM_CHAT_ID", "-100500"), \
             patch.object(bot, "TELEGRAM_ADMIN_ID", ""):
            self.assertTrue(bot.is_authorized_sender("-100500", "7"))

    def test_admin_id_requires_exact_sender_match(self):
        with patch.object(bot, "TELEGRAM_CHAT_ID", "-100500"), \
             patch.object(bot, "TELEGRAM_ADMIN_ID", "7"):
            self.assertTrue(bot.is_authorized_sender("-100500", "7"))
            self.assertFalse(bot.is_authorized_sender("-100500", "8"))

    def test_admin_id_rejects_anonymous_sender(self):
        with patch.object(bot, "TELEGRAM_CHAT_ID", "-100500"), \
             patch.object(bot, "TELEGRAM_ADMIN_ID", "7"):
            self.assertFalse(bot.is_authorized_sender("-100500", None))


class ListCommandKeyboardTests(unittest.TestCase):
    def test_channels_command_attaches_removal_keyboard(self):
        from wheelsparser import menu
        with patch.object(registry, "CHANNELS", ["demo"]), \
             patch.object(bot, "bot_send") as send:
            bot.handle_command("1", "/channels")
            self.assertEqual(send.call_args.kwargs["reply_markup"], menu.channels_list_keyboard())

    def test_twitch_command_attaches_removal_keyboard(self):
        from wheelsparser import menu
        with patch.object(registry, "TWITCH_CHANNELS", ["streamer"]), \
             patch.object(bot, "bot_send") as send:
            bot.handle_command("1", "/twitch")
            self.assertEqual(send.call_args.kwargs["reply_markup"], menu.twitch_list_keyboard())

    def test_words_command_attaches_removal_keyboard(self):
        from wheelsparser import menu
        with patch.object(registry, "KEYWORDS", ["колесо"]), \
             patch.object(bot, "bot_send") as send:
            bot.handle_command("1", "/words")
            self.assertEqual(send.call_args.kwargs["reply_markup"], menu.words_list_keyboard())


class RegistryLocksReleasedBeforeNetworkTests(unittest.TestCase):
    """Сетевые вызовы bot_send не должны выполняться под локами реестра."""

    def test_cmd_addword_releases_lock_before_send(self):
        def check_lock(*args, **kwargs):
            self.assertFalse(registry.KEYWORDS_LOCK._is_owned())

        with patch.object(registry, "KEYWORDS", ["существующее"]), \
             patch.object(bot, "bot_send", side_effect=check_lock) as send:
            bot.cmd_addword("1", "существующее")
            send.assert_called_once()

    def test_cmd_removeword_releases_lock_before_send(self):
        def check_lock(*args, **kwargs):
            self.assertFalse(registry.KEYWORDS_LOCK._is_owned())

        with patch.object(registry, "KEYWORDS", ["существующее"]), \
             patch.object(bot, "bot_send", side_effect=check_lock) as send:
            bot.cmd_removeword("1", "отсутствующее")
            send.assert_called_once()

    def test_cmd_addtwitch_releases_lock_before_send(self):
        def check_lock(*args, **kwargs):
            self.assertFalse(registry.TWITCH_CHANNELS_LOCK._is_owned())

        with patch.object(registry, "TWITCH_CHANNELS", ["streamer"]), \
             patch.object(bot, "bot_send", side_effect=check_lock) as send:
            bot.cmd_addtwitch("1", "streamer")
            send.assert_called_once()

    def test_cmd_removetwitch_releases_lock_before_send(self):
        def check_lock(*args, **kwargs):
            self.assertFalse(registry.TWITCH_CHANNELS_LOCK._is_owned())

        with patch.object(registry, "TWITCH_CHANNELS", ["streamer"]), \
             patch.object(bot, "bot_send", side_effect=check_lock) as send:
            bot.cmd_removetwitch("1", "other")
            send.assert_called_once()

    def test_cmd_add_releases_lock_before_send(self):
        def check_lock(*args, **kwargs):
            self.assertFalse(registry.CHANNELS_LOCK._is_owned())

        with patch.object(registry, "CHANNELS", ["demo"]), \
             patch.object(bot, "check_channel_preview", return_value="ok"), \
             patch.object(bot, "bot_send", side_effect=check_lock) as send:
            bot.cmd_add("1", "@demo")
            send.assert_called_once()

    def test_cmd_remove_releases_lock_before_send(self):
        def check_lock(*args, **kwargs):
            self.assertFalse(registry.CHANNELS_LOCK._is_owned())

        with patch.object(registry, "CHANNELS", ["demo"]), \
             patch.object(bot, "bot_send", side_effect=check_lock) as send:
            bot.cmd_remove("1", "@notfound")
            send.assert_called_once()


def _join_active_api() -> None:
    for thread in threading.enumerate():
        if thread.name == "active-api":
            thread.join(timeout=2.0)


class ActiveReportLockTests(unittest.TestCase):
    def test_active_check_lock_held_until_after_send(self):
        lock_held_during_send = False

        def fake_send(chat_id, text, reply_markup=None):
            nonlocal lock_held_during_send
            # Пытаемся захватить лок без ожидания — если он занят фоновым
            # потоком, acquire вернёт False.
            acquired = active_report._active_check_lock.acquire(blocking=False)
            lock_held_during_send = not acquired
            if acquired:
                active_report._active_check_lock.release()

        with patch.object(active_report, "request_rescan", return_value=True), \
             patch.object(active_report, "classify_wheels", return_value=([], [], 0)), \
             patch.object(active_report, "background_bot_send", side_effect=fake_send):
            active_report.fire_active_check(
                "1", lambda: [{"url": "https://betboom.ru/freestream/1"}]
            )
            _join_active_api()

        self.assertTrue(lock_held_during_send)
        # После завершения лок свободен
        self.assertTrue(active_report._active_check_lock.acquire(blocking=False))
        active_report._active_check_lock.release()


class ActiveReportRescanTests(unittest.TestCase):
    """/active сначала гоняет внеплановый обход каналов, потом читает базу."""

    def test_rescan_runs_before_items_are_loaded(self):
        order: list[str] = []

        def fake_load():
            order.append("load")
            return [{"url": "https://betboom.ru/freestream/1", "channel": "c"}]

        with patch.object(
            active_report, "request_rescan", side_effect=lambda _t: order.append("rescan")
        ), \
             patch.object(
                 active_report, "classify_wheels",
                 return_value=([{"url": "https://betboom.ru/freestream/1", "channel": "c"}], [], 0),
             ), \
             patch.object(active_report, "background_bot_send") as send:
            active_report.fire_active_check("1", fake_load)
            _join_active_api()

        self.assertEqual(order, ["rescan", "load"])
        send.assert_called_once()

    def test_reports_no_wheels_when_base_empty_after_rescan(self):
        with patch.object(active_report, "request_rescan", return_value=True), \
             patch.object(active_report, "classify_wheels") as classify, \
             patch.object(active_report, "background_bot_send") as send:
            active_report.fire_active_check("1", list)
            _join_active_api()

        classify.assert_not_called()
        self.assertIn("не найдено", send.call_args.args[1])

    def test_still_reports_when_rescan_times_out(self):
        with patch.object(active_report, "request_rescan", return_value=False), \
             patch.object(
                 active_report, "classify_wheels",
                 return_value=([{"url": "https://betboom.ru/freestream/1", "channel": "c"}], [], 0),
             ), \
             patch.object(active_report, "background_bot_send") as send:
            active_report.fire_active_check(
                "1", lambda: [{"url": "https://betboom.ru/freestream/1", "channel": "c"}]
            )
            _join_active_api()

        send.assert_called_once()


class CmdActiveRescanTests(unittest.TestCase):
    def test_cmd_active_announces_refresh_and_hands_loader(self):
        with patch.object(bot, "fire_active_check") as fire, \
             patch.object(bot, "bot_send") as send:
            bot.cmd_active("1", "")

        fire.assert_called_once_with("1", bot.wheels_for_active)
        self.assertIn("Обновляю", send.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
