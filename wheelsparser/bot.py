"""Telegram-бот: команды и цикл getUpdates.

Команды принимаются только из доверенного чата TELEGRAM_CHAT_ID.
Каждая команда — отдельный обработчик (chat_id, argument) в COMMAND_HANDLERS;
неизвестные команды игнорируются молча.
"""

from __future__ import annotations

import html
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import requests

from . import db, menu, registry
from .active_report import fire_active_check, lookup_active_number
from .config import (
    ACTIVE_MAX_AGE_HOURS,
    BOT_API,
    CHECK_INTERVAL,
    FREESTREAM_RE,
    REQUEST_TIMEOUT,
    STALE_COMMAND_SECONDS,
    TELEGRAM_CHAT_ID,
    TOP_PERIOD_DAYS,
    TWITCH_USERNAME_RE,
    USERNAME_RE,
    WHEELS_WINDOW_MINUTES,
    icon,
)
from .logging_setup import log
from .net import BOT_SESSION
from .runtime import STOP_EVENT
from .storage import (
    load_bot_offset,
    mark_wheel_removed,
    removed_wheels_today,
    save_bot_offset,
)
from .telegram_api import bot_send
from .timeutils import now_msk
from .urls import normalize_url

BOT_COMMANDS = [
    {"command": "start", "description": "О боте"},
    {"command": "menu", "description": "Меню управления кнопками"},
    {"command": "wheels", "description": f"Колёса за последние {WHEELS_WINDOW_MINUTES} мин"},
    {"command": "active", "description": "Живые колёса за сегодня (сброс в 00:00 МСК)"},
    {"command": "removewheel", "description": "Убрать колесо по номеру из /active: /removewheel 2"},
    {"command": "status", "description": "Статистика: всего / за сегодня / последняя"},
    {"command": "top", "description": f"Каналы по числу колёс за {TOP_PERIOD_DAYS} дн."},
    {"command": "channels", "description": "Список каналов"},
    {"command": "add", "description": "Добавить канал: /add @channel"},
    {"command": "remove", "description": "Убрать канал: /remove @channel"},
    {"command": "words", "description": "Список ключевых слов"},
    {"command": "addword", "description": "Добавить слово: /addword колесо"},
    {"command": "removeword", "description": "Убрать слово: /removeword колесо"},
    {"command": "twitch", "description": "Список Twitch-каналов"},
    {"command": "addtwitch", "description": "Добавить Twitch-канал: /addtwitch channel"},
    {"command": "removetwitch", "description": "Убрать Twitch-канал: /removetwitch channel"},
    {"command": "help", "description": "Справка"},
]


# ----------------------------------------------------------------------------
# Вспомогательные отчёты
# ----------------------------------------------------------------------------

def check_channel_preview(channel: str) -> str:
    """Проверяет канал через t.me/s/<channel> перед добавлением в /add.

    Возвращает:
    - "ok" — канал существует и веб-превью отдаёт сообщения;
    - "not_found" — канал не существует или приватный (404);
    - "no_preview" — страница есть, но ленты сообщений нет: у канала
      отключено веб-превью (или он пуст) — парсер не сможет его читать;
    - "network_error" — проверить не удалось (сеть, 5xx и т.п.).

    Вызывается из потока бота — используем BOT_SESSION.
    """
    try:
        response = BOT_SESSION.get(
            f"https://t.me/s/{channel}", timeout=REQUEST_TIMEOUT
        )
    except requests.RequestException as error:
        log.warning("Бот: не удалось проверить канал @%s: %s", channel, error)
        return "network_error"
    if response.status_code == 404:
        return "not_found"
    if response.status_code != 200:
        return "network_error"
    if "tgme_widget_message_wrap" in response.text:
        return "ok"
    return "no_preview"


def help_text() -> str:
    return (
        "<b>Команды:</b>\n"
        "/menu — то же самое, но кнопками\n"
        f"/wheels — колёса за последние {WHEELS_WINDOW_MINUTES} минут\n"
        "/active — живые колёса за сегодня (сброс в 00:00 МСК)\n"
        "/removewheel номер — убрать колесо из /active до конца суток\n"
        "    (номер — из последнего ответа /active; можно и ссылкой)\n"
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


def recent_wheels(minutes: int = WHEELS_WINDOW_MINUTES) -> list[dict[str, Any]]:
    """Колёса за последние minutes минут, от свежих к старым.

    Записи без url — посты с ключевыми словами: они лежат в той же
    таблице ради ретрая недоставленных уведомлений, но колёсами не
    являются и в списки ссылок не попадают (их отсекает wheels_since).
    """
    cutoff = now_msk() - timedelta(minutes=minutes)
    return list(reversed(db.wheels_since(cutoff)))


def channel_label(item: dict[str, Any]) -> str:
    """Подпись источника находки: @канал или twitch.tv/канал."""
    channel = html.escape(str(item.get("channel", "?")))
    if item.get("source") == "twitch":
        return f"twitch.tv/{channel}"
    return f"@{channel}"


def status_text() -> str:
    # Только находки со ссылкой: записи о ключевых словах хранятся рядом
    # ради ретрая уведомлений, но статистика тут — про колёса.
    stats = db.wheel_stats()
    lines = [
        f"🎁 Найдено ссылок всего: {stats.total}",
        f"📅 За сегодня: {stats.today}",
    ]
    if stats.last is None:
        lines.append("🕑 Последняя ссылка: пока нет")
        return "\n".join(lines)
    found_at = str(stats.last.get("found_at", ""))
    found_time = found_at[11:16] if len(found_at) >= 16 else found_at
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


def wheels_for_active() -> list[dict[str, Any]]:
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

    removed_today = removed_wheels_today()
    seen_urls: set[str] = set()
    unique_items: list[dict[str, Any]] = []
    for item in reversed(fresh_items):  # сначала свежие
        url = normalize_url(str(item.get("url", "")))
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        if url in removed_today:
            continue  # удалено вручную через /removewheel
        unique_items.append(item)
    return unique_items


# ----------------------------------------------------------------------------
# Обработчики команд
# ----------------------------------------------------------------------------

def cmd_start(chat_id: str, _argument: str) -> None:
    total = len(registry.channels_snapshot())
    bot_send(
        chat_id,
        f"{icon('start')} <b>WheelsParser</b>\n"
        "Я мониторю Telegram-каналы стримеров и присылаю ссылки на "
        "фрибет-колёса BetBoom сразу после публикации — ничего "
        "запрашивать не нужно.\n\n"
        "Что я умею:\n"
        f"{icon('link')} ловлю ссылки на колёса в {total} каналах\n"
        f"{icon('scan')} проверяю каналы каждые {CHECK_INTERVAL} сек\n\n"
        "Самое полезное:\n"
        f"/wheels — колёса за последние {WHEELS_WINDOW_MINUTES} минут\n"
        "/active — живые колёса за сегодня\n"
        "/status — статистика находок\n\n"
        "Полный список команд — /help",
        reply_markup=menu.root_open_keyboard(),
    )


def cmd_help(chat_id: str, _argument: str) -> None:
    bot_send(chat_id, help_text(), reply_markup=menu.root_open_keyboard())


def cmd_menu(chat_id: str, _argument: str) -> None:
    bot_send(chat_id, menu.root_text(), reply_markup=menu.root_menu_keyboard())


def cmd_status(chat_id: str, _argument: str) -> None:
    bot_send(chat_id, status_text())


def cmd_top(chat_id: str, argument: str) -> None:
    raw = argument.strip()
    if raw and not raw.isdigit():
        bot_send(
            chat_id,
            "Укажите период в днях: <code>/top 7</code> "
            f"(без аргумента — за {TOP_PERIOD_DAYS} дн.)",
        )
        return
    # Верхняя граница — чтобы «/top 100000» не выглядел осмысленным
    # периодом: истории всё равно не больше MAX_RESULTS записей.
    days = min(max(int(raw), 1), 365) if raw else TOP_PERIOD_DAYS
    bot_send(chat_id, top_text(days))


def cmd_wheels(chat_id: str, _argument: str) -> None:
    wheels = recent_wheels()
    if not wheels:
        bot_send(
            chat_id,
            f"За последние {WHEELS_WINDOW_MINUTES} минут новых колёс не найдено. "
            "Как только появится ссылка — пришлю её сразу.",
        )
        return
    lines = [f"{icon('link')} <b>Колёса за последние {WHEELS_WINDOW_MINUTES} минут:</b>"]
    for item in wheels:
        found_at = str(item.get("found_at", ""))
        found_time = found_at[11:16] if len(found_at) >= 16 else found_at
        channel = html.escape(str(item.get("channel", "")))
        # normalize_url: старые записи могли сохранить URL с &amp; и utm-хвостом.
        url = html.escape(normalize_url(str(item.get("url", ""))))
        referral_mark = " ⚠️ для рефералов" if item.get("referral") else ""
        lines.append(f"• {found_time} — @{channel}{referral_mark}\n{url}")
    bot_send(chat_id, "\n".join(lines))


def cmd_active(chat_id: str, _argument: str) -> None:
    unique_items = wheels_for_active()
    if not unique_items:
        bot_send(
            chat_id,
            f"За последние {ACTIVE_MAX_AGE_HOURS} часов колёс не найдено. "
            "Как только появится ссылка — пришлю её сразу.",
        )
        return
    # Fire-and-forget: поток бота не блокируется.
    # Результат придёт отдельным сообщением после проверки в фоновом потоке.
    bot_send(
        chat_id,
        f"{icon('bell')} Проверяю {len(unique_items)} колёс за сегодня…"
        " Результат пришлю отдельным сообщением.",
    )
    fire_active_check(chat_id, unique_items)


def _resolve_wheel_to_remove(chat_id: str, raw: str) -> str | None:
    """URL колеса по номеру из /active или по прямой ссылке.

    None означает, что пользователю уже отправлена подсказка об ошибке.
    """
    if raw.isdigit():
        number = int(raw)
        url, known = lookup_active_number(number)
        if not url:
            if known:
                hint = f"В последнем ответе /active номера 1–{known}."
            else:
                hint = (
                    "Сначала вызовите /active — номера колёс берутся "
                    "из его последнего ответа."
                )
            bot_send(
                chat_id,
                f"{icon('warn')} Не нашёл колесо с номером {number}. {hint}",
            )
            return None
        return url

    url = normalize_url(raw)
    if not url or not FREESTREAM_RE.match(url):
        bot_send(
            chat_id,
            f"{icon('warn')} Укажите номер колеса из /active "
            "(например <code>/removewheel 2</code>) или ссылку вида "
            "<code>https://betboom.ru/freestream/...</code>",
        )
        return None
    return url


def cmd_removewheel(chat_id: str, argument: str) -> None:
    raw = argument.strip()
    if not raw:
        bot_send(
            chat_id,
            "Укажите номер колеса из ответа /active: "
            "<code>/removewheel 2</code>\n"
            "Работает и ссылка: "
            "<code>/removewheel https://betboom.ru/freestream/...</code>\n"
            "Колесо будет скрыто из /active до конца суток (00:00 МСК).",
        )
        return
    url = _resolve_wheel_to_remove(chat_id, raw)
    if url is None:
        return
    if mark_wheel_removed(url):
        log.info("Бот: колесо удалено из /active вручную: %s", url)
        bot_send(
            chat_id,
            f"{icon('ok')} Колесо удалено и не будет показываться "
            f"в /active до конца суток (00:00 МСК):\n{html.escape(url)}",
        )
    else:
        bot_send(
            chat_id,
            "Это колесо уже удалено из /active. "
            "Список обнулится в 00:00 МСК.",
        )


def cmd_channels(chat_id: str, _argument: str) -> None:
    channels = registry.channels_snapshot()
    listing = "\n".join(f"• @{html.escape(channel)}" for channel in channels)
    bot_send(chat_id, f"<b>Каналы ({len(channels)}):</b>\n{listing}")


def cmd_words(chat_id: str, _argument: str) -> None:
    keywords = registry.keywords_snapshot()
    if not keywords:
        bot_send(chat_id, "Ключевых слов пока нет. Добавьте: /addword колесо")
        return
    listing = "\n".join(f"• {html.escape(keyword)}" for keyword in keywords)
    bot_send(chat_id, f"<b>Ключевые слова ({len(keywords)}):</b>\n{listing}")


def _validate_keyword(chat_id: str, command: str, argument: str) -> str | None:
    keyword = argument.strip()
    if not keyword or len(keyword) > 64:
        bot_send(chat_id, f"Укажите слово: <code>{command} колесо</code>")
        return None
    if "*" in keyword and not (
        keyword.startswith("*") and keyword.endswith("*") and len(keyword) > 2
    ):
        bot_send(
            chat_id,
            "Звёздочки — только с обеих сторон: <code>*колесо*</code> "
            "(поиск по подстроке). Без звёздочек — поиск по границам слова.",
        )
        return None
    return keyword


def cmd_addword(chat_id: str, argument: str) -> None:
    keyword = _validate_keyword(chat_id, "/addword", argument)
    if keyword is None:
        return
    with registry.KEYWORDS_LOCK:
        existing = next(
            (k for k in registry.KEYWORDS if k.casefold() == keyword.casefold()), None
        )
        if existing is not None:
            bot_send(chat_id, f"«{html.escape(existing)}» уже в списке.")
            return
        registry.KEYWORDS.append(keyword)
        registry.save_keywords_file()
        total = len(registry.KEYWORDS)
    bot_send(chat_id, f"{icon('ok')} «{html.escape(keyword)}» добавлено. Слов: {total}")
    log.info("Бот: слово %r добавлено, всего %s", keyword, total)


def cmd_removeword(chat_id: str, argument: str) -> None:
    keyword = _validate_keyword(chat_id, "/removeword", argument)
    if keyword is None:
        return
    with registry.KEYWORDS_LOCK:
        existing = next(
            (k for k in registry.KEYWORDS if k.casefold() == keyword.casefold()), None
        )
        if existing is None:
            bot_send(chat_id, f"«{html.escape(keyword)}» нет в списке.")
            return
        registry.KEYWORDS.remove(existing)
        registry.save_keywords_file()
        total = len(registry.KEYWORDS)
    bot_send(chat_id, f"{icon('stop')} «{html.escape(existing)}» удалено. Слов: {total}")
    log.info("Бот: слово %r удалено, всего %s", keyword, total)


def cmd_twitch(chat_id: str, _argument: str) -> None:
    channels = registry.twitch_channels_snapshot()
    if not channels:
        bot_send(chat_id, "Twitch-каналов пока нет. Добавьте: /addtwitch channel")
        return
    listing = "\n".join(
        f"• twitch.tv/{html.escape(channel)}" for channel in channels
    )
    bot_send(chat_id, f"<b>Twitch-каналы ({len(channels)}):</b>\n{listing}")


def _parse_twitch_channel(chat_id: str, command: str, argument: str) -> str | None:
    candidate = argument.strip()
    if "twitch.tv/" in candidate:
        candidate = candidate.rstrip("/").rsplit("/", 1)[-1]
    match = TWITCH_USERNAME_RE.match(candidate)
    if not match:
        bot_send(chat_id, f"Укажите канал: <code>{command} channel</code>")
        return None
    return match.group(1).lower()


def cmd_addtwitch(chat_id: str, argument: str) -> None:
    channel = _parse_twitch_channel(chat_id, "/addtwitch", argument)
    if channel is None:
        return
    with registry.TWITCH_CHANNELS_LOCK:
        if channel in registry.TWITCH_CHANNELS:
            bot_send(chat_id, f"twitch.tv/{html.escape(channel)} уже в списке.")
            return
        registry.TWITCH_CHANNELS.append(channel)
        registry.save_twitch_channels_file()
        total = len(registry.TWITCH_CHANNELS)
    registry.TWITCH_RELOAD.set()
    bot_send(
        chat_id,
        f"{icon('ok')} twitch.tv/{html.escape(channel)} добавлен. "
        f"Twitch-каналов: {total}",
    )
    log.info("Бот: twitch-канал %s добавлен, всего %s", channel, total)


def cmd_removetwitch(chat_id: str, argument: str) -> None:
    channel = _parse_twitch_channel(chat_id, "/removetwitch", argument)
    if channel is None:
        return
    with registry.TWITCH_CHANNELS_LOCK:
        if channel not in registry.TWITCH_CHANNELS:
            bot_send(chat_id, f"twitch.tv/{html.escape(channel)} нет в списке.")
            return
        registry.TWITCH_CHANNELS.remove(channel)
        registry.save_twitch_channels_file()
        total = len(registry.TWITCH_CHANNELS)
    registry.TWITCH_RELOAD.set()
    bot_send(
        chat_id,
        f"{icon('stop')} twitch.tv/{html.escape(channel)} удалён. "
        f"Twitch-каналов: {total}",
    )
    log.info("Бот: twitch-канал %s удалён, всего %s", channel, total)


def _parse_telegram_channel(chat_id: str, command: str, argument: str) -> str | None:
    match = USERNAME_RE.match(argument)
    if not match:
        bot_send(chat_id, f"Укажите канал: <code>{command} @channel</code>")
        return None
    return match.group(1)


def cmd_add(chat_id: str, argument: str) -> None:
    channel = _parse_telegram_channel(chat_id, "/add", argument)
    if channel is None:
        return
    if channel in registry.channels_snapshot():
        bot_send(chat_id, f"@{html.escape(channel)} уже в списке.")
        return
    # Валидация до добавления. Сетевой запрос выполняем БЕЗ
    # CHANNELS_LOCK, чтобы не блокировать основной цикл парсинга.
    status = check_channel_preview(channel)
    if status == "not_found":
        bot_send(
            chat_id,
            f"{icon('warn')} @{html.escape(channel)} не найден: "
            "канал не существует или приватный. Не добавлен.",
        )
        return
    if status == "no_preview":
        bot_send(
            chat_id,
            f"{icon('warn')} У @{html.escape(channel)} недоступна лента "
            "t.me/s (веб-превью отключено или канал пуст) — парсер не "
            "сможет читать его сообщения. Не добавлен.",
        )
        return
    note = (
        ""
        if status == "ok"
        else (
            f"\n{icon('warn')} Проверить канал не удалось "
            "(сетевая ошибка) — добавлен без проверки."
        )
    )
    with registry.CHANNELS_LOCK:
        if channel in registry.CHANNELS:
            bot_send(chat_id, f"@{html.escape(channel)} уже в списке.")
            return
        registry.CHANNELS.append(channel)
        registry.save_channels_file()
        total = len(registry.CHANNELS)
    bot_send(
        chat_id,
        f"{icon('ok')} @{html.escape(channel)} добавлен. Каналов: {total}{note}",
    )
    log.info("Бот: канал @%s добавлен, всего %s", channel, total)


def cmd_remove(chat_id: str, argument: str) -> None:
    channel = _parse_telegram_channel(chat_id, "/remove", argument)
    if channel is None:
        return
    with registry.CHANNELS_LOCK:
        if channel not in registry.CHANNELS:
            bot_send(chat_id, f"@{html.escape(channel)} нет в списке.")
            return
        registry.CHANNELS.remove(channel)
        registry.save_channels_file()
        total = len(registry.CHANNELS)
    bot_send(
        chat_id, f"{icon('stop')} @{html.escape(channel)} удалён. Каналов: {total}"
    )
    log.info("Бот: канал @%s удалён, всего %s", channel, total)


COMMAND_HANDLERS: dict[str, Callable[[str, str], None]] = {
    "/start": cmd_start,
    "/menu": cmd_menu,
    "/help": cmd_help,
    "/status": cmd_status,
    "/top": cmd_top,
    "/wheels": cmd_wheels,
    "/active": cmd_active,
    "/removewheel": cmd_removewheel,
    "/delwheel": cmd_removewheel,
    "/channels": cmd_channels,
    "/words": cmd_words,
    "/addword": cmd_addword,
    "/removeword": cmd_removeword,
    "/twitch": cmd_twitch,
    "/addtwitch": cmd_addtwitch,
    "/removetwitch": cmd_removetwitch,
    "/add": cmd_add,
    "/remove": cmd_remove,
}


def handle_command(chat_id: str, text: str) -> None:
    """Разбирает «/команда аргумент» и вызывает обработчик.

    Неизвестные команды игнорируются молча: в чат может прилететь
    команда другого бота.
    """
    parts = text.strip().split(maxsplit=1)
    command = parts[0].split("@", 1)[0].lower()
    argument = parts[1].strip() if len(parts) > 1 else ""
    handler = COMMAND_HANDLERS.get(command)
    if handler is not None:
        handler(chat_id, argument)


# ----------------------------------------------------------------------------
# Цикл getUpdates
# ----------------------------------------------------------------------------

def bot_loop() -> None:
    try:
        BOT_SESSION.post(
            f"{BOT_API}/setMyCommands",
            json={"commands": BOT_COMMANDS},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as error:
        log.warning("Бот: не удалось зарегистрировать меню команд: %s", error)

    # offset переживает рестарт: подтверждённый update_id хранится в
    # bot_state.json, чтобы не обработать один и тот же бэклог дважды.
    offset = load_bot_offset()
    while not STOP_EVENT.is_set():
        try:
            response = BOT_SESSION.get(
                f"{BOT_API}/getUpdates",
                params={"timeout": 25, "offset": offset},
                timeout=REQUEST_TIMEOUT + 30,
            )
            response.raise_for_status()
            updates = response.json().get("result", [])
        except (requests.RequestException, ValueError) as error:
            log.warning("Бот: ошибка получения обновлений: %s", error)
            STOP_EVENT.wait(5)
            continue
        offset_before = offset
        stale_count = 0
        for update in updates:
            offset = max(offset, int(update.get("update_id", 0)) + 1)
            message = update.get("message") or {}
            text = str(message.get("text") or "")
            chat_id = str((message.get("chat") or {}).get("id", ""))
            if not chat_id or not text.startswith("/"):
                continue
            if not TELEGRAM_CHAT_ID or chat_id != TELEGRAM_CHAT_ID:
                # Команды принимаем только из доверенного чата. Пустой
                # TELEGRAM_CHAT_ID означал бы «командовать может кто угодно»,
                # поэтому без него команды полностью отключены.
                continue
            # Устаревшие команды (например, отправленные, пока парсер лежал)
            # подтверждаем сдвигом offset, но не выполняем: отвечать на
            # команды суточной давности бессмысленно и создаёт спам в чате.
            message_date = int(message.get("date") or 0)
            if message_date and time.time() - message_date > STALE_COMMAND_SECONDS:
                stale_count += 1
                continue
            try:
                handle_command(chat_id, text)
            except Exception:
                log.exception("Бот: ошибка обработки команды %r", text)
        if offset > offset_before:
            save_bot_offset(offset)
        if stale_count:
            log.info(
                "%s Бот: пропущено устаревших команд из бэклога: %s",
                icon("bell"),
                stale_count,
            )
