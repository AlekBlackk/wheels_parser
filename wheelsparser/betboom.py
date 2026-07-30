"""Клиент API BetBoom: определение статуса колеса без браузера.

Статусы: 'active' (идёт), 'soon' (ещё не начался), 'expired' (завершилось),
'unknown' (проверить не удалось). 'unknown' трактуется fail-open: лучше
лишний раз оповестить, чем пропустить живое колесо из-за сбоя API.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from .config import (
    ACTIVE_CHECK_CONCURRENCY,
    HEADERS,
    REQUEST_TIMEOUT,
    STREAMER_WHEEL_INFO_API,
)
from .logging_setup import log
from .net import PARSER_SESSION, build_session
from .timeutils import today_msk
from .urls import normalize_url

# Кэш завершившихся колёс на текущие сутки МСК: url -> "YYYY-MM-DD".
# Завершившееся колесо не «оживает», поэтому повторные /active за день не
# перепроверяют его через API — к вечеру это главный источник ускорения.
# Кэш общий для parser-потока (precheck перед уведомлением) и фонового
# active-api-потока, поэтому доступ — только под _expired_cache_lock.
_expired_cache: dict[str, str] = {}
_expired_cache_lock = threading.Lock()


def api_info_to_status(info: dict[str, Any]) -> str:
    is_ended = info.get("is_ended")
    if not isinstance(is_ended, bool):
        return "unknown"
    if is_ended:
        return "expired"
    # is_ended у API BetBoom запаздывает: флаг не переключается по таймеру,
    # и колесо может часами числиться «не завершённым» после окончания.
    # Поэтому конец розыгрыша считаем сами: start_dttm + duration_min.
    time_status: str | None = None
    start_raw = info.get("start_dttm")
    duration = info.get("duration_min")
    if (
        isinstance(start_raw, str)
        and isinstance(duration, (int, float))
        and not isinstance(duration, bool)
        and duration > 0
    ):
        try:
            start = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
        except ValueError:
            start = None
        if start is not None and start.tzinfo is not None:
            now = datetime.now(timezone.utc)
            if now >= start + timedelta(minutes=float(duration)):
                return "expired"
            time_status = "soon" if now < start else "active"
    # «Акция скоро начнётся» на сайте показывается по флагу is_early,
    # поэтому он надёжнее расчёта по start_dttm: бывает, что start_dttm
    # уже в прошлом, а розыгрыш стример ещё не запустил. is_early=True
    # всегда означает «ещё не началось» (если не истекло по времени выше).
    is_early = info.get("is_early")
    if isinstance(is_early, bool) and is_early:
        return "soon"
    if time_status is not None:
        return time_status
    if not isinstance(is_early, bool):
        return "unknown"
    return "active"


def check_wheel_status(url: str, session: requests.Session) -> str:
    """Запрашивает статус одного колеса через BetBoom API без браузера.

    Возвращает 'active', 'soon', 'expired' или 'unknown' при любой ошибке.
    Сессия передаётся явно: у каждого потока она своя (см. net.py).
    """
    canonical = normalize_url(url)
    if not canonical:
        return "unknown"
    try:
        response = session.post(
            STREAMER_WHEEL_INFO_API,
            json={"streamer_link": canonical},
            headers={
                **HEADERS,
                "Accept": "application/json",
                "X-Platform": "web",
                "Referer": canonical,
            },
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code != 200:
            log.debug(
                "active-check API: HTTP %s для %s", response.status_code, canonical
            )
            return "unknown"
        payload = response.json()
        return api_info_to_status(payload.get("info", {}))
    except Exception as error:
        log.debug("active-check API: ошибка для %s: %s", canonical, error)
        return "unknown"


def _prune_expired_cache(today: str) -> None:
    with _expired_cache_lock:
        for stale_url in [
            url for url, day in list(_expired_cache.items()) if day != today
        ]:
            _expired_cache.pop(stale_url, None)


def _is_cached_expired(url: str, today: str) -> bool:
    with _expired_cache_lock:
        return _expired_cache.get(url) == today


def _cache_expired(url: str, today: str) -> None:
    with _expired_cache_lock:
        _expired_cache[url] = today


def precheck_wheel_status(
    url: str, session: requests.Session | None = None
) -> str:
    """Статус колеса перед отправкой уведомления.

    Возвращает 'active', 'soon', 'expired' или 'unknown'. При 'unknown'
    уведомление всё равно отправляется (fail-open): лучше лишний раз
    оповестить, чем пропустить живое колесо из-за сбоя API.
    По умолчанию используется PARSER_SESSION — вызывающему из другого
    потока нужно передать свою сессию.
    """
    canonical = normalize_url(url)
    if not canonical:
        return "unknown"
    today = today_msk()
    if _is_cached_expired(canonical, today):
        log.info("precheck [cache]: %s → expired (кэш за сегодня)", canonical)
        return "expired"
    status = check_wheel_status(canonical, session or PARSER_SESSION)
    log.info("precheck [api]: %s → %s", canonical, status)
    if status == "expired":
        _cache_expired(canonical, today)
    return status


def classify_wheels(
    items: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Проверяет список колёс через BetBoom API параллельно.

    Использует ThreadPoolExecutor с ACTIVE_CHECK_CONCURRENCY потоками.
    Кэширует expired-статусы в пределах текущих суток МСК.
    Возвращает кортеж (active_items, soon_items, unknown_count):
      - active_items  — колёса со статусом active (в исходном порядке);
      - soon_items    — колёса, розыгрыш которых ещё не начался (soon);
      - unknown_count — количество колёс с неопределённым статусом.
    """
    today = today_msk()
    _prune_expired_cache(today)

    # requests.Session не потокобезопасна, поэтому общая сессия на все
    # рабочие потоки ThreadPoolExecutor недопустима: у каждого потока —
    # своя сессия через threading.local (создаётся лениво при первом запросе).
    thread_local = threading.local()

    def worker_session() -> requests.Session:
        session = getattr(thread_local, "session", None)
        if session is None:
            session = build_session()
            thread_local.session = session
        return session

    results: list[tuple[int, str]] = []  # (original_index, status)
    lock = threading.Lock()

    def check(index: int, item: dict[str, Any]) -> None:
        url = normalize_url(str(item.get("url", "")))
        if not url:
            with lock:
                results.append((index, "unknown"))
            return
        if _is_cached_expired(url, today):
            log.info("active-check [cache]: %s → expired (кэш за сегодня)", url)
            with lock:
                results.append((index, "expired"))
            return
        status = check_wheel_status(url, worker_session())
        log.info("active-check [api]: %s → %s", url, status)
        if status == "expired":
            _cache_expired(url, today)
        with lock:
            results.append((index, status))

    with ThreadPoolExecutor(max_workers=ACTIVE_CHECK_CONCURRENCY) as pool:
        list(pool.map(lambda args: check(*args), enumerate(items)))

    results.sort(key=lambda pair: pair[0])
    active_items = [items[i] for i, status in results if status == "active"]
    soon_items = [items[i] for i, status in results if status == "soon"]
    unknown_count = sum(1 for _, status in results if status == "unknown")
    return active_items, soon_items, unknown_count
