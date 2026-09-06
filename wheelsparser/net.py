"""HTTP-сессии проекта и их владельцы.

requests.Session НЕ потокобезопасна, поэтому одна общая сессия на все
потоки недопустима. У каждого потока — своя сессия с фиксированным
владельцем; брать чужую нельзя:

    PARSER_SESSION       — поток parser: обход каналов, precheck, уведомления;
    BOT_SESSION          — поток bot: getUpdates и ответы на команды;
    TWITCH_SESSION       — поток twitch-worker: precheck и уведомления из чатов
                           (поток twitch-irc в сеть не ходит вообще — см.
                           :mod:`wheelsparser.twitch`);
    ACTIVE_CHECK_SESSION — фоновый поток active-api: отправка результата /active;
    SUPERVISOR_SESSION   — сервисные уведомления не из своего потока (падение
                           рабочего потока, см. runtime.supervise; срабатывание
                           betboom._apply_stub_guard): у сессии нет одного
                           владельца, поэтому обращения к ней сериализуются
                           локом SUPERVISOR_LOCK ниже.

Рабочие потоки пулов (обход каналов в parser, проверка колёс в /active)
берут сессии из :class:`ThreadLocalSession` ниже: та же схема «одна сессия
на поток», но с ленивым созданием и гарантированным закрытием всех сессий
пула при выходе из контекстного менеджера.
"""

from __future__ import annotations

import threading
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import HEADERS, REQUEST_TIMEOUT

HTTP_TOTAL_RETRIES = 4
HTTP_RETRY_AFTER_MAX = 30
HTTP_BACKOFF_MAX = 10

# Верхняя граница длительности одного запроса с учётом всех попыток и пауз
MAX_REQUEST_DURATION = (
    (1 + HTTP_TOTAL_RETRIES) * REQUEST_TIMEOUT
    + HTTP_TOTAL_RETRIES * HTTP_RETRY_AFTER_MAX
)


def build_session(
    status_forcelist: tuple[int, ...] = (429, 500, 502, 503, 504),
) -> requests.Session:
    # allowed_methods только для GET: повтор POST — это повторная отправка
    # сообщения в Telegram. Ни read-таймаут, ни 5xx/429 не доказывают, что
    # sendMessage не выполнен: запрос сервер уже принял, потерян лишь ответ,
    # и «прозрачный» повтор рассылает дубликат уведомления (тихо — вызывающий
    # код видит успех последней попытки). Ошибки соединения urllib3 повторяет
    # независимо от allowed_methods, и это безопасно: запрос не был отправлен.
    retry = Retry(
        total=HTTP_TOTAL_RETRIES,
        connect=HTTP_TOTAL_RETRIES,
        read=HTTP_TOTAL_RETRIES,
        status=HTTP_TOTAL_RETRIES,
        backoff_factor=1.0,
        status_forcelist=status_forcelist,
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
        retry_after_max=HTTP_RETRY_AFTER_MAX,
        backoff_max=HTTP_BACKOFF_MAX,
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
SUPERVISOR_SESSION = build_session()
# requests.Session не потокобезопасна — любой код, отправляющий сервисное
# уведомление через SUPERVISOR_SESSION не из своего потока, обязан сначала
# взять этот лок (см. docstring модуля).
SUPERVISOR_LOCK = threading.Lock()


class ThreadLocalSession:
    """Потокобезопасный менеджер HTTP-сессий для пулов воркеров.

    Каждый поток пула лениво получает собственный экземпляр requests.Session,
    а при выходе из контекстного менеджера (или вызове close()) все созданные
    сессии корректно закрываются.
    """

    def __init__(
        self,
        session_factory: Any = None,
        status_forcelist: tuple[int, ...] = (429, 500, 502, 503, 504),
    ) -> None:
        self._local = threading.local()
        self._session_factory = session_factory
        self._status_forcelist = status_forcelist
        self._sessions: list[requests.Session] = []
        self._lock = threading.Lock()

    def get(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            if self._session_factory is not None:
                session = self._session_factory()
            else:
                session = build_session(status_forcelist=self._status_forcelist)
            self._local.session = session
            with self._lock:
                self._sessions.append(session)
        return session

    def close(self) -> None:
        with self._lock:
            sessions = list(self._sessions)
            self._sessions.clear()
        for session in sessions:
            try:
                session.close()
            except Exception:
                pass
        self._local = threading.local()

    def __call__(self) -> requests.Session:
        return self.get()

    def __enter__(self) -> ThreadLocalSession:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

