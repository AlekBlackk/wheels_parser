"""Мониторинг доступности и здоровья Telegram-каналов.

Отслеживает серии неудачных запросов (HTTP-ошибки / 404) и пустых лент
(разметка страницы изменилась или канал отключил веб-превью).
"""

from __future__ import annotations

import sys

from . import registry
from .config import (
    CHANNEL_EMPTY_THRESHOLD,
    CHANNEL_FAIL_THRESHOLD,
    icon,
)
from .logging_setup import log
from .telegram_api import send_service_notification

# Счётчики подряд неудачных циклов per-канал.
# Доступ только из parser-потока — блокировка не нужна.
CHANNEL_FAIL_STREAK: dict[str, int] = {}
CHANNEL_FAIL_ALERTED: set[str] = set()

# Счётчики подряд «пустых» циклов: страница канала отдалась (HTTP 200),
# но ни одного поста распознать не удалось. Отдельно от FAIL_STREAK:
# недоступный канал — это одна проблема, разобранная в ноль лента — другая.
CHANNEL_EMPTY_STREAK: dict[str, int] = {}
CHANNEL_EMPTY_ALERTED: set[str] = set()

# Латч уведомления о смене разметки t.me: одно сообщение на серию, а не
# по одному на каждый из десятков каналов.
LAYOUT_ALERTED = False


def _get_service_notify():
    return getattr(
        sys.modules.get("wheelsparser.parser"),
        "send_service_notification",
        send_service_notification,
    )


def _get_channel_empty_threshold() -> int:
    return getattr(
        sys.modules.get("wheelsparser.parser"),
        "CHANNEL_EMPTY_THRESHOLD",
        CHANNEL_EMPTY_THRESHOLD,
    )


def _get_channel_fail_threshold() -> int:
    return getattr(
        sys.modules.get("wheelsparser.parser"),
        "CHANNEL_FAIL_THRESHOLD",
        CHANNEL_FAIL_THRESHOLD,
    )


def _get_channel_fail_streak() -> dict[str, int]:
    return getattr(
        sys.modules.get("wheelsparser.parser"),
        "CHANNEL_FAIL_STREAK",
        CHANNEL_FAIL_STREAK,
    )


def _get_channel_fail_alerted() -> set[str]:
    return getattr(
        sys.modules.get("wheelsparser.parser"),
        "CHANNEL_FAIL_ALERTED",
        CHANNEL_FAIL_ALERTED,
    )


def _get_channel_empty_streak() -> dict[str, int]:
    return getattr(
        sys.modules.get("wheelsparser.parser"),
        "CHANNEL_EMPTY_STREAK",
        CHANNEL_EMPTY_STREAK,
    )


def _get_channel_empty_alerted() -> set[str]:
    return getattr(
        sys.modules.get("wheelsparser.parser"),
        "CHANNEL_EMPTY_ALERTED",
        CHANNEL_EMPTY_ALERTED,
    )


def _get_layout_alerted() -> bool:
    return getattr(
        sys.modules.get("wheelsparser.parser"),
        "LAYOUT_ALERTED",
        LAYOUT_ALERTED,
    )


def _set_layout_alerted(val: bool) -> None:
    global LAYOUT_ALERTED
    LAYOUT_ALERTED = val
    parser = sys.modules.get("wheelsparser.parser")
    if parser and hasattr(parser, "LAYOUT_ALERTED"):
        parser.__dict__["LAYOUT_ALERTED"] = val


def update_channel_fail_streaks(
    checked_channels: list[str], failed_channels: list[str]
) -> None:
    """Обновляет счётчики недоступности и один раз уведомляет о «мёртвом» канале."""
    failed = set(failed_channels)
    fail_threshold = _get_channel_fail_threshold()
    fail_streak = _get_channel_fail_streak()
    fail_alerted = _get_channel_fail_alerted()
    for channel in checked_channels:
        if channel in failed:
            fail_streak[channel] = fail_streak.get(channel, 0) + 1
            if (
                fail_streak[channel] >= fail_threshold
                and channel not in fail_alerted
            ):
                fail_alerted.add(channel)
                log.warning(
                    "%s Канал @%s недоступен %s циклов подряд — отправляю уведомление",
                    icon("warn"),
                    channel,
                    fail_streak[channel],
                )
                _get_service_notify()(
                    f"{icon('warn')} Канал @{channel} недоступен "
                    f"{fail_streak[channel]} циклов подряд.\n"
                    "Возможно, он удалён, стал приватным или отключил веб-превью.\n"
                    f"Убрать из списка: /remove {channel}"
                )
        else:
            # Канал снова доступен — сбрасываем счётчик и разрешаем
            # повторное уведомление при следующей серии неудач.
            fail_streak.pop(channel, None)
            fail_alerted.discard(channel)
    # Чистим счётчики каналов, удалённых через /remove.
    current = set(registry.channels_snapshot())
    for channel in list(fail_streak):
        if channel not in current:
            fail_streak.pop(channel, None)
            fail_alerted.discard(channel)


def update_channel_empty_streaks(
    checked_channels: list[str],
    failed_channels: list[str],
    empty_channels: list[str],
) -> None:
    """Обновляет счётчики «страница есть, постов нет».

    Недоступные каналы пропускаются: у них своя серия (FAIL_STREAK), и
    смешивать эти счётчики нельзя — иначе сетевой сбой выглядел бы как
    поломка разбора.
    """
    failed = set(failed_channels)
    empty = set(empty_channels)
    empty_streak = _get_channel_empty_streak()
    empty_alerted = _get_channel_empty_alerted()
    for channel in checked_channels:
        if channel in failed:
            continue
        if channel in empty:
            empty_streak[channel] = empty_streak.get(channel, 0) + 1
        else:
            # Пришли посты — разбор работает, серия сбрасывается.
            empty_streak.pop(channel, None)
            empty_alerted.discard(channel)
    current = set(registry.channels_snapshot())
    for channel in list(empty_streak):
        if channel not in current:
            empty_streak.pop(channel, None)
            empty_alerted.discard(channel)


def _alert_layout_change(stalled_channels: list[str]) -> None:
    """Уведомляет о вероятной смене разметки t.me (один раз на серию)."""
    empty_alerted = _get_channel_empty_alerted()
    # Помечаем каналы уведомлёнными: частичное восстановление не должно
    # прислать ещё и по отдельному сообщению на каждый из них.
    empty_alerted.update(stalled_channels)
    if _get_layout_alerted():
        return
    _set_layout_alerted(True)
    empty_threshold = _get_channel_empty_threshold()
    log.error(
        "%s Ни один из %s каналов не отдал постов %s циклов подряд — "
        "похоже, изменилась разметка t.me/s",
        icon("warn"),
        len(stalled_channels),
        empty_threshold,
    )
    _get_service_notify()(
        f"{icon('warn')} Парсер получает страницы каналов, но не может "
        f"разобрать ни одного поста: пусто во всех "
        f"{len(stalled_channels)} каналах {empty_threshold} циклов подряд.\n"
        "Скорее всего изменилась вёрстка t.me/s и парсер нужно обновить.\n"
        "Пока это не исправлено, новые колёса из Telegram НЕ находятся."
    )


def _alert_empty_channel(channel: str) -> None:
    """Уведомляет о канале, чья лента разбирается в ноль (один раз на серию)."""
    empty_alerted = _get_channel_empty_alerted()
    if channel in empty_alerted:
        return
    empty_alerted.add(channel)
    empty_streak = _get_channel_empty_streak()
    log.warning(
        "%s Канал @%s открывается, но постов в ленте нет %s циклов подряд",
        icon("warn"),
        channel,
        empty_streak.get(channel, 0),
    )
    _get_service_notify()(
        f"{icon('warn')} Канал @{channel} открывается, но ни одного поста "
        f"в ленте t.me/s распознать не удалось "
        f"({empty_streak.get(channel, 0)} циклов подряд).\n"
        "Возможно, канал очищен или у него отключено веб-превью — "
        "новые сообщения из него не отслеживаются.\n"
        f"Убрать из списка: /remove {channel}"
    )


def report_empty_channels(
    checked_channels: list[str], failed_channels: list[str]
) -> None:
    """Уведомляет о «тихом» отказе разбора: страница есть, постов нет.

    Это единственный сбой, который иначе не виден вообще: канал считается
    успешно проверенным, в логе «каналов N/N», и парсер молча ничего не
    находит. Разом опустевшие ленты ВСЕХ читаемых каналов означают не
    проблему каналов, а смену разметки t.me — про неё сообщение одно,
    а не по одному на канал.
    """
    failed = set(failed_channels)
    empty_threshold = _get_channel_empty_threshold()
    empty_streak = _get_channel_empty_streak()
    readable = [channel for channel in checked_channels if channel not in failed]
    stalled = [
        channel
        for channel in readable
        if empty_streak.get(channel, 0) >= empty_threshold
    ]
    # Одного канала мало: отличить сломанный разбор от просто пустого
    # канала можно только по тому, что молчат сразу все.
    layout_broken = len(readable) >= 2 and len(stalled) == len(readable)
    if not layout_broken:
        _set_layout_alerted(False)
    if not stalled:
        return
    if layout_broken:
        _alert_layout_change(stalled)
        return
    for channel in stalled:
        _alert_empty_channel(channel)
