"""Время проекта — всегда московское.

Метки found_at, счётчики /status «за сегодня», окно /wheels и суточный
сброс /active считаются по МСК независимо от таймзоны сервера.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .config import MSK_TZ


def now_msk() -> datetime:
    """Текущее время в московской зоне — единая точка отсчёта проекта.

    Все пользовательские даты (/status, /wheels, /active, found_at) и
    внутренние расчёты считаются по МСК независимо от таймзоны сервера.
    """
    return datetime.now(MSK_TZ)


def today_msk() -> str:
    """Текущие сутки по МСК в виде «YYYY-MM-DD».

    Ключ для суточных кэшей: удалённых колёс (/removewheel) и
    завершившихся колёс (expired).
    """
    return now_msk().strftime("%Y-%m-%d")


def parse_found_at(value: Any) -> datetime | None:
    """Разбирает found_at и приводит к МСК.

    Наивные метки времени (без зоны) из старых версий freebets.json
    считаются московскими. Возвращает None, если строку разобрать нельзя.
    """
    try:
        moment = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=MSK_TZ)
    return moment.astimezone(MSK_TZ)


def format_found_at(value: Any) -> str:
    """Время находки в читаемом виде (МСК): 16.07.2026 | 20:40:31 | +03:00."""
    moment = parse_found_at(value)
    if moment is None:
        return str(value)
    formatted = f"{moment.strftime('%d.%m.%Y')} | {moment.strftime('%H:%M:%S')}"
    offset = moment.strftime("%z")
    if offset:
        formatted += f" | {offset[:3]}:{offset[3:]}"
    return formatted
