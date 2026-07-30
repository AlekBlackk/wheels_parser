"""Настройки WheelsParser: пути, переменные окружения, константы, значки.

Модуль не зависит ни от чего внутри пакета и читается первым: всё
остальное берёт настройки отсюда. Значения фиксируются один раз при
импорте — .env перечитывается только при перезапуске парсера.
"""

from __future__ import annotations

import os
import re
from datetime import timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

# Файлы состояния лежат в корне репозитория, на уровень выше каталога
# пакета. Так пути не изменились после разбиения на модули и существующие
# channels.txt/freebets.json продолжают работать.
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

CHANNELS_FILE = BASE_DIR / "channels.txt"
KEYWORDS_FILE = BASE_DIR / "keywords.txt"
TWITCH_CHANNELS_FILE = BASE_DIR / "twitch_channels.txt"
OUTPUT_FILE = BASE_DIR / "freebets.json"
SEEN_FILE = BASE_DIR / "seen_ids.json"
BOT_STATE_FILE = BASE_DIR / "bot_state.json"
REMOVED_WHEELS_FILE = BASE_DIR / "removed_wheels.json"
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

# --- Twitch ---
# Мониторинг Twitch-чатов: анонимное IRC-подключение, токены и OAuth не нужны.
# Реагируем ТОЛЬКО на ссылки betboom.ru/freestream от стримера, модераторов,
# VIP и известных ботов; ключевые слова в Twitch-чатах не ищутся.
TWITCH_ENABLED = env_bool("TWITCH_ENABLED", True)
TWITCH_IRC_HOST = "irc.chat.twitch.tv"
TWITCH_IRC_PORT = 6697
TWITCH_USERNAME_RE = re.compile(r"^@?#?([A-Za-z0-9][A-Za-z0-9_]{2,24})$")
# Известные чат-боты, чьим ссылкам доверяем: обычно колесо публикует бот
# по команде стримера. Как правило, боты и так имеют бейдж модератора,
# но проверка по имени страхует, если бейдж не выдан.
DEFAULT_TWITCH_BOTS = [
    "nightbot", "streamelements", "moobot", "fossabot", "wizebot", "streamlabs",
]
TWITCH_BOTS = {
    name.strip().lstrip("@").lower()
    for name in os.getenv("TWITCH_BOTS", ",".join(DEFAULT_TWITCH_BOTS)).split(",")
    if name.strip()
}
TWITCH_ROLE_ICONS = {
    "broadcaster": "🎥",
    "moderator": "🗡",
    "vip": "💎",
    "bot": "🤖",
}

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
    "ok": "✅",
    "warn": "⚠️",
    "link": "\U0001f381",
    "stop": "\U0001f6d1",
    "bell": "\U0001f514",
    "scan": "\U0001f50d",
    "bot": "⌨️",
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
    """Значок для лога и сообщений: эмодзи или ASCII при USE_ICONS=false."""
    return (ICONS if USE_ICONS else ASCII_ICONS)[name]
