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

        # Каждый прогон «длится» дольше HEALTHY_RUN_SECONDS: разовые сбои,
        # а не крэш-луп — про каждый нужно сообщить.
        with patch.object(runtime, "HEALTHY_RUN_SECONDS", 0.0):
            runtime.supervise(
                target, "demo", lambda name, error, backoff: reported.append(name)
            )()

        self.assertEqual(reported, ["demo", "demo"])

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


if __name__ == "__main__":
    unittest.main()
