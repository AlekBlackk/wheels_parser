import logging
import threading
import time
import unittest
from unittest.mock import patch

import requests

from wheelsparser import app, logging_setup, runtime

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

    def test_redacting_formatter_redacts_traceback(self):
        formatter = logging_setup.RedactingFormatter("%(message)s")
        try:
            raise ConnectionError(f"url=/bot{TOKEN}/sendMessage failed")
        except ConnectionError:
            import sys
            exc_info = sys.exc_info()

        record = logging.LogRecord(
            name="wheelsparser",
            level=logging.ERROR,
            pathname=__file__,
            lineno=10,
            msg="Request failed",
            args=(),
            exc_info=exc_info,
        )
        with patch.object(logging_setup, "TELEGRAM_BOT_TOKEN", TOKEN):
            formatted = formatter.format(record)

        self.assertNotIn(TOKEN, formatted)
        self.assertIn("***TOKEN***", formatted)
        self.assertIn("ConnectionError: url=/bot***TOKEN***/sendMessage failed", formatted)

    def test_console_formatter_redacts_traceback(self):
        formatter = logging_setup.ConsoleFormatter("%(message)s")
        try:
            raise RuntimeError(f"secret token: {TOKEN}")
        except RuntimeError:
            import sys
            exc_info = sys.exc_info()

        record = logging.LogRecord(
            name="wheelsparser",
            level=logging.ERROR,
            pathname=__file__,
            lineno=10,
            msg="Crash",
            args=(),
            exc_info=exc_info,
        )
        with patch.object(logging_setup, "TELEGRAM_BOT_TOKEN", TOKEN):
            formatted = formatter.format(record)

        self.assertNotIn(TOKEN, formatted)
        self.assertIn("***TOKEN***", formatted)

    def test_setup_logging_attaches_redacting_formatters(self):
        logger = logging_setup.setup_logging()
        for handler in logger.handlers:
            self.assertIsInstance(handler.formatter, logging_setup.RedactingFormatter)
        # Маскировка перенесена на хендлеры — на самом логгере фильтра больше нет
        has_redact_filter = any(
            isinstance(f, logging_setup.RedactTokenFilter) for f in logger.filters
        )
        self.assertFalse(has_redact_filter)

    def test_redacting_formatter_format_exception_redacts_directly(self):
        formatter = logging_setup.RedactingFormatter("%(message)s")
        try:
            raise ValueError(f"leak {TOKEN}")
        except ValueError:
            import sys
            exc_info = sys.exc_info()

        with patch.object(logging_setup, "TELEGRAM_BOT_TOKEN", TOKEN):
            exc_text = formatter.formatException(exc_info)

        self.assertNotIn(TOKEN, exc_text)
        self.assertIn("***TOKEN***", exc_text)


class ParseLoopRescanTests(unittest.TestCase):
    """Команда /active будит поток parser на внеплановый обход каналов."""

    def setUp(self):
        runtime.STOP_EVENT.clear()
        self.addCleanup(runtime.STOP_EVENT.clear)
        self.addCleanup(runtime.mark_rescan_done)
        self.addCleanup(runtime.take_rescan_request)
        self.addCleanup(lambda: runtime.wait_before_next_cycle(0.0))

    def test_rescan_request_triggers_extra_cycle_and_completes(self):
        cycles: list[bool] = []

        def fake_cycle(_seen, baseline=False):
            cycles.append(baseline)
            if len(cycles) >= 2:  # плановый + внеплановый — дальше выходим
                runtime.STOP_EVENT.set()
                runtime._WAKE_PARSER.set()

        with patch.object(app, "process_cycle", side_effect=fake_cycle), \
             patch.object(app, "CHECK_INTERVAL", 100):
            loop = threading.Thread(
                target=app._run_parse_loop, args=({}, False), daemon=True
            )
            loop.start()
            self.addCleanup(loop.join, 2.0)
            self.addCleanup(runtime._WAKE_PARSER.set)
            self.addCleanup(runtime.STOP_EVENT.set)
            time.sleep(0.1)  # первый цикл отработал, поток ждёт следующего

            completed = runtime.request_rescan(2.0)
            loop.join(2.0)

        self.assertFalse(loop.is_alive())
        self.assertGreaterEqual(len(cycles), 2)
        self.assertTrue(completed)


if __name__ == "__main__":
    unittest.main()
