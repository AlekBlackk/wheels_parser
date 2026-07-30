"""HTTP-сессии проекта и их владельцы.

requests.Session НЕ потокобезопасна, поэтому одна общая сессия на все
потоки недопустима. У каждого потока — своя сессия с фиксированным
владельцем; брать чужую нельзя:

    PARSER_SESSION       — поток parser: обход каналов, precheck, уведомления;
    BOT_SESSION          — поток bot: getUpdates и ответы на команды;
    TWITCH_SESSION       — поток twitch: precheck и уведомления из чатов;
    ACTIVE_CHECK_SESSION — фоновый поток active-api: отправка результата /active.

Рабочие потоки пула /active создают собственные сессии через
threading.local (см. :mod:`wheelsparser.betboom`).
"""

from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import HEADERS


def build_session() -> requests.Session:
    # allowed_methods только для GET: повтор POST — это повторная отправка
    # сообщения в Telegram. Ни read-таймаут, ни 5xx/429 не доказывают, что
    # sendMessage не выполнен: запрос сервер уже принял, потерян лишь ответ,
    # и «прозрачный» повтор рассылает дубликат уведомления (тихо — вызывающий
    # код видит успех последней попытки). Ошибки соединения urllib3 повторяет
    # независимо от allowed_methods, и это безопасно: запрос не был отправлен.
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        status=4,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


PARSER_SESSION = build_session()
BOT_SESSION = build_session()
TWITCH_SESSION = build_session()
ACTIVE_CHECK_SESSION = build_session()
