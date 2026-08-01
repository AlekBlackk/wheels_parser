import queue
import unittest
from datetime import timedelta
from unittest.mock import patch

from wheelsparser import alerts, registry, twitch

WHEEL = "https://betboom.ru/freestream/stream1"


class IrcParsingTests(unittest.TestCase):
    def test_parses_tags_prefix_command_and_rest(self):
        line = (
            "@badges=broadcaster/1;mod=0 :user!user@user.tmi.twitch.tv "
            "PRIVMSG #demo :привет"
        )
        tags, prefix, command, rest = twitch.parse_irc_line(line)

        self.assertEqual(tags["badges"], "broadcaster/1")
        self.assertEqual(prefix, "user!user@user.tmi.twitch.tv")
        self.assertEqual(command, "PRIVMSG")
        self.assertEqual(rest, "#demo :привет")

    def test_parses_line_without_tags(self):
        tags, prefix, command, _rest = twitch.parse_irc_line("PING :tmi.twitch.tv")
        self.assertEqual(tags, {})
        self.assertEqual(prefix, "")
        self.assertEqual(command, "PING")


class AuthorRoleTests(unittest.TestCase):
    def test_reads_roles_from_badges(self):
        roles = twitch.author_roles({"badges": "broadcaster/1,subscriber/12"}, "someone")
        self.assertEqual(roles, ["broadcaster"])

    def test_mod_tag_counts_as_moderator(self):
        self.assertEqual(twitch.author_roles({"mod": "1"}, "someone"), ["moderator"])

    def test_known_bot_login_is_trusted_without_badges(self):
        self.assertEqual(twitch.author_roles({}, "nightbot"), ["bot"])

    def test_plain_viewer_has_no_roles(self):
        self.assertEqual(twitch.author_roles({}, "viewer"), [])


class ReadStreamIdleTimeoutTests(unittest.TestCase):
    class DeadSocket:
        """Симулирует полуоткрытое TCP: recv() всегда таймаутится."""

        def recv(self, _size):
            raise TimeoutError

    def test_raises_connection_error_after_idle_timeout(self):
        with (
            patch.object(twitch, "TWITCH_IDLE_TIMEOUT_SECONDS", 10),
            patch("time.monotonic", side_effect=[0.0, 0.0, 20.0]),
        ):
            with self.assertRaises(ConnectionError):
                twitch._read_stream(self.DeadSocket())

    def test_does_not_raise_before_idle_timeout(self):
        calls = {"n": 0}

        def fake_recv(_size):
            calls["n"] += 1
            if calls["n"] > 2:
                twitch.STOP_EVENT.set()
            raise TimeoutError

        socket_stub = type("Sock", (), {"recv": staticmethod(fake_recv)})()
        self.addCleanup(twitch.STOP_EVENT.clear)
        with (
            patch.object(twitch, "TWITCH_IDLE_TIMEOUT_SECONDS", 10),
            patch("time.monotonic", side_effect=[0.0, 1.0, 2.0, 3.0]),
        ):
            twitch._read_stream(socket_stub)  # не должно бросить исключение


class HandleMessageTests(unittest.TestCase):
    def _start(self, patcher):
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    def setUp(self):
        self._start(patch.dict(alerts.LAST_URL_ALERT, clear=True))
        self._start(patch.object(twitch, "precheck_wheel", return_value=("active", False, "")))
        self.notify = self._start(
            patch.object(twitch, "send_telegram_notification", return_value=True)
        )
        while not twitch.TWITCH_NEW_ENTRIES.empty():
            twitch.TWITCH_NEW_ENTRIES.get_nowait()

    def queued(self):
        entries = []
        while not twitch.TWITCH_NEW_ENTRIES.empty():
            entries.append(twitch.TWITCH_NEW_ENTRIES.get_nowait())
        return entries

    def test_broadcaster_link_is_notified_and_queued(self):
        twitch.handle_twitch_message(
            "demo", "streamer", {"badges": "broadcaster/1"}, f"колесо {WHEEL}"
        )

        entries = self.queued()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["url"], WHEEL)
        self.assertEqual(entries[0]["source"], "twitch")
        self.assertEqual(entries[0]["author_roles"], ["broadcaster"])
        self.notify.assert_called_once()

    def test_viewer_link_is_ignored(self):
        twitch.handle_twitch_message("demo", "viewer", {}, f"колесо {WHEEL}")

        self.assertEqual(self.queued(), [])
        self.notify.assert_not_called()

    def test_expired_wheel_is_not_notified_and_does_not_start_cooldown(self):
        # Пропуск expired-«хвоста» — не уведомление: кулдаун ставить нельзя,
        # иначе перезапуск того же колеса на том же адресе в пределах
        # REALERT_COOLDOWN_MINUTES останется без уведомления (см.
        # test_expired_wheel_does_not_block_later_restart_notification).
        with patch.object(twitch, "precheck_wheel", return_value=("expired", False, "")):
            twitch.handle_twitch_message(
                "demo", "streamer", {"badges": "broadcaster/1"}, WHEEL
            )

        self.assertEqual(self.queued(), [])
        self.notify.assert_not_called()
        self.assertIsNone(alerts.last_alert(WHEEL))

    def test_expired_wheel_does_not_block_later_restart_notification(self):
        with patch.object(twitch, "precheck_wheel", return_value=("expired", False, "")):
            twitch.handle_twitch_message(
                "demo", "streamer", {"badges": "broadcaster/1"}, WHEEL
            )
        self.assertEqual(self.queued(), [])
        self.notify.assert_not_called()

        # Колесо перезапущено на том же адресе (setUp мокает precheck_wheel
        # обратно на "active") — уведомление обязано уйти, а не молчать
        # до конца REALERT_COOLDOWN_MINUTES.
        twitch.handle_twitch_message(
            "demo", "streamer", {"badges": "broadcaster/1"}, WHEEL
        )

        entries = self.queued()
        self.assertEqual(len(entries), 1)
        self.notify.assert_called_once()

    def test_repeated_link_within_cooldown_is_sent_once(self):
        for _ in range(2):
            twitch.handle_twitch_message(
                "demo", "nightbot", {}, f"розыгрыш {WHEEL}"
            )

        self.assertEqual(len(self.queued()), 1)
        self.notify.assert_called_once()

    def test_cooldown_skip_is_logged_for_diagnostics(self):
        # Раньше подавление кулдауном было немым continue — диагностировать
        # «почему не пришло» было неоткуда.
        twitch.handle_twitch_message("demo", "nightbot", {}, f"розыгрыш {WHEEL}")
        self.notify.reset_mock()
        self.queued()

        with self.assertLogs(twitch.log, level="INFO") as logs:
            twitch.handle_twitch_message("demo", "nightbot", {}, f"розыгрыш {WHEEL}")

        self.notify.assert_not_called()
        self.assertEqual(self.queued(), [])
        self.assertTrue(any(WHEEL in line and "кулдаун" in line for line in logs.output))

    def test_message_without_wheel_link_is_skipped(self):
        twitch.handle_twitch_message(
            "demo", "streamer", {"badges": "broadcaster/1"}, "просто колесо"
        )
        self.assertEqual(self.queued(), [])
        self.notify.assert_not_called()

    def test_unexpected_send_error_still_queues_entry_for_history_and_retry(self):
        # mark_url_alert (кулдаун) уже сработал до отправки — без записи
        # в очереди находка терялась бы полностью: ни в базе, ни на
        # ретрае, только «съеденный» кулдаун на REALERT_COOLDOWN_MINUTES.
        # send_telegram_notification сама ловит requests.RequestException,
        # поэтому здесь нужен именно неожиданный тип исключения.
        self.notify.side_effect = TypeError("boom")

        twitch.handle_twitch_message(
            "demo", "streamer", {"badges": "broadcaster/1"}, f"колесо {WHEEL}"
        )

        entries = self.queued()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["url"], WHEEL)
        self.assertFalse(entries[0]["notified"])
        self.assertIsNotNone(alerts.last_alert(WHEEL))


class EnqueueTests(unittest.TestCase):
    """IRC-поток обязан только читать сокет: любой сетевой вызов в нём
    задерживает ответ на PING, и Twitch выбрасывает парсер из чата."""

    def setUp(self):
        patcher = patch.object(twitch, "TWITCH_JOBS", queue.Queue(maxsize=2))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_message_with_link_is_queued(self):
        before = twitch.now_msk()

        self.assertTrue(
            twitch.enqueue_twitch_message(
                "demo", "streamer", {"badges": "broadcaster/1"}, f"колесо {WHEEL}"
            )
        )

        channel, login, tags, text, received_at = twitch.TWITCH_JOBS.get_nowait()
        self.assertEqual((channel, login), ("demo", "streamer"))
        self.assertEqual(tags, {"badges": "broadcaster/1"})
        self.assertIn(WHEEL, text)
        self.assertGreaterEqual(received_at, before)

    def test_message_without_link_is_not_queued(self):
        """Обычная болтовня не должна попадать в очередь вообще."""
        self.assertFalse(
            twitch.enqueue_twitch_message("demo", "viewer", {}, "го колесо когда")
        )
        self.assertTrue(twitch.TWITCH_JOBS.empty())

    def test_full_queue_drops_message_without_raising(self):
        for _ in range(2):
            twitch.enqueue_twitch_message("demo", "streamer", {}, WHEEL)

        self.assertFalse(
            twitch.enqueue_twitch_message("demo", "streamer", {}, WHEEL)
        )
        self.assertEqual(twitch.TWITCH_JOBS.qsize(), 2)


class WorkerLoopTests(unittest.TestCase):
    def setUp(self):
        twitch.STOP_EVENT.clear()
        self.addCleanup(twitch.STOP_EVENT.clear)
        patcher = patch.object(twitch, "TWITCH_JOBS", queue.Queue())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_worker_processes_queued_message(self):
        received_at = twitch.now_msk()
        twitch.TWITCH_JOBS.put(("demo", "streamer", {}, WHEEL, received_at))

        with patch.object(
            twitch,
            "handle_twitch_message",
            side_effect=lambda *_args: twitch.STOP_EVENT.set(),
        ) as handle:
            twitch.twitch_worker_loop()

        handle.assert_called_once_with("demo", "streamer", {}, WHEEL, received_at)

    def test_worker_survives_failing_message(self):
        for index in range(2):
            twitch.TWITCH_JOBS.put(("demo", "streamer", {}, f"{WHEEL}/{index}", None))
        calls = {"n": 0}

        def explode(*_args):
            calls["n"] += 1
            if calls["n"] >= 2:
                twitch.STOP_EVENT.set()
            raise RuntimeError("сбой обработки")

        with patch.object(twitch, "handle_twitch_message", side_effect=explode):
            twitch.twitch_worker_loop()

        self.assertEqual(calls["n"], 2)


class ReadStreamRoutingTests(unittest.TestCase):
    def setUp(self):
        twitch.STOP_EVENT.clear()
        registry.TWITCH_RELOAD.clear()
        self.addCleanup(twitch.STOP_EVENT.clear)

    def test_privmsg_is_queued_and_not_processed_inline(self):
        line = (
            "@badges=broadcaster/1 :streamer!streamer@streamer.tmi.twitch.tv "
            f"PRIVMSG #demo :колесо {WHEEL}\r\n"
        ).encode()
        chunks = [line]

        def fake_recv(_size):
            if chunks:
                return chunks.pop(0)
            twitch.STOP_EVENT.set()
            raise TimeoutError

        socket_stub = type("Sock", (), {"recv": staticmethod(fake_recv)})()

        with patch.object(twitch, "enqueue_twitch_message") as enqueue, \
             patch.object(twitch, "handle_twitch_message") as handle:
            twitch._read_stream(socket_stub)

        enqueue.assert_called_once_with(
            "demo", "streamer", {"badges": "broadcaster/1"}, f"колесо {WHEEL}"
        )
        # Регрессия: обработка (и поход в сеть) в IRC-потоке недопустима.
        handle.assert_not_called()


class ReceivedAtTests(unittest.TestCase):
    """found_at и кулдаун считаются от момента получения сообщения, а не
    от момента, когда до него добрался обработчик."""

    def setUp(self):
        patcher = patch.dict(alerts.LAST_URL_ALERT, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        while not twitch.TWITCH_NEW_ENTRIES.empty():
            twitch.TWITCH_NEW_ENTRIES.get_nowait()

    def test_found_at_uses_message_time_not_processing_time(self):
        received_at = twitch.now_msk() - timedelta(minutes=5)

        with patch.object(twitch, "precheck_wheel", return_value=("active", False, "")), \
             patch.object(twitch, "send_telegram_notification", return_value=True):
            twitch.handle_twitch_message(
                "demo", "streamer", {"badges": "broadcaster/1"}, WHEEL, received_at
            )

        entry = twitch.TWITCH_NEW_ENTRIES.get_nowait()
        self.assertEqual(
            entry["found_at"], received_at.isoformat(timespec="seconds")
        )
        self.assertEqual(alerts.last_alert(WHEEL), received_at)


if __name__ == "__main__":
    unittest.main()
