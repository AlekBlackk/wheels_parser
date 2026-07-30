import unittest
from unittest.mock import patch

import requests

from wheelsparser import app, logging_setup

TOKEN = "1234567:AAsecret-bot-token"


class CrashNoticeTests(unittest.TestCase):
    """Сообщение о падении потока уходит в чат — токен в нём недопустим."""

    def test_token_is_redacted_from_crash_notice(self):
        error = requests.RequestException(
            f"HTTPSConnectionPool: /bot{TOKEN}/getUpdates read timed out"
        )

        with patch.object(logging_setup, "TELEGRAM_BOT_TOKEN", TOKEN), \
             patch.object(app, "send_service_notification") as send:
            app._notify_thread_crash("bot", error, 5.0)

        text = send.call_args.args[0]
        self.assertNotIn(TOKEN, text)
        self.assertIn("***TOKEN***", text)
        self.assertIn("bot", text)

    def test_crash_notice_names_thread_and_backoff(self):
        with patch.object(app, "send_service_notification") as send:
            app._notify_thread_crash("twitch-irc", RuntimeError("сбой"), 40.0)

        text = send.call_args.args[0]
        self.assertIn("twitch-irc", text)
        self.assertIn("RuntimeError: сбой", text)
        self.assertIn("40 с", text)


class RedactTokenTests(unittest.TestCase):
    def test_empty_token_leaves_text_untouched(self):
        with patch.object(logging_setup, "TELEGRAM_BOT_TOKEN", ""):
            self.assertEqual(logging_setup.redact_token("текст"), "текст")

    def test_token_is_replaced_everywhere(self):
        with patch.object(logging_setup, "TELEGRAM_BOT_TOKEN", TOKEN):
            redacted = logging_setup.redact_token(f"{TOKEN} и ещё раз {TOKEN}")

        self.assertEqual(redacted, "***TOKEN*** и ещё раз ***TOKEN***")


if __name__ == "__main__":
    unittest.main()
