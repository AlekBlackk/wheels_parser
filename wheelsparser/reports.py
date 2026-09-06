"""Форматирование отчётов и текстов команд бота.

Чистые функции «данные → текст»: генерация сообщений для команд /help,
/status, /top, /channels, /words, /twitch, /wheels, /active.
Отделено от сетевого транспорта Telegram (бот, getUpdates).
"""

from __future__ import annotations

import html
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from . import db, registry
from .config import (
    ACTIVE_MAX_AGE_HOURS,
    CHECK_INTERVAL,
    TOP_PERIOD_DAYS,
    WHEELS_WINDOW_MINUTES,
)
from .db import WheelEntry
from .storage import removed_wheels_today
from .timeutils import format_found_time, now_msk
from .urls import normalize_url


def channel_label(item: WheelEntry | dict[str, Any]) -> str:
    """Подпись источника находки: @канал или twitch.tv/канал."""
    channel = html.escape(str(item.get("channel", "?")))
    if item.get("source") == "twitch":
        return f"twitch.tv/{channel}"
    return f"@{channel}"


def help_text() -> str:
    """Текст справки по командам бота."""
    return (
        "<b>Команды:</b>\n"
        "/menu — то же самое, но кнопками\n"
        f"/wheels — колёса за последние {WHEELS_WINDOW_MINUTES} минут\n"
        "/active — живые колёса за сегодня (сброс в 00:00 МСК)\n"
        "/removewheel номер — убрать колесо из /active до конца суток\n"
        "    (номер — из последнего ответа /active или /wheels; можно "
        "ссылкой или кнопкой ❌)\n"
        "/status — статистика найденных ссылок\n"
        f"/top — какие каналы дают колёса чаще (за {TOP_PERIOD_DAYS} дн.)\n"
        "    (период задаётся числом дней: <code>/top 7</code>)\n"
        "/channels — список отслеживаемых каналов\n"
        "/add @channel — добавить канал\n"
        "/remove @channel — убрать канал\n"
        "/twitch — список Twitch-каналов\n"
        "/addtwitch channel — добавить Twitch-канал\n"
        "/removetwitch channel — убрать Twitch-канал\n"
        "/words — список ключевых слов\n"
        "/addword слово — добавить ключевое слово\n"
        "    (слово — по границам слова, *слово* — по подстроке)\n"
        "/removeword слово — убрать ключевое слово\n"
        "/help — эта справка\n\n"
        f"Каналов под мониторингом: {len(registry.channels_snapshot())}\n"
        f"Twitch-каналов: {len(registry.twitch_channels_snapshot())}\n"
        f"Ключевых слов: {len(registry.keywords_snapshot())}\n"
        f"Интервал проверки: {CHECK_INTERVAL} сек"
    )


def status_text() -> str:
    """Сводка для /status: статистика найденных ссылок."""
    stats = db.wheel_stats()
    lines = [
        f"🎁 Найдено ссылок всего: {stats.total}",
        f"📅 За сегодня: {stats.today}",
    ]
    if stats.last is None:
        lines.append("🕑 Последняя ссылка: пока нет")
        return "\n".join(lines)
    found_time = format_found_time(stats.last.get("found_at", ""))
    url = html.escape(normalize_url(str(stats.last.get("url", ""))))
    lines.append(
        f"🕑 Последняя ссылка: {found_time} ({channel_label(stats.last)})"
    )
    if url:
        lines.append(url)
    return "\n".join(lines)


def top_text(days: int = TOP_PERIOD_DAYS) -> str:
    """Рейтинг каналов по числу найденных колёс за последние days суток."""
    counts = db.channel_counts(now_msk() - timedelta(days=days))
    if not counts:
        return f"За последние {days} дн. находок пока нет."
    lines = [f"📊 <b>Колёс за {days} дн. по каналам:</b>"]
    lines.extend(
        f"{position}. {channel_label({'channel': row.channel, 'source': row.source})}"
        f" — {row.wheels}"
        for position, row in enumerate(counts, start=1)
    )
    return "\n".join(lines)


def channels_text(channels: list[str] | None = None) -> str:
    """Текст списка каналов для /channels."""
    active_channels = channels if channels is not None else registry.channels_snapshot()
    if not active_channels:
        return "Каналов пока нет. Добавьте: /add @channel"
    listing = "\n".join(f"• @{html.escape(channel)}" for channel in active_channels)
    return f"<b>Каналы ({len(active_channels)}):</b>\n{listing}"


def words_text(keywords: list[str] | None = None) -> str:
    """Текст списка ключевых слов для /words."""
    active_keywords = keywords if keywords is not None else registry.keywords_snapshot()
    if not active_keywords:
        return "Ключевых слов пока нет. Добавьте: /addword колесо"
    listing = "\n".join(f"• {html.escape(keyword)}" for keyword in active_keywords)
    return f"<b>Ключевые слова ({len(active_keywords)}):</b>\n{listing}"


def twitch_text(channels: list[str] | None = None) -> str:
    """Текст списка Twitch-каналов для /twitch."""
    active_channels = channels if channels is not None else registry.twitch_channels_snapshot()
    if not active_channels:
        return "Twitch-каналов пока нет. Добавьте: /addtwitch channel"
    listing = "\n".join(
        f"• twitch.tv/{html.escape(channel)}" for channel in active_channels
    )
    return f"<b>Twitch-каналы ({len(active_channels)}):</b>\n{listing}"


def recent_wheels(minutes: int = WHEELS_WINDOW_MINUTES) -> list[WheelEntry]:
    """Колёса за последние minutes минут, от свежих к старым.

    Записи без url — посты с ключевыми словами: они лежат в той же
    таблице ради ретрая недоставленных уведомлений, но колёсами не
    являются и в списки ссылок не попадают (их отсекает wheels_since).
    """
    cutoff = now_msk() - timedelta(minutes=minutes)
    return list(reversed(db.wheels_since(cutoff)))


def wheels_for_active(
    removed_wheels_fn: Callable[[], set[str]] | None = None,
) -> list[WheelEntry]:
    """Уникальные колёса за сегодня — кандидаты на проверку в /active.

    Берём только записи текущих суток по Москве и не старше
    ACTIVE_MAX_AGE_HOURS, чтобы зависшие записи не оставались в /active.
    Дедупликация — по каноническому URL: в найденных сообщениях могут
    отличаться query-параметры (utm и т.п.), но это всё равно одно колесо.
    Колёса, снятые вручную через /removewheel, отбрасываются.
    """
    now = now_msk()
    day_cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0)
    age_cutoff = now - timedelta(hours=ACTIVE_MAX_AGE_HOURS)
    fresh_items = db.wheels_since(max(day_cutoff, age_cutoff))

    get_removed = removed_wheels_fn or removed_wheels_today
    removed_today = get_removed()
    seen_urls: set[str] = set()
    unique_items: list[WheelEntry] = []
    for item in reversed(fresh_items):  # сначала свежие
        url = normalize_url(str(item.get("url", "")))
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        if url in removed_today:
            continue  # удалено вручную через /removewheel
        unique_items.append(item)
    return unique_items
