"""Политика повторов HTTP-сессий.

Повтор POST — это повторная доставка сообщения в Telegram: сервер уже
принял запрос, а клиент увидел таймаут или 5xx и отправил его снова.
Тесты поднимают локальный HTTP-сервер и считают реально полученные
запросы — mock здесь бесполезен, повторы делает urllib3 внутри адаптера.
"""

from __future__ import annotations

import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

from wheelsparser.net import build_session


class _CountingHandler(BaseHTTPRequestHandler):
    """Считает запросы; поведение первого ответа задаётся классом-владельцем."""

    protocol_version = "HTTP/1.0"

    def _handle(self, method: str) -> None:
        server: _CountingServer = self.server  # type: ignore[assignment]
        server.requests.append(method)
        first = len(server.requests) == 1
        if first and server.first_status is not None:
            self.send_response(server.first_status)
            self.end_headers()
            return
        if first and server.first_delay:
            time.sleep(server.first_delay)
        self.send_response(200)
        self.end_headers()

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        self._handle("POST")

    def log_message(self, *args: object) -> None:
        pass


class _CountingServer(HTTPServer):
    first_status: int | None = None
    first_delay: float = 0.0

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _CountingHandler)
        self.requests: list[str] = []


class RetryPolicyTests(unittest.TestCase):
    def start_server(
        self, first_status: int | None = None, first_delay: float = 0.0
    ) -> _CountingServer:
        server = _CountingServer()
        server.first_status = first_status
        server.first_delay = first_delay
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        return server

    def url(self, server: _CountingServer) -> str:
        return f"http://127.0.0.1:{server.server_port}/sendMessage"

    def test_post_is_not_repeated_after_server_error(self):
        # 502 от шлюза Telegram не значит «сообщение не доставлено»:
        # повтор POST здесь рассылает дубликат уведомления.
        server = self.start_server(first_status=502)
        session = build_session()

        response = session.post(self.url(server), json={"text": "hi"}, timeout=5)

        self.assertEqual(response.status_code, 502)
        self.assertEqual(server.requests, ["POST"])

    def test_post_is_not_repeated_after_read_timeout(self):
        # Запрос сервер уже принял; клиент не дождался ответа. Повтор
        # доставит второе сообщение — единственный безопасный ответ ошибка.
        server = self.start_server(first_delay=2.0)
        session = build_session()

        with self.assertRaises(requests.RequestException):
            session.post(self.url(server), json={"text": "hi"}, timeout=0.5)

        self.assertEqual(server.requests, ["POST"])

    def test_get_is_still_repeated_after_server_error(self):
        # Чтение t.me и API BetBoom идемпотентно — повторы там нужны.
        server = self.start_server(first_status=502)
        session = build_session()

        response = session.get(self.url(server), timeout=5)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(server.requests, ["GET", "GET"])


if __name__ == "__main__":
    unittest.main()
