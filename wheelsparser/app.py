"""Точка входа: инициализация состояния и запуск потоков.

Потоки:
    parser        — обход Telegram-каналов раз в CHECK_INTERVAL;
    bot           — приём команд Telegram (если заданы токен и chat_id);
    twitch-irc    — чтение Twitch-чатов по IRC (если TWITCH_ENABLED);
    twitch-worker — обработка сообщений из чатов (precheck, уведомления).

Все рабочие потоки запускаются через runtime.supervise: необработанное
исключение перезапускает поток и уходит сервисным уведомлением, а не
оставляет процесс «живым, но неработающим».

Главный поток только ждёт STOP_EVENT: обработчик Ctrl+C выполняется
именно в нём и не может прервать блокирующий сетевой вызов.
"""

from __future__ import annotations

import functools
import logging
import threading
import time
from collections.abc import Callable

from . import registry
from .alerts import seed_url_alerts_from_history
from .bot import BOT_COMMANDS, bot_loop
from .config import (
    ALERT_ON_FIRST_RUN,
    CHECK_INTERVAL,
    LOCK_FILE,
    REQUEST_TIMEOUT,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    TWITCH_ENABLED,
    icon,
)
from .db import close_connection, init_db
from .logging_setup import force_utf8_console, log, redact_token, setup_logging
from .net import SUPERVISOR_SESSION
from .parser import load_pending_expired_retry, process_cycle
from .runtime import (
    STOP_EVENT,
    acquire_single_instance_lock,
    install_signal_handlers,
    supervise,
)
from .storage import ensure_data_dir, load_seen, save_seen
from .telegram_api import send_service_notification
from .twitch import twitch_loop, twitch_worker_loop

# Сообщения о падении потоков шлются из самого упавшего потока, каким бы он
# ни был, поэтому у SUPERVISOR_SESSION нет владельца: сериализуем обращения
# к ней локом (requests.Session не потокобезопасна — см. net.py).
_CRASH_NOTICE_LOCK = threading.Lock()


def _notify_thread_crash(name: str, error: BaseException, backoff: float) -> None:
    """Сервисное уведомление о падении рабочего потока (см. runtime.supervise).

    Текст исключения проходит через redact_token: ошибки requests содержат
    URL вида https://api.telegram.org/bot<TOKEN>/..., и пересылать такое
    в чат нельзя.
    """
    reason = redact_token(f"{type(error).__name__}: {error}")
    with _CRASH_NOTICE_LOCK:
        send_service_notification(
            f"{icon('warn')} Поток «{name}» аварийно завершился: {reason}\n"
            f"Перезапускаю автоматически (пауза {backoff:.0f} с). "
            "Подробности — в parser.log.",
            SUPERVISOR_SESSION,
        )


def _start_supervised(target: Callable[[], None], name: str) -> threading.Thread:
    """Запускает daemon-поток под супервизором и возвращает его."""
    thread = threading.Thread(
        target=supervise(target, name, _notify_thread_crash),
        name=name,
        daemon=True,
    )
    thread.start()
    return thread


def _seed_registry_files() -> None:
    """Фиксирует стартовые списки в файлах при первом запуске.

    channels.txt / keywords.txt / twitch_channels.txt — единственные
    источники правды; env-переменные используются только для их создания.
    """
    if registry.SEED_CHANNELS_FILE:
        registry.save_channels_file()
        log.info(
            "%s Создан channels.txt (каналов: %s) — теперь это единственный источник правды",
            icon("ok"),
            len(registry.CHANNELS),
        )
    if registry.SEED_KEYWORDS_FILE:
        registry.save_keywords_file()
        log.info(
            "%s Создан keywords.txt (ключевых слов: %s) — управляйте словами "
            "через файл или /addword и /removeword",
            icon("ok"),
            len(registry.KEYWORDS),
        )
    if registry.SEED_TWITCH_FILE:
        registry.save_twitch_channels_file()
        log.info(
            "%s Создан twitch_channels.txt (каналов: %s) — управляйте "
            "каналами через файл или /addtwitch и /removetwitch",
            icon("ok"),
            len(registry.TWITCH_CHANNELS),
        )
    for note_level, note in registry.CHANNEL_LOAD_NOTES:
        log.log(
            note_level,
            "%s %s",
            icon("warn") if note_level >= logging.WARNING else icon("bell"),
            note,
        )


def _start_bot_thread() -> None:
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        _start_supervised(bot_loop, "bot")
        command_list = " ".join(f"/{item['command']}" for item in BOT_COMMANDS)
        log.info("%s Команды бота активны: %s", icon("bot"), command_list)
    elif TELEGRAM_BOT_TOKEN:
        log.warning(
            "%s TELEGRAM_CHAT_ID не задан — команды бота отключены: "
            "иначе управлять парсером мог бы любой пользователь Telegram",
            icon("warn"),
        )


def _start_twitch_thread() -> None:
    if not TWITCH_ENABLED:
        log.info("%s Twitch-мониторинг выключен (TWITCH_ENABLED=false)", icon("bell"))
        return
    seed_url_alerts_from_history()
    # Два потока: чтение сокета и обработка сообщений. Сеть — только в
    # обработчике, иначе IRC-поток пропускает PING (см. twitch.py).
    _start_supervised(twitch_worker_loop, "twitch-worker")
    _start_supervised(twitch_loop, "twitch-irc")
    twitch_total = len(registry.twitch_channels_snapshot())
    if twitch_total:
        log.info(
            "%s Twitch-мониторинг запущен · каналов %s · только ссылки "
            "от стримера/модов/VIP/ботов",
            icon("scan"),
            twitch_total,
        )
    else:
        log.info(
            "%s Twitch-мониторинг активен, каналов пока нет — добавьте: "
            "/addtwitch channel",
            icon("bell"),
        )


def _run_parse_loop(seen: dict[str, dict[str, str]], baseline: bool) -> None:
    """Цикл парсинга (daemon-поток).

    Интервал отсчитывается от НАЧАЛА цикла: иначе реальный период равен
    «длительность цикла + CHECK_INTERVAL» и расписание дрейфует.
    try/except вокруг process_cycle: одиночная ошибка цикла не должна
    убивать daemon-поток (сценарий «поток умер, процесс жив»).
    """
    cycle_started = time.monotonic()
    try:
        process_cycle(seen, baseline=baseline)
    except Exception:
        log.exception(
            "%s Необработанная ошибка в цикле парсинга — жду следующий цикл",
            icon("warn"),
        )
    while not STOP_EVENT.is_set():
        elapsed = time.monotonic() - cycle_started
        if STOP_EVENT.wait(max(5.0, CHECK_INTERVAL - elapsed)):
            break
        cycle_started = time.monotonic()
        try:
            process_cycle(seen)
        except Exception:
            log.exception(
                "%s Необработанная ошибка в цикле парсинга — жду следующий цикл",
                icon("warn"),
            )


def main() -> int:
    # Порядок важен: сначала каталог данных (и перенос старых файлов из
    # корня), затем логирование — RotatingFileHandler открывает LOG_FILE
    # в DATA_DIR сразу, а перенесённый parser.log должен успеть переехать
    # ДО открытия нового.
    moved = ensure_data_dir()
    force_utf8_console()
    setup_logging()
    if moved:
        log.info(
            "%s Файлы состояния перенесены в %s: %s",
            icon("ok"),
            "data/",
            ", ".join(moved),
        )

    # Держим lock_handle до конца работы процесса.
    lock_handle = acquire_single_instance_lock()
    if lock_handle is None:
        log.error(
            "%s Уже запущен другой экземпляр WheelsParser (lock: %s) — выход",
            icon("stop"),
            LOCK_FILE.name,
        )
        return 1

    install_signal_handlers()
    registry.init()
    _seed_registry_files()

    # База находок открывается после ensure_data_dir(): к этому моменту
    # старый freebets.json из корня уже лежит в data/ и его можно перенести.
    init_db()
    seen, has_state = load_seen()
    # Ссылки, ошибочно признанные expired до рестарта: их посты уже
    # «увидены», и без восстановления этого списка перепроверка была бы
    # больше неоткуда взять (см. parser.PENDING_EXPIRED_RETRY).
    load_pending_expired_retry()

    notifications = (
        "включены" if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID else "выключены"
    )
    log.info(
        "%s WheelsParser запущен · каналов %s · twitch-каналов %s · "
        "ключевых слов %s · интервал %ss",
        icon("start"),
        len(registry.CHANNELS),
        len(registry.TWITCH_CHANNELS),
        len(registry.KEYWORDS),
        CHECK_INTERVAL,
    )
    log.info("%s Telegram-уведомления: %s", icon("bell"), notifications)

    _start_bot_thread()
    _start_twitch_thread()

    baseline = not has_state and not ALERT_ON_FIRST_RUN
    if baseline:
        log.info("Первый запуск: создаю базовое состояние без старых уведомлений")

    # Весь сетевой ввод-вывод — в отдельном daemon-потоке. Обработчик Ctrl+C
    # выполняется только в главном потоке и не может прервать блокирующий
    # сетевой вызов (особенно на Windows), поэтому главный поток должен
    # только ждать STOP_EVENT — тогда сигнал обрабатывается мгновенно.
    parser_thread = _start_supervised(
        functools.partial(_run_parse_loop, seen, baseline),
        "parser",
    )

    while not STOP_EVENT.is_set():
        STOP_EVENT.wait(1)

    # Даём циклу шанс корректно дописать файлы, но не ждём вечно.
    parser_thread.join(timeout=REQUEST_TIMEOUT + 5)
    if parser_thread.is_alive():
        log.warning(
            "%s Цикл не успел завершиться за отведённое время — выхожу; "
            "состояние сохранено после предыдущего цикла",
            icon("warn"),
        )
    else:
        save_seen(seen)
    # Закрываем соединение главного потока: SQLite сливает WAL в основной
    # файл базы. Соединения рабочих потоков закрывать некому — они daemon,
    # но их WAL восстанавливается при следующем открытии базы.
    close_connection()
    log.info("%s WheelsParser остановлен", icon("stop"))
    return 0
