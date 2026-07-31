"""Клиент API BetBoom: определение статуса колеса без браузера.

Статусы: 'active' (идёт), 'soon' (ещё не начался), 'expired' (завершилось),
'unknown' (проверить не удалось). 'unknown' трактуется fail-open: лучше
лишний раз оповестить, чем пропустить живое колесо из-за сбоя API.
"""

from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from .config import (
    ACTIVE_CHECK_CONCURRENCY,
    HEADERS,
    MSK_TZ,
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


# У API BetBoom нет флага «колесо для рефералов» — стример помечает это
# только текстом («Розыгрыш фрибетов для рефералов») или «ref» в адресе.
# \bреф ловит «рефералов», «рефы», «рефовод», «рефка»; граница слова
# отсекает «префикс» и т.п.
REFERRAL_TEXT_RE = re.compile(r"\bреф", re.IGNORECASE)


def is_referral_wheel(
    url: str, info: dict[str, Any] | None, post_text: str = ""
) -> bool:
    """True, если колесо предназначено для рефералов.

    Три сигнала (OR): текст title/description из API, подстрока «ref»
    в slug URL и текст поста/сообщения чата, где нашлась ссылка. Slug и
    пост — запасные сигналы: работают при сбое API и при выключенном
    precheck (стример не всегда пишет про рефералов в описании колеса).
    post_text вызывающий передаёт только для поста с одной ссылкой:
    в посте с несколькими колёсами неизвестно, к какому из них относится
    «для рефов», и метка ушла бы на все.
    """
    if info:
        text = f"{info.get('title', '')} {info.get('description', '')}"
        if REFERRAL_TEXT_RE.search(text):
            return True
    if post_text and REFERRAL_TEXT_RE.search(post_text):
        return True
    slug = normalize_url(url).rsplit("/", 1)[-1]
    return "ref" in slug.lower()


def wheel_window(info: dict[str, Any]) -> tuple[datetime | None, datetime | None]:
    """Начало и конец розыгрыша по start_dttm + duration_min (обе метки — UTC).

    (None, None), если поля отсутствуют, неразбираемы или без таймзоны:
    считать окно по наивной метке нельзя — неизвестно, чьё это время.
    """
    start_raw = info.get("start_dttm")
    duration = info.get("duration_min")
    if not (
        isinstance(start_raw, str)
        and isinstance(duration, (int, float))
        and not isinstance(duration, bool)
        and duration > 0
    ):
        return None, None
    try:
        start = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
    except ValueError:
        return None, None
    if start.tzinfo is None:
        return None, None
    return start, start + timedelta(minutes=float(duration))


def wheel_end_msk(info: dict[str, Any] | None) -> datetime | None:
    """Момент окончания розыгрыша в МСК — дедлайн для показа человеку."""
    if not info:
        return None
    _start, end = wheel_window(info)
    return end.astimezone(MSK_TZ) if end is not None else None


def wheel_ends_at(info: dict[str, Any] | None) -> str:
    """Дедлайн колеса как ISO-строка МСК (пустая, если срок неизвестен).

    Строка, а не datetime: значение уезжает в базу находок и обратно.
    """
    end = wheel_end_msk(info)
    return end.isoformat(timespec="seconds") if end is not None else ""


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
    start, end = wheel_window(info)
    if start is not None and end is not None:
        now = datetime.now(timezone.utc)
        if now >= end:
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
    # Сюда попадают колёса без пригодного start_dttm. Розыгрыш идёт только
    # с момента старта, поэтому у активного колеса время старта в info есть
    # всегда. Его отсутствие означает «стример создал колесо, но не запустил»:
    # на странице «Акция скоро начнётся» и кнопки участия нет — даже при
    # is_early=false. Такое колесо в /active показывать нельзя.
    return "soon"


def fetch_wheel_info(
    url: str, session: requests.Session
) -> dict[str, Any] | None:
    """Запрашивает info одного колеса через BetBoom API без браузера.

    Возвращает словарь info или None при любой ошибке (сеть, не-200,
    неожиданный формат). Сессия передаётся явно: у каждого потока она
    своя (см. net.py).
    """
    canonical = normalize_url(url)
    if not canonical:
        return None
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
            return None
        info = response.json().get("info", {})
        return info if isinstance(info, dict) else None
    except Exception as error:
        log.debug("active-check API: ошибка для %s: %s", canonical, error)
        return None


def check_wheel_status(url: str, session: requests.Session) -> str:
    """Статус одного колеса: 'active', 'soon', 'expired' или 'unknown'."""
    info = fetch_wheel_info(url, session)
    if info is None:
        return "unknown"
    return api_info_to_status(info)


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


def precheck_wheel(
    url: str, session: requests.Session | None = None, post_text: str = ""
) -> tuple[str, bool, str]:
    """Статус колеса, реф-флаг и дедлайн перед отправкой уведомления.

    Возвращает ('active'/'soon'/'expired'/'unknown', is_referral, ends_at),
    где ends_at — ISO-строка МСК с концом розыгрыша или "" если срок
    неизвестен (сбой API или колесо без start_dttm).
    При 'unknown' уведомление всё равно отправляется (fail-open): лучше
    лишний раз оповестить, чем пропустить живое колесо из-за сбоя API.
    Реф-флаг при недоступном info считается по slug URL и тексту поста
    (post_text, см. is_referral_wheel).
    По умолчанию используется PARSER_SESSION — вызывающему из другого
    потока нужно передать свою сессию.
    """
    canonical = normalize_url(url)
    if not canonical:
        return "unknown", False, ""
    today = today_msk()
    _prune_expired_cache(today)
    if _is_cached_expired(canonical, today):
        log.info("precheck [cache]: %s → expired (кэш за сегодня)", canonical)
        return "expired", is_referral_wheel(canonical, None, post_text), ""
    info = fetch_wheel_info(canonical, session or PARSER_SESSION)
    status = "unknown" if info is None else api_info_to_status(info)
    referral = is_referral_wheel(canonical, info, post_text)
    log.info(
        "precheck [api]: %s → %s%s",
        canonical,
        status,
        " (для рефералов)" if referral else "",
    )
    if status == "expired":
        _cache_expired(canonical, today)
    return status, referral, wheel_ends_at(info)


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
        info = fetch_wheel_info(url, worker_session())
        status = "unknown" if info is None else api_info_to_status(info)
        # Реф-флаг и дедлайн обновляются по свежему info: старые записи
        # (до появления этих полей) получают их прямо при /active.
        if not item.get("referral") and is_referral_wheel(url, info):
            item["referral"] = True
        ends_at = wheel_ends_at(info)
        if ends_at:
            item["ends_at"] = ends_at
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
