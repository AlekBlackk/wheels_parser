"""Разбор веб-страниц Telegram-каналов (t.me/s/<channel>).

Извлекает сообщения, ссылки, пересылки и HTML-превью с помощью BeautifulSoup.
"""

from __future__ import annotations

import html
import re
import sys
from typing import Any
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

from .config import (
    MESSAGES_PER_CHANNEL,
    PREVIEW_CHAR_LIMIT,
    REQUEST_TIMEOUT,
    USERNAME_RE,
)
from .logging_setup import log
from .net import PARSER_SESSION
from .urls import (
    extract_urls,
    find_disallowed_domains,
    find_urls,
    is_betboom_host,
    legacy_normalize_url,
    message_content_hash,
)

# Заголовок репоста в веб-превью: <a class="tgme_widget_message_forwarded_from_name"
# href="https://t.me/orig">Название</a>. У репоста из скрытого источника
# (закрытый канал, пользователь без юзернейма) тег тот же, но это <span>
# без href — такой репост первоисточника не выдаёт.
FORWARD_SOURCE_SELECTOR = "a.tgme_widget_message_forwarded_from_name"
_TELEGRAM_HOSTS = frozenset({"t.me", "www.t.me", "telegram.me", "www.telegram.me"})


def message_preview_html(text_element: Any, limit: int = PREVIEW_CHAR_LIMIT) -> str:
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
            if is_betboom_host(href):
                if visible + len(label) > limit:
                    parts.append("…")
                    break
                parts.append(
                    f'<a href="{html.escape(href, quote=True)}">{html.escape(label)}</a>'
                )
                visible += len(label) + 1
            else:
                display = label if label == href else f"{label} ({href})"
                if visible + len(display) > limit:
                    cut = display[: limit - visible].rstrip()
                    if cut:
                        parts.append(html.escape(cut))
                    parts.append("…")
                    break
                parts.append(html.escape(display))
                visible += len(display) + 1
    return " ".join(parts)


def forwarded_from_channel(message: Any) -> str:
    """Юзернейм канала-первоисточника репоста или "" если его нет.

    href из разметки не является доверенным вводом: берём из него только
    первый сегмент пути и пропускаем его через USERNAME_RE — тот же
    фильтр, что и у /add. Иначе в channels.txt мог бы приехать мусор
    (t.me/+инвайт, ссылка на чужой домен, путь вида t.me/s/...).
    """
    link = message.select_one(FORWARD_SOURCE_SELECTOR)
    if link is None:
        return ""
    parts = urlsplit(str(link.get("href", "")).strip())
    if parts.netloc.lower() not in _TELEGRAM_HOSTS:
        return ""
    segments = [segment for segment in parts.path.split("/") if segment]
    if not segments:
        return ""
    match = USERNAME_RE.match(segments[0])
    return match.group(1) if match else ""


def fetch_channel(
    channel: str, session: requests.Session | None = None
) -> list[dict[str, Any]] | None:
    """Последние сообщения канала через веб-превью t.me/s/<channel>.

    None означает, что канал прочитать не удалось (404, сетевая ошибка или
    сбой разбора HTML — см. ниже). Пустой список — страница получена, но
    ни одного поста распознать не удалось: это не то же самое, что «нет
    новых сообщений», и вызывающий обязан различать эти случаи (см.
    update_channel_empty_streaks).
    По умолчанию используется PARSER_SESSION — параллельный опрос каналов
    (см. _fetch_all_channels) передаёт сессию своего воркера, так как
    requests.Session не потокобезопасна.
    """
    url = f"https://t.me/s/{channel}"
    http_session = session or PARSER_SESSION
    try:
        response = http_session.get(url, timeout=REQUEST_TIMEOUT)
        if response.status_code == 404:
            log.warning("[%s] канал не найден или приватный (404)", channel)
            return None
        response.raise_for_status()
    except requests.RequestException as error:
        log.warning("[%s] ошибка запроса: %s", channel, error)
        return None

    try:
        # response.content вместо response.text: если сервер не указал
        # charset, requests подставляет latin-1 и кириллица превращается
        # в кракозябры. BeautifulSoup сам определяет UTF-8 по <meta charset>
        # страницы.
        soup = BeautifulSoup(response.content, "html.parser")
        messages = soup.select(".tgme_widget_message_wrap")[-MESSAGES_PER_CHANNEL:]
        results: list[dict[str, Any]] = []
        preview_fn = getattr(
            sys.modules.get("wheelsparser.parser"),
            "message_preview_html",
            message_preview_html,
        )
        for message in messages:
            bubble = message.select_one(".tgme_widget_message")
            if not bubble:
                continue
            message_id = str(bubble.get("data-post", "")).strip()
            if not message_id:
                continue
            text_element = message.select_one(".tgme_widget_message_text")
            text = text_element.get_text(" ", strip=True) if text_element else ""
            # session передаётся дальше, чтобы раскрыть известные
            # сокращатели (vk.cc и т.п., см. urls.resolve_shortlink) —
            # иначе колесо, спрятанное за ними, даже не матчится
            # FREESTREAM_RE и теряется молча, до прекчека дело не доходит.
            urls = find_urls(message, text, http_session)
            results.append({
                "id": message_id,
                "text": text,
                "preview_html": preview_fn(text_element),
                "urls": urls,
                # Домены поста вне betboom.ru/t.me — сигнал скам-рекламы
                # («колесо на 60000$», ведущее на сторонний сайт), см.
                # notify_keywords и urls.find_disallowed_domains.
                "disallowed_domains": find_disallowed_domains(
                    message, text, http_session
                ),
                # Канал-первоисточник, если пост — репост (см.
                # suggest_forward_source): кандидат в мониторинг.
                "forwarded_from": forwarded_from_channel(message),
                "hash": message_content_hash(text, urls),
                # Хэш в формате старых версий (URL с query-параметрами):
                # сравнение с ним не даёт принять смену формата хэша за
                # правку поста.
                "legacy_hash": message_content_hash(
                    text, extract_urls(message, text, legacy_normalize_url)
                ),
                "message_url": f"https://t.me/{message_id}",
            })
        return results
    except Exception:
        # Разбор одного канала не должен ронять весь цикл: у пула
        # воркеров (см. _fetch_all_channels) нет своего try/except, и
        # необработанное исключение здесь вылетело бы из pool.map и
        # оставило бы непроверенными все остальные каналы этого цикла.
        log.exception("[%s] не удалось разобрать страницу канала", channel)
        return None
