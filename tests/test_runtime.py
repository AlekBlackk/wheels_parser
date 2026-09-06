import threading
import time
import unittest
from unittest.mock import patch

from wheelsparser import runtime


class SuperviseTests(unittest.TestCase):
    """Необработанное исключение не должно убивать рабочий поток навсегда."""

    def setUp(self):
        runtime.STOP_EVENT.clear()
        self.addCleanup(runtime.STOP_EVENT.clear)
        # Паузы перезапуска в тестах не ждём.
        patcher = patch.object(runtime, "RESTART_BACKOFF_SECONDS", 0.0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_normal_return_is_not_restarted(self):
        calls = {"n": 0}

        def target():
            calls["n"] += 1

        runtime.supervise(target, "demo")()

        self.assertEqual(calls["n"], 1)

    def test_crashed_target_is_restarted(self):
        calls = {"n": 0}

        def target():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("сбой")

        runtime.supervise(target, "demo")()

        self.assertEqual(calls["n"], 2)

    def test_crash_is_reported_once_per_series(self):
        """Крэш-луп не должен превращаться в поток сообщений в Telegram."""
        calls = {"n": 0}
        reported = []

        def target():
            calls["n"] += 1
            if calls["n"] >= 3:
                runtime.STOP_EVENT.set()
            raise RuntimeError("сбой")

        runtime.supervise(
            target, "demo", lambda name, error, backoff: reported.append(name)
        )()

        self.assertEqual(calls["n"], 3)
        self.assertEqual(reported, ["demo"])

    def test_report_is_allowed_again_after_healthy_run(self):
        calls = {"n": 0}
        reported = []

        def target():
            calls["n"] += 1
            if calls["n"] >= 3:
                runtime.STOP_EVENT.set()
            raise RuntimeError("сбой")

        # Каждый прогон «длится» дольше HEALTHY_RENOTIFY_SECONDS: разовые
        # сбои, а не крэш-луп — про каждый нужно сообщить.
        with patch.object(runtime, "HEALTHY_RENOTIFY_SECONDS", 0.0):
            runtime.supervise(
                target, "demo", lambda name, error, backoff: reported.append(name)
            )()

        self.assertEqual(reported, ["demo", "demo"])

    def test_healthy_run_resets_backoff_without_reopening_notifications(self):
        """Поток живёт дольше HEALTHY_RUN_SECONDS, но короче
        HEALTHY_RENOTIFY_SECONDS, и снова падает: backoff обнуляется, а
        сервисное уведомление уходит только один раз — иначе это поток
        сообщений в Telegram.
        """
        calls = {"n": 0}
        reported = []

        def target():
            calls["n"] += 1
            if calls["n"] >= 4:
                runtime.STOP_EVENT.set()
            raise RuntimeError("сбой")

        with (
            patch.object(runtime, "HEALTHY_RUN_SECONDS", 0.0),
            patch.object(runtime, "HEALTHY_RENOTIFY_SECONDS", 1e9),
        ):
            runtime.supervise(
                target, "demo", lambda name, error, backoff: reported.append(name)
            )()

        self.assertEqual(reported, ["demo"])

    def test_stop_event_prevents_restart(self):
        calls = {"n": 0}

        def target():
            calls["n"] += 1
            runtime.STOP_EVENT.set()
            raise RuntimeError("сбой при остановке")

        runtime.supervise(target, "demo")()

        self.assertEqual(calls["n"], 1)

    def test_failing_crash_report_does_not_kill_supervisor(self):
        calls = {"n": 0}

        def target():
            calls["n"] += 1
            if calls["n"] >= 2:
                runtime.STOP_EVENT.set()
            raise RuntimeError("сбой")

        def broken_report(_name, _error, _backoff):
            raise ValueError("Telegram недоступен")

        runtime.supervise(target, "demo", broken_report)()

        self.assertEqual(calls["n"], 2)

    def test_system_exit_is_not_restarted(self):
        """SystemExit — это остановка, а не сбой."""
        calls = {"n": 0}

        def target():
            calls["n"] += 1
            raise SystemExit(0)

        with self.assertRaises(SystemExit):
            runtime.supervise(target, "demo")()

        self.assertEqual(calls["n"], 1)


class RescanRequestTests(unittest.TestCase):
    """Команда /active просит поток parser сделать внеплановый обход каналов."""

    def setUp(self):
        runtime.STOP_EVENT.clear()
        self.addCleanup(runtime.STOP_EVENT.clear)
        # Слить возможное недопринятое состояние после теста: приём запроса
        # и отметка о завершении цикла возвращают события в исходный вид.
        self.addCleanup(runtime.mark_rescan_done)
        self.addCleanup(runtime.take_rescan_request)
        self.addCleanup(lambda: runtime.wait_before_next_cycle(0.0))

    def test_request_rescan_wakes_idle_parser_and_returns_after_cycle(self):
        woke = threading.Event()

        def fake_parser_loop():
            runtime.wait_before_next_cycle(5.0)
            on_demand = runtime.take_rescan_request()
            woke.set()
            if on_demand:
                runtime.mark_rescan_done()

        parser = threading.Thread(target=fake_parser_loop)
        parser.start()
        time.sleep(0.05)  # дать потоку войти в ожидание

        completed = runtime.request_rescan(2.0)
        parser.join(2.0)

        self.assertTrue(woke.is_set())
        self.assertTrue(completed)

    def test_request_rescan_times_out_when_parser_is_busy(self):
        completed = runtime.request_rescan(0.1)

        self.assertFalse(completed)
        # Запрос не потерян: ближайший цикл его подхватит.
        self.assertTrue(runtime.take_rescan_request())

    def test_stop_wakes_parser_out_of_between_cycles_pause(self):
        released = threading.Event()

        def waiter():
            runtime.wait_before_next_cycle(5.0)
            released.set()

        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.05)

        runtime.request_stop(2, None)
        thread.join(1.0)

        self.assertTrue(released.is_set())

    def test_stop_releases_active_thread_blocked_in_request_rescan(self):
        """Ctrl+C посреди /active не оставляет фоновый поток висеть до таймаута."""
        result = {}

        def active_thread():
            result["completed"] = runtime.request_rescan(30.0)

        thread = threading.Thread(target=active_thread)
        thread.start()
        time.sleep(0.05)  # поток вошёл в _RESCAN_DONE.wait

        runtime.request_stop(2, None)
        thread.join(1.0)

        self.assertFalse(thread.is_alive())
        self.assertFalse(result["completed"])

    def test_request_rescan_returns_immediately_when_already_stopping(self):
        runtime.STOP_EVENT.set()
        self.assertFalse(runtime.request_rescan(30.0))


if __name__ == "__main__":
    unittest.main()
