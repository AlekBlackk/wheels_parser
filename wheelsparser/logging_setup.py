"""Логирование: цветная консоль, ротация файла, маскировка токена бота.

Все модули пользуются одним объектом ``log``. При импорте у логгера нет
обработчиков — файлы не создаются, пока :func:`setup_logging` не вызван
явно из ``app.main()``. Так ``import wheelsparser`` (в том числе в тестах)
не оставляет parser.log на диске.
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


def redact_token(text: str) -> str:
    """Заменяет токен бота на «***TOKEN***».

    Ошибки requests содержат полный URL вида
    https://api.telegram.org/bot<TOKEN>/... — такой текст нельзя ни писать
    в parser.log, ни пересылать в чат (см. app._notify_thread_crash).
    """
    if not TELEGRAM_BOT_TOKEN:
        return text
    return text.replace(TELEGRAM_BOT_TOKEN, "***TOKEN***")


class RedactTokenFilter(logging.Filter):
    """Маскирует токен бота в сообщениях лога."""

    def filter(self, record: logging.LogRecord) -> bool:
        if TELEGRAM_BOT_TOKEN:
            message = record.getMessage()
            if TELEGRAM_BOT_TOKEN in message:
                record.msg = redact_token(message)
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
    """Подключает обработчики (консоль + файл) к логгеру ``log``.

    Вызывается один раз из ``app.main()`` ПОСЛЕ создания каталога данных:
    RotatingFileHandler открывает LOG_FILE сразу, каталог обязан существовать.
    Повторный вызов безопасен — старые обработчики снимаются.
    """
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
    for existing in list(logger.filters):
        logger.removeFilter(existing)
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


# Без обработчиков до setup_logging(): импорт пакета не трогает диск.
# Сообщения, отправленные до настройки, уходят в logging.lastResort (stderr).
log = logging.getLogger("wheelsparser")
