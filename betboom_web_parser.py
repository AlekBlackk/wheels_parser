#!/usr/bin/env python3
"""WheelsParser: monitor public Telegram channels for BetBoom freestream links."""

from __future__ import annotations

import functools
import hashlib
import html
import json
import logging
import os
import random
import re
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from colorama import Fore, Style, just_fix_windows_console

    just_fix_windows_console()
    HAS_COLOR = True
except ImportError:
    HAS_COLOR = False


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

CHANNELS_FILE = BASE_DIR / "channels.txt"
KEYWORDS_FILE = BASE_DIR / "keywords.txt"
OUTPUT_FILE = BASE_DIR / "freebets.json"
SEEN_FILE = BASE_DIR / "seen_ids.json"
LOG_FILE = BASE_DIR / "parser.log"
LOCK_FILE = BASE_DIR / "wheelsparser.lock"

DEFAULT_CHANNELS = [
    "amam0610", "aunkereEZ", "risenhaha", "zaykapoehali", "AdamStaya",
    "mugretnug", "mugretnugbet", "PAPAdota2", "NeretCast", "YBNFedor",
    "hoochcs2", "solo322berezin", "KRATtv", "dayneZz", "jestercast",
    "obshakstaya", "meowbettt", "mechanogun", "Vophets", "GShikaryan",
    "acoolbazarit",
]

# Ключевые слова по умолчанию. Поиск регистронезависимый:
# «колесо», «Колесо» и «КОЛЕСО» — одно и то же слово.
# Формат записи:
#   слово    — поиск по границам слова с учётом русских окончаний
#              («колесо» найдёт «колеса», «колесом», «колёсами»,
#              но не «колесовать» и не «околесица»);
#   *слово*  — поиск по подстроке (найдёт и «суперколесо»).
DEFAULT_KEYWORDS = ["колесо"]


def env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


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


# Сообщения о загрузке каналов; логгер на этом этапе ещё не создан,
# поэтому копим их здесь и выводим в main().
CHANNEL_LOAD_NOTES: list[tuple[int, str]] = []


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


CHANNELS, _SEED_CHANNELS_FILE = load_channels()
CHANNELS_LOCK = threading.RLock()
KEYWORDS, _SEED_KEYWORDS_FILE = load_keywords()
KEYWORDS_LOCK = threading.RLock()
CHECK_INTERVAL = env_int("CHECK_INTERVAL", 60, 10)
REQUEST_TIMEOUT = env_int("REQUEST_TIMEOUT", 15, 5)
MESSAGES_PER_CHANNEL = env_int("MESSAGES_PER_CHANNEL", 50, 10)
MAX_SEEN_PER_CHANNEL = env_int("MAX_SEEN_PER_CHANNEL", 2000, 100)
# Максимум записей в freebets.json: без лимита файл и память растут бесконечно.
# При превышении старые записи отбрасываются в конце цикла.
MAX_RESULTS = env_int("MAX_RESULTS", 5000, 100)
WHEELS_WINDOW_MINUTES = env_int("WHEELS_WINDOW_MINUTES", 10, 1)
ACTIVE_MAX_AGE_HOURS = env_int("ACTIVE_MAX_AGE_HOURS", 20, 1)
# /active смотрит только на колёса, найденные сегодня по МСК: счётчик «N из M»
# сбрасывается каждый день в 00:00 по Москве (UTC+3, без летнего времени).
MSK_TZ = timezone(timedelta(hours=3), "MSK")
# Сколько потоков одновременно опрашивают API BetBoom при /active.
ACTIVE_CHECK_CONCURRENCY = env_int("ACTIVE_CHECK_CONCURRENCY", 3, 1)
# Повторное уведомление о том же URL разрешено после этого кулдауна (мин).
# Колёса BetBoom живут на постоянных адресах (/staya, /neret, ...), поэтому
# «вечная» дедупликация по URL пропускала повторные запуски того же колеса.
REALERT_COOLDOWN_MINUTES = env_int("REALERT_COOLDOWN_MINUTES", 30, 1)
# Проверять колесо через API BetBoom перед отправкой уведомления. Посты
# нередко содержат «хвосты» — старые href на прошлые колёса, невидимые
# в Telegram, но попадающие в HTML-разметку (стример скопировал прошлый пост
# и обновил только видимый текст). Завершившиеся колёса не рассылаются.
PRECHECK_WHEELS = env_bool("PRECHECK_WHEELS", True)
# Команды старше этого возраста (сек) подтверждаются, но не выполняются —
# защита от бэклога getUpdates, накопившегося за время простоя парсера.
STALE_COMMAND_SECONDS = env_int("STALE_COMMAND_SECONDS", 120, 10)
# Уведомление о «мёртвом» канале после N подряд неудачных циклов.
CHANNEL_FAIL_THRESHOLD = env_int("CHANNEL_FAIL_THRESHOLD", 5, 2)
ALERT_ON_FIRST_RUN = env_bool("ALERT_ON_FIRST_RUN", False)
USE_COLORS = env_bool("USE_COLORS", True)
USE_ICONS = env_bool("USE_ICONS", True)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
BOT_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
USERNAME_RE = re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{3,31})$")
STREAMER_WHEEL_INFO_API = "https://betboom.ru/api/streamer-wheel/action/get-info"

FREESTREAM_RE = re.compile(
    r"https?://(?:www\.)?betboom\.ru/freestream/[A-Za-z0-9_~:/?#\[\]@!$&'()*+,;=%.-]+",
    re.IGNORECASE,
)
TRAILING_PUNCTUATION = ".,;:!?)]}>'\""
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}


ICONS = {
    "start": "\U0001f3a1",
    "ok": "\u2705",
    "warn": "\u26a0\ufe0f",
    "link": "\U0001f381",
    "stop": "\U0001f6d1",
    "bell": "\U0001f514",
    "scan": "\U0001f50d",
    "bot": "\u2328\ufe0f",
}
ASCII_ICONS = {
    "start": "[*]",
    "ok": "[OK]",
    "warn": "[!]",
    "link": "[NEW]",
    "stop": "[x]",
    "bell": "[i]",
    "scan": "[>>]",
    "bot": "[BOT]",
}


def icon(name: str) -> str:
    return (ICONS if USE_ICONS else ASCII_ICONS)[name]


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


log = setup_logging()


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


SESSION = build_session()  # только main-поток: парсинг и уведомления
BOT_SESSION = build_session()  # только поток бота: getUpdates, ответы, /active
STOP_EVENT = threading.Event()  # потокобезопасный флаг остановки


def request_stop(_signum: int, _frame: Any) -> None:
    if STOP_EVENT.is_set():
        # Второй Ctrl+C — не ждём graceful shutdown, выходим сразу.
        # Состояние не теряется: save_seen() вызывается в конце каждого цикла.
        log.warning("%s Повторный Ctrl+C — принудительный выход", icon("stop"))
        os._exit(1)
    STOP_EVENT.set()
    log.info(
        "Получен сигнал остановки; завершаю текущий цикл "
        "(ещё раз Ctrl+C — немедленный выход)"
    )


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        log.warning("Не удалось прочитать %s: %s", path.name, error)
        return default


def atomic_write_json(path: Path, data: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def load_seen() -> tuple[dict[str, dict[str, str]], bool]:
    existed = SEEN_FILE.exists()
    raw = read_json(SEEN_FILE, {})
    if not isinstance(raw, dict):
        raw = {}
    seen: dict[str, dict[str, str]] = {}
    for channel, value in raw.items():
        if isinstance(value, dict):
            # Новый формат: id сообщения -> хэш содержимого.
            seen[channel] = {
                str(message_id): str(content_hash or "")
                for message_id, content_hash in value.items()
            }
        elif isinstance(value, list):
            # Старый формат (список id): хэшей ещё нет. Пустая строка
            # означает «содержимое неизвестно» — правкой не считается,
            # хэш просто запоминается при следующем цикле.
            seen[channel] = {str(message_id): "" for message_id in value}
        else:
            seen[channel] = {}
    with CHANNELS_LOCK:
        for channel in CHANNELS:
            seen.setdefault(channel, {})
    has_state = existed and any(seen.values())
    return seen, has_state


def message_id_sort_key(message_id: str) -> int:
    """Числовой суффикс из data-post вида 'channel/12345'.

    Лексикографическая сортировка строк здесь опасна: 'channel/999' > 'channel/1000',
    и при обрезке лимита свежие ID вылетали бы вместо старых.
    """
    try:
        return int(message_id.rsplit("/", 1)[-1])
    except ValueError:
        return 0  # нестандартный ID уйдёт в начало и обрежется первым


def save_seen(seen: dict[str, dict[str, str]]) -> None:
    with CHANNELS_LOCK:
        active = set(CHANNELS)
    # Каналы, удалённые через /remove, в файл не пишем — иначе seen_ids.json
    # копит их ID вечно. Из памяти (seen) не удаляем: если канал вернут через
    # /add до рестарта, старые сообщения не вызовут ложных уведомлений.
    serializable = {
        channel: {
            message_id: messages[message_id]
            for message_id in sorted(messages, key=message_id_sort_key)[
                -MAX_SEEN_PER_CHANNEL:
            ]
        }
        for channel, messages in seen.items()
        if channel in active
    }
    atomic_write_json(SEEN_FILE, serializable)


def load_results() -> list[dict[str, Any]]:
    data = read_json(OUTPUT_FILE, [])
    return data if isinstance(data, list) else []


def normalize_url(url: str) -> str:
    cleaned = url.strip().rstrip(TRAILING_PUNCTUATION)
    parts = urlsplit(cleaned)
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    if netloc == "www.betboom.ru":
        netloc = "betboom.ru"
    return urlunsplit((scheme, netloc, parts.path, parts.query, ""))


def find_urls(message: Any, text: str) -> list[str]:
    candidates = [link.get("href", "") for link in message.find_all("a", href=True)]
    candidates.extend(FREESTREAM_RE.findall(text))
    urls: list[str] = []
    for candidate in candidates:
        match = FREESTREAM_RE.match(candidate)
        if not match:
            continue
        normalized = normalize_url(match.group(0))
        if normalized not in urls:
            urls.append(normalized)
    return urls


def message_content_hash(text: str, urls: list[str]) -> str:
    """Хэш содержимого сообщения для обнаружения правок постов.

    Считается по нормализованному тексту (схлопнутые пробелы) и списку
    найденных ссылок: правка href без изменения видимого текста тоже
    меняет хэш. Усечён до 16 hex-символов — криптостойкость не нужна,
    важна только смена значения при реальном изменении содержимого.
    """
    normalized = re.sub(r"\s+", " ", text).strip()
    payload = normalized + "\n" + "\n".join(urls)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


# Частые русские окончания для поиска по границам слова: «колесо» найдёт
# «колеса», «колесом», «колёсами». Порядок не влияет на корректность
# (regex перебирает альтернативы с backtracking), но длинные идут первыми.
_RU_ENDINGS = (
    "ами", "ями", "ого", "его", "ому", "ему", "ыми", "ими",
    "ая", "яя", "ое", "ее", "ые", "ие", "ой", "ей", "ом", "ем",
    "ам", "ям", "ах", "ях", "ов", "ев", "ым", "им", "ых", "их",
    "ую", "юю", "ий", "ый",
    "а", "я", "о", "е", "у", "ю", "ы", "и", "й", "ь",
)
_WORD_CHARS = "0-9A-Za-zА-Яа-яЁё_"


def _normalize_for_match(text: str) -> str:
    """casefold + «ё» → «е», чтобы «колёса» совпадало с «колеса»."""
    return text.casefold().replace("ё", "е")


@functools.lru_cache(maxsize=256)
def _keyword_regex(keyword: str) -> re.Pattern[str]:
    """Компилирует регэксп для одного ключевого слова.

    - «*слово*» — поиск по подстроке (старое поведение: найдёт «суперколесо»);
    - «слово» — по границам слова с учётом русских окончаний:
      «колесо» найдёт «колесо», «колеса», «колесом», «колёсами»,
      но не «колесовать» и не «околесица».
    Для фраз («фрибет колесо») окончания допускаются у каждого слова.
    """
    raw = _normalize_for_match(keyword.strip())
    if raw.startswith("*") and raw.endswith("*") and len(raw) > 2:
        return re.compile(re.escape(raw.strip("*")))
    endings = "|".join(_RU_ENDINGS)
    token_patterns: list[str] = []
    for token in raw.split():
        stem = token
        # Окончание самого ключевого слова тоже отбрасываем:
        # «колесо» → основа «колес» + любое окончание из списка.
        for ending in _RU_ENDINGS:
            if stem.endswith(ending) and len(stem) - len(ending) >= 3:
                stem = stem[: len(stem) - len(ending)]
                break
        token_patterns.append(rf"{re.escape(stem)}(?:{endings})?")
    body = r"\s+".join(token_patterns)
    return re.compile(rf"(?<![{_WORD_CHARS}]){body}(?![{_WORD_CHARS}])")


def find_keywords(text: str) -> list[str]:
    """Ключевые слова, найденные в тексте сообщения.

    Поиск регистронезависимый, «ё» и «е» считаются одной буквой.
    «слово» ищется по границам слова с учётом окончаний,
    «*слово*» — по подстроке (см. _keyword_regex).
    """
    if not text:
        return []
    normalized = _normalize_for_match(text)
    with KEYWORDS_LOCK:
        keywords = list(KEYWORDS)
    return [
        keyword
        for keyword in keywords
        if _keyword_regex(keyword).search(normalized)
    ]


def format_found_at(value: Any) -> str:
    """Время находки в читаемом виде: 16.07.2026 | 20:40:31 | +03:00."""
    try:
        moment = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return str(value)
    formatted = f"{moment.strftime('%d.%m.%Y')} | {moment.strftime('%H:%M:%S')}"
    offset = moment.strftime("%z")
    if offset:
        formatted += f" | {offset[:3]}:{offset[3:]}"
    return formatted


def message_preview_html(text_element: Any, limit: int = 200) -> str:
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
            if visible + len(label) > limit:
                parts.append("…")
                break
            parts.append(
                f'<a href="{html.escape(href, quote=True)}">{html.escape(label)}</a>'
            )
        else:
            if visible + len(label) > limit:
                cut = label[: limit - visible].rstrip()
                if cut:
                    parts.append(html.escape(cut))
                parts.append("…")
                break
            parts.append(html.escape(label))
        visible += len(label) + 1
    return " ".join(parts)


def fetch_channel(channel: str) -> list[dict[str, Any]] | None:
    url = f"https://t.me/s/{channel}"
    try:
        response = SESSION.get(url, timeout=REQUEST_TIMEOUT)
        if response.status_code == 404:
            log.warning("[%s] канал не найден или приватный (404)", channel)
            return None
        response.raise_for_status()
    except requests.RequestException as error:
        log.warning("[%s] ошибка запроса: %s", channel, error)
        return None

    soup = BeautifulSoup(response.text, "html.parser")
    messages = soup.select(".tgme_widget_message_wrap")[-MESSAGES_PER_CHANNEL:]
    results: list[dict[str, Any]] = []
    for message in messages:
        bubble = message.select_one(".tgme_widget_message")
        if not bubble:
            continue
        message_id = str(bubble.get("data-post", "")).strip()
        if not message_id:
            continue
        text_element = message.select_one(".tgme_widget_message_text")
        text = text_element.get_text(" ", strip=True) if text_element else ""
        urls = find_urls(message, text)
        results.append({
            "id": message_id,
            "text": text,
            "preview_html": message_preview_html(text_element),
            "urls": urls,
            "hash": message_content_hash(text, urls),
            "message_url": f"https://t.me/{message_id}",
        })
    return results


def send_telegram_notification(entry: dict[str, Any]) -> bool:
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return False
    source_note = " (пост отредактирован)" if entry.get("edited") else ""
    status_notes = {
        "active": "колесо активно",
        "soon": "розыгрыш ещё не начался",
        "unknown": "не удалось проверить (API BetBoom не ответил)",
    }
    status_note = status_notes.get(str(entry.get("status", "")))
    status_line = f"Статус: {status_note}\n" if status_note else ""
    text = (
        f"{icon('start')} Новая ссылка WheelsParser{source_note}\n"
        f"Канал: @{entry['channel']}\n"
        f"Найдено: {format_found_at(entry['found_at'])}\n"
        f"Ссылка: {entry['url']}\n"
        f"{status_line}"
        f"Пост: {entry['message_url']}"
    )
    endpoint = f"{BOT_API}/sendMessage"
    try:
        response = SESSION.post(
            endpoint,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return True
    except requests.RequestException as error:
        log.error("Не удалось отправить Telegram-уведомление: %s", error)
        return False


def send_keyword_notification(entry: dict[str, Any]) -> bool:
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
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
    endpoint = f"{BOT_API}/sendMessage"
    try:
        response = SESSION.post(
            endpoint,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": html_text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code == 400:
            # Telegram отклонил HTML-разметку — шлём обычный текст без ссылок.
            response = SESSION.post(
                endpoint,
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": f"{header}Текст: {plain_preview}{footer}",
                    "disable_web_page_preview": True,
                },
                timeout=REQUEST_TIMEOUT,
            )
        response.raise_for_status()
        return True
    except requests.RequestException as error:
        log.error("Не удалось отправить уведомление о ключевых словах: %s", error)
        return False


def send_service_notification(text: str) -> bool:
    """Сервисное сообщение в доверенный чат (вызывается из parser-потока)."""
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return False
    try:
        response = SESSION.post(
            f"{BOT_API}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "disable_web_page_preview": True,
            },
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return True
    except requests.RequestException as error:
        log.error("Не удалось отправить сервисное уведомление: %s", error)
        return False


# ----------------------------------------------------------------------------
# Бот: команды /start /help /wheels /channels /add /remove
# ----------------------------------------------------------------------------

BOT_COMMANDS = [
    {"command": "start", "description": "О боте"},
    {"command": "wheels", "description": f"Колёса за последние {WHEELS_WINDOW_MINUTES} мин"},
    {"command": "active", "description": "Живые колёса за сегодня (сброс в 00:00 МСК)"},
    {"command": "status", "description": "Статистика: всего / за сегодня / последняя"},
    {"command": "channels", "description": "Список каналов"},
    {"command": "add", "description": "Добавить канал: /add @channel"},
    {"command": "remove", "description": "Убрать канал: /remove @channel"},
    {"command": "words", "description": "Список ключевых слов"},
    {"command": "addword", "description": "Добавить слово: /addword колесо"},
    {"command": "removeword", "description": "Убрать слово: /removeword колесо"},
    {"command": "help", "description": "Справка"},
]


def _api_info_to_status(info: dict[str, Any]) -> str:
    is_ended = info.get("is_ended")
    if not isinstance(is_ended, bool):
        return "unknown"
    if is_ended:
        return "expired"
    # is_ended у API BetBoom запаздывает: флаг не переключается по таймеру,
    # и колесо может часами числиться «не завершённым» после окончания.
    # Поэтому конец розыгрыша считаем сами: start_dttm + duration_min.
    start_raw = info.get("start_dttm")
    duration = info.get("duration_min")
    if (
        isinstance(start_raw, str)
        and isinstance(duration, (int, float))
        and not isinstance(duration, bool)
        and duration > 0
    ):
        try:
            start = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
        except ValueError:
            start = None
        if start is not None and start.tzinfo is not None:
            now = datetime.now(timezone.utc)
            if now >= start + timedelta(minutes=float(duration)):
                return "expired"
            if now < start:
                return "soon"
            return "active"
    # Fallback: время посчитать не удалось — старое поведение по is_early.
    is_early = info.get("is_early")
    if not isinstance(is_early, bool):
        return "unknown"
    return "soon" if is_early else "active"


def _freestream_url(url: str) -> str:
    """Нормализует URL колеса для API, кэша и дедупликации."""
    cleaned = str(url).strip().rstrip(TRAILING_PUNCTUATION)
    parts = urlsplit(cleaned)
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    if netloc == "www.betboom.ru":
        netloc = "betboom.ru"
    return urlunsplit((scheme, netloc, parts.path.rstrip("/"), "", ""))


def _check_wheel_api(item: dict[str, Any], session: requests.Session) -> str:
    """Запрашивает статус одного колеса через BetBoom API без браузера.

    Возвращает 'active', 'soon', 'expired' или 'unknown' при любой ошибке.
    """
    url = _freestream_url(str(item.get("url", "")))
    if not url:
        return "unknown"
    try:
        response = session.post(
            STREAMER_WHEEL_INFO_API,
            json={"streamer_link": url},
            headers={
                **HEADERS,
                "Accept": "application/json",
                "X-Platform": "web",
                "Referer": url,
            },
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code != 200:
            log.debug("active-check API: HTTP %s для %s", response.status_code, url)
            return "unknown"
        payload = response.json()
        return _api_info_to_status(payload.get("info", {}))
    except Exception as error:
        log.debug("active-check API: ошибка для %s: %s", url, error)
        return "unknown"


def _get_active_api(
    items: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Проверяет список колёс через BetBoom API параллельно.

    Использует ThreadPoolExecutor с ACTIVE_CHECK_CONCURRENCY потоками.
    Кэширует expired-статусы в пределах текущих суток МСК.
    Возвращает кортеж (active_items, unknown_count):
      - active_items  — колёса со статусом active/soon (в исходном порядке);
      - unknown_count — количество колёс с неопределённым статусом.
    """
    from concurrent.futures import ThreadPoolExecutor

    today = datetime.now(MSK_TZ).strftime("%Y-%m-%d")
    # Чистим устаревшие записи кэша.
    with _expired_cache_lock:
        for stale_url in [
            u for u, d in list(_expired_cache.items()) if d != today
        ]:
            _expired_cache.pop(stale_url, None)

    session = build_session()
    results: list[tuple[int, str]] = []  # (original_index, status)
    lock = threading.Lock()

    def check(index: int, item: dict[str, Any]) -> None:
        url = _freestream_url(str(item.get("url", "")))
        if not url:
            with lock:
                results.append((index, "unknown"))
            return
        with _expired_cache_lock:
            cached_expired = _expired_cache.get(url) == today
        if cached_expired:
            log.info("active-check [cache]: %s → expired (кэш за сегодня)", url)
            with lock:
                results.append((index, "expired"))
            return
        status = _check_wheel_api(item, session)
        log.info("active-check [api]: %s → %s", url, status)
        if status == "expired":
            with _expired_cache_lock:
                _expired_cache[url] = today
        with lock:
            results.append((index, status))

    with ThreadPoolExecutor(max_workers=ACTIVE_CHECK_CONCURRENCY) as pool:
        list(pool.map(lambda args: check(*args), enumerate(items)))

    results.sort(key=lambda pair: pair[0])
    active_items = [
        items[i] for i, status in results if status in ("active", "soon")
    ]
    unknown_count = sum(1 for _, status in results if status == "unknown")
    return active_items, unknown_count


# Один /active за раз (non-blocking acquire).
_active_check_lock = threading.Lock()

# Кэш завершившихся колёс на текущие сутки МСК: url -> "YYYY-MM-DD".
# Завершившееся колесо не «оживает», поэтому повторные /active за день не
# перепроверяют его через API — к вечеру это главный источник ускорения.
# Кэш общий для parser-потока (precheck перед уведомлением) и фонового
# active-api-потока, поэтому доступ — только под _expired_cache_lock.
_expired_cache: dict[str, str] = {}
_expired_cache_lock = threading.Lock()


def precheck_wheel_status(url: str) -> str:
    """Статус колеса перед отправкой уведомления (вызывается из parser-потока).

    Возвращает 'active', 'soon', 'expired' или 'unknown'. При 'unknown'
    уведомление всё равно отправляется (fail-open): лучше лишний раз
    оповестить, чем пропустить живое колесо из-за сбоя API.
    Использует SESSION — она принадлежит parser-потоку.
    """
    canonical = _freestream_url(url)
    if not canonical:
        return "unknown"
    today = datetime.now(MSK_TZ).strftime("%Y-%m-%d")
    with _expired_cache_lock:
        if _expired_cache.get(canonical) == today:
            log.info("precheck [cache]: %s → expired (кэш за сегодня)", canonical)
            return "expired"
    status = _check_wheel_api({"url": canonical}, SESSION)
    log.info("precheck [api]: %s → %s", canonical, status)
    if status == "expired":
        with _expired_cache_lock:
            _expired_cache[canonical] = today
    return status

# Отдельная HTTP-сессия для отправки результатов /active из фонового потока.
# requests.Session не является потокобезопасной — BOT_SESSION принадлежит
# только боту-потоку и нельзя использовать его из фонового потока.
ACTIVE_CHECK_SESSION = build_session()


def _background_bot_send(chat_id: str, text: str) -> None:
    """Отправляет сообщение в Telegram из фонового потока (active-api).

    Использует ACTIVE_CHECK_SESSION, чтобы не делить BOT_SESSION между потоками.
    """
    try:
        ACTIVE_CHECK_SESSION.post(
            f"{BOT_API}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=REQUEST_TIMEOUT,
        ).raise_for_status()
    except requests.RequestException as error:
        log.warning("Бот: не удалось ответить в чат %s: %s", chat_id, error)


def _format_active_result(
    active_items: list[dict[str, Any]] | None,
    total: int,
    unknown_count: int = 0,
) -> str:
    """Форматирует ответ команды /active для отправки в Telegram.

    unknown_count > 0 означает, что часть колёс не удалось проверить
    (таймаут или ошибка API) — результат может быть неполным.
    """
    if active_items is None:
        return (
            f"{icon('warn')} Не удалось проверить колёса через API BetBoom. "
            "Это ошибка проверки, а не «активных нет» — "
            "подробности в parser.log."
        )
    # Все колёса вернули unknown — скорее всего сетевая ошибка.
    if not active_items and unknown_count > 0 and unknown_count == total:
        return (
            f"{icon('warn')} Не удалось определить статус {total} колёс "
            "(API не ответил).\n"
            "Попробуйте /active ещё раз через несколько секунд."
        )
    if not active_items:
        suffix = (
            f"\n⚠️ {unknown_count} колёс не удалось проверить (таймаут) — "
            "результат может быть неполным."
            if unknown_count
            else ""
        )
        return (
            f"{icon('warn')} Среди {total} колёс за сегодня "
            "активных не найдено.\n"
            f"Все розыгрыши уже завершились или ещё не начались.{suffix}"
        )
    suffix = (
        f"\n⚠️ {unknown_count} колёс не удалось проверить (таймаут) — "
        "список может быть неполным."
        if unknown_count
        else ""
    )
    lines = [
        f"{icon('link')} <b>Активные колёса ({len(active_items)} из "
        f"{total} за сегодня):</b>"
    ]
    for item in active_items:
        found_at = str(item.get("found_at", ""))
        found_time = found_at[11:16] if len(found_at) >= 16 else found_at
        channel = html.escape(str(item.get("channel", "")))
        url = html.escape(str(item.get("url", "")))
        lines.append(f"• {found_time} — @{channel}\n{url}")
    if suffix:
        lines.append(suffix)
    return "\n".join(lines)


def _fire_active_check(chat_id: str, unique_items: list[dict[str, Any]]) -> None:
    """Fire-and-forget: запускает API-проверку в daemon-потоке, немедленно возвращается.

    Поток бота не блокируется. Результат придёт отдельным сообщением через
    _background_bot_send после завершения проверки в фоновом потоке.
    Если проверка уже идёт — бот сообщает об этом и возвращается.
    """
    if not _active_check_lock.acquire(blocking=False):
        bot_send(chat_id, f"{icon('warn')} Проверка уже выполняется, подождите…")
        return

    total = len(unique_items)

    def _run_and_send() -> None:
        active_items: list[dict[str, Any]] | None = None
        unknown_count = 0
        try:
            active_items, unknown_count = _get_active_api(unique_items)
        except Exception as error:
            log.error("active-check: проверка колёс не удалась: %s", error)
        finally:
            _active_check_lock.release()

        text = _format_active_result(active_items, total, unknown_count)
        _background_bot_send(chat_id, text)

    threading.Thread(target=_run_and_send, daemon=True, name="active-api").start()


def check_channel_preview(channel: str) -> str:
    """Проверяет канал через t.me/s/<channel> перед добавлением в /add.

    Возвращает:
    - "ok" — канал существует и веб-превью отдаёт сообщения;
    - "not_found" — канал не существует или приватный (404);
    - "no_preview" — страница есть, но ленты сообщений нет: у канала
      отключено веб-превью (или он пуст) — парсер не сможет его читать;
    - "network_error" — проверить не удалось (сеть, 5xx и т.п.).

    Вызывается из потока бота — используем BOT_SESSION.
    """
    try:
        response = BOT_SESSION.get(
            f"https://t.me/s/{channel}", timeout=REQUEST_TIMEOUT
        )
    except requests.RequestException as error:
        log.warning("Бот: не удалось проверить канал @%s: %s", channel, error)
        return "network_error"
    if response.status_code == 404:
        return "not_found"
    if response.status_code != 200:
        return "network_error"
    if "tgme_widget_message_wrap" in response.text:
        return "ok"
    return "no_preview"


def help_text() -> str:
    with CHANNELS_LOCK:
        total = len(CHANNELS)
    with KEYWORDS_LOCK:
        total_words = len(KEYWORDS)
    return (
        "<b>Команды:</b>\n"
        f"/wheels — колёса за последние {WHEELS_WINDOW_MINUTES} минут\n"
        "/active — живые колёса за сегодня (сброс в 00:00 МСК)\n"
        "/status — статистика найденных ссылок\n"
        "/channels — список отслеживаемых каналов\n"
        "/add @channel — добавить канал\n"
        "/remove @channel — убрать канал\n"
        "/words — список ключевых слов\n"
        "/addword слово — добавить ключевое слово\n"
        "    (слово — по границам слова, *слово* — по подстроке)\n"
        "/removeword слово — убрать ключевое слово\n"
        "/help — эта справка\n\n"
        f"Каналов под мониторингом: {total}\n"
        f"Ключевых слов: {total_words}\n"
        f"Интервал проверки: {CHECK_INTERVAL} сек"
    )


def bot_send(chat_id: str, text: str) -> None:
    try:
        BOT_SESSION.post(
            f"{BOT_API}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=REQUEST_TIMEOUT,
        ).raise_for_status()
    except requests.RequestException as error:
        log.warning("Бот: не удалось ответить в чат %s: %s", chat_id, error)


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


def recent_wheels(minutes: int = WHEELS_WINDOW_MINUTES) -> list[dict[str, Any]]:
    now = datetime.now().astimezone()
    fresh: list[dict[str, Any]] = []
    for item in load_results():
        try:
            found = datetime.fromisoformat(str(item.get("found_at", "")))
        except ValueError:
            continue
        if found.tzinfo is None:
            found = found.astimezone()
        if now - found <= timedelta(minutes=minutes):
            fresh.append(item)
    fresh.sort(key=lambda item: str(item.get("found_at", "")), reverse=True)
    return fresh


def status_text() -> str:
    items = load_results()
    total = len(items)
    today = datetime.now().astimezone().date()
    today_count = 0
    last_item: dict[str, Any] | None = None
    last_found: datetime | None = None
    for item in items:
        try:
            found = datetime.fromisoformat(str(item.get("found_at", "")))
        except ValueError:
            continue
        if found.tzinfo is None:
            found = found.astimezone()
        if found.date() == today:
            today_count += 1
        if last_found is None or found > last_found:
            last_found = found
            last_item = item

    lines = [
        f"🎁 Найдено ссылок всего: {total}",
        f"📅 За сегодня: {today_count}",
    ]
    if last_item and last_found:
        channel = html.escape(str(last_item.get("channel", "?")))
        url = html.escape(str(last_item.get("url", "")))
        lines.append(f"🕑 Последняя ссылка: {last_found.strftime('%H:%M')} (@{channel})")
        if url:
            lines.append(url)
    else:
        lines.append("🕑 Последняя ссылка: пока нет")
    return "\n".join(lines)


def handle_command(chat_id: str, text: str) -> None:
    parts = text.strip().split(maxsplit=1)
    command = parts[0].split("@", 1)[0].lower()
    argument = parts[1].strip() if len(parts) > 1 else ""

    if command == "/start":
        with CHANNELS_LOCK:
            total = len(CHANNELS)
        bot_send(
            chat_id,
            f"{icon('start')} <b>WheelsParser</b>\n"
            "Я мониторю Telegram-каналы стримеров и присылаю ссылки на "
            "фрибет-колёса BetBoom сразу после публикации — ничего "
            "запрашивать не нужно.\n\n"
            "Что я умею:\n"
            f"{icon('link')} ловлю ссылки на колёса в {total} каналах\n"
            f"{icon('scan')} проверяю каналы каждые {CHECK_INTERVAL} сек\n\n"
            "Самое полезное:\n"
            f"/wheels — колёса за последние {WHEELS_WINDOW_MINUTES} минут\n"
            "/active — живые колёса за сегодня\n"
            "/status — статистика находок\n\n"
            "Полный список команд — /help",
        )
    elif command == "/help":
        bot_send(chat_id, help_text())
    elif command == "/status":
        bot_send(chat_id, status_text())
    elif command == "/wheels":
        wheels = recent_wheels()
        if not wheels:
            bot_send(
                chat_id,
                f"За последние {WHEELS_WINDOW_MINUTES} минут новых колёс не найдено. "
                "Как только появится ссылка — пришлю её сразу.",
            )
            return
        lines = [f"{icon('link')} <b>Колёса за последние {WHEELS_WINDOW_MINUTES} минут:</b>"]
        for item in wheels:
            found_at = str(item.get("found_at", ""))
            found_time = found_at[11:16] if len(found_at) >= 16 else found_at
            channel = html.escape(str(item.get("channel", "")))
            url = html.escape(str(item.get("url", "")))
            lines.append(f"• {found_time} — @{channel}\n{url}")
        bot_send(chat_id, "\n".join(lines))
    elif command == "/active":
        # Показываем только колёса текущих суток по Москве и не старше заданного
        # срока, чтобы зависшие записи не оставались в /active.
        now_msk = datetime.now(MSK_TZ)
        day_cutoff = now_msk.replace(hour=0, minute=0, second=0, microsecond=0)
        age_cutoff = now_msk - timedelta(hours=ACTIVE_MAX_AGE_HOURS)
        cutoff = max(day_cutoff, age_cutoff)
        fresh_items: list[dict[str, Any]] = []
        for item in load_results():
            try:
                found = datetime.fromisoformat(str(item.get("found_at", "")))
            except ValueError:
                continue
            if found.tzinfo is None:
                found = found.astimezone()
            if found >= cutoff:
                fresh_items.append(item)
        # Дедупликация по каноническому URL. В найденных сообщениях могут
        # отличаться query-параметры (utm и т.п.), но это всё равно одно колесо.
        seen_urls: set[str] = set()
        unique_items: list[dict[str, Any]] = []
        for item in reversed(fresh_items):  # сначала свежие
            url = _freestream_url(str(item.get("url", "")))
            if url and url not in seen_urls:
                seen_urls.add(url)
                unique_items.append(item)

        if not unique_items:
            bot_send(
                chat_id,
                f"За последние {ACTIVE_MAX_AGE_HOURS} часов колёс не найдено. "
                "Как только появится ссылка — пришлю её сразу.",
            )
            return

        # Fire-and-forget: поток бота не блокируется.
        # Результат придёт отдельным сообщением после проверки в фоновом потоке.
        bot_send(
            chat_id,
            f"{icon('bell')} Проверяю {len(unique_items)} колёс за сегодня…"
            " Результат пришлю отдельным сообщением.",
        )
        _fire_active_check(chat_id, unique_items)
    elif command == "/channels":
        with CHANNELS_LOCK:
            total = len(CHANNELS)
            listing = "\n".join(f"• @{html.escape(channel)}" for channel in CHANNELS)
        bot_send(chat_id, f"<b>Каналы ({total}):</b>\n{listing}")
    elif command == "/words":
        with KEYWORDS_LOCK:
            total = len(KEYWORDS)
            listing = "\n".join(f"• {html.escape(keyword)}" for keyword in KEYWORDS)
        if not total:
            bot_send(chat_id, "Ключевых слов пока нет. Добавьте: /addword колесо")
        else:
            bot_send(chat_id, f"<b>Ключевые слова ({total}):</b>\n{listing}")
    elif command in ("/addword", "/removeword"):
        keyword = argument.strip()
        if not keyword or len(keyword) > 64:
            bot_send(chat_id, f"Укажите слово: <code>{command} колесо</code>")
            return
        if "*" in keyword and not (
            keyword.startswith("*") and keyword.endswith("*") and len(keyword) > 2
        ):
            bot_send(
                chat_id,
                "Звёздочки — только с обеих сторон: <code>*колесо*</code> "
                "(поиск по подстроке). Без звёздочек — поиск по границам слова.",
            )
            return
        with KEYWORDS_LOCK:
            existing = next(
                (k for k in KEYWORDS if k.casefold() == keyword.casefold()), None
            )
            if command == "/addword":
                if existing is not None:
                    bot_send(chat_id, f"«{html.escape(existing)}» уже в списке.")
                    return
                KEYWORDS.append(keyword)
                save_keywords_file()
                total = len(KEYWORDS)
                bot_send(
                    chat_id,
                    f"{icon('ok')} «{html.escape(keyword)}» добавлено. Слов: {total}",
                )
                log.info("Бот: слово %r добавлено, всего %s", keyword, total)
            else:
                if existing is None:
                    bot_send(chat_id, f"«{html.escape(keyword)}» нет в списке.")
                    return
                KEYWORDS.remove(existing)
                save_keywords_file()
                total = len(KEYWORDS)
                bot_send(
                    chat_id,
                    f"{icon('stop')} «{html.escape(existing)}» удалено. Слов: {total}",
                )
                log.info("Бот: слово %r удалено, всего %s", keyword, total)
    elif command in ("/add", "/remove"):
        match = USERNAME_RE.match(argument)
        if not match:
            bot_send(chat_id, f"Укажите канал: <code>{command} @channel</code>")
            return
        channel = match.group(1)
        if command == "/add":
            with CHANNELS_LOCK:
                already = channel in CHANNELS
            if already:
                bot_send(chat_id, f"@{html.escape(channel)} уже в списке.")
                return
            # Валидация до добавления. Сетевой запрос выполняем БЕЗ
            # CHANNELS_LOCK, чтобы не блокировать основной цикл парсинга.
            status = check_channel_preview(channel)
            if status == "not_found":
                bot_send(
                    chat_id,
                    f"{icon('warn')} @{html.escape(channel)} не найден: "
                    "канал не существует или приватный. Не добавлен.",
                )
                return
            if status == "no_preview":
                bot_send(
                    chat_id,
                    f"{icon('warn')} У @{html.escape(channel)} недоступна лента "
                    "t.me/s (веб-превью отключено или канал пуст) — парсер не "
                    "сможет читать его сообщения. Не добавлен.",
                )
                return
            note = (
                ""
                if status == "ok"
                else (
                    f"\n{icon('warn')} Проверить канал не удалось "
                    "(сетевая ошибка) — добавлен без проверки."
                )
            )
            with CHANNELS_LOCK:
                if channel in CHANNELS:
                    bot_send(chat_id, f"@{html.escape(channel)} уже в списке.")
                    return
                CHANNELS.append(channel)
                save_channels_file()
                total = len(CHANNELS)
            bot_send(
                chat_id,
                f"{icon('ok')} @{html.escape(channel)} добавлен. "
                f"Каналов: {total}{note}",
            )
            log.info("Бот: канал @%s добавлен, всего %s", channel, total)
        else:
            with CHANNELS_LOCK:
                if channel not in CHANNELS:
                    bot_send(chat_id, f"@{html.escape(channel)} нет в списке.")
                    return
                CHANNELS.remove(channel)
                save_channels_file()
                total = len(CHANNELS)
            bot_send(
                chat_id,
                f"{icon('stop')} @{html.escape(channel)} удалён. Каналов: {total}",
            )
            log.info("Бот: канал @%s удалён, всего %s", channel, total)


def bot_loop() -> None:
    try:
        BOT_SESSION.post(
            f"{BOT_API}/setMyCommands",
            json={"commands": BOT_COMMANDS},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as error:
        log.warning("Бот: не удалось зарегистрировать меню команд: %s", error)

    offset = 0
    while not STOP_EVENT.is_set():
        try:
            response = BOT_SESSION.get(
                f"{BOT_API}/getUpdates",
                params={"timeout": 25, "offset": offset},
                timeout=REQUEST_TIMEOUT + 30,
            )
            response.raise_for_status()
            updates = response.json().get("result", [])
        except (requests.RequestException, ValueError) as error:
            log.warning("Бот: ошибка получения обновлений: %s", error)
            STOP_EVENT.wait(5)
            continue
        stale_count = 0
        for update in updates:
            offset = max(offset, int(update.get("update_id", 0)) + 1)
            message = update.get("message") or {}
            text = str(message.get("text") or "")
            chat_id = str((message.get("chat") or {}).get("id", ""))
            if not chat_id or not text.startswith("/"):
                continue
            if not TELEGRAM_CHAT_ID or chat_id != TELEGRAM_CHAT_ID:
                # Команды принимаем только из доверенного чата. Пустой
                # TELEGRAM_CHAT_ID означал бы «командовать может кто угодно»,
                # поэтому без него команды полностью отключены.
                continue
            # Устаревшие команды (например, отправленные, пока парсер лежал)
            # подтверждаем сдвигом offset, но не выполняем: отвечать на
            # команды суточной давности бессмысленно и создаёт спам в чате.
            message_date = int(message.get("date") or 0)
            if message_date and time.time() - message_date > STALE_COMMAND_SECONDS:
                stale_count += 1
                continue
            try:
                handle_command(chat_id, text)
            except Exception:
                log.exception("Бот: ошибка обработки команды %r", text)
        if stale_count:
            log.info(
                "%s Бот: пропущено устаревших команд из бэклога: %s",
                icon("bell"),
                stale_count,
            )


# Мониторинг «мёртвых» каналов: счётчики подряд неудачных циклов per-канал.
# Доступ только из parser-потока — блокировка не нужна.
CHANNEL_FAIL_STREAK: dict[str, int] = {}
CHANNEL_FAIL_ALERTED: set[str] = set()


def _update_channel_fail_streaks(
    checked_channels: list[str], failed_channels: list[str]
) -> None:
    """Обновляет счётчики недоступности и один раз уведомляет о «мёртвом» канале."""
    failed = set(failed_channels)
    for channel in checked_channels:
        if channel in failed:
            CHANNEL_FAIL_STREAK[channel] = CHANNEL_FAIL_STREAK.get(channel, 0) + 1
            if (
                CHANNEL_FAIL_STREAK[channel] >= CHANNEL_FAIL_THRESHOLD
                and channel not in CHANNEL_FAIL_ALERTED
            ):
                CHANNEL_FAIL_ALERTED.add(channel)
                log.warning(
                    "%s Канал @%s недоступен %s циклов подряд — отправляю уведомление",
                    icon("warn"),
                    channel,
                    CHANNEL_FAIL_STREAK[channel],
                )
                send_service_notification(
                    f"{icon('warn')} Канал @{channel} недоступен "
                    f"{CHANNEL_FAIL_STREAK[channel]} циклов подряд.\n"
                    "Возможно, он удалён, стал приватным или отключил веб-превью.\n"
                    f"Убрать из списка: /remove {channel}"
                )
        else:
            # Канал снова доступен — сбрасываем счётчик и разрешаем
            # повторное уведомление при следующей серии неудач.
            CHANNEL_FAIL_STREAK.pop(channel, None)
            CHANNEL_FAIL_ALERTED.discard(channel)
    # Чистим счётчики каналов, удалённых через /remove.
    with CHANNELS_LOCK:
        current = set(CHANNELS)
    for channel in list(CHANNEL_FAIL_STREAK):
        if channel not in current:
            CHANNEL_FAIL_STREAK.pop(channel, None)
            CHANNEL_FAIL_ALERTED.discard(channel)


def process_cycle(
    seen: dict[str, dict[str, str]],
    results: list[dict[str, Any]],
    baseline: bool = False,
) -> int:
    cycle_started = time.monotonic()
    with CHANNELS_LOCK:
        total_channels = len(CHANNELS)
    log.info("%s Начинаю проверку · каналов %s", icon("scan"), total_channels)
    now = datetime.now().astimezone()
    # URL -> время последней находки. Раньше дедупликация была глобальной
    # («один URL — одно уведомление за всю историю»), из-за чего повторный
    # запуск колеса на том же адресе молча игнорировался. Теперь повтор
    # подавляется только в течение REALERT_COOLDOWN_MINUTES.
    last_found: dict[str, datetime] = {}
    for item in results:
        item_url = str(item.get("url", ""))
        if not item_url:
            continue
        try:
            found = datetime.fromisoformat(str(item.get("found_at", "")))
        except ValueError:
            continue
        if found.tzinfo is None:
            found = found.astimezone()
        if item_url not in last_found or found > last_found[item_url]:
            last_found[item_url] = found
    new_entries: list[dict[str, Any]] = []
    failed_channels: list[str] = []
    checked_channels: list[str] = []

    with CHANNELS_LOCK:
        channels = list(CHANNELS)

    for index, channel in enumerate(channels):
        if STOP_EVENT.is_set():
            break
        messages = fetch_channel(channel)
        checked_channels.append(channel)
        if messages is None:
            failed_channels.append(channel)
            messages = []
        channel_seen = seen.setdefault(channel, {})
        # Канал, добавленный через /add на лету, сначала проходит «тихий» цикл,
        # чтобы не рассылать уведомления по его старым сообщениям.
        channel_baseline = baseline or (not channel_seen and not ALERT_ON_FIRST_RUN)
        for message in messages:
            previous_hash = channel_seen.get(message["id"])
            is_new_message = previous_hash is None
            # Правка поста: хэш содержимого изменился. Пустой сохранённый хэш
            # означает «содержимое неизвестно» (миграция со старого формата
            # seen_ids.json) — правкой не считаем, просто запоминаем хэш.
            is_edited_message = (
                not is_new_message
                and bool(previous_hash)
                and previous_hash != message["hash"]
            )
            channel_seen[message["id"]] = message["hash"]
            if channel_baseline or not (is_new_message or is_edited_message):
                continue
            for url in message["urls"]:
                previous = last_found.get(url)
                if previous and now - previous <= timedelta(
                    minutes=REALERT_COOLDOWN_MINUTES
                ):
                    continue  # недавно уже оповещали об этом колесе
                # Проверяем колесо через API BetBoom до отправки: «хвосты» —
                # старые href на прошлые (уже завершившиеся) колёса — молча
                # пропускаем. Статусы 'active'/'soon'/'unknown' рассылаются,
                # unknown — fail-open, чтобы не терять живые колёса при сбое API.
                status = precheck_wheel_status(url) if PRECHECK_WHEELS else ""
                if status == "expired":
                    log.info(
                        "%s Пропускаю %s [@%s]: колесо уже завершилось (API BetBoom)",
                        icon("warn"),
                        url,
                        channel,
                    )
                    last_found[url] = now
                    continue
                entry = {
                    "url": url,
                    "found_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "channel": channel,
                    "msg_id": message["id"],
                    "message_url": message["message_url"],
                    "preview": message["text"][:200],
                    "edited": is_edited_message,
                    "status": status,
                    "notified": False,
                }
                entry["notified"] = send_telegram_notification(entry)
                results.append(entry)
                new_entries.append(entry)
                last_found[url] = now
                log.info(
                    "%s %s [@%s]: %s",
                    icon("link"),
                    "Ссылка из правки поста" if is_edited_message else "Новая ссылка",
                    channel,
                    url,
                    extra={"highlight": True},
                )
            # Поиск по ключевым словам — только для новых сообщений без ссылок:
            # ссылки не дублируют уведомление о найденном колесе, а правки
            # постов проверяем лишь на ссылки — иначе каждая мелкая правка
            # текста с ключевым словом слала бы повторное уведомление.
            if is_new_message and not message["urls"]:
                matched = find_keywords(message["text"])
                if matched:
                    entry = {
                        "found_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                        "channel": channel,
                        "msg_id": message["id"],
                        "message_url": message["message_url"],
                        "preview": message["text"][:200],
                        "preview_html": message.get("preview_html", ""),
                        "keywords": matched,
                    }
                    send_keyword_notification(entry)
                    log.info(
                        "%s Ключевые слова (%s) [@%s]: %s",
                        icon("bell"),
                        ", ".join(matched),
                        channel,
                        entry["message_url"],
                        extra={"highlight": True},
                    )
        if index < len(channels) - 1:
            STOP_EVENT.wait(1.5 + random.uniform(0.0, 1.0))

    _update_channel_fail_streaks(checked_channels, failed_channels)
    save_seen(seen)
    if new_entries:
        if len(results) > MAX_RESULTS:
            # Обрезаем на месте (del, а не переприсваивание): список results
            # общий между циклами, терять ссылку на него нельзя.
            del results[: len(results) - MAX_RESULTS]
        atomic_write_json(OUTPUT_FILE, results)
    status_icon = icon("warn") if failed_channels else icon("ok")
    # Следующий запуск отсчитывается от НАЧАЛА цикла (см. parse_loop).
    elapsed = time.monotonic() - cycle_started
    next_at = (
        datetime.now().astimezone()
        + timedelta(seconds=max(5.0, CHECK_INTERVAL - elapsed))
    ).strftime("%H:%M:%S")
    suffix = "" if STOP_EVENT.is_set() else f" · следующая проверка в {next_at}"
    log.info(
        "%s Цикл завершён · каналы %s/%s · новых ссылок: %s%s",
        status_icon,
        len(channels) - len(failed_channels),
        len(channels),
        len(new_entries),
        suffix,
    )
    if failed_channels:
        log.warning("%s Недоступные каналы: %s", icon("warn"), ", ".join(failed_channels))
    return len(new_entries)


def acquire_single_instance_lock() -> Any | None:
    """Не даёт запустить второй экземпляр парсера.

    Два процесса с одним токеном конфликтуют в getUpdates (409 Conflict),
    поэтому при старте берём эксклюзивную блокировку lock-файла.
    ОС снимает блокировку автоматически при любом завершении процесса,
    так что «зависших» lock-файлов после падения не остаётся.
    """
    lock_handle = open(LOCK_FILE, "a+", encoding="utf-8")
    try:
        if os.name == "nt":
            import msvcrt

            lock_handle.seek(0)
            msvcrt.locking(lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_handle.close()
        return None
    lock_handle.seek(0)
    lock_handle.truncate()
    lock_handle.write(str(os.getpid()))
    lock_handle.flush()
    return lock_handle


def main() -> int:
    # Держим lock_handle до конца работы процесса.
    lock_handle = acquire_single_instance_lock()
    if lock_handle is None:
        log.error(
            "%s Уже запущен другой экземпляр WheelsParser (lock: %s) — выход",
            icon("stop"),
            LOCK_FILE.name,
        )
        return 1

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)

    # channels.txt — единственный источник правды: при первом запуске
    # фиксируем в нём стартовый список (из env или дефолтный).
    if _SEED_CHANNELS_FILE:
        save_channels_file()
        log.info(
            "%s Создан channels.txt (каналов: %s) — теперь это единственный источник правды",
            icon("ok"),
            len(CHANNELS),
        )
    if _SEED_KEYWORDS_FILE:
        save_keywords_file()
        log.info(
            "%s Создан keywords.txt (ключевых слов: %s) — управляйте словами через файл или /addword и /removeword",
            icon("ok"),
            len(KEYWORDS),
        )
    for note_level, note in CHANNEL_LOAD_NOTES:
        log.log(note_level, "%s %s", icon("warn") if note_level >= logging.WARNING else icon("bell"), note)

    seen, has_state = load_seen()
    results = load_results()
    if not OUTPUT_FILE.exists():
        atomic_write_json(OUTPUT_FILE, results)

    notifications = "включены" if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID else "выключены"
    log.info(
        "%s WheelsParser запущен · каналов %s · ключевых слов %s · интервал %ss",
        icon("start"),
        len(CHANNELS),
        len(KEYWORDS),
        CHECK_INTERVAL,
    )
    log.info("%s Telegram-уведомления: %s", icon("bell"), notifications)

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        threading.Thread(target=bot_loop, name="bot", daemon=True).start()
        command_list = " ".join(f"/{item['command']}" for item in BOT_COMMANDS)
        log.info("%s Команды бота активны: %s", icon("bot"), command_list)
    elif TELEGRAM_BOT_TOKEN:
        log.warning(
            "%s TELEGRAM_CHAT_ID не задан — команды бота отключены: "
            "иначе управлять парсером мог бы любой пользователь Telegram",
            icon("warn"),
        )

    baseline = not has_state and not ALERT_ON_FIRST_RUN
    if baseline:
        log.info("Первый запуск: создаю базовое состояние без старых уведомлений")

    # Весь сетевой ввод-вывод — в отдельном daemon-потоке. Обработчик Ctrl+C
    # выполняется только в главном потоке и не может прервать блокирующий
    # сетевой вызов (особенно на Windows), поэтому главный поток должен
    # только ждать STOP_EVENT — тогда сигнал обрабатывается мгновенно.
    def parse_loop() -> None:
        # Интервал отсчитывается от НАЧАЛА цикла: иначе реальный период
        # равен «длительность цикла + CHECK_INTERVAL» и расписание дрейфует.
        cycle_started = time.monotonic()
        process_cycle(seen, results, baseline=baseline)
        while not STOP_EVENT.is_set():
            elapsed = time.monotonic() - cycle_started
            if STOP_EVENT.wait(max(5.0, CHECK_INTERVAL - elapsed)):
                break
            cycle_started = time.monotonic()
            process_cycle(seen, results)

    parser_thread = threading.Thread(target=parse_loop, name="parser", daemon=True)
    parser_thread.start()

    while not STOP_EVENT.is_set():
        STOP_EVENT.wait(1)

    # Даём циклу шанс корректно дописать файлы, но не ждём вечно.
    parser_thread.join(timeout=REQUEST_TIMEOUT + 5)
    if parser_thread.is_alive():
        log.warning(
            "%s Цикл не успел завершиться за отведённое время — выхожу; "
            "состояние сохранено после предыдущего цикла",
            icon("warn"),
        )
    else:
        save_seen(seen)
    log.info("%s WheelsParser остановлен", icon("stop"))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        log.exception("Критическая ошибка")
        sys.exit(1)
