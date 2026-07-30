"""Логирование: цветная консоль, ротация файла, маскировка токена бота.

Логгер создаётся при импорте — все модули пользуются одним объектом
``log``. Импортировать модуль повторно безопасно: setup_logging()
вызывается один раз.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from .config import LOG_FILE, TELEGRAM_BOT_TOKEN, USE_COLORS

try:
    from colorama import Fore, Style, just_fix_windows_console

    just_fix_windows_console()
    HAS_COLOR = True
except ImportError:
    HAS_COLOR = False


class RedactTokenFilter(logging.Filter):
    """Маскирует токен бота в сообщениях лога.

    Ошибки requests содержат полный URL вида
    https://api.telegram.org/bot<TOKEN>/... — без фильтра токен
    попадает в parser.log (например, при 409 Conflict).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if TELEGRAM_BOT_TOKEN:
            message = record.getMessage()
            if TELEGRAM_BOT_TOKEN in message:
                record.msg = message.replace(TELEGRAM_BOT_TOKEN, "***TOKEN***")
                record.args = None
        return True


class ConsoleFormatter(logging.Formatter):
    """Цвета для консоли: предупреждения жёлтые, ошибки красные, новые ссылки зелёные."""

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        if not (HAS_COLOR and USE_COLORS):
            return message
        if getattr(record, "highlight", False):
            return f"{Style.BRIGHT}{Fore.GREEN}{message}{Style.RESET_ALL}"
        if record.levelno >= logging.ERROR:
            return f"{Style.BRIGHT}{Fore.RED}{message}{Style.RESET_ALL}"
        if record.levelno == logging.WARNING:
            return f"{Fore.YELLOW}{message}{Style.RESET_ALL}"
        return message


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("wheelsparser")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    console = logging.StreamHandler()
    console.setFormatter(
        ConsoleFormatter("%(asctime)s %(message)s", datefmt="%H:%M:%S")
    )
    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logger.addFilter(RedactTokenFilter())
    logger.addHandler(console)
    logger.addHandler(file_handler)
    return logger


def force_utf8_console() -> None:
    """Перевод stdout/stderr в UTF-8 (актуально для Windows).

    В cmd.exe/PowerShell консоль по умолчанию работает в cp866/cp1251:
    эмодзи и часть символов вызывают UnicodeEncodeError или кракозябры.
    errors="replace" гарантирует, что вывод не уронит парсер даже там,
    где UTF-8 недоступен.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            continue


force_utf8_console()
log = setup_logging()
