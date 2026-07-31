"""Управление жизненным циклом процесса: стоп-флаг, сигналы, single instance."""

from __future__ import annotations

import os
import signal
import threading
import time
from collections.abc import Callable
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


# --- Супервизор рабочих потоков ---------------------------------------------
# Потоки daemon'ы и никем не проверяются: необработанное исключение убивает
# поток навсегда, а процесс продолжает работать как ни в чём не бывало
# («бот молчит, парсер жив»). supervise() оборачивает тело потока и
# перезапускает его после сбоя с экспоненциальной паузой.

# Поток, отработавший дольше этого времени, считается «здоровым»: пауза
# перезапуска сбрасывается на начальную, и о следующем сбое снова уведомляем.
# Иначе редкие сбои раз в сутки копили бы backoff до максимума.
HEALTHY_RUN_SECONDS = 60.0
RESTART_BACKOFF_SECONDS = 5.0
RESTART_BACKOFF_MAX_SECONDS = 300.0


def supervise(
    target: Callable[[], None],
    name: str,
    on_crash: Callable[[str, BaseException, float], None] | None = None,
) -> Callable[[], None]:
    """Оборачивает тело потока перезапуском после необработанного исключения.

    Штатное завершение target (например, по STOP_EVENT) не перезапускается.
    on_crash вызывается один раз на серию сбоев — крэш-луп не должен
    превращаться в поток сообщений в Telegram; после «здорового» прогона
    (HEALTHY_RUN_SECONDS) уведомление разрешается снова.

    Ловится Exception, а не BaseException: SystemExit и KeyboardInterrupt
    означают остановку, их перезапускать нельзя.
    """

    def runner() -> None:
        backoff = RESTART_BACKOFF_SECONDS
        notified = False
        while not STOP_EVENT.is_set():
            started = time.monotonic()
            try:
                target()
                return
            except Exception as error:
                if STOP_EVENT.is_set():
                    return
                if time.monotonic() - started >= HEALTHY_RUN_SECONDS:
                    # Сбой после долгой нормальной работы — считаем разовым.
                    backoff = RESTART_BACKOFF_SECONDS
                    notified = False
                log.exception(
                    "%s Поток «%s» аварийно завершился — перезапуск через %.0f с",
                    icon("warn"),
                    name,
                    backoff,
                )
                if on_crash is not None and not notified:
                    notified = True
                    try:
                        on_crash(name, error, backoff)
                    except Exception:
                        log.exception(
                            "Не удалось сообщить о падении потока «%s»", name
                        )
            STOP_EVENT.wait(backoff)
            backoff = min(backoff * 2, RESTART_BACKOFF_MAX_SECONDS)

    return runner


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

            # mypy проверяет эту ветку под платформу инструмента (Windows
            # в CI/локально не имеет fcntl), хотя на POSIX атрибуты есть.
            fcntl.flock(  # type: ignore[attr-defined]
                lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB  # type: ignore[attr-defined]
            )
    except OSError:
        lock_handle.close()
        return None
    lock_handle.seek(0)
    lock_handle.truncate()
    lock_handle.write(str(os.getpid()))
    lock_handle.flush()
    return lock_handle
