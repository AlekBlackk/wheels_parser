"""Основной цикл: обход Telegram-каналов и рассылка находок."""

from __future__ import annotations

import html
import queue
import random
import re
import time
from datetime import datetime, timedelta
from typing import Any

import requests
from bs4 import BeautifulSoup

from . import registry
from .alerts import last_alert, mark_url_alert
from .betboom import is_referral_wheel, precheck_wheel
from .config import (
    ALERT_ON_FIRST_RUN,
    CHANNEL_FAIL_THRESHOLD,
    CHECK_INTERVAL,
    MESSAGES_PER_CHANNEL,
    NOTIFY_RETRY_MAX_PER_CYCLE,
    NOTIFY_RETRY_WINDOW_MINUTES,
    PRECHECK_WHEELS,
    REALERT_COOLDOWN_MINUTES,
    REQUEST_TIMEOUT,
    icon,
)
from .keywords import find_keywords
from .logging_setup import log
from .net import PARSER_SESSION
from .runtime import STOP_EVENT
from .storage import save_results, save_seen
from .telegram_api import (
    notifications_enabled,
    send_keyword_notification,
    send_multi_telegram_notification,
    send_service_notification,
    send_telegram_notification,
)
from .timeutils import now_msk, parse_found_at
from .twitch import TWITCH_NEW_ENTRIES
from .urls import (
    extract_urls,
    find_urls,
    legacy_normalize_url,
    message_content_hash,
    normalize_url,
)

# ----------------------------------------------------------------------------
# Чтение канала
# ----------------------------------------------------------------------------

def message_preview_html(text_element: Any, limit: int = 200) -> str:
    """HTML-превью текста поста для отправки с parse_mode=HTML.

    Кликабельные ссылки из поста (например «Твич | ВК») сохраняются как
    <a href="...">, остальной текст экранируется. limit ограничивает видимую
    длину текста (HTML-теги не считаются).
    """
    if text_element is None:
        return ""
    tokens: list[tuple[str, str, str]] = []  # (вид, текст/подпись, href)

    def walk(node: Any) -> None:
        for child in node.children:
            if getattr(child, "name", None) is None:
                chunk = re.sub(r"\s+", " ", str(child)).strip()
                if chunk:
                    tokens.append(("text", chunk, ""))
            elif child.name == "a" and child.get("href"):
                label = re.sub(r"\s+", " ", child.get_text(" ", strip=True)).strip()
                href = str(child["href"]).strip()
                if label and href:
                    tokens.append(("link", label, href))
                elif label:
                    tokens.append(("text", label, ""))
            else:
                walk(child)

    walk(text_element)

    parts: list[str] = []
    visible = 0
    for kind, label, href in tokens:
        if visible >= limit:
            parts.append("…")
            break
        if kind == "link":
            if visible + len(label) > limit:
                parts.append("…")
                break
            parts.append(
                f'<a href="{html.escape(href, quote=True)}">{html.escape(label)}</a>'
            )
        else:
            if visible + len(label) > limit:
                cut = label[: limit - visible].rstrip()
                if cut:
                    parts.append(html.escape(cut))
                parts.append("…")
                break
            parts.append(html.escape(label))
        visible += len(label) + 1
    return " ".join(parts)


def fetch_channel(channel: str) -> list[dict[str, Any]] | None:
    """Последние сообщения канала через веб-превью t.me/s/<channel>.

    None означает, что канал прочитать не удалось (404 или сетевая ошибка).
    """
    url = f"https://t.me/s/{channel}"
    try:
        response = PARSER_SESSION.get(url, timeout=REQUEST_TIMEOUT)
        if response.status_code == 404:
            log.warning("[%s] канал не найден или приватный (404)", channel)
            return None
        response.raise_for_status()
    except requests.RequestException as error:
        log.warning("[%s] ошибка запроса: %s", channel, error)
        return None

    # response.content вместо response.text: если сервер не указал charset,
    # requests подставляет latin-1 и кириллица превращается в кракозябры.
    # BeautifulSoup сам определяет UTF-8 по <meta charset> страницы.
    soup = BeautifulSoup(response.content, "html.parser")
    messages = soup.select(".tgme_widget_message_wrap")[-MESSAGES_PER_CHANNEL:]
    results: list[dict[str, Any]] = []
    for message in messages:
        bubble = message.select_one(".tgme_widget_message")
        if not bubble:
            continue
        message_id = str(bubble.get("data-post", "")).strip()
        if not message_id:
            continue
        text_element = message.select_one(".tgme_widget_message_text")
        text = text_element.get_text(" ", strip=True) if text_element else ""
        urls = find_urls(message, text)
        results.append({
            "id": message_id,
            "text": text,
            "preview_html": message_preview_html(text_element),
            "urls": urls,
            "hash": message_content_hash(text, urls),
            # Хэш в формате старых версий (URL с query-параметрами): сравнение
            # с ним не даёт принять смену формата хэша за правку поста.
            "legacy_hash": message_content_hash(
                text, extract_urls(message, text, legacy_normalize_url)
            ),
            "message_url": f"https://t.me/{message_id}",
        })
    return results


# ----------------------------------------------------------------------------
# Мониторинг «мёртвых» каналов
# ----------------------------------------------------------------------------
# Счётчики подряд неудачных циклов per-канал.
# Доступ только из parser-потока — блокировка не нужна.

CHANNEL_FAIL_STREAK: dict[str, int] = {}
CHANNEL_FAIL_ALERTED: set[str] = set()


def update_channel_fail_streaks(
    checked_channels: list[str], failed_channels: list[str]
) -> None:
    """Обновляет счётчики недоступности и один раз уведомляет о «мёртвом» канале."""
    failed = set(failed_channels)
    for channel in checked_channels:
        if channel in failed:
            CHANNEL_FAIL_STREAK[channel] = CHANNEL_FAIL_STREAK.get(channel, 0) + 1
            if (
                CHANNEL_FAIL_STREAK[channel] >= CHANNEL_FAIL_THRESHOLD
                and channel not in CHANNEL_FAIL_ALERTED
            ):
                CHANNEL_FAIL_ALERTED.add(channel)
                log.warning(
                    "%s Канал @%s недоступен %s циклов подряд — отправляю уведомление",
                    icon("warn"),
                    channel,
                    CHANNEL_FAIL_STREAK[channel],
                )
                send_service_notification(
                    f"{icon('warn')} Канал @{channel} недоступен "
                    f"{CHANNEL_FAIL_STREAK[channel]} циклов подряд.\n"
                    "Возможно, он удалён, стал приватным или отключил веб-превью.\n"
                    f"Убрать из списка: /remove {channel}"
                )
        else:
            # Канал снова доступен — сбрасываем счётчик и разрешаем
            # повторное уведомление при следующей серии неудач.
            CHANNEL_FAIL_STREAK.pop(channel, None)
            CHANNEL_FAIL_ALERTED.discard(channel)
    # Чистим счётчики каналов, удалённых через /remove.
    current = set(registry.channels_snapshot())
    for channel in list(CHANNEL_FAIL_STREAK):
        if channel not in current:
            CHANNEL_FAIL_STREAK.pop(channel, None)
            CHANNEL_FAIL_ALERTED.discard(channel)


# ----------------------------------------------------------------------------
# Обработка сообщений
# ----------------------------------------------------------------------------

def drain_twitch_entries() -> list[dict[str, Any]]:
    """Забирает находки twitch-потока: уведомления по ним уже отправлены,
    осталось сохранить их в freebets.json и учесть в дедупликации."""
    entries: list[dict[str, Any]] = []
    while True:
        try:
            entries.append(TWITCH_NEW_ENTRIES.get_nowait())
        except queue.Empty:
            return entries


def index_last_found(results: list[dict[str, Any]]) -> dict[str, datetime]:
    """URL -> время последней находки.

    Раньше дедупликация была глобальной («один URL — одно уведомление за всю
    историю»), из-за чего повторный запуск колеса на том же адресе молча
    игнорировался. Теперь повтор подавляется только в течение
    REALERT_COOLDOWN_MINUTES.
    """
    last_found: dict[str, datetime] = {}
    for item in results:
        # Канонизация и здесь: старые записи freebets.json могли сохранить
        # URL с query-параметрами — без нормализации кулдаун их не увидит.
        item_url = normalize_url(str(item.get("url", "")))
        if not item_url:
            continue
        found = parse_found_at(item.get("found_at"))
        if found is None:
            continue
        if item_url not in last_found or found > last_found[item_url]:
            last_found[item_url] = found
    return last_found


def retry_failed_notifications(results: list[dict[str, Any]], now: datetime) -> int:
    """Повторно отправляет уведомления, недоставленные в своём цикле.

    Пост обрабатывается по хэшу содержимого только один раз (см.
    process_message) — без ретрая сбой Telegram ровно в момент отправки
    терял бы находку навсегда, хотя она и осталась в freebets.json с
    notified=False. Записи из send_multi_telegram_notification (несколько
    ссылок в одном посте) при ретрае отправляются по одной обычным
    уведомлением — предупреждение «уточните вручную» теряется, но сами
    ссылки доходят, что важнее для этого редкого повторного случая.
    Лимит и окно ограничивают стоимость длительного сбоя: не тратим
    весь цикл на HTTP-ретраи по всему бэклогу разом.

    Записи с delivery_unknown пропускаются: sendMessage не идемпотентен,
    и при таймауте чтения или 5xx сообщение могло уже уйти в чат —
    повтор такой отправки рассылал бы дубликаты (см.
    telegram_api.delivery_unknown).
    """
    if not notifications_enabled():
        return 0
    retried = 0
    for entry in results:
        if retried >= NOTIFY_RETRY_MAX_PER_CYCLE:
            break
        if entry.get("notified") or not entry.get("url"):
            continue
        if entry.get("delivery_unknown"):
            continue
        found = parse_found_at(entry.get("found_at"))
        if found is None or now - found > timedelta(minutes=NOTIFY_RETRY_WINDOW_MINUTES):
            continue
        entry["notified"] = send_telegram_notification(entry)
        retried += 1
        if entry["notified"]:
            log.info(
                "%s Уведомление доставлено повторной попыткой: %s",
                icon("ok"),
                entry.get("url"),
            )
    return retried


def _is_on_cooldown(url: str, now: datetime, last_found: dict[str, datetime]) -> bool:
    previous = last_found.get(url)
    # Кулдаун общий с Twitch: находки twitch-потока с момента последнего
    # цикла ещё не попали в results, но уже отмечены в LAST_URL_ALERT.
    cross_source = last_alert(url)
    if cross_source and (previous is None or cross_source > previous):
        previous = cross_source
    return bool(
        previous and now - previous <= timedelta(minutes=REALERT_COOLDOWN_MINUTES)
    )


def _mark_handled(url: str, now: datetime, last_found: dict[str, datetime]) -> None:
    last_found[url] = now
    mark_url_alert(url, now)


def collect_pending_entries(
    message: dict[str, Any],
    channel: str,
    now: datetime,
    last_found: dict[str, datetime],
    is_edited_message: bool,
) -> list[dict[str, Any]]:
    """Ссылки поста, о которых нужно оповестить (с учётом кулдауна и precheck)."""
    pending: list[dict[str, Any]] = []
    for url in message["urls"]:
        if _is_on_cooldown(url, now, last_found):
            continue  # недавно уже оповещали об этом колесе
        # Проверяем колесо через API BetBoom до отправки: «хвосты» —
        # старые href на прошлые (уже завершившиеся) колёса — молча
        # пропускаем. Статусы 'active'/'soon'/'unknown' рассылаются,
        # unknown — fail-open, чтобы не терять живые колёса при сбое API.
        if PRECHECK_WHEELS:
            status, referral = precheck_wheel(url)
        else:
            status, referral = "", is_referral_wheel(url, None)
        if status == "expired":
            log.info(
                "%s Пропускаю %s [@%s]: колесо уже завершилось (API BetBoom)",
                icon("warn"),
                url,
                channel,
            )
            _mark_handled(url, now, last_found)
            continue
        pending.append({
            "url": url,
            "found_at": now_msk().isoformat(timespec="seconds"),
            "channel": channel,
            "msg_id": message["id"],
            "message_url": message["message_url"],
            "preview": message["text"][:200],
            "edited": is_edited_message,
            "status": status,
            "referral": referral,
            "notified": False,
        })
    return pending


def notify_pending_entries(
    pending: list[dict[str, Any]],
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
        _mark_handled(entry["url"], now, last_found)
        log.info(
            "%s %s [@%s]: %s",
            icon("link"),
            "Ссылка из правки поста" if is_edited_message else "Новая ссылка",
            channel,
            entry["url"],
            extra={"highlight": True},
        )
    elif pending:
        sent = send_multi_telegram_notification(pending)
        for entry in pending:
            entry["notified"] = sent
            _mark_handled(entry["url"], now, last_found)
        log.info(
            "%s %s ссылок в одном посте [@%s]: %s",
            icon("link"),
            len(pending),
            channel,
            ", ".join(entry["url"] for entry in pending),
            extra={"highlight": True},
        )


def notify_keywords(message: dict[str, Any], channel: str) -> None:
    matched = find_keywords(message["text"])
    if not matched:
        return
    entry = {
        "found_at": now_msk().isoformat(timespec="seconds"),
        "channel": channel,
        "msg_id": message["id"],
        "message_url": message["message_url"],
        "preview": message["text"][:200],
        "preview_html": message.get("preview_html", ""),
        "keywords": matched,
    }
    send_keyword_notification(entry)
    log.info(
        "%s Ключевые слова (%s) [@%s]: %s",
        icon("bell"),
        ", ".join(matched),
        channel,
        entry["message_url"],
        extra={"highlight": True},
    )


def process_message(
    message: dict[str, Any],
    channel: str,
    channel_seen: dict[str, str],
    channel_baseline: bool,
    now: datetime,
    last_found: dict[str, datetime],
) -> list[dict[str, Any]]:
    """Обрабатывает одно сообщение канала и возвращает новые записи истории."""
    previous_hash = channel_seen.get(message["id"])
    is_new_message = previous_hash is None
    # Правка поста: хэш содержимого изменился. Пустой сохранённый хэш
    # означает «содержимое неизвестно» (миграция со старого формата
    # seen_ids.json) — правкой не считаем, просто запоминаем хэш.
    # Совпадение с legacy_hash (формат до канонизации URL) — тоже
    # не правка: содержимое поста не менялось, изменился только
    # способ расчёта хэша; сохранённый хэш молча обновляется ниже.
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

    # Поиск по ключевым словам — только для новых сообщений без ссылок:
    # ссылки не дублируют уведомление о найденном колесе, а правки
    # постов проверяем лишь на ссылки — иначе каждая мелкая правка
    # текста с ключевым словом слала бы повторное уведомление.
    if is_new_message and not message["urls"]:
        notify_keywords(message, channel)
    return pending


# ----------------------------------------------------------------------------
# Цикл
# ----------------------------------------------------------------------------

def process_cycle(
    seen: dict[str, dict[str, str]],
    results: list[dict[str, Any]],
    baseline: bool = False,
) -> int:
    cycle_started = time.monotonic()
    channels = registry.channels_snapshot()
    log.info("%s Начинаю проверку · каналов %s", icon("scan"), len(channels))
    now = now_msk()
    twitch_entries = drain_twitch_entries()
    if twitch_entries:
        results.extend(twitch_entries)
    retried = retry_failed_notifications(results, now)
    last_found = index_last_found(results)
    new_entries: list[dict[str, Any]] = []
    failed_channels: list[str] = []
    checked_channels: list[str] = []

    for index, channel in enumerate(channels):
        if STOP_EVENT.is_set():
            break
        messages = fetch_channel(channel)
        checked_channels.append(channel)
        if messages is None:
            failed_channels.append(channel)
            messages = []
        channel_seen = seen.setdefault(channel, {})
        # Канал, добавленный через /add на лету, сначала проходит «тихий» цикл,
        # чтобы не рассылать уведомления по его старым сообщениям.
        channel_baseline = baseline or (not channel_seen and not ALERT_ON_FIRST_RUN)
        for message in messages:
            entries = process_message(
                message, channel, channel_seen, channel_baseline, now, last_found
            )
            results.extend(entries)
            new_entries.extend(entries)
        if index < len(channels) - 1:
            STOP_EVENT.wait(1.5 + random.uniform(0.0, 1.0))

    update_channel_fail_streaks(checked_channels, failed_channels)
    save_seen(seen)
    if new_entries or twitch_entries or retried:
        save_results(results)
    status_icon = icon("warn") if failed_channels else icon("ok")
    # Следующий запуск отсчитывается от НАЧАЛА цикла (см. app.parse_loop).
    elapsed = time.monotonic() - cycle_started
    next_at = (
        now_msk() + timedelta(seconds=max(5.0, CHECK_INTERVAL - elapsed))
    ).strftime("%H:%M:%S")
    suffix = "" if STOP_EVENT.is_set() else f" · следующая проверка в {next_at}"
    log.info(
        "%s Цикл завершён · каналы %s/%s · новых ссылок: %s%s",
        status_icon,
        len(channels) - len(failed_channels),
        len(channels),
        len(new_entries),
        suffix,
    )
    if failed_channels:
        log.warning("%s Недоступные каналы: %s", icon("warn"), ", ".join(failed_channels))
    return len(new_entries)
