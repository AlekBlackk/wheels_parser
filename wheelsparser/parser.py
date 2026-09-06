"""Основной цикл: обход Telegram-каналов и рассылка находок."""

from __future__ import annotations

import queue
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any

from . import db, menu, registry
from .betboom import (
    precheck_wheel,
    process_candidate_wheel,
)
from .channel_health import (
    CHANNEL_EMPTY_ALERTED,
    CHANNEL_EMPTY_STREAK,
    CHANNEL_FAIL_ALERTED,
    CHANNEL_FAIL_STREAK,
    LAYOUT_ALERTED,
    _alert_empty_channel,
    _alert_layout_change,
    report_empty_channels,
    update_channel_empty_streaks,
    update_channel_fail_streaks,
)
from .config import (
    ALERT_ON_FIRST_RUN,
    CHANNEL_EMPTY_THRESHOLD,
    CHANNEL_FAIL_THRESHOLD,
    CHANNEL_FETCH_CONCURRENCY,
    CHECK_INTERVAL,
    MAX_RESULTS,
    PREVIEW_CHAR_LIMIT,
    REALERT_COOLDOWN_MINUTES,
    icon,
)
from .db import WheelEntry, make_wheel_entry
from .keywords import find_keywords
from .logging_setup import log
from .net import PARSER_SESSION, ThreadLocalSession, build_session
from .predictive import drain_predictive_entries
from .retries import (
    _RETRY_CANDIDATE_POOL_MULTIPLIER,
    PENDING_EXPIRED_RETRY,
    RETRY_ATTEMPT_COUNTS,
    _drop_pending_expired,
    _is_on_cooldown,
    _mark_handled,
    _register_pending_expired,
    load_pending_expired_retry,
    retry_expired_links,
    retry_failed_notifications,
)
from .runtime import STOP_EVENT
from .storage import (
    load_pending_expired,
    mark_channel_suggested,
    save_pending_expired,
    save_seen,
)
from .telegram_api import (
    notifications_enabled,
    send_keyword_notification,
    send_multi_telegram_notification,
    send_service_notification,
    send_telegram_notification,
)
from .telegram_scrape import (
    _TELEGRAM_HOSTS,
    FORWARD_SOURCE_SELECTOR,
    fetch_channel,
    forwarded_from_channel,
    message_preview_html,
)
from .timeutils import now_msk, parse_found_at
from .twitch import TWITCH_NEW_ENTRIES, TWITCH_PENDING_RETRY
from .urls import (
    extract_urls,
    find_disallowed_domains,
    find_urls,
    is_betboom_host,
    legacy_normalize_url,
    message_content_hash,
    normalize_url,
)

__all__ = [
    "CHANNEL_EMPTY_ALERTED",
    "CHANNEL_EMPTY_STREAK",
    "CHANNEL_EMPTY_THRESHOLD",
    "CHANNEL_FAIL_ALERTED",
    "CHANNEL_FAIL_STREAK",
    "CHANNEL_FAIL_THRESHOLD",
    "FORWARD_SOURCE_SELECTOR",
    "LAYOUT_ALERTED",
    "PARSER_SESSION",
    "PENDING_EXPIRED_RETRY",
    "RETRY_ATTEMPT_COUNTS",
    "_RETRY_CANDIDATE_POOL_MULTIPLIER",
    "_TELEGRAM_HOSTS",
    "_alert_empty_channel",
    "_alert_layout_change",
    "_drop_pending_expired",
    "_fetch_all_channels",
    "_is_on_cooldown",
    "_mark_handled",
    "_register_pending_expired",
    "collect_pending_entries",
    "drain_twitch_entries",
    "drain_twitch_retry_registrations",
    "extract_urls",
    "fetch_channel",
    "find_disallowed_domains",
    "find_urls",
    "forwarded_from_channel",
    "index_last_found",
    "is_betboom_host",
    "legacy_normalize_url",
    "load_cooldown_window",
    "load_pending_expired",
    "load_pending_expired_retry",
    "message_content_hash",
    "message_preview_html",
    "notifications_enabled",
    "notify_keywords",
    "notify_pending_entries",
    "process_cycle",
    "process_message",
    "report_empty_channels",
    "retry_expired_links",
    "retry_failed_notifications",
    "save_pending_expired",
    "suggest_forward_source",
    "update_channel_empty_streaks",
    "update_channel_fail_streaks",
]


def _fetch_all_channels(
    channels: list[str],
) -> list[tuple[str, list[dict[str, Any]] | None]]:
    """Скачивает страницы всех каналов параллельно, возвращая результаты
    в исходном порядке channels (не в порядке завершения запросов).

    Одновременно выполняется не больше CHANNEL_FETCH_CONCURRENCY запросов.
    Использует ThreadLocalSession, гарантирующий закрытие сессий воркеров (p2-misc-6, p2-dup-3).
    """
    with ThreadLocalSession(session_factory=build_session) as worker_session:
        def fetch(channel: str) -> tuple[str, list[dict[str, Any]] | None]:
            if STOP_EVENT.is_set():
                return channel, None
            try:
                return channel, fetch_channel(channel, worker_session())
            except Exception:
                log.exception("[%s] сбой при опросе канала", channel)
                return channel, None

        with ThreadPoolExecutor(max_workers=CHANNEL_FETCH_CONCURRENCY) as pool:
            return list(pool.map(fetch, channels))


def drain_twitch_entries() -> list[WheelEntry]:
    """Забирает находки twitch-потока: уведомления по ним уже отправлены,
    осталось сохранить их в базу и учесть в дедупликации."""
    entries: list[WheelEntry] = []
    while True:
        try:
            entries.append(TWITCH_NEW_ENTRIES.get_nowait())
        except queue.Empty:
            return entries


def drain_twitch_retry_registrations() -> None:
    """Регистрирует на ретрай ссылки twitch-worker'а, пропущенные как
    expired/soon (см. twitch.TWITCH_PENDING_RETRY).

    PENDING_EXPIRED_RETRY трогает только parser-поток (см. storage.py —
    словарь и файл без лока), поэтому twitch-worker не пишет туда
    напрямую, а кладёт заявку в очередь; parser забирает её в начале
    каждого цикла, как и TWITCH_NEW_ENTRIES.
    """
    while True:
        try:
            job = TWITCH_PENDING_RETRY.get_nowait()
        except queue.Empty:
            return
        _register_pending_expired(
            job["url"], job["channel"], job["message"], job["post_text"], job["now"]
        )


def load_cooldown_window(now: datetime) -> list[WheelEntry]:
    """Находки, способные подавить повтор: только они и нужны кулдауну.

    Старше REALERT_COOLDOWN_MINUTES кулдаун не смотрит, поэтому вся
    история из базы не поднимается — берётся окно.
    """
    return db.wheels_since(now - timedelta(minutes=REALERT_COOLDOWN_MINUTES))


def index_last_found(results: list[WheelEntry]) -> dict[str, datetime]:
    """URL -> время последней находки.

    Раньше дедупликация была глобальной («один URL — одно уведомление за всю
    историю»), из-за чего повторный запуск колеса на том же адресе молча
    игнорировался. Теперь повтор подавляется только в течение
    REALERT_COOLDOWN_MINUTES.
    """
    last_found: dict[str, datetime] = {}
    for item in results:
        item_url = normalize_url(str(item.get("url", "")))
        if not item_url:
            continue
        found = parse_found_at(item.get("found_at"))
        if found is None:
            continue
        if item_url not in last_found or found > last_found[item_url]:
            last_found[item_url] = found
    return last_found


def collect_pending_entries(
    message: dict[str, Any],
    channel: str,
    now: datetime,
    last_found: dict[str, datetime],
    is_edited_message: bool,
) -> list[WheelEntry]:
    """Ссылки поста, о которых нужно оповестить (с учётом кулдауна и precheck)."""
    pending: list[WheelEntry] = []
    post_text = message["text"] if len(message["urls"]) == 1 else ""
    for url in message["urls"]:
        entry, _status, retry_needed = process_candidate_wheel(
            url,
            channel,
            now,
            post_text=post_text,
            last_found=last_found,
            source="telegram",
            msg_id=message["id"],
            message_url=message["message_url"],
            preview=message["text"][:PREVIEW_CHAR_LIMIT],
            preview_html=message.get("preview_html", ""),
            is_edited=is_edited_message,
            precheck_fn=precheck_wheel,
        )
        if retry_needed:
            _register_pending_expired(url, channel, message, post_text, now)
            continue
        if entry is not None:
            pending.append(entry)
    return pending


def notify_pending_entries(
    pending: list[WheelEntry],
    channel: str,
    now: datetime,
    last_found: dict[str, datetime],
    is_edited_message: bool,
) -> None:
    """Рассылает уведомления по ссылкам одного поста.

    Один пост может дать несколько «новых» ссылок — например, «хвост»:
    старый href от копипасты прошлого поста рядом с актуальной ссылкой.
    Статус API их не всегда различает (см. betboom.api_info_to_status —
    fail-open по дизайну), поэтому вместо N отдельных «Новая ссылка» шлём
    одно сообщение со списком: какая ссылка настоящая, решает человек.
    """
    if len(pending) == 1:
        entry = pending[0]
        entry["notified"] = send_telegram_notification(entry)
        _mark_handled(str(entry.get("url", "")), now, last_found)
        log.info(
            "%s %s [@%s]: %s",
            icon("link"),
            "Ссылка из правки поста" if is_edited_message else "Новая ссылка",
            channel,
            entry.get("url", ""),
            extra={"highlight": True},
        )
    elif pending:
        sent = send_multi_telegram_notification(pending)
        for entry in pending:
            entry["notified"] = sent
            _mark_handled(str(entry.get("url", "")), now, last_found)
        log.info(
            "%s %s ссылок в одном посте [@%s]: %s",
            icon("link"),
            len(pending),
            channel,
            ", ".join(str(entry.get("url", "")) for entry in pending),
            extra={"highlight": True},
        )


def notify_keywords(message: dict[str, Any], channel: str) -> list[WheelEntry]:
    """Уведомление о ключевых словах и запись о нём для истории."""
    matched = find_keywords(message["text"])
    if not matched:
        return []
    disallowed_domains = message.get("disallowed_domains") or []
    if disallowed_domains:
        log.info(
            "%s Пропускаю ключевые слова (%s) [@%s]: подозрительная ссылка (%s)",
            icon("warn"),
            ", ".join(matched),
            channel,
            ", ".join(disallowed_domains),
        )
        return []
    entry = make_wheel_entry(
        found_at=now_msk().isoformat(timespec="seconds"),
        channel=channel,
        source="telegram",
        msg_id=message["id"],
        message_url=message["message_url"],
        preview=message["text"][:PREVIEW_CHAR_LIMIT],
        preview_html=message.get("preview_html", ""),
        keywords=matched,
        notified=False,
    )
    entry["notified"] = send_keyword_notification(entry)
    log.info(
        "%s Ключевые слова (%s) [@%s]: %s",
        icon("bell"),
        ", ".join(matched),
        channel,
        entry.get("message_url", ""),
        extra={"highlight": True},
    )
    return [entry]


def suggest_forward_source(message: dict[str, Any], channel: str) -> None:
    """Предлагает админу добавить канал-первоисточник репоста."""
    source = message.get("forwarded_from", "")
    if not source:
        return
    source_key = source.casefold()
    if source_key == channel.casefold():
        return
    monitored = {name.casefold() for name in registry.channels_snapshot()}
    if source_key in monitored:
        return
    if not (message["urls"] or find_keywords(message["text"])):
        return
    if not mark_channel_suggested(source_key):
        return
    log.info(
        "%s Найден первоисточник репоста: @%s (репост в @%s)",
        icon("scan"),
        source,
        channel,
    )
    send_service_notification(
        f"{icon('scan')} Похоже, нашёлся первоисточник: @{source}\n"
        f"Его пост с колесом репостнул @{channel}, но самого канала нет "
        f"в мониторинге.\n{message['message_url']}",
        reply_markup=menu.channel_suggestion_keyboard(source),
    )


def process_message(
    message: dict[str, Any],
    channel: str,
    channel_seen: dict[str, str],
    channel_baseline: bool,
    now: datetime,
    last_found: dict[str, datetime],
) -> list[WheelEntry]:
    """Обрабатывает одно сообщение канала и возвращает новые записи истории."""
    previous_hash = channel_seen.get(message["id"])
    is_new_message = previous_hash is None
    is_edited_message = (
        not is_new_message
        and bool(previous_hash)
        and previous_hash != message["hash"]
        and previous_hash != message["legacy_hash"]
    )
    channel_seen[message["id"]] = message["hash"]
    if channel_baseline or not (is_new_message or is_edited_message):
        return []

    pending = collect_pending_entries(
        message, channel, now, last_found, is_edited_message
    )
    notify_pending_entries(pending, channel, now, last_found, is_edited_message)

    if is_new_message and not pending:
        pending.extend(notify_keywords(message, channel))
    suggest_forward_source(message, channel)
    return pending


def process_cycle(
    seen: dict[str, dict[str, str]],
    baseline: bool = False,
) -> int:
    cycle_started = time.monotonic()
    channels = registry.channels_snapshot()
    log.info("%s Начинаю проверку · каналов %s", icon("scan"), len(channels))
    now = now_msk()
    twitch_entries = drain_twitch_entries() + drain_predictive_entries()
    drain_twitch_retry_registrations()
    try:
        db.insert_entries(twitch_entries)
    except Exception:
        log.exception(
            "%s Не удалось записать находки Twitch в базу — они "
            "потеряны для истории, продолжаю цикл",
            icon("warn"),
        )
    retry_failed_notifications(now)
    last_found = index_last_found(load_cooldown_window(now))
    new_entries: list[WheelEntry] = []
    expired_retry_entries = retry_expired_links(now, last_found)
    if expired_retry_entries:
        try:
            db.insert_entries(expired_retry_entries)
        except Exception:
            log.exception(
                "%s Не удалось записать находку из ретрая expired-ссылок в базу",
                icon("warn"),
            )
        else:
            new_entries.extend(expired_retry_entries)
    failed_channels: list[str] = []
    empty_channels: list[str] = []
    checked_channels: list[str] = []

    fetched = [] if STOP_EVENT.is_set() else _fetch_all_channels(channels)

    for channel, messages in fetched:
        if STOP_EVENT.is_set():
            break
        checked_channels.append(channel)
        if messages is None:
            failed_channels.append(channel)
            messages = []
        elif not messages:
            empty_channels.append(channel)
        channel_seen = seen.setdefault(channel, {})
        channel_baseline = baseline or (not channel_seen and not ALERT_ON_FIRST_RUN)
        for message in messages:
            entries = process_message(
                message, channel, channel_seen, channel_baseline, now, last_found
            )
            try:
                db.insert_entries(entries)
            except Exception:
                log.exception(
                    "%s Не удалось записать находку в базу [@%s, %s]",
                    icon("warn"),
                    channel,
                    message.get("id"),
                )
            else:
                new_entries.extend(entries)

    update_channel_fail_streaks(checked_channels, failed_channels)
    update_channel_empty_streaks(checked_channels, failed_channels, empty_channels)
    report_empty_channels(checked_channels, failed_channels)
    save_seen(seen)
    if new_entries or twitch_entries:
        db.prune(MAX_RESULTS)
    status_icon = icon("warn") if failed_channels or empty_channels else icon("ok")
    empty_note = f" · пустых лент: {len(empty_channels)}" if empty_channels else ""
    stopped_note = (
        f" · остановлено, пропущено {len(channels) - len(checked_channels)}"
        if len(checked_channels) < len(channels)
        else ""
    )
    elapsed = time.monotonic() - cycle_started
    next_at = (
        now_msk() + timedelta(seconds=max(5.0, CHECK_INTERVAL - elapsed))
    ).strftime("%H:%M:%S")
    suffix = "" if STOP_EVENT.is_set() else f" · следующая проверка в {next_at}"
    if elapsed > CHECK_INTERVAL:
        log.warning(
            "%s Цикл занял %.1fс — дольше CHECK_INTERVAL (%sс); "
            "следующий начинается без паузы",
            icon("warn"),
            elapsed,
            CHECK_INTERVAL,
        )
    log.info(
        "%s Цикл завершён · каналы %s/%s%s%s · новых ссылок: %s%s",
        status_icon,
        len(checked_channels) - len(failed_channels),
        len(checked_channels),
        empty_note,
        stopped_note,
        sum(1 for entry in new_entries if entry.get("url")),
        suffix,
    )
    if failed_channels:
        log.warning("%s Недоступные каналы: %s", icon("warn"), ", ".join(failed_channels))
    if empty_channels:
        log.warning(
            "%s Каналы без распознанных постов: %s",
            icon("warn"),
            ", ".join(empty_channels),
        )
    return sum(1 for entry in new_entries if entry.get("url"))
