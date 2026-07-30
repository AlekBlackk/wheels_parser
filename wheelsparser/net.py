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
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        status=4,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),
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
