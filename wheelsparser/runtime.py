"""Управление жизненным циклом процесса: стоп-флаг, сигналы, single instance."""

from __future__ import annotations

import os
import signal
import sys
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

# --- Внеплановый обход каналов по запросу бота (/active) --------------------
# Поток parser в норме спит CHECK_INTERVAL между циклами. Команда /active
# будит его досрочно, ждёт завершения цикла и только потом строит отчёт по
# свежей базе. Три события вместо Condition: Event.wait не умеет ждать
# несколько событий сразу, поэтому и остановка, и запрос /active дёргают
# общий _WAKE_PARSER, а своё состояние держат в отдельных флагах.
_WAKE_PARSER = threading.Event()
_RESCAN_REQUESTED = threading.Event()
_RESCAN_DONE = threading.Event()
_RESCAN_DONE.set()


def request_rescan(timeout: float) -> bool:
    """Запросить внеплановый обход каналов и дождаться его конца.

    Вызывается из фонового потока /active. Возвращает True, если поток
    parser принял запрос и прогнал внеплановый цикл за timeout секунд;
    False — обход не завершился вовремя или процесс останавливается.
    Запрос при этом не теряется: ближайший штатный цикл его подхватит.
    """
    if STOP_EVENT.is_set():
        return False
    _RESCAN_DONE.clear()
    _RESCAN_REQUESTED.set()
    _WAKE_PARSER.set()
    _RESCAN_DONE.wait(timeout)
    # Обход выполнен, только если поток parser принял запрос
    # (take_rescan_request снял _RESCAN_REQUESTED) и дошёл до mark_rescan_done.
    # На остановке request_stop дёргает _RESCAN_DONE, чтобы не держать этот
    # поток до таймаута, но запрос остаётся непринятым — честный False.
    return not _RESCAN_REQUESTED.is_set() and _RESCAN_DONE.is_set()


def wait_before_next_cycle(timeout: float) -> None:
    """Пауза потока parser между циклами.

    Прерывается досрочно остановкой процесса или запросом /active — как
    STOP_EVENT.wait, но ещё и на внеплановый обход.
    """
    _WAKE_PARSER.wait(timeout)
    _WAKE_PARSER.clear()


def take_rescan_request() -> bool:
    """True, если /active просил внеплановый обход; сбрасывает запрос."""
    if _RESCAN_REQUESTED.is_set():
        _RESCAN_REQUESTED.clear()
        return True
    return False


def mark_rescan_done() -> None:
    """Сообщить ожидающему /active, что внеплановый обход завершён."""
    _RESCAN_DONE.set()


def request_stop(_signum: int, _frame: Any) -> None:
    if STOP_EVENT.is_set():
        # Второй Ctrl+C — не ждём graceful shutdown, выходим сразу.
        # Состояние не теряется: save_seen() вызывается в конце каждого цикла.
        log.warning("%s Повторный Ctrl+C — принудительный выход", icon("stop"))
        os._exit(1)
    STOP_EVENT.set()
    _WAKE_PARSER.set()  # разбудить поток parser из паузы между циклами
    _RESCAN_DONE.set()  # не держать фоновый поток /active на request_rescan
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

# Поток, отработавший дольше этого времени, считается «оправившимся»: пауза
# перезапуска сбрасывается на начальную. Иначе редкие сбои раз в сутки
# копили бы backoff до максимума.
HEALTHY_RUN_SECONDS = 60.0
RESTART_BACKOFF_SECONDS = 5.0
RESTART_BACKOFF_MAX_SECONDS = 300.0

# Повторно уведомлять о падении разрешаем только после заметно более долгого
# чистого прогона, чем нужен для сброса backoff. Иначе поток, стабильно
# живущий чуть дольше HEALTHY_RUN_SECONDS и снова падающий, шлёт сервисное
# уведомление на каждый цикл — тот самый «поток сообщений в Telegram».
HEALTHY_RENOTIFY_SECONDS = RESTART_BACKOFF_MAX_SECONDS


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
                healthy_for = time.monotonic() - started
                if healthy_for >= HEALTHY_RUN_SECONDS:
                    # Сбой после нормальной работы — пауза перезапуска с нуля.
                    backoff = RESTART_BACKOFF_SECONDS
                if healthy_for >= HEALTHY_RENOTIFY_SECONDS:
                    # Достаточно долгий чистый прогон — новую серию сбоев
                    # снова считаем достойной сервисного уведомления.
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
        # sys.platform, а не os.name: mypy распознаёт именно sys.platform
        # как условие платформы и не проверяет недостижимую на текущей ОС
        # ветку — иначе mypy в CI на Linux спотыкался бы об отсутствующий
        # msvcrt, а на Windows — об отсутствующий fcntl.
        if sys.platform == "win32":
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
