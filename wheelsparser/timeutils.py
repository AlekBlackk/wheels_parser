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


def parse_msk(value: Any) -> datetime | None:
    """Разбирает ISO-метку времени из истории находок и приводит к МСК.

    Наивные метки времени (без зоны) из старых версий файла считаются
    московскими. Возвращает None, если строку разобрать нельзя.
    """
    try:
        moment = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=MSK_TZ)
    return moment.astimezone(MSK_TZ)


def parse_found_at(value: Any) -> datetime | None:
    """Время находки (found_at) в МСК или None, если разобрать нельзя."""
    return parse_msk(value)


def format_deadline(value: Any, remaining: bool = True) -> str:
    """Дедлайн колеса для человека: «21:40 (осталось 12 мин)».

    Остаток считается от текущего момента, поэтому строка верна и при
    повторной отправке уведомления спустя циклы. Пустая строка означает
    «срок неизвестен» — вызывающий такую строку не показывает.
    remaining=False даёт только время окончания: в списках, где строка
    уже заключена в скобки, вложенные скобки читать невозможно.
    """
    end = parse_msk(value)
    if end is None:
        return ""
    if not remaining:
        return end.strftime("%H:%M")
    seconds_left = (end - now_msk()).total_seconds()
    if seconds_left <= 0:
        return f"{end.strftime('%H:%M')} (время вышло)"
    minutes_left = max(1, int(seconds_left // 60))
    if minutes_left < 60:
        left_note = f"осталось {minutes_left} мин"
    else:
        left_note = f"осталось {minutes_left // 60} ч {minutes_left % 60} мин"
    return f"{end.strftime('%H:%M')} ({left_note})"


def format_found_time(value: Any) -> str:
    """«ЧЧ:ММ» из ISO-строки found_at («YYYY-MM-DDTHH:MM:SS+03:00»).

    Строковый срез, а не parse_msk: found_at уже в МСК (см. модульную
    докстроку), лишний парсинг и обратное форматирование не нужны. Значение
    короче ожидаемого возвращается как есть — вызывающему всё равно нечего
    показать точнее.
    """
    found_at = str(value)
    return found_at[11:16] if len(found_at) >= 16 else found_at


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
