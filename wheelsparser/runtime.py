"""Управление жизненным циклом процесса: стоп-флаг, сигналы, single instance."""

from __future__ import annotations

import os
import signal
import threading
from typing import Any

from .config import LOCK_FILE, icon
from .logging_setup import log

# Потокобезопасный флаг остановки; STOP_EVENT.wait(n) используется вместо
# time.sleep(n), чтобы остановка не ждала конца паузы. Все циклы
# (parser, bot, twitch) проверяют флаг и завершаются сами.
STOP_EVENT = threading.Event()


def request_stop(_signum: int, _frame: Any) -> None:
    if STOP_EVENT.is_set():
        # Второй Ctrl+C — не ждём graceful shutdown, выходим сразу.
        # Состояние не теряется: save_seen() вызывается в конце каждого цикла.
        log.warning("%s Повторный Ctrl+C — принудительный выход", icon("stop"))
        os._exit(1)
    STOP_EVENT.set()
    log.info(
        "Получен сигнал остановки; завершаю текущий цикл "
        "(ещё раз Ctrl+C — немедленный выход)"
    )


def install_signal_handlers() -> None:
    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)


def acquire_single_instance_lock() -> Any | None:
    """Не даёт запустить второй экземпляр парсера.

    Два процесса с одним токеном конфликтуют в getUpdates (409 Conflict),
    поэтому при старте берём эксклюзивную блокировку lock-файла.
    ОС снимает блокировку автоматически при любом завершении процесса,
    так что «зависших» lock-файлов после падения не остаётся.
    """
    lock_handle = open(LOCK_FILE, "a+", encoding="utf-8")
    try:
        if os.name == "nt":
            import msvcrt

            lock_handle.seek(0)
            msvcrt.locking(lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_handle.close()
        return None
    lock_handle.seek(0)
    lock_handle.truncate()
    lock_handle.write(str(os.getpid()))
    lock_handle.flush()
    return lock_handle
