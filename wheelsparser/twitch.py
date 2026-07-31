"""Twitch: анонимное чтение чатов через IRC (без токенов и OAuth).

Ловим ТОЛЬКО ссылки betboom.ru/freestream от стримера, модераторов, VIP
и известных ботов. Ключевые слова в Twitch-чатах не ищутся: зрители пишут
«колесо» постоянно, и уведомления превратились бы в спам.

У IRC нет истории: читаются только сообщения, пришедшие пока парсер
запущен. Ссылки из времени простоя не восстанавливаются.

Работа разнесена на два потока:

    twitch-irc    — только читает сокет и складывает сообщения со ссылкой
                    в TWITCH_JOBS. Никаких сетевых вызовов: пока поток ждёт
                    HTTP-ответ, он не читает сокет и не отвечает на PING,
                    а Twitch за это рвёт соединение (precheck при недоступном
                    API BetBoom блокировал поток до ~70 с — этого достаточно,
                    чтобы вылететь из чата);
    twitch-worker — забирает сообщения из очереди, ходит в API BetBoom
                    (precheck) и шлёт уведомления. Он один, поэтому порядок
                    сообщений сохраняется, а параллельных запросов к API нет.
"""

from __future__ import annotations

import queue
import random
import socket
import ssl
import time
from datetime import datetime
from typing import Any

from . import registry
from .alerts import cooldown_active, mark_url_alert
from .betboom import is_referral_wheel, precheck_wheel
from .config import (
    FREESTREAM_RE,
    PRECHECK_WHEELS,
    REQUEST_TIMEOUT,
    TWITCH_BOTS,
    TWITCH_IDLE_TIMEOUT_SECONDS,
    TWITCH_IRC_HOST,
    TWITCH_IRC_PORT,
    TWITCH_QUEUE_MAXSIZE,
    icon,
)
from .logging_setup import log
from .net import TWITCH_SESSION
from .runtime import STOP_EVENT
from .telegram_api import send_telegram_notification
from .timeutils import now_msk
from .urls import normalize_url

# Находки twitch-потока: уведомления по ним уже отправлены, parser-поток
# забирает записи в начале каждого цикла и сохраняет их в freebets.json
# (results принадлежит parser-потоку, трогать его из другого потока нельзя).
TWITCH_NEW_ENTRIES: queue.Queue[dict[str, Any]] = queue.Queue()

# Сообщения со ссылкой, ждущие обработки: (канал, автор, теги, текст, время).
# Время фиксируется в момент получения сообщения, а не обработки: found_at
# и кулдаун должны считаться от момента, когда ссылка появилась в чате.
# Очередь ограничена: затянувшийся сбой API BetBoom не должен съедать память.
TwitchJob = tuple[str, str, dict[str, str], str, datetime]
TWITCH_JOBS: queue.Queue[TwitchJob] = queue.Queue(maxsize=TWITCH_QUEUE_MAXSIZE)


def parse_irc_line(line: str) -> tuple[dict[str, str], str, str, str]:
    """Разбирает строку IRC на (tags, prefix, command, rest)."""
    tags: dict[str, str] = {}
    if line.startswith("@"):
        raw_tags, _, line = line[1:].partition(" ")
        for part in raw_tags.split(";"):
            key, _, value = part.partition("=")
            tags[key] = value
    prefix = ""
    if line.startswith(":"):
        prefix, _, line = line[1:].partition(" ")
    command, _, rest = line.partition(" ")
    return tags, prefix, command, rest


def author_roles(tags: dict[str, str], login: str) -> list[str]:
    """Роли автора: broadcaster/moderator/vip/bot. Пустой список — обычный зритель."""
    badges = {badge.split("/", 1)[0] for badge in tags.get("badges", "").split(",")}
    roles: list[str] = [
        role for role in ("broadcaster", "moderator", "vip") if role in badges
    ]
    if "moderator" not in roles and tags.get("mod") == "1":
        roles.append("moderator")
    if login in TWITCH_BOTS:
        roles.append("bot")
    return roles


def handle_twitch_message(
    channel: str,
    login: str,
    tags: dict[str, str],
    text: str,
    received_at: datetime | None = None,
) -> None:
    """Обрабатывает одно сообщение Twitch-чата.

    Ходит в сеть (precheck и отправка уведомления), поэтому вызывается
    ТОЛЬКО из потока twitch-worker — см. модульную документацию.
    received_at — время получения сообщения; по умолчанию текущее.
    """
    received_at = received_at or now_msk()
    urls: list[str] = []
    for candidate in FREESTREAM_RE.findall(text):
        normalized = normalize_url(candidate)
        if normalized and normalized not in urls:
            urls.append(normalized)
    if not urls:
        return
    roles = author_roles(tags, login)
    if not roles:
        log.info(
            "twitch [#%s]: ссылка от @%s проигнорирована (не стример/мод/VIP/бот)",
            channel,
            login,
        )
        return
    for url in urls:
        now = received_at
        if cooldown_active(url, now):
            continue  # недавно уже оповещали об этом колесе (TG или Twitch)
        if PRECHECK_WHEELS:
            status, referral, ends_at = precheck_wheel(url, TWITCH_SESSION)
        else:
            status, referral, ends_at = "", is_referral_wheel(url, None), ""
        if status == "expired":
            log.info(
                "%s Пропускаю %s [twitch #%s]: колесо уже завершилось (API BetBoom)",
                icon("warn"),
                url,
                channel,
            )
            mark_url_alert(url, now)
            continue
        entry = {
            "url": url,
            "found_at": now.isoformat(timespec="seconds"),
            "channel": channel,
            "source": "twitch",
            "author": login,
            "author_roles": roles,
            "msg_id": tags.get("id", ""),
            "message_url": f"https://www.twitch.tv/{channel}",
            "preview": text[:200],
            "edited": False,
            "status": status,
            "referral": referral,
            "ends_at": ends_at,
            "notified": False,
        }
        # Помечаем ДО отправки: даже при сбое уведомления повторной
        # рассылки того же колеса в течение кулдауна не будет.
        mark_url_alert(url, now)
        entry["notified"] = send_telegram_notification(entry, TWITCH_SESSION)
        TWITCH_NEW_ENTRIES.put(entry)
        log.info(
            "%s Новая ссылка из Twitch [#%s, от @%s]: %s",
            icon("link"),
            channel,
            login,
            url,
            extra={"highlight": True},
        )


def enqueue_twitch_message(
    channel: str, login: str, tags: dict[str, str], text: str
) -> bool:
    """Ставит сообщение в очередь обработки (вызывается из потока twitch-irc).

    Делает только дешёвую работу: сообщения без ссылки на колесо (а это
    практически весь чат) отсеиваются регэкспом здесь же, поэтому очередь
    наполняется единицами сообщений и не растёт от обычной болтовни.
    Возвращает True, если сообщение принято в обработку.
    """
    if not FREESTREAM_RE.search(text):
        return False
    try:
        TWITCH_JOBS.put_nowait((channel, login, tags, text, now_msk()))
    except queue.Full:
        # Очередь переполняется только при долгом сбое обработчика. Ссылка
        # теряется, поэтому это ошибка, а не предупреждение.
        log.error(
            "%s Twitch [#%s]: очередь обработки переполнена (%s), "
            "сообщение от @%s пропущено",
            icon("warn"),
            channel,
            TWITCH_QUEUE_MAXSIZE,
            login,
        )
        return False
    return True


def twitch_worker_loop() -> None:
    """Поток twitch-worker: precheck и уведомления по сообщениям из очереди."""
    while not STOP_EVENT.is_set():
        try:
            job = TWITCH_JOBS.get(timeout=1.0)
        except queue.Empty:
            continue
        try:
            handle_twitch_message(*job)
        except Exception:
            log.exception("Twitch: ошибка обработки сообщения")
        finally:
            TWITCH_JOBS.task_done()


def _connect(channels: list[str]) -> ssl.SSLSocket:
    """Подключается к IRC Twitch анонимно и джойнит каналы."""
    raw_socket = socket.create_connection(
        (TWITCH_IRC_HOST, TWITCH_IRC_PORT), timeout=REQUEST_TIMEOUT
    )
    sock = ssl.create_default_context().wrap_socket(
        raw_socket, server_hostname=TWITCH_IRC_HOST
    )
    sock.settimeout(5.0)
    # Анонимный вход: ник justinfan<цифры>, пароль не нужен. Читать чат
    # можно без регистрации приложения и OAuth-токенов.
    nick = f"justinfan{random.randint(10_000, 99_999)}"
    # tags — бейджи авторов (broadcaster/moderator/vip),
    # commands — служебные сообщения вроде RECONNECT.
    sock.sendall(b"CAP REQ :twitch.tv/tags twitch.tv/commands\r\n")
    sock.sendall(f"NICK {nick}\r\n".encode())
    for index, channel in enumerate(channels):
        sock.sendall(f"JOIN #{channel}\r\n".encode())
        # Лимит Twitch на частоту JOIN — обязательная пауза между каналами.
        if index < len(channels) - 1:
            STOP_EVENT.wait(0.6)
    return sock


def _idle_timeout_exceeded(last_activity: float) -> bool:
    return time.monotonic() - last_activity > TWITCH_IDLE_TIMEOUT_SECONDS


def _read_stream(sock: ssl.SSLSocket) -> None:
    """Читает чат, пока не попросят остановиться или переподключиться.

    Полуоткрытое TCP-соединение (сервер не шлёт RST) не даёт recv() ни
    ошибки, ни данных — только повторяющиеся таймауты, и без вотчдога
    поток крутился бы в continue бесконечно, не читая чат и не логируя
    проблему. last_activity сбрасывается на каждый полученный чанк
    (включая PING) — таймаут ловит только реально мёртвое соединение.
    """
    buffer = b""
    last_activity = time.monotonic()
    while not STOP_EVENT.is_set() and not registry.TWITCH_RELOAD.is_set():
        try:
            chunk = sock.recv(4096)
        except TimeoutError:
            if _idle_timeout_exceeded(last_activity):
                raise ConnectionError(
                    f"нет данных от Twitch IRC дольше {TWITCH_IDLE_TIMEOUT_SECONDS} с"
                ) from None
            continue
        except ssl.SSLError as error:
            if "timed out" not in str(error).lower():
                raise
            if _idle_timeout_exceeded(last_activity):
                raise ConnectionError(
                    f"нет данных от Twitch IRC дольше {TWITCH_IDLE_TIMEOUT_SECONDS} с"
                ) from None
            continue
        if not chunk:
            raise ConnectionError("соединение закрыто сервером")
        last_activity = time.monotonic()
        buffer += chunk
        while b"\r\n" in buffer:
            raw_line, buffer = buffer.split(b"\r\n", 1)
            line = raw_line.decode("utf-8", errors="replace")
            if not line:
                continue
            if line.startswith("PING"):
                sock.sendall(line.replace("PING", "PONG", 1).encode() + b"\r\n")
                continue
            tags, prefix, command, rest = parse_irc_line(line)
            if command == "RECONNECT":
                raise ConnectionError("сервер запросил RECONNECT")
            if command != "PRIVMSG":
                continue
            login = prefix.split("!", 1)[0].lower()
            target, _, message_text = rest.partition(" :")
            chat = target.strip().lstrip("#").lower()
            # Только постановка в очередь: обработка (и любой сетевой
            # вызов) — в потоке twitch-worker, иначе пропустим PING.
            try:
                enqueue_twitch_message(chat, login, tags, message_text)
            except Exception:
                log.exception("Twitch: ошибка постановки сообщения в очередь")


def twitch_loop() -> None:
    """Поток Twitch: держит IRC-соединение и переподключается при обрывах."""
    backoff = 5.0
    while not STOP_EVENT.is_set():
        registry.TWITCH_RELOAD.clear()
        channels = registry.twitch_channels_snapshot()
        if not channels:
            # Каналов нет — ждём /addtwitch или остановки.
            STOP_EVENT.wait(5)
            continue
        sock: ssl.SSLSocket | None = None
        try:
            sock = _connect(channels)
            log.info(
                "%s Twitch: подключён, чатов под мониторингом: %s",
                icon("ok"),
                len(channels),
            )
            backoff = 5.0
            _read_stream(sock)
        except OSError as error:
            if STOP_EVENT.is_set():
                break
            log.warning(
                "%s Twitch: ошибка соединения (%s) — переподключение через %.0f с",
                icon("warn"),
                error,
                backoff,
            )
            STOP_EVENT.wait(backoff)
            backoff = min(backoff * 2, 120.0)
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
