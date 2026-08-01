"""Отправка сообщений через Telegram Bot API.

Каждая функция принимает (или знает) сессию своего потока — см. net.py.
Уведомления молча выключены, если токен или chat_id не заданы.
"""

from __future__ import annotations

import html
from typing import Any

import requests

from .config import (
    BOT_API,
    REQUEST_TIMEOUT,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    TWITCH_ROLE_ICONS,
    icon,
)
from .logging_setup import log
from .net import ACTIVE_CHECK_SESSION, BOT_SESSION, PARSER_SESSION
from .timeutils import format_deadline, format_found_at

# Пояснение к статусу колеса в тексте уведомления.
STATUS_NOTES = {
    "active": "колесо активно",
    "soon": "розыгрыш ещё не начался",
    "unknown": "не удалось проверить (API BetBoom не ответил)",
}
# В сообщении о нескольких ссылках пояснения короче, и expired тоже возможен:
# такую ссылку показываем в списке, чтобы человек видел «хвост» поста.
MULTI_STATUS_NOTES = {
    "active": "колесо активно",
    "soon": "розыгрыш ещё не начался",
    "expired": "уже завершилось",
    "unknown": "не удалось проверить",
}


def notifications_enabled() -> bool:
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


def delivery_unknown(error: requests.RequestException) -> bool:
    """True, если по ошибке нельзя понять, доставлено сообщение или нет.

    sendMessage не идемпотентен: повторная отправка — это второе сообщение
    в чате, поэтому ретраятся только сбои, где можно быть уверенным, что
    исходный запрос не был обработан. Ответ с любым кодом (включая 5xx)
    означает, что шлюз Telegram запрос обработал и точно не отправил
    сообщение — 5xx у Bot API означает отказ обработки, а не «принял,
    но потерял ответ», поэтому такие сбои ретраю подлежат. Настоящая
    неопределённость — только там, где ответа нет вовсе: таймаут чтения
    означает, что запрос ушёл и остался без ответа, и неизвестно, был ли
    он обработан (см. parser.retry_failed_notifications).
    """
    response = getattr(error, "response", None)
    if response is not None:
        return False
    # Ответа нет. Ошибка соединения означает, что запрос не ушёл;
    # таймаут чтения — что ушёл и остался без ответа.
    return isinstance(error, requests.Timeout) and not isinstance(
        error, requests.ConnectTimeout
    )


def _mark_delivery(entries: list[dict[str, Any]], error: requests.RequestException) -> None:
    if not delivery_unknown(error):
        return
    for entry in entries:
        entry["delivery_unknown"] = True
    log.warning(
        "%s Статус доставки уведомления неизвестен (%s) — повтор не отправляю, "
        "чтобы не продублировать сообщение; проверьте чат",
        icon("warn"),
        error,
    )


def _post_message(
    session: requests.Session,
    chat_id: str,
    text: str,
    parse_mode: str | None = None,
) -> requests.Response:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    return session.post(
        f"{BOT_API}/sendMessage", json=payload, timeout=REQUEST_TIMEOUT
    )


def send_telegram_notification(
    entry: dict[str, Any], session: requests.Session | None = None
) -> bool:
    """Уведомление об одной новой ссылке."""
    if not notifications_enabled():
        return False
    source_note = " (пост отредактирован)" if entry.get("edited") else ""
    status_note = STATUS_NOTES.get(str(entry.get("status", "")))
    status_line = f"Статус: {status_note}\n" if status_note else ""
    # Дедлайн считается в момент отправки, поэтому «осталось N мин» верно
    # и для повторной попытки спустя несколько циклов.
    deadline = format_deadline(entry.get("ends_at"))
    deadline_line = f"Окончание: до {deadline}\n" if deadline else ""
    referral_line = (
        f"{icon('warn')} Колесо для рефералов\n" if entry.get("referral") else ""
    )
    if entry.get("source") == "twitch":
        badge_icons = "".join(
            TWITCH_ROLE_ICONS.get(role, "")
            for role in entry.get("author_roles", [])
        )
        origin_line = (
            f"Канал: twitch.tv/{entry['channel']} "
            f"(сообщение от {badge_icons}@{entry.get('author', '?')})\n"
        )
        post_line = f"Чат: {entry['message_url']}"
    else:
        origin_line = f"Канал: @{entry['channel']}\n"
        post_line = f"Пост: {entry['message_url']}"
    text = (
        f"{icon('start')} Новая ссылка WheelsParser{source_note}\n"
        f"{origin_line}"
        f"Найдено: {format_found_at(entry['found_at'])}\n"
        f"Ссылка: {entry['url']}\n"
        f"{referral_line}"
        f"{status_line}"
        f"{deadline_line}"
        f"{post_line}"
    )
    try:
        _post_message(session or PARSER_SESSION, TELEGRAM_CHAT_ID, text).raise_for_status()
        return True
    except requests.RequestException as error:
        log.error("Не удалось отправить Telegram-уведомление: %s", error)
        _mark_delivery([entry], error)
        return False


def send_multi_telegram_notification(
    entries: list[dict[str, Any]], session: requests.Session | None = None
) -> bool:
    """Одно уведомление о нескольких новых ссылках из ОДНОГО поста.

    Пост может содержать «хвост» — старый href, оставшийся от копипасты
    прошлого поста, рядом с актуальной ссылкой. API BetBoom не всегда
    отличает такой хвост от активного колеса (см. betboom.api_info_to_status —
    fail-open по дизайну), поэтому вместо N отдельных «Новая ссылка»
    шлём одно сообщение со списком: какая ссылка настоящая, решает человек.
    """
    if not notifications_enabled():
        return False
    first = entries[0]
    source_note = " (пост отредактирован)" if first.get("edited") else ""
    lines = [
        f"{icon('start')} Новая ссылка WheelsParser{source_note}",
        f"Канал: @{first['channel']}",
        f"Найдено: {format_found_at(first['found_at'])}",
        f"{icon('warn')} В посте несколько ссылок — уточните вручную, какая актуальна:",
    ]
    for entry in entries:
        note = MULTI_STATUS_NOTES.get(str(entry.get("status", "")), "")
        if entry.get("referral"):
            note = f"{note}, для рефералов" if note else "для рефералов"
        deadline = format_deadline(entry.get("ends_at"), remaining=False)
        if deadline:
            note = f"{note}, до {deadline}" if note else f"до {deadline}"
        suffix = f" ({note})" if note else ""
        lines.append(f"{entry['url']}{suffix}")
    lines.append(f"Пост: {first['message_url']}")
    try:
        _post_message(
            session or PARSER_SESSION, TELEGRAM_CHAT_ID, "\n".join(lines)
        ).raise_for_status()
        return True
    except requests.RequestException as error:
        log.error(
            "Не удалось отправить Telegram-уведомление (несколько ссылок): %s", error
        )
        _mark_delivery(entries, error)
        return False


def send_keyword_notification(entry: dict[str, Any]) -> bool:
    """Уведомление о посте с ключевыми словами (вызывается из parser-потока).

    Возвращает признак доставки: пост обрабатывается по хэшу один раз,
    поэтому сбой здесь не должен молча терять находку — вызывающий
    сохраняет запись и повторяет отправку (см. parser.retry_failed_notifications).
    """
    if not notifications_enabled():
        return False
    plain_preview = str(entry.get("preview", ""))
    preview_html = str(entry.get("preview_html") or "").strip()
    if not preview_html:
        preview_html = html.escape(plain_preview)
    header = (
        f"{icon('bell')} Ключевые слова: {', '.join(entry['keywords'])}\n"
        f"Канал: @{entry['channel']}\n"
        f"Найдено: {format_found_at(entry['found_at'])}\n"
    )
    footer = f"\nПост: {entry['message_url']}"
    html_text = f"{html.escape(header)}Текст: {preview_html}{html.escape(footer)}"
    try:
        response = _post_message(
            PARSER_SESSION, TELEGRAM_CHAT_ID, html_text, parse_mode="HTML"
        )
        if response.status_code == 400:
            # Telegram отклонил HTML-разметку — шлём обычный текст без ссылок.
            response = _post_message(
                PARSER_SESSION,
                TELEGRAM_CHAT_ID,
                f"{header}Текст: {plain_preview}{footer}",
            )
        response.raise_for_status()
        return True
    except requests.RequestException as error:
        log.error("Не удалось отправить уведомление о ключевых словах: %s", error)
        _mark_delivery([entry], error)
        return False


def send_service_notification(
    text: str, session: requests.Session | None = None
) -> bool:
    """Сервисное сообщение в доверенный чат.

    По умолчанию отправляется из parser-потока его сессией. Вызывающему из
    другого потока нужно передать свою сессию: requests.Session не
    потокобезопасна (см. net.py).
    """
    if not notifications_enabled():
        return False
    try:
        _post_message(
            session or PARSER_SESSION, TELEGRAM_CHAT_ID, text
        ).raise_for_status()
        return True
    except requests.RequestException as error:
        log.error("Не удалось отправить сервисное уведомление: %s", error)
        return False


def bot_send(chat_id: str, text: str) -> None:
    """Ответ на команду — только из потока бота (BOT_SESSION)."""
    try:
        _post_message(BOT_SESSION, chat_id, text, parse_mode="HTML").raise_for_status()
    except requests.RequestException as error:
        log.warning("Бот: не удалось ответить в чат %s: %s", chat_id, error)


def background_bot_send(chat_id: str, text: str) -> None:
    """Ответ из фонового потока active-api (ACTIVE_CHECK_SESSION).

    Отдельная сессия нужна, чтобы не делить BOT_SESSION между потоками.
    """
    try:
        _post_message(
            ACTIVE_CHECK_SESSION, chat_id, text, parse_mode="HTML"
        ).raise_for_status()
    except requests.RequestException as error:
        log.warning("Бот: не удалось ответить в чат %s: %s", chat_id, error)
