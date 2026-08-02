import unittest
from datetime import timedelta
from unittest.mock import Mock, patch

import requests

from wheelsparser import telegram_api
from wheelsparser.timeutils import now_msk


def fake_session(status_code=200):
    response = Mock(status_code=status_code)
    response.raise_for_status.return_value = None
    session = Mock()
    session.post.return_value = response
    return session


class NotificationTests(unittest.TestCase):
    def setUp(self):
        for name, value in (
            ("TELEGRAM_BOT_TOKEN", "token"),
            ("TELEGRAM_CHAT_ID", "42"),
        ):
            patcher = patch.object(telegram_api, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def sent_text(self, session):
        return session.post.call_args.kwargs["json"]["text"]

    def test_telegram_notification_reports_channel_link_and_status(self):
        session = fake_session()
        entry = {
            "url": "https://betboom.ru/freestream/a",
            "found_at": "2026-07-30T20:40:31+03:00",
            "channel": "demo",
            "message_url": "https://t.me/demo/1",
            "status": "active",
        }

        self.assertTrue(telegram_api.send_telegram_notification(entry, session))
        text = self.sent_text(session)
        self.assertIn("Канал: @demo", text)
        self.assertIn("https://betboom.ru/freestream/a", text)
        self.assertIn("колесо активно", text)
        self.assertIn("Пост: https://t.me/demo/1", text)

    def test_twitch_notification_uses_chat_origin_and_role_icons(self):
        session = fake_session()
        entry = {
            "url": "https://betboom.ru/freestream/a",
            "found_at": "2026-07-30T20:40:31+03:00",
            "channel": "streamer",
            "source": "twitch",
            "author": "somebot",
            "author_roles": ["moderator", "bot"],
            "message_url": "https://www.twitch.tv/streamer",
            "status": "soon",
        }

        self.assertTrue(telegram_api.send_telegram_notification(entry, session))
        text = self.sent_text(session)
        self.assertIn("twitch.tv/streamer", text)
        self.assertIn("@somebot", text)
        self.assertIn("🤖", text)
        self.assertIn("розыгрыш ещё не начался", text)

    def test_notification_shows_deadline_with_time_left(self):
        session = fake_session()
        entry = {
            "url": "https://betboom.ru/freestream/a",
            "found_at": "2026-07-30T20:40:31+03:00",
            "channel": "demo",
            "message_url": "https://t.me/demo/1",
            "status": "active",
            "ends_at": (
                now_msk() + timedelta(minutes=12, seconds=30)
            ).isoformat(timespec="seconds"),
        }

        self.assertTrue(telegram_api.send_telegram_notification(entry, session))
        self.assertIn("Окончание: до", self.sent_text(session))
        self.assertIn("осталось 12 мин", self.sent_text(session))

    def test_notification_omits_deadline_when_unknown(self):
        session = fake_session()
        entry = {
            "url": "https://betboom.ru/freestream/a",
            "found_at": "2026-07-30T20:40:31+03:00",
            "channel": "demo",
            "message_url": "https://t.me/demo/1",
            "status": "unknown",
            "ends_at": "",
        }

        self.assertTrue(telegram_api.send_telegram_notification(entry, session))
        self.assertNotIn("Окончание", self.sent_text(session))

    def test_notification_marks_referral_wheel(self):
        session = fake_session()
        entry = {
            "url": "https://betboom.ru/freestream/a",
            "found_at": "2026-07-30T20:40:31+03:00",
            "channel": "demo",
            "message_url": "https://t.me/demo/1",
            "status": "active",
            "referral": True,
        }

        self.assertTrue(telegram_api.send_telegram_notification(entry, session))
        self.assertIn("Колесо для рефералов", self.sent_text(session))

    def test_notification_without_flag_has_no_referral_line(self):
        session = fake_session()
        entry = {
            "url": "https://betboom.ru/freestream/a",
            "found_at": "2026-07-30T20:40:31+03:00",
            "channel": "demo",
            "message_url": "https://t.me/demo/1",
            "status": "active",
        }

        self.assertTrue(telegram_api.send_telegram_notification(entry, session))
        self.assertNotIn("рефералов", self.sent_text(session))

    def test_multi_notification_marks_referral_urls(self):
        session = fake_session()
        entries = [
            {
                "url": "https://betboom.ru/freestream/a",
                "found_at": "2026-07-30T20:40:31+03:00",
                "channel": "demo",
                "message_url": "https://t.me/demo/1",
                "status": "active",
                "referral": True,
            },
            {
                "url": "https://betboom.ru/freestream/b",
                "found_at": "2026-07-30T20:40:31+03:00",
                "channel": "demo",
                "message_url": "https://t.me/demo/1",
                "status": "expired",
            },
        ]

        self.assertTrue(telegram_api.send_multi_telegram_notification(entries, session))
        text = self.sent_text(session)
        self.assertIn(
            "https://betboom.ru/freestream/a (колесо активно, для рефералов)", text
        )
        self.assertIn("https://betboom.ru/freestream/b (уже завершилось)", text)

    def test_multi_notification_lists_every_url_with_status_note(self):
        session = fake_session()
        entries = [
            {
                "url": "https://betboom.ru/freestream/a",
                "found_at": "2026-07-30T20:40:31+03:00",
                "channel": "demo",
                "message_url": "https://t.me/demo/1",
                "status": "active",
            },
            {
                "url": "https://betboom.ru/freestream/b",
                "found_at": "2026-07-30T20:40:31+03:00",
                "channel": "demo",
                "message_url": "https://t.me/demo/1",
                "status": "expired",
            },
        ]

        self.assertTrue(telegram_api.send_multi_telegram_notification(entries, session))
        text = self.sent_text(session)
        self.assertIn("несколько ссылок", text)
        self.assertIn("https://betboom.ru/freestream/a (колесо активно)", text)
        self.assertIn("https://betboom.ru/freestream/b (уже завершилось)", text)

    def test_notifications_are_disabled_without_chat_id(self):
        session = fake_session()
        with patch.object(telegram_api, "TELEGRAM_CHAT_ID", ""):
            entry = {"url": "u", "found_at": "", "channel": "c", "message_url": "m"}
            self.assertFalse(telegram_api.send_telegram_notification(entry, session))
        session.post.assert_not_called()


class KeywordNotificationTests(unittest.TestCase):
    def setUp(self):
        for name, value in (
            ("TELEGRAM_BOT_TOKEN", "token"),
            ("TELEGRAM_CHAT_ID", "42"),
        ):
            patcher = patch.object(telegram_api, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.entry = {
            "keywords": ["колесо"],
            "channel": "demo",
            "found_at": "2026-07-30T20:40:31+03:00",
            "message_url": "https://t.me/demo/1",
            "preview": "будет колесо",
            "preview_html": '<a href="https://t.me/x">Твич</a> будет колесо',
        }

    def test_sends_html_preview(self):
        session = fake_session()
        with patch.object(telegram_api, "PARSER_SESSION", session):
            self.assertTrue(telegram_api.send_keyword_notification(self.entry))
        payload = session.post.call_args.kwargs["json"]
        self.assertEqual(payload["parse_mode"], "HTML")
        self.assertIn('<a href="https://t.me/x">Твич</a>', payload["text"])

    def test_falls_back_to_plain_text_when_telegram_rejects_html(self):
        rejected = Mock(status_code=400)
        accepted = Mock(status_code=200)
        accepted.raise_for_status.return_value = None
        session = Mock()
        session.post.side_effect = [rejected, accepted]

        with patch.object(telegram_api, "PARSER_SESSION", session):
            self.assertTrue(telegram_api.send_keyword_notification(self.entry))

        self.assertEqual(session.post.call_count, 2)
        fallback = session.post.call_args.kwargs["json"]
        self.assertNotIn("parse_mode", fallback)
        self.assertIn("будет колесо", fallback["text"])


class DeliveryUnknownTests(unittest.TestCase):
    """Ошибка отправки не всегда значит «сообщение не доставлено».

    Telegram мог принять sendMessage и не донести ответ (таймаут чтения) —
    повтор такой отправки шлёт дубликат, поэтому такие случаи помечаются
    delivery_unknown и ретраю не подлежат. 5xx от шлюза Telegram под это
    не подпадает: такой ответ означает, что запрос обработан и точно не
    отправлен, поэтому ретраится как обычный отказ.
    """

    def setUp(self):
        for name, value in (
            ("TELEGRAM_BOT_TOKEN", "token"),
            ("TELEGRAM_CHAT_ID", "42"),
        ):
            patcher = patch.object(telegram_api, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def entry(self):
        return {
            "url": "https://betboom.ru/freestream/a",
            "found_at": "2026-07-30T20:40:31+03:00",
            "channel": "demo",
            "message_url": "https://t.me/demo/1",
            "status": "active",
        }

    def failing_session(self, error):
        session = Mock()
        session.post.side_effect = error
        return session

    def test_read_timeout_marks_delivery_unknown(self):
        entry = self.entry()
        session = self.failing_session(requests.ReadTimeout("read timed out"))

        self.assertFalse(telegram_api.send_telegram_notification(entry, session))
        self.assertTrue(entry["delivery_unknown"])

    def test_server_error_keeps_entry_retriable(self):
        # 502 от шлюза Telegram означает, что запрос обработан и
        # НЕ отправлен (в отличие от таймаута чтения, где ответа нет
        # вовсе) — такой отказ должен ретраиться, а не теряться навсегда.
        entry = self.entry()
        response = Mock(status_code=502)
        error = requests.HTTPError("502 Server Error", response=response)
        response.raise_for_status.side_effect = error
        session = Mock()
        session.post.return_value = response

        self.assertFalse(telegram_api.send_telegram_notification(entry, session))
        self.assertFalse(entry.get("delivery_unknown"))

    def test_connection_error_keeps_entry_retriable(self):
        entry = self.entry()
        session = self.failing_session(requests.ConnectionError("dns failure"))

        self.assertFalse(telegram_api.send_telegram_notification(entry, session))
        self.assertFalse(entry.get("delivery_unknown"))

    def test_rate_limit_keeps_entry_retriable(self):
        entry = self.entry()
        response = Mock(status_code=429)
        error = requests.HTTPError("429 Too Many Requests", response=response)
        response.raise_for_status.side_effect = error
        session = Mock()
        session.post.return_value = response

        self.assertFalse(telegram_api.send_telegram_notification(entry, session))
        self.assertFalse(entry.get("delivery_unknown"))

    def test_multi_notification_marks_every_entry(self):
        entries = [self.entry(), self.entry()]
        session = self.failing_session(requests.ReadTimeout("read timed out"))

        self.assertFalse(telegram_api.send_multi_telegram_notification(entries, session))
        self.assertTrue(all(entry["delivery_unknown"] for entry in entries))


class ReplyMarkupTests(unittest.TestCase):
    def test_bot_send_includes_reply_markup_when_given(self):
        session = fake_session()
        keyboard = {"inline_keyboard": [[{"text": "Меню", "callback_data": "m:root"}]]}
        with patch.object(telegram_api, "BOT_SESSION", session):
            telegram_api.bot_send("1", "text", reply_markup=keyboard)
        self.assertEqual(session.post.call_args.kwargs["json"]["reply_markup"], keyboard)

    def test_bot_send_omits_reply_markup_when_not_given(self):
        session = fake_session()
        with patch.object(telegram_api, "BOT_SESSION", session):
            telegram_api.bot_send("1", "text")
        self.assertNotIn("reply_markup", session.post.call_args.kwargs["json"])

    def test_background_bot_send_includes_reply_markup(self):
        session = fake_session()
        keyboard = {"inline_keyboard": [[{"text": "x", "callback_data": "y"}]]}
        with patch.object(telegram_api, "ACTIVE_CHECK_SESSION", session):
            telegram_api.background_bot_send("1", "text", reply_markup=keyboard)
        self.assertEqual(session.post.call_args.kwargs["json"]["reply_markup"], keyboard)


class EditMessageTextTests(unittest.TestCase):
    def test_sends_chat_message_id_text_and_keyboard(self):
        session = fake_session()
        keyboard = {"inline_keyboard": [[{"text": "← Назад", "callback_data": "m:root"}]]}
        with patch.object(telegram_api, "BOT_SESSION", session):
            telegram_api.edit_message_text("1", 55, "<b>Раздел</b>", keyboard)
        payload = session.post.call_args.kwargs["json"]
        self.assertEqual(payload["chat_id"], "1")
        self.assertEqual(payload["message_id"], 55)
        self.assertEqual(payload["text"], "<b>Раздел</b>")
        self.assertEqual(payload["parse_mode"], "HTML")
        self.assertEqual(payload["reply_markup"], keyboard)
        session.post.assert_called_once_with(
            f"{telegram_api.BOT_API}/editMessageText",
            json=payload,
            timeout=telegram_api.REQUEST_TIMEOUT,
        )

    def test_omits_reply_markup_when_not_given(self):
        session = fake_session()
        with patch.object(telegram_api, "BOT_SESSION", session):
            telegram_api.edit_message_text("1", 55, "text")
        self.assertNotIn("reply_markup", session.post.call_args.kwargs["json"])

    def test_not_modified_error_is_not_logged_as_failure(self):
        response = Mock(
            status_code=400,
            text='{"description":"Bad Request: message is not modified"}',
        )
        error = requests.HTTPError("400", response=response)
        response.raise_for_status.side_effect = error
        session = Mock()
        session.post.return_value = response
        with patch.object(telegram_api, "BOT_SESSION", session), \
             patch.object(telegram_api.log, "warning") as warn:
            telegram_api.edit_message_text("1", 55, "text")
        warn.assert_not_called()

    def test_other_errors_are_logged(self):
        session = Mock()
        session.post.side_effect = requests.ConnectionError("boom")
        with patch.object(telegram_api, "BOT_SESSION", session), \
             patch.object(telegram_api.log, "warning") as warn:
            telegram_api.edit_message_text("1", 55, "text")
        warn.assert_called_once()


if __name__ == "__main__":
    unittest.main()
