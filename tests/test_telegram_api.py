import unittest
from unittest.mock import Mock, patch

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


if __name__ == "__main__":
    unittest.main()
