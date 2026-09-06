"""Повторные попытки: недоставленные уведомления и перепроверка expired-ссылок.

Обеспечивает надёжную доставку уведомлений при сбоях сети Telegram
и перепроверку ссылок BetBoom, которые могли быть преждевременно признаны завершёнными.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from typing import Any

from . import db
from .alerts import last_alert, mark_url_alert
from .betboom import precheck_wheel
from .config import (
    NOTIFY_RETRY_MAX_PER_CYCLE,
    NOTIFY_RETRY_WINDOW_MINUTES,
    PREVIEW_CHAR_LIMIT,
    REALERT_COOLDOWN_MINUTES,
    icon,
)
from .db import WheelEntry, make_wheel_entry
from .logging_setup import log
from .storage import load_pending_expired, save_pending_expired
from .telegram_api import (
    notifications_enabled,
    send_keyword_notification,
    send_telegram_notification,
)
from .timeutils import now_msk


def _get_send_telegram_notification():
    return getattr(
        sys.modules.get("wheelsparser.parser"),
        "send_telegram_notification",
        send_telegram_notification,
    )


def _get_send_keyword_notification():
    return getattr(
        sys.modules.get("wheelsparser.parser"),
        "send_keyword_notification",
        send_keyword_notification,
    )


def _get_precheck_wheel():
    return getattr(
        sys.modules.get("wheelsparser.parser"),
        "precheck_wheel",
        precheck_wheel,
    )


def _get_notifications_enabled():
    return getattr(
        sys.modules.get("wheelsparser.parser"),
        "notifications_enabled",
        notifications_enabled,
    )


def _get_is_on_cooldown():
    return getattr(
        sys.modules.get("wheelsparser.parser"),
        "_is_on_cooldown",
        _is_on_cooldown,
    )


def _get_mark_handled():
    return getattr(
        sys.modules.get("wheelsparser.parser"),
        "_mark_handled",
        _mark_handled,
    )

# Множитель к NOTIFY_RETRY_MAX_PER_CYCLE для выборки кандидатов из базы.
_RETRY_CANDIDATE_POOL_MULTIPLIER = 5

# id -> число подряд неудачных попыток ретрая. Только в памяти (не переживает рестарт).
RETRY_ATTEMPT_COUNTS: dict[int, int] = {}


def retry_failed_notifications(now: datetime) -> int:
    """Повторно отправляет уведомления, недоставленные в своём цикле."""
    notify_enabled = _get_notifications_enabled()
    if not notify_enabled():
        return 0
    cutoff = now - timedelta(minutes=NOTIFY_RETRY_WINDOW_MINUTES)
    candidates = db.pending_retry(
        cutoff, NOTIFY_RETRY_MAX_PER_CYCLE * _RETRY_CANDIDATE_POOL_MULTIPLIER
    )
    # list.sort устойчива: записи с равным числом попыток остаются в порядке found_at.
    candidates.sort(key=lambda entry: RETRY_ATTEMPT_COUNTS.get(entry["id"], 0))
    retried = 0
    send_tg = _get_send_telegram_notification()
    send_kw = _get_send_keyword_notification()
    for entry in candidates[:NOTIFY_RETRY_MAX_PER_CYCLE]:
        if entry.get("url"):
            entry["notified"] = send_tg(entry)
            target = str(entry.get("url"))
        elif entry.get("keywords"):
            entry["notified"] = send_kw(entry)
            target = str(entry.get("message_url", ""))
        else:
            continue
        retried += 1
        db.update_delivery(entry)
        if entry["notified"] or entry.get("delivery_unknown"):
            RETRY_ATTEMPT_COUNTS.pop(entry["id"], None)
            if entry["notified"]:
                log.info(
                    "%s Уведомление доставлено повторной попыткой: %s",
                    icon("ok"),
                    target,
                )
        else:
            RETRY_ATTEMPT_COUNTS[entry["id"]] = (
                RETRY_ATTEMPT_COUNTS.get(entry["id"], 0) + 1
            )

    # Очистка счётчиков для id, выпавших из окна ретраев (p2-misc-1): иначе
    # словарь растёт без предела. Кандидат попадает в счётчик, только если
    # был среди выбранных из базы (candidates), а выборка идёт по возрастанию
    # found_at — то есть id покидает её лишь состарившись за окном, не раньше.
    active_ids = {c["id"] for c in candidates}
    for entry_id in list(RETRY_ATTEMPT_COUNTS):
        if entry_id not in active_ids:
            RETRY_ATTEMPT_COUNTS.pop(entry_id, None)

    return retried


# Ссылки, пропущенные precheck'ом как «expired»: url -> метаданные поста.
PENDING_EXPIRED_RETRY: dict[str, dict[str, Any]] = {}


def load_pending_expired_retry() -> None:
    """Восстанавливает PENDING_EXPIRED_RETRY из pending_expired.json."""
    PENDING_EXPIRED_RETRY.clear()
    PENDING_EXPIRED_RETRY.update(load_pending_expired())


def _register_pending_expired(
    url: str, channel: str, message: dict[str, Any], post_text: str, now: datetime
) -> None:
    """Добавляет ссылку на ретрай, если её там ещё нет, и сохраняет на диск."""
    if url in PENDING_EXPIRED_RETRY:
        return
    PENDING_EXPIRED_RETRY[url] = {
        "channel": channel,
        "msg_id": message["id"],
        "message_url": message["message_url"],
        "preview": message["text"][:PREVIEW_CHAR_LIMIT],
        "post_text": post_text,
        "first_seen": now,
    }
    save_pending_expired(PENDING_EXPIRED_RETRY)


def _drop_pending_expired(url: str) -> None:
    """Убирает ссылку из ретрая (если была) и сохраняет изменение на диск."""
    if PENDING_EXPIRED_RETRY.pop(url, None) is not None:
        save_pending_expired(PENDING_EXPIRED_RETRY)


def _is_on_cooldown(url: str, now: datetime, last_found: dict[str, datetime]) -> bool:
    previous = last_found.get(url)
    cross_source = last_alert(url)
    if cross_source and (previous is None or cross_source > previous):
        previous = cross_source
    return bool(
        previous and now - previous <= timedelta(minutes=REALERT_COOLDOWN_MINUTES)
    )


def _mark_handled(url: str, now: datetime, last_found: dict[str, datetime]) -> None:
    last_found[url] = now
    mark_url_alert(url, now)


def retry_expired_links(
    now: datetime, last_found: dict[str, datetime]
) -> list[WheelEntry]:
    """Перепроверяет ссылки, ранее пропущенные как expired/soon."""
    if not PENDING_EXPIRED_RETRY:
        return []
    cutoff = timedelta(minutes=NOTIFY_RETRY_WINDOW_MINUTES)
    new_entries: list[WheelEntry] = []
    for url in list(PENDING_EXPIRED_RETRY):
        info = PENDING_EXPIRED_RETRY[url]
        if now - info["first_seen"] > cutoff:
            _drop_pending_expired(url)
            log.info(
                "%s Перестаю перепроверять %s [@%s]: %s мин без изменения статуса",
                icon("bell"),
                url,
                info["channel"],
                NOTIFY_RETRY_WINDOW_MINUTES,
            )
            continue
        is_on_cooldown = _get_is_on_cooldown()
        if is_on_cooldown(url, now, last_found):
            log.info(
                "%s Прекращаю ретрай %s [@%s]: уведомление уже ушло из другого "
                "источника, пока ссылка ждала перепроверки",
                icon("bell"),
                url,
                info["channel"],
            )
            _drop_pending_expired(url)
            continue
        precheck = _get_precheck_wheel()
        status, referral, ends_at = precheck(
            url, post_text=info["post_text"], use_cache=False
        )
        if status != "active":
            continue
        _drop_pending_expired(url)
        entry = make_wheel_entry(
            url=url,
            channel=info["channel"],
            found_at=now_msk().isoformat(timespec="seconds"),
            source="telegram",
            msg_id=info["msg_id"],
            message_url=info["message_url"],
            preview=info["preview"],
            edited=False,
            status=status,
            referral=referral,
            ends_at=ends_at,
            notified=False,
        )
        send_tg = _get_send_telegram_notification()
        entry["notified"] = send_tg(entry)
        mark_handled = _get_mark_handled()
        mark_handled(url, now, last_found)
        log.info(
            "%s Ссылка ожила после expired [@%s]: %s -> %s",
            icon("link"),
            info["channel"],
            url,
            status,
            extra={"highlight": True},
        )
        new_entries.append(entry)
    return new_entries
