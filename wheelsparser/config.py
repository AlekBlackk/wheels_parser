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

# Корень репозитория — на уровень выше каталога пакета. Здесь живут
# редактируемые руками файлы: .env и списки каналов/слов (channels.txt,
# keywords.txt, twitch_channels.txt).
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# Машинное состояние (JSON, лог, lock) — в отдельном каталоге data/, чтобы
# не смешиваться с исходниками. Путь переопределяется WHEELSPARSER_DATA_DIR.
# Каталог создаётся, а старые файлы из корня переносятся в app.main()
# (см. storage.ensure_data_dir) — сам импорт пакета диск не трогает.
DATA_DIR = Path(os.getenv("WHEELSPARSER_DATA_DIR", "") or (BASE_DIR / "data"))

CHANNELS_FILE = BASE_DIR / "channels.txt"
KEYWORDS_FILE = BASE_DIR / "keywords.txt"
TWITCH_CHANNELS_FILE = BASE_DIR / "twitch_channels.txt"
# История находок. freebets.json остаётся только ради разового переноса
# в базу при первом запуске новой версии (см. db.init_db).
DB_FILE = DATA_DIR / "wheels.db"
OUTPUT_FILE = DATA_DIR / "freebets.json"
SEEN_FILE = DATA_DIR / "seen_ids.json"
BOT_STATE_FILE = DATA_DIR / "bot_state.json"
REMOVED_WHEELS_FILE = DATA_DIR / "removed_wheels.json"
# Ссылки, ошибочно признанные expired и ждущие перепроверки (см.
# parser.PENDING_EXPIRED_RETRY) — переживают рестарт: пост уже помечен
# «увиденным» в seen_ids.json, и без этого файла рестарт терял бы находку
# навсегда вместо повторной проверки на следующих циклах.
PENDING_EXPIRED_FILE = DATA_DIR / "pending_expired.json"
# Каналы-первоисточники, о которых админу уже предлагали добавление
# (см. parser.suggest_forward_source). Помним навсегда и переживаем
# рестарт: одно предложение на канал — админ либо добавил его кнопкой,
# либо сознательно проигнорировал, и повторять это не нужно.
SUGGESTED_CHANNELS_FILE = DATA_DIR / "suggested_channels.json"
# Рубеж перебора слагов по каждой серии колёс (см. predictive.py):
# до какого индекса адреса уже проверены. Переживает рестарт, иначе
# сканер каждый раз заново перебирал бы давно завершившиеся колёса.
STREAMERS_FILE = DATA_DIR / "streamers.json"
# Серии слагов, отправленные в отставку после исчерпания пустых проверок.
RETIRED_STREAMERS_FILE = DATA_DIR / "retired_streamers.json"
LOG_FILE = DATA_DIR / "parser.log"
LOCK_FILE = DATA_DIR / "wheelsparser.lock"

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
    # Пустое значение — это «не задано», а не False. В env.example ключи
    # объявлены именно так (`PRECHECK_WHEELS=`), и копия env.example в .env
    # молча выключала бы прекчек: статус колеса не проверялся бы вовсе, а
    # уведомления уходили по всем найденным ссылкам подряд.
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# Пауза между обходами каналов. Задержка обнаружения ссылки — это в
# среднем половина интервала плюс длительность цикла, и при 60с именно
# ожидание, а не работа, съедало почти всё время: обход 35 каналов
# укладывается в единицы секунд (см. CHANNEL_FETCH_CONCURRENCY).
# 10с — нижняя граница, которую выдерживает t.me: это ~3.4 запроса в
# секунду при 34 каналах (проверено нагрузочным прогоном, 429 не было).
# Если в parser.log пойдут 429 по каналам — поднимите значение в .env.
CHECK_INTERVAL = env_int("CHECK_INTERVAL", 10, 10)
REQUEST_TIMEOUT = env_int("REQUEST_TIMEOUT", 15, 5)
MESSAGES_PER_CHANNEL = env_int("MESSAGES_PER_CHANNEL", 50, 10)
# Длина превью текста поста/сообщения в уведомлениях и истории находок
# (preview, message_preview_html). Не настраивается через env: это лимит
# формата уведомления, а не поведения парсера.
PREVIEW_CHAR_LIMIT = 200
# Сколько каналов опрашивается одновременно. Раньше каналы читались строго
# по одному с паузой между ними — при полусотне каналов цикл не укладывался
# в CHECK_INTERVAL, и реальная задержка обнаружения ссылки росла вместе со
# списком. У каждого воркера своя requests.Session (см. parser._fetch_all_channels).
CHANNEL_FETCH_CONCURRENCY = env_int("CHANNEL_FETCH_CONCURRENCY", 8, 1)
# Таймаут одного запроса страницы канала — короче REQUEST_TIMEOUT: страница
# t.me отдаётся за доли секунды, а всё, что тянется дольше, дешевле бросить
# и перечитать в следующем цикле (он начнётся через CHECK_INTERVAL), чем
# держать воркер пула и задерживать уведомления по остальным каналам.
CHANNEL_FETCH_TIMEOUT = env_int("CHANNEL_FETCH_TIMEOUT", 8, 2)
MAX_SEEN_PER_CHANNEL = env_int("MAX_SEEN_PER_CHANNEL", 2000, 100)
# Максимум записей истории в wheels.db: без лимита база растёт бесконечно.
# При превышении старые записи удаляются в конце цикла с находкой.
MAX_RESULTS = env_int("MAX_RESULTS", 5000, 100)
WHEELS_WINDOW_MINUTES = env_int("WHEELS_WINDOW_MINUTES", 10, 1)
# Период по умолчанию для /top — рейтинга каналов по числу колёс.
TOP_PERIOD_DAYS = env_int("TOP_PERIOD_DAYS", 30, 1)
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
# TTL кэша expired-статусов в betboom.py (сек), НЕ связан с REALERT_COOLDOWN_MINUTES.
# Кэш существует только для того, чтобы не бить по API BetBoom повторно за
# один и тот же «хвост» (старый href), когда он всплывает в нескольких
# постах подряд (например, один стрим репостят в несколько каналов почти
# одновременно). TTL должен быть коротким: пока запись в кэше жива, свежий
# пост с тем же URL (реальный перезапуск колеса) будет ошибочно принят за
# тот же самый «хвост» и пропущен без проверки API — см.
# parser.collect_pending_entries, где кулдаун на такой пропуск намеренно
# НЕ ставится именно ради быстрого повторного обнаружения. retry_expired_links
# всегда обходит этот кэш (precheck_wheel(..., use_cache=False)) — его смысл
# как раз в честной перепроверке, а не в ожидании TTL.
EXPIRED_CACHE_TTL_SECONDS = env_int("EXPIRED_CACHE_TTL_SECONDS", 120, 5)
# Столько подряд идущих 'unknown' означают, что проверка статуса ослепла:
# заглушка API, протухшая подпись, блокировка. У живого API такая серия по
# разным колёсам невероятна — статус хоть иногда, да определяется.
# Срабатывание один раз пишется в parser.log (сервисного уведомления в
# Telegram намеренно нет — слишком шумно), статус при этом НЕ подменяется:
# уведомление и так требует явного active, а неподтверждённые ссылки ждут
# в очереди перепроверки — см. betboom._note_status_health.
# Раньше счётчик считал подряд идущие 'expired' и переводил их в 'unknown'
# (fail-open). Это оказалось источником уведомлений о завершившихся
# колёсах: серия expired подряд — штатное состояние парсера, а не признак
# сбоя API.
BETBOOM_STUB_GUARD_THRESHOLD = env_int("BETBOOM_STUB_GUARD_THRESHOLD", 8, 2)
# Проверять колесо через API BetBoom перед отправкой уведомления. Посты
# нередко содержат «хвосты» — старые href на прошлые колёса, невидимые
# в Telegram, но попадающие в HTML-разметку (стример скопировал прошлый пост
# и обновил только видимый текст). Завершившиеся колёса не рассылаются.
PRECHECK_WHEELS = env_bool("PRECHECK_WHEELS", True)
# Каждый пост обрабатывается по хэшу содержимого только один раз (см.
# parser.process_message), поэтому сбой отправки Telegram-уведомления в
# момент обработки означает потерю находки навсегда, если её не повторить.
# Записи с notified=False, найденные не позже этого окна (мин), повторно
# отправляются в начале следующих циклов, пока не будут доставлены.
NOTIFY_RETRY_WINDOW_MINUTES = env_int("NOTIFY_RETRY_WINDOW_MINUTES", 180, 1)
# Лимит повторных отправок за один цикл — защита от долгого сбоя Telegram:
# без него цикл тратил бы время на HTTP-ретраи по всему бэклогу разом.
NOTIFY_RETRY_MAX_PER_CYCLE = env_int("NOTIFY_RETRY_MAX_PER_CYCLE", 10, 1)
# Команды старше этого возраста (сек) подтверждаются, но не выполняются —
# защита от бэклога getUpdates, накопившегося за время простоя парсера.
STALE_COMMAND_SECONDS = env_int("STALE_COMMAND_SECONDS", 120, 10)
# Уведомление о «мёртвом» канале после N подряд неудачных циклов.
CHANNEL_FAIL_THRESHOLD = env_int("CHANNEL_FAIL_THRESHOLD", 5, 2)
# Уведомление о «пустой ленте» после N подряд циклов, в которых страница
# канала отдалась с HTTP 200, но ни одного поста распознать не удалось.
# Это единственный отказ, который иначе не виден вообще: при смене вёрстки
# t.me парсер продолжает считать каналы исправными и молча ничего не находит.
CHANNEL_EMPTY_THRESHOLD = env_int("CHANNEL_EMPTY_THRESHOLD", 3, 2)
ALERT_ON_FIRST_RUN = env_bool("ALERT_ON_FIRST_RUN", False)
USE_COLORS = env_bool("USE_COLORS", True)
USE_ICONS = env_bool("USE_ICONS", True)

# Верхняя граница длины ключевого слова в /addword — против случайной вставки
# целого поста вместо слова; не настраивается через env.
KEYWORD_MAX_LENGTH = 64

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TELEGRAM_ADMIN_ID = os.getenv("TELEGRAM_ADMIN_ID", "").strip()
BOT_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
USERNAME_RE = re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{3,31})$")

# --- Twitch ---
# Мониторинг Twitch-чатов: анонимное IRC-подключение, токены и OAuth не нужны.
# Реагируем ТОЛЬКО на ссылки betboom.ru/freestream от стримера, модераторов,
# VIP и известных ботов; ключевые слова в Twitch-чатах не ищутся.
TWITCH_ENABLED = env_bool("TWITCH_ENABLED", True)
TWITCH_IRC_HOST = "irc.chat.twitch.tv"
TWITCH_IRC_PORT = 6697
# Watchdog «мёртвого» IRC-соединения: если за это время не пришло ни байта,
# считаем сокет зависшим (полуоткрытое TCP без RST от сервера — recv() даёт
# только повторяющиеся таймауты, не ошибку и не пустые данные) и
# переподключаемся. Twitch шлёт PING каждые ~5 минут даже в тихом чате,
# поэтому порог должен быть заметно больше этого интервала.
TWITCH_IDLE_TIMEOUT_SECONDS = env_int("TWITCH_IDLE_TIMEOUT_SECONDS", 360, 60)
# Очередь сообщений с ссылками между IRC-потоком и обработчиком. IRC-поток
# обязан только читать сокет: любой сетевой вызов в нём задерживает ответ на
# PING, и Twitch рвёт соединение. Очередь ограничена, чтобы затянувшийся сбой
# API BetBoom не съедал память; в неё попадают только сообщения со ссылкой,
# поэтому обычный чат её не наполняет.
TWITCH_QUEUE_MAXSIZE = env_int("TWITCH_QUEUE_MAXSIZE", 500, 10)
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

# ----------------------------------------------------------------------------
# Перебор слагов колёс (predictive.py)
# ----------------------------------------------------------------------------
# Адреса колёс у стримера идут серией: zonertg4, zonertg5, … / LOLLY08,
# LOLLY09, … Разведка показала, что страница колеса существует ДО того, как
# стример выложит ссылку (создано, но не запущено — статус 'soon'), а
# несуществующий слаг честно отдаёт 404. Значит следующий адрес серии можно
# проверить заранее и поймать колесо, которое иначе нашлось бы только по
# посту — или не нашлось бы вовсе: на момент разведки у одной серии из
# восьми существующих адресов в базе было три.
PREDICTIVE_ENABLED = env_bool("PREDICTIVE_ENABLED", True)
# Пауза цикла сканирования. Короче нет смысла: колёса живут от получаса,
# а частый перебор — лишний повод для WAF.
PREDICTIVE_SCAN_INTERVAL = env_int("PREDICTIVE_SCAN_INTERVAL", 900, 60)
# Пауза между запросами внутри цикла. Перебор по чужому адресному
# пространству — именно то, что фильтры вроде Cloudflare ловят по частоте
# 404, поэтому запросы идут редко и по одному.
PREDICTIVE_REQUEST_DELAY_SECONDS = env_int("PREDICTIVE_REQUEST_DELAY_SECONDS", 2, 1)
# Насколько адресов вперёд заглядывать за один цикл. Перебор прекращается
# на первом 404: реальные серии сплошные (проверено на zonertg4…11 и
# LOLLY08…15), поэтому «дыра» означает конец серии, а не пропуск.
PREDICTIVE_LOOKAHEAD = env_int("PREDICTIVE_LOOKAHEAD", 3, 1)
# Потолок запросов в сутки (МСК). Страховка от разрастания: серий может
# стать много, и без лимита сканер незаметно превратился бы в долбёжку.
PREDICTIVE_DAILY_BUDGET = env_int("PREDICTIVE_DAILY_BUDGET", 500, 10)
# Пауза после явного отказа (403/429) — сканер замолкает, а админ получает
# уведомление. Бан по IP убил бы весь парсер, а не только сканер.
PREDICTIVE_BLOCK_COOLDOWN_MINUTES = env_int("PREDICTIVE_BLOCK_COOLDOWN_MINUTES", 60, 5)
# Потолок пустых проверок серии подряд (когда lookahead не нашёл ни одного
# нового колеса), после которого серия считается завершённой и удаляется
# из streamers.json, освобождая бюджет сканера.
PREDICTIVE_MAX_EMPTY_SCANS = env_int("PREDICTIVE_MAX_EMPTY_SCANS", 8, 1)
# Допустимый формат префикса серии слагов: от 2 до 32 символов (латиница,
# цифры, дефис, подчёркивание). Защищает frontier от мусора и опечаток.
PREDICTIVE_PREFIX_RE = re.compile(r"^[A-Za-z0-9_-]{2,32}$")
# Слаг серии: имя (в исходном регистре — он значим, lolly08 отдаёт 404
# там, где LOLLY08 отдаёт 200) плюс числовой хвост. Ширина хвоста важна:
# LOLLY08 дополнен нулём, zonertg4 — нет.
SLUG_SERIES_RE = re.compile(r"^(?P<prefix>.*[^0-9])(?P<index>[0-9]+)$")

STREAMER_WHEEL_INFO_API = "https://betboom.ru/api/streamer-wheel/action/get-info"
# Сколько секунд переиспользуется подпись действия (action_uid + JWT) со
# страницы колеса. get-info принимает не адрес колеса, а его action_uid, и
# требует подписи в заголовке x-action-signature — оба значения берутся из
# __NEXT_DATA__ страницы, то есть на каждую проверку приходится ДВА запроса
# (страница + API). action_uid у колеса постоянен, а у JWT срок жизни сутки,
# поэтому пара кэшируется: без этого ретрай одной ссылки (раз в минуту до
# трёх часов, см. NOTIFY_RETRY_WINDOW_MINUTES) тянул бы страницу колеса
# каждый цикл. TTL берётся с большим запасом до истечения JWT — просроченная
# подпись роняет ответ в заглушку, а её парсер трактует как 'unknown'.
ACTION_SIGNATURE_TTL_SECONDS = env_int("ACTION_SIGNATURE_TTL_SECONDS", 3600, 60)

# Схема и www. — опциональны: Twitch-боты (nightbot, StreamElements и т.п.)
# нередко режут https:// в сообщениях чата, а обычный regex с обязательным
# https?:// такие ссылки вообще не находил (см. urls.normalize_url — она
# достраивает схему обратно, чтобы канонический URL был одним и тем же
# независимо от источника). (?<![\w.-]) — граница перед доменом: без неё
# опциональная схема заставила бы findall() матчить и «хвост» чужого имени
# («evilbetboom.ru/freestream/x» → ложно распознавался бы как betboom.ru).
# Символьный класс пути включает ':' и '/' (нужны для query/fragment), из-за
# чего без стоп-условия regex проглатывал склеенные без пробела повторы
# ссылки («...kekw1https://betboom.ru/freestream/kekw1...» — зрители так
# постят, чтобы обойти анти-дубль фильтр Twitch) в один гигантский match.
# Он каждый раз рос новой длины и не совпадал с предыдущим по строке, поэтому
# кулдаун в alerts.cooldown_active (ключ — точная строка url) не срабатывал,
# и одно и то же колесо уходило в Telegram по несколько раз подряд.
# Негативный lookahead останавливает жадный класс перед началом следующей
# ссылки на betboom.ru/freestream/.
FREESTREAM_RE = re.compile(
    r"(?<![\w.-])(?:https?://)?(?:www\.)?betboom\.ru/freestream/"
    r"(?:(?!(?:https?://)?(?:www\.)?betboom\.ru/freestream/)"
    r"[A-Za-z0-9_~:/?#\[\]@!$&'()*+,;=%.-])+",
    re.IGNORECASE,
)
TRAILING_PUNCTUATION = ".,;:!?)]}>'\""

# Известные сокращатели ссылок (см. urls.resolve_shortlink). Стримеры
# прячут за ними ссылку на колесо (обычно реф-ссылку), и без раскрытия
# такая ссылка не матчится FREESTREAM_RE и теряется молча — до прекчека
# дело не доходит, потому что регэксп даже не видит в тексте betboom.ru.
# t.me сюда сознательно не входит: это не сокращатель произвольных ссылок,
# а адрес поста/канала в самом Telegram — резолвить там нечего.
SHORTENER_DOMAINS = frozenset({"vk.cc", "clck.ru", "bit.ly", "tinyurl.com"})
# Таймаут одного HEAD/GET к сокращателю — короче REQUEST_TIMEOUT: это
# вспомогательный шаг обработки одного сообщения, а не запрос к целевому
# сайту, и таймаут не должен ощутимо тормозить разбор всего канала.
SHORTENER_RESOLVE_TIMEOUT = env_int("SHORTENER_RESOLVE_TIMEOUT", 2, 1)
# Потолок кандидатов на сокращатель, раскрываемых для одного сообщения.
# Защита от долгой блокировки воркера при посте со множеством ссылок на сокращатели.
MAX_SHORTLINKS_PER_MESSAGE = env_int("MAX_SHORTLINKS_PER_MESSAGE", 5, 1)
# TTL кэша раскрытых ссылок (сек, см. urls._shortlink_cache). Канал
# перечитывает последние MESSAGES_PER_CHANNEL сообщений каждый цикл
# (CHECK_INTERVAL, по умолчанию 60с) независимо от того, видели их уже
# или нет — без кэша один и тот же сокращённый URL резолвился бы заново
# каждый цикл, пока пост не вывалится из окна. Долгий TTL безопасен:
# цель сокращателя практически никогда не меняется после публикации.
SHORTENER_CACHE_TTL_SECONDS = env_int("SHORTENER_CACHE_TTL_SECONDS", 1800, 60)
# Кандидат на раскрытие: ссылка на один из SHORTENER_DOMAINS. Формат пути
# в разных сокращателях отличается, поэтому символьный класс — как у
# FREESTREAM_RE (без пробела/кавычек), а не привязан к конкретному сервису.
SHORTENER_CANDIDATE_RE = re.compile(
    r"(?<![\w.-])(?:https?://)?(?:www\.)?(?:"
    + "|".join(re.escape(domain) for domain in sorted(SHORTENER_DOMAINS))
    + r")/[A-Za-z0-9_~:/?#\[\]@!$&'()*+,;=%.-]+",
    re.IGNORECASE,
)

# Домены, разрешённые в посте, найденном по ключевому слову (см.
# urls.find_disallowed_domains). Ссылочная (freestream) ветка уже
# домен-специфична сама по себе (см. FREESTREAM_RE) — это ограничение
# только для алертов по ключевым словам без ссылки на betboom.ru:
# скам-казино нередко пишет «колесо на 60000$» и ведёт на свой сайт —
# без проверки домена такой пост уходил бы алертом наравне с настоящим
# колесом. t.me/telegram.me разрешены — это ссылка на другой пост/канал
# в том же Telegram, а не сторонний сайт. Площадки стримеров (twitch, vk,
# youtube) — тоже: пост с колесом почти всегда несёт подпись «Твич | ВК»,
# и без них проверка глушила бы не скам, а обычные посты тех самых каналов,
# за которыми и ведётся мониторинг. Проверка идёт и по поддоменам
# (см. urls.find_disallowed_domains), поэтому www.twitch.tv тоже разрешён.
ALLOWED_KEYWORD_DOMAINS = frozenset({
    "betboom.ru", "t.me", "telegram.me",
    "twitch.tv", "vk.com", "vk.ru", "youtube.com", "youtu.be",
})
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
