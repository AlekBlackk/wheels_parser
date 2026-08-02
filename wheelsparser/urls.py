"""Канонизация ссылок на колёса и хэш содержимого поста.

Единая каноническая форма URL используется везде: при извлечении ссылок,
в дедупликации, в кулдауне повторных уведомлений, в /active и в кэшах.
"""

from __future__ import annotations

import hashlib
import html
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .config import FREESTREAM_RE, TRAILING_PUNCTUATION

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
    """
    cleaned = html.unescape(str(url)).strip().rstrip(TRAILING_PUNCTUATION)
    if cleaned and not _SCHEME_RE.match(cleaned):
        cleaned = f"https://{cleaned}"
    parts = urlsplit(cleaned)
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    if netloc == "www.betboom.ru":
        netloc = "betboom.ru"
    return urlunsplit((scheme, netloc, parts.path.rstrip("/"), "", ""))


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
    candidates = [link.get("href", "") for link in node.find_all("a", href=True)]
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


def find_urls(node: Any, text: str) -> list[str]:
    """Канонические ссылки на колёса из сообщения."""
    return extract_urls(node, text, normalize_url)


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
