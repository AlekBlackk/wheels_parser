"""Канонизация ссылок на колёса и хэш содержимого поста.

Единая каноническая форма URL используется везде: при извлечении ссылок,
в дедупликации, в кулдауне повторных уведомлений, в /active и в кэшах.
"""

from __future__ import annotations

import hashlib
import html
import re
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests

from .config import (
    ALLOWED_KEYWORD_DOMAINS,
    FREESTREAM_RE,
    MAX_SHORTLINKS_PER_MESSAGE,
    SHORTENER_CACHE_TTL_SECONDS,
    SHORTENER_CANDIDATE_RE,
    SHORTENER_DOMAINS,
    SHORTENER_RESOLVE_TIMEOUT,
    TRAILING_PUNCTUATION,
)
from .logging_setup import log
from .timeutils import now_msk

_SCHEME_RE = re.compile(r"^https?://", re.IGNORECASE)


def normalize_url(url: str) -> str:
    """Единая каноническая форма URL колеса.

    Используется везде: при извлечении ссылок из постов, для кулдауна
    повторных уведомлений, в /active, в precheck и в кэше завершившихся
    колёс. Query-параметры (utm и т.п.) и завершающий «/» отбрасываются:
    это тот же адрес колеса, различия в хвосте не должны создавать дубликаты
    и двойные уведомления. HTML-сущности (&amp; и т.п.) раскодируются:
    ссылка могла быть извлечена из «сырого» HTML или сохранена
    старой версией в экранированном виде.
    Ссылка без схемы (Twitch-чат: боты часто режут https://, см.
    config.FREESTREAM_RE) достраивается до https://: без этого urlsplit
    принял бы весь адрес за path, а не netloc, и один и тот же URL из
    разных источников (Telegram/Twitch) перестал бы быть одной канонической
    строкой — сломались бы дедупликация, кулдаун и expired-кэш.
    Схема всегда приводится к https, даже если во входном URL указан http:
    Telegram нередко кладёт в href схему http (а в видимом тексте ссылки
    схемы вовсе нет — она достраивается выше), и без унификации один и тот
    же пост давал бы ДВЕ разные «канонические» ссылки (http из href и https
    из текста) — список ссылок поста менялся, хэш содержимого «плыл», и
    правка поста определялась ложно (см. регресс после введения
    schemeless-матчинга: 4 старых поста в @whylollybet разом посчитались
    отредактированными). API BetBoom к тому же отвечает только на https —
    http-вариант молча уходил в 'unknown' и рассылался fail-open.
    """
    cleaned = html.unescape(str(url)).strip().rstrip(TRAILING_PUNCTUATION)
    if cleaned and not _SCHEME_RE.match(cleaned):
        cleaned = f"https://{cleaned}"
    parts = urlsplit(cleaned)
    scheme = "https" if parts.scheme.lower() in ("http", "https") else parts.scheme.lower()
    netloc = parts.netloc.lower()
    if netloc == "www.betboom.ru":
        netloc = "betboom.ru"
    return urlunsplit((scheme, netloc, parts.path.rstrip("/"), "", ""))


def is_betboom_host(url: str) -> bool:
    """Проверяет, ведёт ли ссылка на betboom.ru или его поддомен."""
    try:
        norm = normalize_url(url)
        host = urlsplit(norm).hostname
        if not host:
            return False
        host = host.lower()
        return host == "betboom.ru" or host.endswith(".betboom.ru")
    except Exception:
        return False


def legacy_normalize_url(url: str) -> str:
    """Нормализация URL старых версий парсера (query-параметры сохранялись).

    Нужна только для миграции seen_ids.json: хэши сообщений, посчитанные
    старой версией, содержат URL с query-параметрами. Сравнение с
    «легаси»-хэшем позволяет не принять смену формата за правку поста
    и не рассылать повторные уведомления после обновления парсера.
    """
    cleaned = str(url).strip().rstrip(TRAILING_PUNCTUATION)
    parts = urlsplit(cleaned)
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    if netloc == "www.betboom.ru":
        netloc = "betboom.ru"
    return urlunsplit((scheme, netloc, parts.path, parts.query, ""))


def extract_urls(
    node: Any, text: str, normalizer: Callable[[str], str]
) -> list[str]:
    """Ссылки на колёса из HTML-узла и текста, приведённые normalizer."""
    candidates: list[str] = []
    if node is not None and hasattr(node, "find_all"):
        for link in node.find_all("a", href=True):
            if (
                hasattr(link, "find_parent")
                and (
                    link.find_parent(class_="tgme_widget_message_reply") is not None
                    or link.find_parent(class_="js-message_reply_text") is not None
                    or link.find_parent(class_="tgme_widget_message_reply_text") is not None
                )
            ):
                continue
            href = link.get("href", "")
            if href:
                candidates.append(href)
    candidates.extend(FREESTREAM_RE.findall(text))
    urls: list[str] = []
    for candidate in candidates:
        match = FREESTREAM_RE.match(candidate)
        if not match:
            continue
        normalized = normalizer(match.group(0))
        if normalized not in urls:
            urls.append(normalized)
    return urls


def find_shortlink_candidates_in_text(text: str) -> list[str]:
    """Ссылки на известные сокращатели (см. config.SHORTENER_DOMAINS) —
    кандидаты на раскрытие через resolve_shortlink. Используется и здесь
    (текст без HTML-узла — Twitch-чат), и внутри find_shortlink_candidates."""
    candidates: list[str] = []
    for raw in SHORTENER_CANDIDATE_RE.findall(text):
        normalized = raw if _SCHEME_RE.match(raw) else f"https://{raw}"
        if normalized not in candidates:
            candidates.append(normalized)
    return candidates[:MAX_SHORTLINKS_PER_MESSAGE]


def find_shortlink_candidates(node: Any, text: str) -> list[str]:
    """Ссылки на известные сокращатели из HTML-узла и текста поста.

    Источники те же, что у extract_urls: <a href> и голый текст."""
    candidates = find_shortlink_candidates_in_text(text)
    if node is not None and hasattr(node, "find_all"):
        for link in node.find_all("a", href=True):
            if (
                hasattr(link, "find_parent")
                and (
                    link.find_parent(class_="tgme_widget_message_reply") is not None
                    or link.find_parent(class_="js-message_reply_text") is not None
                    or link.find_parent(class_="tgme_widget_message_reply_text") is not None
                )
            ):
                continue
            href = str(link.get("href", "")).strip()
            match = SHORTENER_CANDIDATE_RE.match(href)
            if not match:
                continue
            normalized = match.group(0)
            if not _SCHEME_RE.match(normalized):
                normalized = f"https://{normalized}"
            if normalized not in candidates:
                candidates.append(normalized)
    return candidates[:MAX_SHORTLINKS_PER_MESSAGE]


def _is_shortener_domain(domain: str) -> bool:
    return any(
        domain == shortener or domain.endswith(f".{shortener}")
        for shortener in SHORTENER_DOMAINS
    )


# Раскрытые сокращатели: url -> (конечный адрес или None, момент раскрытия).
# Канал перечитывает последние MESSAGES_PER_CHANNEL сообщений каждый цикл
# независимо от того, видели их уже или нет (см. config.SHORTENER_CACHE_TTL_SECONDS)
# — без кэша один и тот же URL резолвился бы заново каждый CHECK_INTERVAL,
# пока пост не вывалится из окна. None кэшируется наравне с успехом: сбой
# сети или мёртвая ссылка не должны бить по сокращателю на каждом цикле.
_shortlink_cache: dict[str, tuple[str | None, datetime]] = {}
_shortlink_cache_lock = threading.Lock()

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


def _cached_resolution(url: str) -> tuple[bool, str | None]:
    """(есть_в_кэше, значение). Отдельный булев — закэшированное значение
    само может быть None (сокращатель не раскрылся)."""
    cutoff = timedelta(seconds=SHORTENER_CACHE_TTL_SECONDS)
    now = now_msk()
    with _shortlink_cache_lock:
        for stale_url, (_value, when) in list(_shortlink_cache.items()):
            if now - when > cutoff:
                _shortlink_cache.pop(stale_url, None)
        if url in _shortlink_cache:
            return True, _shortlink_cache[url][0]
    return False, None


def _cache_resolution(url: str, value: str | None) -> None:
    with _shortlink_cache_lock:
        _shortlink_cache[url] = (value, now_msk())


def _fetch_redirect_location(url: str, session: requests.Session) -> str | None:
    """Location одного редиректа с известного сокращателя.

    HEAD дешевле GET, но часть сокращателей отвечает на него 404/405 и
    отдаёт Location только на GET — тогда пробуем GET с allow_redirects=False
    и сразу закрываем ответ (stream=True не даёт requests скачать тело:
    Location уже есть в заголовках, а качать саму страницу-цель не нужно).
    """
    try:
        response = session.head(
            url, timeout=SHORTENER_RESOLVE_TIMEOUT, allow_redirects=False
        )
        if response.status_code not in _REDIRECT_STATUSES:
            response = session.get(
                url,
                timeout=SHORTENER_RESOLVE_TIMEOUT,
                allow_redirects=False,
                stream=True,
            )
            response.close()
    except Exception as error:
        # Широкий except — как в betboom.fetch_wheel_info: это один
        # вспомогательный сетевой вызов, различать типы ошибок незачем,
        # любая означает «раскрыть не удалось».
        log.debug("resolve_shortlink: ошибка запроса к %s: %s", url, error)
        return None
    if response.status_code not in _REDIRECT_STATUSES:
        return None
    return response.headers.get("Location")


def resolve_shortlink(
    url: str, session: requests.Session, max_hops: int = 2
) -> str | None:
    """Раскрывает известный сокращатель (см. config.SHORTENER_DOMAINS) до
    конечного адреса или возвращает None.

    None означает: это не сокращатель, редирект прочитать не удалось
    (таймаут, сеть, отсутствующий заголовок Location) или цепочка длиннее
    max_hops — стример почти никогда не прячет колесо больше, чем за одним
    сокращателем, а более длинная цепочка — либо чужая реклама, либо
    попытка обойти проверку, гоняться за ней незачем.
    Результат (включая None) кэшируется на SHORTENER_CACHE_TTL_SECONDS.
    """
    if not _is_shortener_domain(urlsplit(url).netloc.lower()):
        return None
    hit, cached = _cached_resolution(url)
    if hit:
        return cached
    current = url
    result: str | None = None
    for _ in range(max_hops):
        location = _fetch_redirect_location(current, session)
        if not location:
            result = None
            break
        current = urljoin(current, location)
        if not _is_shortener_domain(urlsplit(current).netloc.lower()):
            result = current
            break
    _cache_resolution(url, result)
    return result


def find_urls(node: Any, text: str, session: requests.Session | None = None) -> list[str]:
    """Канонические ссылки на колёса из сообщения.

    session, если передан, раскрывает известные сокращатели (см.
    resolve_shortlink) — без него ссылка вида vk.cc/abc, спрятанная за
    сокращателем, даже не матчится FREESTREAM_RE и теряется молча, до
    прекчека дело не доходит. Без session (тесты, вызовы без сети) шаг
    просто пропускается — остальное поведение не меняется.
    """
    urls = extract_urls(node, text, normalize_url)
    if session is None:
        return urls
    for candidate in find_shortlink_candidates(node, text):
        resolved = resolve_shortlink(candidate, session)
        if resolved is None:
            continue
        match = FREESTREAM_RE.match(resolved)
        if not match:
            continue
        normalized = normalize_url(match.group(0))
        if normalized not in urls:
            urls.append(normalized)
    return urls


# Распространённые расширения файлов, которые не являются TLD, но могут
# ошибочно совпасть с доменами при поиске в сыром тексте (например, photo.png, rules.pdf).
_COMMON_FILE_EXTENSIONS = frozenset({
    "png", "jpg", "jpeg", "gif", "webp", "svg", "ico", "bmp", "tiff",
    "mp4", "mkv", "avi", "mov", "webm", "wav", "mp3", "ogg", "flac",
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "csv",
    "zip", "rar", "7z", "tar", "gz", "iso", "dmg", "apk", "exe",
})


# Поиск URL (https?://...) и голых доменов в тексте.
# Домен: последовательность меток (буквы, цифры, дефисы), разделённых точками,
# и TLD от 2 до 24 латинских букв. Перед доменом не должно быть символов слова,
# точки, дефиса или @.
_RAW_URL_OR_DOMAIN_RE = re.compile(
    r"(?<![A-Za-z0-9_@.-])"
    r"(?:https?://)?"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,24}"
    r"(?![0-9A-Za-zА-Яа-яЁё_.-])"
    r"(?::\d+)?"
    r"(?:/[^\s<>\"'()]*)*",
    re.IGNORECASE,
)


def find_disallowed_domains(
    node: Any, text: str, session: requests.Session | None = None
) -> list[str]:
    """Домены ссылок поста, не входящие в ALLOWED_KEYWORD_DOMAINS.

    Ссылочная (freestream) находка уже домен-специфична сама по себе
    (см. FREESTREAM_RE) — эта проверка нужна только алерту по ключевым
    словам без ссылки на betboom.ru (см. parser.notify_keywords). Скам-
    казино нередко копирует формулировки типа «колесо на 60000$», но ведёт
    на свой сайт — без проверки такой пост уходил бы алертом наравне с
    настоящим колесом (пример: канал слал колесо и рекламу казино вперемешку,
    оба текста содержат слово «колесо»).

    Ищет ссылки как в тегах <a href>, так и в сыром тексте (text и node.get_text()),
    так как в цитатах и тексте Telegram ссылки не всегда оборачиваются в <a>.
    Поддерживает полные URL (https?://...) и голые домены.
    session, если передан, раскрывает известные сокращатели и проверяет уже
    конечный домен (см. resolve_shortlink): сам сокращатель в
    ALLOWED_KEYWORD_DOMAINS попадать не должен — им прикрывается и легитимная
    ссылка (vk.cc -> vk.com), и скам, поэтому доверять домену сокращателя
    напрямую нельзя. Без session или при неудачном раскрытии домен
    сокращателя остаётся как есть — fail-secure: расценивается как
    подозрительный, а не как разрешённый.
    Порядок сохраняется, дубликаты домена схлопываются.
    """
    candidates: list[str] = []

    # 1. Ссылки из тегов <a href>
    if node is not None and hasattr(node, "find_all"):
        for link in node.find_all("a", href=True):
            href = str(link.get("href", "")).strip()
            if href and href not in candidates:
                candidates.append(href)

    # 2. Поиск URL и доменов в сыром тексте (text и node.get_text())
    texts_to_scan: list[str] = []
    if text:
        texts_to_scan.append(text)
    if node is not None and hasattr(node, "get_text"):
        node_text = node.get_text(" ", strip=True)
        if node_text and node_text != text:
            texts_to_scan.append(node_text)

    for content in texts_to_scan:
        for match in _RAW_URL_OR_DOMAIN_RE.finditer(content):
            raw = match.group(0).strip().rstrip(TRAILING_PUNCTUATION)
            if raw and raw not in candidates:
                candidates.append(raw)

    domains: list[str] = []
    for candidate in candidates:
        cleaned = candidate.strip().rstrip(TRAILING_PUNCTUATION)
        if not cleaned:
            continue

        # Проверяем схему: отсекаем не-веб протоколы (mailto:, tg://, javascript:, tel: и т.п.)
        if "://" in cleaned:
            scheme = cleaned.split("://", 1)[0].lower()
            if scheme not in ("http", "https"):
                continue
            url_to_parse = cleaned
        else:
            # Для строк без "://" проверяем, не является ли это схемой вроде mailto: или tel:
            # Схема не содержит точек (в отличие от домена с портом вроде welvura.com:8080)
            colon_match = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*):", cleaned)
            if colon_match and "." not in colon_match.group(1):
                continue
            url_to_parse = f"https://{cleaned}"

        normalized_href = normalize_url(url_to_parse)
        parsed = urlsplit(normalized_href)
        domain = parsed.hostname or parsed.netloc
        if not domain or "." not in domain:
            continue
        domain = domain.lower()

        # Отсекаем совпадения с именами файлов (photo.png, rules.pdf и т.п.),
        # у которых расширение не является интернет-TLD
        tld = domain.rsplit(".", 1)[-1]
        if tld in _COMMON_FILE_EXTENSIONS:
            continue

        if _is_shortener_domain(domain) and session is not None:
            resolved = resolve_shortlink(normalized_href, session)
            if resolved is not None:
                resolved_host = urlsplit(resolved).hostname or urlsplit(resolved).netloc
                if resolved_host and "." in resolved_host:
                    domain = resolved_host.lower()

        if not domain or "." not in domain or domain in domains:
            continue

        if any(
            domain == allowed or domain.endswith(f".{allowed}")
            for allowed in ALLOWED_KEYWORD_DOMAINS
        ):
            continue

        domains.append(domain)

    return domains


def message_content_hash(text: str, urls: list[str]) -> str:
    """Хэш содержимого сообщения для обнаружения правок постов.

    Считается по нормализованному тексту (схлопнутые пробелы) и списку
    найденных ссылок: правка href без изменения видимого текста тоже
    меняет хэш. Усечён до 16 hex-символов — криптостойкость не нужна,
    важна только смена значения при реальном изменении содержимого.
    """
    normalized = re.sub(r"\s+", " ", text).strip()
    payload = normalized + "\n" + "\n".join(urls)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]
