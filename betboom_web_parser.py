#!/usr/bin/env python3
"""WheelsParser: monitor public Telegram channels for BetBoom freestream links."""

from __future__ import annotations

import asyncio
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

try:
    from playwright.async_api import async_playwright
    from playwright.async_api import TimeoutError as PlaywrightTimeout

    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

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
# /active смотрит только на колёса, найденные сегодня по МСК: счётчик «N из M»
# сбрасывается каждый день в 00:00 по Москве (UTC+3, без летнего времени).
MSK_TZ = timezone(timedelta(hours=3), "MSK")
# Сколько вкладок Playwright проверяют колёса одновременно при /active.
ACTIVE_CHECK_CONCURRENCY = env_int("ACTIVE_CHECK_CONCURRENCY", 4, 1)
# Повторное уведомление о том же URL разрешено после этого кулдауна (мин).
# Колёса BetBoom живут на постоянных адресах (/staya, /neret, ...), поэтому
# «вечная» дедупликация по URL пропускала повторные запуски того же колеса.
REALERT_COOLDOWN_MINUTES = env_int("REALERT_COOLDOWN_MINUTES", 30, 1)
# Команды старше этого возраста (сек) подтверждаются, но не выполняются —
# защита от бэклога getUpdates, накопившегося за время простоя парсера.
STALE_COMMAND_SECONDS = env_int("STALE_COMMAND_SECONDS", 120, 10)
ALERT_ON_FIRST_RUN = env_bool("ALERT_ON_FIRST_RUN", False)
USE_COLORS = env_bool("USE_COLORS", True)
USE_ICONS = env_bool("USE_ICONS", True)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
BOT_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
USERNAME_RE = re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{3,31})$")

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


def load_seen() -> tuple[dict[str, set[str]], bool]:
    existed = SEEN_FILE.exists()
    raw = read_json(SEEN_FILE, {})
    if not isinstance(raw, dict):
        raw = {}
    seen = {
        channel: set(value if isinstance(value, list) else [])
        for channel, value in raw.items()
    }
    with CHANNELS_LOCK:
        for channel in CHANNELS:
            seen.setdefault(channel, set())
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


def save_seen(seen: dict[str, set[str]]) -> None:
    with CHANNELS_LOCK:
        active = set(CHANNELS)
    # Каналы, удалённые через /remove, в файл не пишем — иначе seen_ids.json
    # копит их ID вечно. Из памяти (seen) не удаляем: если канал вернут через
    # /add до рестарта, старые сообщения не вызовут ложных уведомлений.
    serializable = {
        channel: sorted(ids, key=message_id_sort_key)[-MAX_SEEN_PER_CHANNEL:]
        for channel, ids in seen.items()
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


def find_keywords(text: str) -> list[str]:
    """Ключевые слова, найденные в тексте сообщения.

    Поиск регистронезависимый (casefold): «Колесо», «КОЛЕСО» и «колесо»
    совпадают. Ищется вхождение подстроки, поэтому «колесо» найдёт и
    «колесом», «колесо!», «суперколесо».
    """
    if not text:
        return []
    lowered = text.casefold()
    with KEYWORDS_LOCK:
        keywords = list(KEYWORDS)
    return [keyword for keyword in keywords if keyword.casefold() in lowered]


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
        results.append({
            "id": message_id,
            "text": text,
            "urls": find_urls(message, text),
            "message_url": f"https://t.me/{message_id}",
        })
    return results


def send_telegram_notification(entry: dict[str, Any]) -> bool:
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return False
    text = (
        f"{icon('start')} Новая ссылка WheelsParser\n"
        f"Канал: @{entry['channel']}\n"
        f"Найдено: {entry['found_at']}\n"
        f"Ссылка: {entry['url']}\n"
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
    text = (
        f"{icon('bell')} Ключевые слова: {', '.join(entry['keywords'])}\n"
        f"Канал: @{entry['channel']}\n"
        f"Найдено: {entry['found_at']}\n"
        f"Текст: {entry['preview']}\n"
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
        log.error("Не удалось отправить уведомление о ключевых словах: %s", error)
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


# Маркеры состояния колеса — ищем их в отрендеренном HTML (Playwright)
_ACTIVE_MARKERS = ("До розыгрыша",)
_EXPIRED_MARKERS = ("Пока ждёшь следующий запуск", "Пока ждешь следующий запуск")
_SOON_MARKERS = ("Акция скоро начнётся", "Акция скоро начнется")
_ALL_MARKERS = _ACTIVE_MARKERS + _EXPIRED_MARKERS + _SOON_MARKERS

# JS-предикат для wait_for_function — ждём появления любого из маркеров
_WAIT_JS = """
() => {
    const t = document.body ? document.body.innerText : "";
    return t.includes("До розыгрыша") ||
           t.includes("Пока ждёшь") ||
           t.includes("Пока ждешь") ||
           t.includes("Акция скоро начнётся") ||
           t.includes("Акция скоро начнется");
}
"""


def _text_to_status(text: str) -> str:
    if any(m in text for m in _ACTIVE_MARKERS):
        return "active"
    if any(m in text for m in _EXPIRED_MARKERS):
        return "expired"
    if any(m in text for m in _SOON_MARKERS):
        return "soon"
    return "unknown"


def get_active_wheels(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Проверяет список колёс и возвращает только активные.
    Если Playwright установлен — открывает один headless-браузер на весь список.
    """
    if HAS_PLAYWRIGHT:
        return _get_active_playwright(items)
    return _get_active_requests(items)


_BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}


def _get_active_playwright(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Синхронная обёртка: запускает параллельную проверку колёс."""
    try:
        return asyncio.run(_get_active_playwright_async(items))
    except Exception as error:
        log.error("Playwright: проверка колёс не удалась: %s", error)
        return []


async def _get_active_playwright_async(
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Проверяет колёса параллельно (ACTIVE_CHECK_CONCURRENCY вкладок).

    Раньше колёса проверялись по одному (до 50 сек на URL) — на десяток ссылок
    уходили минуты. Теперь вкладки работают параллельно, а картинки/видео/шрифты
    блокируются — текстовым маркерам они не нужны, а грузятся дольше всего.
    """
    found: list[tuple[int, dict[str, Any]]] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=HEADERS["User-Agent"],
            extra_http_headers={"Accept-Language": HEADERS["Accept-Language"]},
            locale="ru-RU",
        )

        async def block_heavy(route: Any) -> None:
            if route.request.resource_type in _BLOCKED_RESOURCE_TYPES:
                await route.abort()
            else:
                await route.continue_()

        await context.route("**/*", block_heavy)

        semaphore = asyncio.Semaphore(ACTIVE_CHECK_CONCURRENCY)

        async def check(index: int, item: dict[str, Any]) -> None:
            url = str(item.get("url", ""))
            if not url:
                return
            async with semaphore:
                page = await context.new_page()
                try:
                    await page.goto(url, timeout=20_000, wait_until="domcontentloaded")
                    try:
                        await page.wait_for_function(_WAIT_JS, timeout=10_000)
                    except PlaywrightTimeout:
                        log.debug("Playwright: таймаут ожидания маркера на %s", url)
                    text = await page.inner_text("body")
                    status = _text_to_status(text)
                    log.debug("active-check [playwright]: %s → %s", url, status)
                    if status in ("active", "soon"):
                        found.append((index, item))
                except Exception as error:
                    log.debug("Playwright: ошибка %s: %s", url, error)
                finally:
                    await page.close()

        await asyncio.gather(*(check(i, item) for i, item in enumerate(items)))
        await context.close()
        await browser.close()
    found.sort(key=lambda pair: pair[0])
    return [item for _, item in found]


def _get_active_requests(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Fallback через requests — работает только если сайт делает SSR.
    Для BetBoom (чистый CSR) практически не работает, но оставляем как запасной вариант.
    """
    active: list[dict[str, Any]] = []
    for item in items:
        url = str(item.get("url", ""))
        if not url:
            continue
        try:
            # Вызывается из потока бота (/active) — используем BOT_SESSION,
            # чтобы не делить requests.Session между потоками.
            response = BOT_SESSION.get(url, timeout=REQUEST_TIMEOUT)
            if response.status_code == 200:
                status = _text_to_status(response.text)
                log.debug("active-check [requests]: %s → %s", url, status)
                if status in ("active", "soon"):
                    active.append(item)
        except requests.RequestException as error:
            log.debug("active-check requests ошибка %s: %s", url, error)
        time.sleep(0.5)
    return active


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
        lines = ["# Одно ключевое слово (или фраза) на строку. Регистр не важен."]
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
        bot_send(
            chat_id,
            f"{icon('start')} <b>WheelsParser</b>\n"
            "Я слежу за Telegram-каналами стримеров и присылаю новые ссылки "
            "сразу после публикации.\n\n"
            + help_text(),
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
        # Сброс каждый день в 00:00 по МСК: показываем и проверяем только те
        # колёса, что найдены с начала текущих суток по Москве.
        cutoff = datetime.now(MSK_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
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
        # Дедупликация по URL
        seen_urls: set[str] = set()
        unique_items: list[dict[str, Any]] = []
        for item in reversed(fresh_items):  # сначала свежие
            url = str(item.get("url", ""))
            if url and url not in seen_urls:
                seen_urls.add(url)
                unique_items.append(item)

        if not unique_items:
            bot_send(
                chat_id,
                "Сегодня (с 00:00 МСК) колёс ещё не найдено. "
                "Как только появится ссылка — пришлю её сразу.",
            )
            return

        pw_note = "" if HAS_PLAYWRIGHT else " (⚠️ Playwright не установлен, результат может быть неточным)"
        bot_send(
            chat_id,
            f"{icon('bell')} Проверяю все сохранённые колёса за сегодня…"
            f"{pw_note} Немного подождите",
        )

        active_items = get_active_wheels(unique_items)

        if not active_items:
            bot_send(
                chat_id,
                f"{icon('warn')} Среди {len(unique_items)} колёс за сегодня "
                "активных не найдено.\n"
                "Все розыгрыши уже завершились или ещё не начались.",
            )
            return

        lines = [
            f"{icon('link')} <b>Активные колёса ({len(active_items)} из "
            f"{len(unique_items)} за сегодня):</b>"
        ]
        for item in active_items:
            found_at = str(item.get("found_at", ""))
            found_time = found_at[11:16] if len(found_at) >= 16 else found_at
            channel = html.escape(str(item.get("channel", "")))
            url = html.escape(str(item.get("url", "")))
            lines.append(f"• {found_time} — @{channel}\n{url}")
        bot_send(chat_id, "\n".join(lines))
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
        with CHANNELS_LOCK:
            if command == "/add":
                if channel in CHANNELS:
                    bot_send(chat_id, f"@{html.escape(channel)} уже в списке.")
                    return
                CHANNELS.append(channel)
                save_channels_file()
                total = len(CHANNELS)
                bot_send(
                    chat_id,
                    f"{icon('ok')} @{html.escape(channel)} добавлен. Каналов: {total}",
                )
                log.info("Бот: канал @%s добавлен, всего %s", channel, total)
            else:
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
            if TELEGRAM_CHAT_ID and chat_id != TELEGRAM_CHAT_ID:
                continue  # игнорируем чужие чаты
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


def process_cycle(
    seen: dict[str, set[str]], results: list[dict[str, Any]], baseline: bool = False
) -> int:
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

    with CHANNELS_LOCK:
        channels = list(CHANNELS)

    for index, channel in enumerate(channels):
        if STOP_EVENT.is_set():
            break
        messages = fetch_channel(channel)
        if messages is None:
            failed_channels.append(channel)
            messages = []
        channel_seen = seen.setdefault(channel, set())
        # Канал, добавленный через /add на лету, сначала проходит «тихий» цикл,
        # чтобы не рассылать уведомления по его старым сообщениям.
        channel_baseline = baseline or (not channel_seen and not ALERT_ON_FIRST_RUN)
        for message in messages:
            is_new_message = message["id"] not in channel_seen
            channel_seen.add(message["id"])
            if channel_baseline or not is_new_message:
                continue
            for url in message["urls"]:
                previous = last_found.get(url)
                if previous and now - previous <= timedelta(
                    minutes=REALERT_COOLDOWN_MINUTES
                ):
                    continue  # недавно уже оповещали об этом колесе
                entry = {
                    "url": url,
                    "found_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "channel": channel,
                    "msg_id": message["id"],
                    "message_url": message["message_url"],
                    "preview": message["text"][:200],
                    "notified": False,
                }
                entry["notified"] = send_telegram_notification(entry)
                results.append(entry)
                new_entries.append(entry)
                last_found[url] = now
                log.info(
                    "%s Новая ссылка [@%s]: %s",
                    icon("link"),
                    channel,
                    url,
                    extra={"highlight": True},
                )
            # Поиск по ключевым словам — только для сообщений без ссылок,
            # чтобы не дублировать уведомление о найденном колесе.
            if not message["urls"]:
                matched = find_keywords(message["text"])
                if matched:
                    entry = {
                        "found_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                        "channel": channel,
                        "msg_id": message["id"],
                        "message_url": message["message_url"],
                        "preview": message["text"][:200],
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

    save_seen(seen)
    if new_entries:
        if len(results) > MAX_RESULTS:
            # Обрезаем на месте (del, а не переприсваивание): список results
            # общий между циклами, терять ссылку на него нельзя.
            del results[: len(results) - MAX_RESULTS]
        atomic_write_json(OUTPUT_FILE, results)
    status_icon = icon("warn") if failed_channels else icon("ok")
    next_at = (
        datetime.now().astimezone() + timedelta(seconds=CHECK_INTERVAL)
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
    поэтому при старте берём эксклюзивну�� блокировку lock-файла.
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

    if TELEGRAM_BOT_TOKEN:
        threading.Thread(target=bot_loop, name="bot", daemon=True).start()
        command_list = " ".join(f"/{item['command']}" for item in BOT_COMMANDS)
        log.info("%s Команды бота активны: %s", icon("bot"), command_list)

    baseline = not has_state and not ALERT_ON_FIRST_RUN
    if baseline:
        log.info("Первый запуск: создаю базовое состояние без старых уведомлений")

    # Весь сетевой ввод-вывод — в отдельном daemon-потоке. Обработчик Ctrl+C
    # выполняется только в главном потоке и не может прервать блокирующий
    # сетевой вызов (особенно на Windows), поэтому главный поток должен
    # только ждать STOP_EVENT — тогда сигнал обрабатывается мгновенно.
    def parse_loop() -> None:
        process_cycle(seen, results, baseline=baseline)
        while not STOP_EVENT.is_set():
            if STOP_EVENT.wait(CHECK_INTERVAL):
                break
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
