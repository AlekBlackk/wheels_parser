"""Списки под мониторингом: Telegram-каналы, ключевые слова, Twitch-каналы.

Источник правды — текстовые файлы в корне репозитория (channels.txt,
keywords.txt, twitch_channels.txt). Переменные окружения используются
только для первичной инициализации, пока файла ещё нет.

Списки изменяемы на лету (команды бота), поэтому обращаться к ним нужно
через модуль (``registry.CHANNELS``), а не через ``from ... import CHANNELS``:
последнее заморозило бы ссылку и сломало подмену в тестах. Любое чтение и
изменение — под соответствующим локом.
"""

from __future__ import annotations

import logging
import os
import threading

from .config import (
    CHANNELS_FILE,
    DEFAULT_CHANNELS,
    DEFAULT_KEYWORDS,
    KEYWORDS_FILE,
    TWITCH_CHANNELS_FILE,
)

# Сообщения о загрузке списков: логгер на этапе импорта настроек ещё
# может быть не нужен, поэтому копим их здесь и выводим в app.main().
CHANNEL_LOAD_NOTES: list[tuple[int, str]] = []


# ----------------------------------------------------------------------------
# Telegram-каналы
# ----------------------------------------------------------------------------

def parse_channels(raw: str) -> list[str]:
    channels = [item.strip().lstrip("@") for item in raw.split(",") if item.strip()]
    return list(dict.fromkeys(channels))


def read_channels_file() -> list[str]:
    channels = []
    for line in CHANNELS_FILE.read_text(encoding="utf-8").splitlines():
        value = line.split("#", 1)[0].strip().lstrip("@")
        if value:
            channels.append(value)
    return list(dict.fromkeys(channels))


def load_channels() -> tuple[list[str], bool]:
    """Возвращает (каналы, нужно_ли_создать_channels_txt).

    Единственный источник правды — channels.txt. Переменная окружения
    WHEELSPARSER_CHANNELS используется только для первичной инициализации:
    если channels.txt ещё не существует, список из env записывается в файл
    при старте. Дальше все изменения (/add, /remove, ручная правка файла)
    живут в channels.txt и переживают рестарт.
    """
    env_channels = parse_channels(os.getenv("WHEELSPARSER_CHANNELS", ""))

    if CHANNELS_FILE.exists():
        file_channels = read_channels_file()
        if file_channels:
            if env_channels and set(env_channels) != set(file_channels):
                CHANNEL_LOAD_NOTES.append((
                    logging.WARNING,
                    "WHEELSPARSER_CHANNELS задана, но игнорируется: источник "
                    "правды — channels.txt. Удалите channels.txt, чтобы заново "
                    "инициализировать список каналов из переменной окружения.",
                ))
            return file_channels, False

    if env_channels:
        CHANNEL_LOAD_NOTES.append((
            logging.INFO,
            "Список каналов из WHEELSPARSER_CHANNELS сохранён в channels.txt; "
            "дальше управляйте каналами через channels.txt или команды "
            "/add и /remove.",
        ))
        return env_channels, True

    return DEFAULT_CHANNELS.copy(), True


# ----------------------------------------------------------------------------
# Ключевые слова
# ----------------------------------------------------------------------------

def _dedupe_keywords(keywords: list[str]) -> list[str]:
    """Убирает дубликаты без учёта регистра, сохраняя первое написание."""
    unique: list[str] = []
    seen_folded: set[str] = set()
    for keyword in keywords:
        folded = keyword.casefold()
        if folded not in seen_folded:
            seen_folded.add(folded)
            unique.append(keyword)
    return unique


def read_keywords_file() -> list[str]:
    keywords = []
    for line in KEYWORDS_FILE.read_text(encoding="utf-8").splitlines():
        value = line.split("#", 1)[0].strip()
        if value:
            keywords.append(value)
    return _dedupe_keywords(keywords)


def load_keywords() -> tuple[list[str], bool]:
    """Возвращает (ключевые слова, нужно_ли_создать_keywords.txt).

    keywords.txt — единственный источник правды (как channels.txt для каналов).
    Если файла нет или он пуст, он создаётся со словами из DEFAULT_KEYWORDS.
    """
    if KEYWORDS_FILE.exists():
        file_keywords = read_keywords_file()
        if file_keywords:
            return file_keywords, False

    return DEFAULT_KEYWORDS.copy(), True


# ----------------------------------------------------------------------------
# Twitch-каналы
# ----------------------------------------------------------------------------

def parse_twitch_channels(raw: str) -> list[str]:
    channels = [
        item.strip().lstrip("@#").lower()
        for item in raw.split(",")
        if item.strip()
    ]
    return list(dict.fromkeys(channels))


def read_twitch_channels_file() -> list[str]:
    channels = []
    for line in TWITCH_CHANNELS_FILE.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        channels.append(value.lstrip("@").lower())
    return list(dict.fromkeys(channels))


def load_twitch_channels() -> tuple[list[str], bool]:
    """Возвращает (twitch-каналы, нужно_ли_создать_twitch_channels_txt).

    Источник правды — twitch_channels.txt (как channels.txt для Telegram).
    Переменная WHEELSPARSER_TWITCH_CHANNELS используется только для первичной
    инициализации, пока файла ещё нет; дальше все изменения (/addtwitch,
    /removetwitch, ручная правка) живут в файле и переживают рестарт.
    """
    env_channels = parse_twitch_channels(
        os.getenv("WHEELSPARSER_TWITCH_CHANNELS", "")
    )
    if TWITCH_CHANNELS_FILE.exists():
        return read_twitch_channels_file(), False
    if env_channels:
        CHANNEL_LOAD_NOTES.append((
            logging.INFO,
            "Список Twitch-каналов из WHEELSPARSER_TWITCH_CHANNELS сохранён "
            "в twitch_channels.txt; дальше управляйте каналами через файл "
            "или команды /addtwitch и /removetwitch.",
        ))
        return env_channels, True
    return [], False


# ----------------------------------------------------------------------------
# Состояние в памяти
# ----------------------------------------------------------------------------
# Списки пустые до вызова init() из app.main(): импорт пакета не читает
# файлы с диска (и тесты не зависят от реальных channels.txt/keywords.txt).
# init() наполняет списки НА МЕСТЕ (slice-присваивание), не подменяя объект:
# другие модули уже могли захватить ссылку через registry.CHANNELS.

CHANNELS: list[str] = []
SEED_CHANNELS_FILE = False
CHANNELS_LOCK = threading.RLock()
KEYWORDS: list[str] = []
SEED_KEYWORDS_FILE = False
KEYWORDS_LOCK = threading.RLock()
TWITCH_CHANNELS: list[str] = []
SEED_TWITCH_FILE = False
TWITCH_CHANNELS_LOCK = threading.RLock()

# Сигнал twitch-потоку переподключиться (список каналов изменился на лету).
TWITCH_RELOAD = threading.Event()


def init() -> None:
    """Загружает списки из файлов (или значений по умолчанию/env).

    Вызывается один раз из ``app.main()`` до старта потоков. Повторный
    вызов безопасен: списки перечитываются заново.
    """
    global SEED_CHANNELS_FILE, SEED_KEYWORDS_FILE, SEED_TWITCH_FILE
    channels, SEED_CHANNELS_FILE = load_channels()
    with CHANNELS_LOCK:
        CHANNELS[:] = channels
    keywords, SEED_KEYWORDS_FILE = load_keywords()
    with KEYWORDS_LOCK:
        KEYWORDS[:] = keywords
    twitch, SEED_TWITCH_FILE = load_twitch_channels()
    with TWITCH_CHANNELS_LOCK:
        TWITCH_CHANNELS[:] = twitch


def channels_snapshot() -> list[str]:
    with CHANNELS_LOCK:
        return list(CHANNELS)


def keywords_snapshot() -> list[str]:
    with KEYWORDS_LOCK:
        return list(KEYWORDS)


def twitch_channels_snapshot() -> list[str]:
    with TWITCH_CHANNELS_LOCK:
        return list(TWITCH_CHANNELS)


# ----------------------------------------------------------------------------
# Запись файлов
# ----------------------------------------------------------------------------

def save_channels_file() -> None:
    with CHANNELS_LOCK:
        lines = ["# Один публичный Telegram-канал на строку. Символ @ необязателен."]
        lines.extend(CHANNELS)
    CHANNELS_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def save_keywords_file() -> None:
    with KEYWORDS_LOCK:
        lines = [
            "# Одно ключевое слово (или фраза) на строку. Регистр не важен.",
            "# слово — поиск по границам слова с учётом русских окончаний;",
            "# *слово* — поиск по подстроке (найдёт и «суперколесо»).",
        ]
        lines.extend(KEYWORDS)
    KEYWORDS_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def save_twitch_channels_file() -> None:
    with TWITCH_CHANNELS_LOCK:
        lines = ["# Один Twitch-канал (логин) на строку, без @ и без #."]
        lines.extend(TWITCH_CHANNELS)
    TWITCH_CHANNELS_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
