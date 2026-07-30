import unittest
from unittest.mock import Mock, patch

import requests

from wheelsparser import telegram_api


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

    Telegram мог принять sendMessage и не донести ответ (таймаут чтения,
    502 от шлюза). Повтор такой отправки шлёт дубликат, поэтому такие
    случаи помечаются delivery_unknown и ретраю не подлежат.
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

    def test_server_error_marks_delivery_unknown(self):
        entry = self.entry()
        response = Mock(status_code=502)
        error = requests.HTTPError("502 Server Error", response=response)
        response.raise_for_status.side_effect = error
        session = Mock()
        session.post.return_value = response

        self.assertFalse(telegram_api.send_telegram_notification(entry, session))
        self.assertTrue(entry["delivery_unknown"])

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


if __name__ == "__main__":
    unittest.main()
