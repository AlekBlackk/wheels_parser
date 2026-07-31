"""Кулдаун повторных уведомлений — общий для Telegram и Twitch.

Одно и то же колесо, найденное в двух источниках, не рассылается дважды.
Состояние живёт в памяти и восстанавливается из базы находок при старте.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta

from . import db
from .config import REALERT_COOLDOWN_MINUTES
from .timeutils import now_msk, parse_found_at
from .urls import normalize_url

# url -> время последнего отправленного уведомления (МСК).
LAST_URL_ALERT: dict[str, datetime] = {}
LAST_URL_ALERT_LOCK = threading.Lock()


def cooldown_active(url: str, now: datetime) -> bool:
    """True, если об этом колесе уже оповещали в пределах кулдауна."""
    with LAST_URL_ALERT_LOCK:
        previous = LAST_URL_ALERT.get(url)
    return bool(
        previous
        and now - previous <= timedelta(minutes=REALERT_COOLDOWN_MINUTES)
    )


def last_alert(url: str) -> datetime | None:
    with LAST_URL_ALERT_LOCK:
        return LAST_URL_ALERT.get(url)


def _prune_stale_alerts() -> None:
    """Убирает записи старше кулдауна — иначе LAST_URL_ALERT растёт бессрочно.

    Сверяется с реальным «сейчас» (а не с `when` из mark_url_alert): при
    сидировании из истории `when` — это старые found_at, и по ним нельзя
    судить об актуальности других записей.
    """
    cutoff = timedelta(minutes=REALERT_COOLDOWN_MINUTES)
    now = now_msk()
    with LAST_URL_ALERT_LOCK:
        for stale_url in [
            url for url, when in LAST_URL_ALERT.items() if now - when > cutoff
        ]:
            LAST_URL_ALERT.pop(stale_url, None)


def mark_url_alert(url: str, when: datetime) -> None:
    with LAST_URL_ALERT_LOCK:
        existing = LAST_URL_ALERT.get(url)
        if existing is None or when > existing:
            LAST_URL_ALERT[url] = when
    _prune_stale_alerts()


def seed_url_alerts_from_history() -> None:
    """Восстанавливает кулдаун уведомлений из базы после рестарта.

    Без этого Twitch-кулдаун жил только в памяти: после перезапуска парсера
    та же ссылка могла уйти в Telegram повторно раньше времени.
    Поднимается только окно кулдауна — записи старше него на повтор всё
    равно не влияют.
    """
    cutoff = now_msk() - timedelta(minutes=REALERT_COOLDOWN_MINUTES)
    for item in db.wheels_since(cutoff):
        url = normalize_url(str(item.get("url", "")))
        if not url:
            continue
        found = parse_found_at(item.get("found_at"))
        if found is not None:
            mark_url_alert(url, found)
