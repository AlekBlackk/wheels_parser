"""Клиент API BetBoom: определение статуса колеса без браузера.

Статусы: 'active' (идёт), 'soon' (ещё не начался), 'expired' (завершилось),
'missing' (страницы колеса нет — HTTP 404), 'unknown' (проверить не удалось).

Уведомление уходит только по подтверждённому 'active' (см.
process_candidate_wheel). Прежний fail-open — «лучше лишний раз оповестить,
чем пропустить живое колесо» — на практике означал рассылку по любому сбою
сети, протухшей подписи и битому адресу, потому что все они дают 'unknown'.
Неподтверждённая ссылка не теряется: она уходит в очередь перепроверки
(retries.retry_expired_links) и приходит сама, когда колесо запустится.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from .alerts import claim_url_alert, release_url_alert
from .config import (
    ACTION_SIGNATURE_TTL_SECONDS,
    ACTIVE_CHECK_CONCURRENCY,
    BETBOOM_STUB_GUARD_THRESHOLD,
    EXPIRED_CACHE_TTL_SECONDS,
    HEADERS,
    MSK_TZ,
    PRECHECK_WHEELS,
    REALERT_COOLDOWN_MINUTES,
    REQUEST_TIMEOUT,
    STREAMER_WHEEL_INFO_API,
    icon,
)
from .db import WheelEntry, make_wheel_entry
from .logging_setup import log
from .net import PARSER_SESSION, ThreadLocalSession, build_session
from .timeutils import now_msk
from .urls import normalize_url

# Кэш завершившихся колёс: url -> момент, когда колесо признано expired.
# TTL — EXPIRED_CACHE_TTL_SECONDS (короткий, минуты, НЕ REALERT_COOLDOWN_MINUTES
# — см. config.py): единственная задача кэша — не бить по API повторно за
# один и тот же «хвост», всплывший в нескольких постах подряд. Долгий TTL
# (раньше — REALERT_COOLDOWN_MINUTES, 30 мин) означал, что реальный перезапуск
# колеса на том же адресе новым постом молча пропускался почти полчаса.
# Кэш общий для parser-потока (precheck перед уведомлением) и фонового
# active-api-потока, поэтому доступ — только под _expired_cache_lock.
_expired_cache: dict[str, datetime] = {}
_expired_cache_lock = threading.Lock()


# Подписи действий: url -> (action_uid, подпись, момент получения).
# get-info принимает action_uid, а не адрес колеса, и требует заголовка
# x-action-signature (см. _fetch_action_signature). Оба значения лежат в
# __NEXT_DATA__ страницы колеса, поэтому проверка стоит двух запросов —
# кэш сводит их обратно к одному. TTL — ACTION_SIGNATURE_TTL_SECONDS.
# Кэш общий для parser-потока, twitch-worker и пула /active, поэтому доступ
# — только под _signature_cache_lock.
_signature_cache: dict[str, tuple[str, str, datetime]] = {}
_signature_cache_lock = threading.Lock()

# Состояние страницы колеса Next.js кладёт в этот тег. bs4 здесь не нужен:
# из двадцати килобайт разметки берётся один известный скрипт.
NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)


class WheelPageMissing(Exception):
    """Страницы колеса по такому адресу нет: BetBoom ответил 404.

    Отдельный случай от «проверить не удалось»: слаг не существует
    (опечатка стримера, обрезанная ссылка, удалённое колесо), и
    перепроверять его следующие часы бессмысленно — в отличие от сетевого
    сбоя, который через минуту может пройти.
    """


def _cached_signature(url: str) -> tuple[str, str] | None:
    cutoff = timedelta(seconds=ACTION_SIGNATURE_TTL_SECONDS)
    now = now_msk()
    with _signature_cache_lock:
        for stale_url, (_uid, _sig, when) in list(_signature_cache.items()):
            if now - when > cutoff:
                _signature_cache.pop(stale_url, None)
        entry = _signature_cache.get(url)
    return (entry[0], entry[1]) if entry is not None else None


def _fetch_action_signature(
    url: str, session: requests.Session
) -> tuple[str, str] | None:
    """action_uid колеса и подпись запроса со страницы колеса.

    Возвращает (action_uid, signature) или None, если страницу не удалось
    получить или разобрать. Значения берутся из __NEXT_DATA__:
    ``props.pageProps.uid`` — постоянный идентификатор действия,
    ``props.pageProps.hash`` — JWT со сроком жизни сутки, который API ждёт
    в заголовке x-action-signature. Результат кэшируется, см. _signature_cache.
    """
    cached = _cached_signature(url)
    if cached is not None:
        return cached
    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT)
        if response.status_code == 404:
            raise WheelPageMissing(url)
        if response.status_code != 200:
            log.debug("wheel-page: HTTP %s для %s", response.status_code, url)
            return None
        match = NEXT_DATA_RE.search(response.text)
        if match is None:
            log.debug("wheel-page: __NEXT_DATA__ не найден на %s", url)
            return None
        page_props = json.loads(match.group(1))["props"]["pageProps"]
        action_uid = page_props.get("uid")
        signature = page_props.get("hash")
    except WheelPageMissing:
        raise
    except Exception as error:
        log.debug("wheel-page: не удалось разобрать %s: %s", url, error)
        return None
    if not (
        isinstance(action_uid, str)
        and isinstance(signature, str)
        and action_uid
        and signature
    ):
        log.debug("wheel-page: на %s нет uid/hash", url)
        return None
    with _signature_cache_lock:
        _signature_cache[url] = (action_uid, signature, now_msk())
    return action_uid, signature


# У API BetBoom нет флага «колесо для рефералов» — стример помечает это
# только текстом («Розыгрыш фрибетов для рефералов») или «ref» в адресе.
# \bреф ловит «рефералов», «рефы», «рефовод», «рефка»; граница слова
# отсекает «префикс» и т.п.
REFERRAL_TEXT_RE = re.compile(r"\bреф", re.IGNORECASE)


def is_referral_wheel(
    url: str, info: dict[str, Any] | None, post_text: str = ""
) -> bool:
    """True, если колесо предназначено для рефералов.

    Три сигнала (OR): текст title/description из API, подстрока «ref»
    в slug URL и текст поста/сообщения чата, где нашлась ссылка. Slug и
    пост — запасные сигналы: работают при сбое API и при выключенном
    precheck (стример не всегда пишет про рефералов в описании колеса).
    post_text вызывающий передаёт только для поста с одной ссылкой:
    в посте с несколькими колёсами неизвестно, к какому из них относится
    «для рефов», и метка ушла бы на все.
    """
    if info:
        text = f"{info.get('title', '')} {info.get('description', '')}"
        if REFERRAL_TEXT_RE.search(text):
            return True
    if post_text and REFERRAL_TEXT_RE.search(post_text):
        return True
    slug = normalize_url(url).rsplit("/", 1)[-1]
    return "ref" in slug.lower()


def wheel_window(info: dict[str, Any]) -> tuple[datetime | None, datetime | None]:
    """Начало и конец розыгрыша по start_dttm + duration_min (обе метки — UTC).

    (None, None), если поля отсутствуют, неразбираемы или без таймзоны:
    считать окно по наивной метке нельзя — неизвестно, чьё это время.
    """
    start_raw = info.get("start_dttm")
    duration = info.get("duration_min")
    if not (
        isinstance(start_raw, str)
        and isinstance(duration, (int, float))
        and not isinstance(duration, bool)
        and duration > 0
    ):
        return None, None
    try:
        start = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
    except ValueError:
        return None, None
    if start.tzinfo is None:
        return None, None
    return start, start + timedelta(minutes=float(duration))


def wheel_end_msk(info: dict[str, Any] | None) -> datetime | None:
    """Момент окончания розыгрыша в МСК — дедлайн для показа человеку."""
    if not info:
        return None
    _start, end = wheel_window(info)
    return end.astimezone(MSK_TZ) if end is not None else None


def wheel_ends_at(info: dict[str, Any] | None) -> str:
    """Дедлайн колеса как ISO-строка МСК (пустая, если срок неизвестен).

    Строка, а не datetime: значение уезжает в базу находок и обратно.
    """
    end = wheel_end_msk(info)
    return end.isoformat(timespec="seconds") if end is not None else ""


def api_info_to_status(info: dict[str, Any]) -> str:
    is_ended = info.get("is_ended")
    if not isinstance(is_ended, bool):
        return "unknown"
    # is_ended у API BetBoom запаздывает: флаг не переключается по таймеру,
    # и колесо может часами числиться «не завершённым» после окончания.
    # Поэтому конец розыгрыша считаем сами: start_dttm + duration_min.
    time_status: str | None = None
    start, end = wheel_window(info)
    is_early = info.get("is_early")
    # Заглушку опознаём по форме ответа: в настоящем info всегда есть
    # идентификатор действия, а заглушка отдаёт только
    # {title, prizes, is_ended, start_dttm = время запроса, rules_link}.
    # Такой ответ о колесе не сообщает ничего — верить его is_ended нельзя.
    # Прежняя проверка («нет ни окна розыгрыша, ни булева is_early») била
    # мимо: она задевала и настоящие ответы без start_dttm, и наоборот
    # пропустила бы заглушку, если та однажды придёт с окном.
    # Живая проверка 2026-09-06: подписанный запрос (action_uid +
    # x-action-signature) отдаёт action_uid по каждому адресу, а заглушка
    # приходит только на неверный контракт — без подписи, с чужой подписью
    # или в старом виде {streamer_link}.
    if not (info.get("action_uid") or info.get("action_id")):
        return "unknown"
    # Идентификатор действия есть, но ни окна розыгрыша, ни булева
    # is_early — такой ответ о состоянии колеса всё равно молчит.
    # Считать его 'soon' (как получалось бы дальше по функции) —
    # значит выдать неудачу проверки за определённый статус: в /active
    # колесо перестанет попадать в «не удалось проверить», а счётчик
    # здоровья API сбросится на пустом ответе.
    if start is None and not isinstance(is_early, bool):
        return "unknown"
    if is_ended:
        return "expired"
    if start is not None and end is not None:
        now = datetime.now(timezone.utc)
        if now >= end:
            return "expired"
        time_status = "soon" if now < start else "active"
    # «Акция скоро начнётся» на сайте показывается по флагу is_early,
    # поэтому он надёжнее расчёта по start_dttm: бывает, что start_dttm
    # уже в прошлом, а розыгрыш стример ещё не запустил. is_early=True
    # всегда означает «ещё не началось» (если не истекло по времени выше).
    if isinstance(is_early, bool) and is_early:
        return "soon"
    if time_status is not None:
        return time_status
    # Сюда попадают колёса без пригодного start_dttm. Розыгрыш идёт
    # только с момента старта, поэтому у активного колеса время старта есть
    # всегда. Его отсутствие означает «стример создал колесо, но не запустил»:
    # на странице «Акция скоро начнётся» и кнопки участия нет — даже при
    # is_early=false. Такое колесо в /active показывать нельзя.
    return "soon"


def fetch_wheel_info(
    url: str, session: requests.Session
) -> dict[str, Any] | None:
    """Запрашивает info одного колеса через BetBoom API без браузера.

    Возвращает словарь info или None при любой ошибке (сеть, не-200,
    неожиданный формат). Сессия передаётся явно: у каждого потока она
    своя (см. net.py).

    Запрос идёт по подписанному контракту: тело — {"action_uid": ...},
    подпись — в заголовке x-action-signature (см. _fetch_action_signature).
    Прежний вариант (тело {"streamer_link": ...} без подписи) API с августа
    2026 не отвергает, а отвечает заглушкой: одинаковый ответ на любой
    адрес, с is_ended=true и без duration_min/is_early — то есть живое
    колесо выглядело завершившимся, и уведомления пропадали все до единого.
    """
    canonical = normalize_url(url)
    if not canonical:
        return None
    signature = _fetch_action_signature(canonical, session)
    if signature is None:
        return None
    action_uid, action_signature = signature
    try:
        response = session.post(
            STREAMER_WHEEL_INFO_API,
            json={"action_uid": action_uid},
            headers={
                **HEADERS,
                "Accept": "application/json",
                "X-Platform": "web",
                "Referer": canonical,
                "x-action-signature": action_signature,
            },
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code != 200:
            log.debug(
                "active-check API: HTTP %s для %s", response.status_code, canonical
            )
            return None
        info = response.json().get("info", {})
        return info if isinstance(info, dict) else None
    except Exception as error:
        log.debug("active-check API: ошибка для %s: %s", canonical, error)
        return None


def _prune_expired_cache() -> None:
    """Убирает записи старше EXPIRED_CACHE_TTL_SECONDS — иначе кэш растёт бессрочно."""
    cutoff = timedelta(seconds=EXPIRED_CACHE_TTL_SECONDS)
    now = now_msk()
    with _expired_cache_lock:
        for stale_url in [
            url for url, when in _expired_cache.items() if now - when > cutoff
        ]:
            _expired_cache.pop(stale_url, None)


def _is_cached_expired(url: str) -> bool:
    with _expired_cache_lock:
        return url in _expired_cache


def _cache_expired(url: str) -> None:
    with _expired_cache_lock:
        _expired_cache[url] = now_msk()


# Детектор «проверка статуса ослепла» — см. config.BETBOOM_STUB_GUARD_THRESHOLD.
# Общий на все URL и все потоки (parser, twitch-worker, пул /active):
# признак поломки — что статус не определяется НИ для одного колеса, а не
# поведение одного конкретного адреса, поэтому счётчик глобальный, а не
# per-URL.
#
# Раньше здесь был fail-open: после порога подряд идущих expired все
# следующие expired подменялись на unknown, а unknown уходил в Telegram —
# «лучше лишнее уведомление, чем пропущенное колесо». Это и оказалось
# источником уведомлений о неактивных колёсах: серия expired подряд —
# штатное состояние (в parser.log 84 expired против 36 active), так что
# гвард исправно превращал завершившиеся колёса в рассылку. Теперь
# уведомление уходит только по явному active (см. process_candidate_wheel),
# и подменять статус незачем — вместо этого гвард пишет в parser.log.
#
# Наблюдаемых сбоя два, и признаки у них разные, поэтому счётчика тоже
# два. Первый — «проверка ослепла»: подряд идущие unknown (заглушка без
# action_uid, протухшая подпись, блокировка). Второй — «правдоподобная
# заглушка»: ответы приходят с виду настоящие, но ни одного живого
# колеса среди них нет. Второй случай проверкой формы ответа не ловится
# в принципе (см. api_info_to_status), а тихо стоит он ровно так же:
# ноль уведомлений при работающем парсере. Порог у него выше — серия
# expired подряд бывает и в норме (ночью, к концу дня), а цена ложной
# тревоги теперь всего одна строка в логе, не рассылка.
_status_health_lock = threading.Lock()
_consecutive_unknown = 0
_blind_warning_logged = False
_consecutive_without_live = 0
_silence_warning_logged = False
# Столько проверок подряд без единого active/soon считаются поводом
# заподозрить заглушку, которую не отличить по форме ответа.
NO_LIVE_WHEEL_THRESHOLD = BETBOOM_STUB_GUARD_THRESHOLD * 5


def _note_status_health(status: str) -> None:
    """Отмечает в parser.log, что статус колёс перестал определяться.

    Вызывать только для СВЕЖЕГО результата проверки (не для ответов из
    _expired_cache — повтор старого решения ничего не доказывает).
    Статус не меняет: это чистое наблюдение.

    Сервисных уведомлений в Telegram по срабатыванию намеренно нет
    (слишком шумно), сообщения живут только в parser.log.
    """
    global _consecutive_unknown, _blind_warning_logged
    global _consecutive_without_live, _silence_warning_logged
    log_blind = False
    log_silent = False
    log_blind_recovery = False
    log_live_recovery = False
    blind_streak = 0
    silent_streak = 0
    with _status_health_lock:
        if status == "unknown":
            _consecutive_unknown += 1
            blind_streak = _consecutive_unknown
            if blind_streak >= BETBOOM_STUB_GUARD_THRESHOLD:
                log_blind = not _blind_warning_logged
                _blind_warning_logged = True
        else:
            log_blind_recovery = _blind_warning_logged
            _consecutive_unknown = 0
            _blind_warning_logged = False
        # active/soon — единственное доказательство, что API вообще
        # способен показать живое колесо. expired и missing таким
        # доказательством не являются: правдоподобная заглушка выглядит
        # ровно как бесконечная серия expired.
        if status in ("active", "soon"):
            log_live_recovery = _silence_warning_logged
            _consecutive_without_live = 0
            _silence_warning_logged = False
        else:
            _consecutive_without_live += 1
            silent_streak = _consecutive_without_live
            if silent_streak >= NO_LIVE_WHEEL_THRESHOLD:
                log_silent = not _silence_warning_logged
                _silence_warning_logged = True
    if log_blind:
        log.error(
            "%s BetBoom API: %s проверок подряд не дали статуса — похоже, "
            "контракт get-info снова сломан (заглушка, протухшая подпись "
            "или блокировка). Уведомления о новых колёсах не уходят: они "
            "требуют явного active. Ссылки ждут в очереди перепроверки.",
            icon("warn"),
            blind_streak,
        )
    if log_silent:
        log.error(
            "%s BetBoom API: %s проверок подряд без единого живого колеса. "
            "Ночью это норма, но если продолжается днём — возможно, API "
            "отдаёт правдоподобную заглушку, и парсер молчит зря. Стоит "
            "открыть любое известное живое колесо руками.",
            icon("warn"),
            silent_streak,
        )
    if log_blind_recovery:
        log.info(
            "%s BetBoom API: статус снова определяется — проверка колёс "
            "восстановилась.",
            icon("ok"),
        )
    if log_live_recovery:
        log.info(
            "%s BetBoom API: снова виден живой розыгрыш — подозрение на "
            "заглушку снято.",
            icon("ok"),
        )


def precheck_wheel(
    url: str,
    session: requests.Session | None = None,
    post_text: str = "",
    use_cache: bool = True,
    feed_status_health: bool = True,
) -> tuple[str, bool, str]:
    """Статус колеса, реф-флаг и дедлайн перед отправкой уведомления.

    Возвращает ('active'/'soon'/'expired'/'missing'/'unknown',
    is_referral, ends_at), где ends_at — ISO-строка МСК с концом розыгрыша
    или "" если срок неизвестен (сбой API или колесо без start_dttm).
    'missing' — страницы колеса по адресу нет (404), 'unknown' — проверить
    не удалось. Уведомление уходит только по 'active'
    (см. process_candidate_wheel).
    Реф-флаг при недоступном info считается по slug URL и тексту поста
    (post_text, см. is_referral_wheel).
    По умолчанию используется PARSER_SESSION — вызывающему из другого
    потока нужно передать свою сессию.
    use_cache=False пропускает чтение expired-кэша и всегда идёт в API —
    этим пользуется parser.retry_expired_links: его смысл в честной
    перепроверке ссылки, а не в ожидании EXPIRED_CACHE_TTL_SECONDS. Успешный
    результат всё равно пишется в кэш (если снова expired) — другие «хвосты»
    того же URL по-прежнему выигрывают от дедупликации.
    feed_status_health=False исключает результат из счётчика «проверка
    ослепла» (см. _note_status_health). Нужен перебору слагов
    (predictive.py): он намеренно ходит по чужому адресному пространству,
    где несуществующие слаги и сбои — норма, а не признак поломки API.
    """
    canonical = normalize_url(url)
    if not canonical:
        return "unknown", False, ""
    _prune_expired_cache()
    if use_cache and _is_cached_expired(canonical):
        log.info(
            "precheck [cache]: %s → expired (кэш %sс)",
            canonical,
            EXPIRED_CACHE_TTL_SECONDS,
        )
        return "expired", is_referral_wheel(canonical, None, post_text), ""
    try:
        info = fetch_wheel_info(canonical, session or PARSER_SESSION)
    except WheelPageMissing:
        info = None
        status = "missing"
    else:
        status = "unknown" if info is None else api_info_to_status(info)
    if feed_status_health:
        _note_status_health(status)
    referral = is_referral_wheel(canonical, info, post_text)
    log.info(
        "precheck [api]: %s → %s%s",
        canonical,
        status,
        " (для рефералов)" if referral else "",
    )
    if status == "expired":
        _cache_expired(canonical)
    return status, referral, wheel_ends_at(info)


# Причина пропуска для лога — по статусу прекчека.
SKIP_REASONS = {
    "expired": "колесо уже завершилось",
    "soon": "розыгрыш ещё не начался",
    "missing": "страницы колеса не существует",
    "unknown": "статус проверить не удалось",
}


def process_candidate_wheel(
    url: str,
    channel: str,
    now: datetime,
    *,
    post_text: str = "",
    session: requests.Session | None = None,
    last_found: dict[str, datetime] | None = None,
    source: str = "telegram",
    author: str = "",
    author_roles: list[str] | None = None,
    msg_id: str = "",
    message_url: str = "",
    preview: str = "",
    preview_html: str = "",
    is_edited: bool = False,
    source_label: str = "",
    precheck_fn: Callable[..., tuple[str, bool, str]] | None = None,
) -> tuple[WheelEntry | None, str, bool]:
    """Единый конвейер обработки найденной ссылки (кулдаун, precheck, WheelEntry).

    Возвращает (entry, status, retry_needed):
    - При активном кулдауне: (None, "cooldown", False)
    - При любом статусе кроме 'active': кулдаун сбрасывается,
      (None, status, True) — ссылку стоит перепроверить позже.
      'missing' тоже: 404 бывает не только у выдуманного слага, но и
      у живого колеса при блокировке или сбое CDN, а цена ошибки
      несимметрична — лишние дешёвые GET против потерянного колеса.
      Мусорные адреса из очереди уходят сами по NOTIFY_RETRY_WINDOW_MINUTES.
    - При 'active' и при выключенном PRECHECK_WHEELS: (entry, status, False)
    """
    label = source_label or (f"@{channel}" if source == "telegram" else f"{source} #{channel}")
    if not claim_url_alert(url, now, last_found):
        log.info(
            "%s Пропускаю %s [%s]: недавно уже оповещали (кулдаун %s мин)",
            icon("bell"),
            url,
            label,
            REALERT_COOLDOWN_MINUTES,
        )
        return None, "cooldown", False

    check_fn = precheck_fn or precheck_wheel
    if PRECHECK_WHEELS:
        status, referral, ends_at = check_fn(
            url, session, post_text=post_text
        )
    else:
        status, referral, ends_at = "", is_referral_wheel(url, None, post_text), ""

    # Белый список: уведомление уходит только по явному 'active'. Пустой
    # статус — это выключенный PRECHECK_WHEELS, то есть осознанный отказ от
    # проверки, а не её неудача, поэтому он проходит.
    # Раньше здесь стоял чёрный список ("expired", "soon"), и всё
    # остальное — в первую очередь 'unknown' — уходило в Telegram по
    # принципу «лучше лишнее уведомление, чем пропущенное колесо». На
    # практике это и давало поток уведомлений о неактивных колёсах:
    # 'unknown' возникает при любом сбое сети, протухшей подписи и 404 на
    # слаг. Молчание надёжнее мусора: неопределившиеся ссылки уходят в
    # очередь перепроверки (retry_needed) и приходят сами, как только
    # колесо действительно запустится — см. retries.retry_expired_links,
    # окно NOTIFY_RETRY_WINDOW_MINUTES.
    if status not in ("active", ""):
        release_url_alert(url, now)
        log.info(
            "%s Пропускаю %s [%s]: %s (API BetBoom)",
            icon("warn"),
            url,
            label,
            SKIP_REASONS.get(status, status),
        )
        return None, status, True

    entry = make_wheel_entry(
        url=url,
        channel=channel,
        found_at=now.isoformat(timespec="seconds"),
        source=source,
        author=author,
        author_roles=author_roles,
        msg_id=msg_id,
        message_url=message_url,
        preview=preview,
        preview_html=preview_html,
        edited=is_edited,
        status=status,
        referral=referral,
        ends_at=ends_at,
        notified=False,
    )
    return entry, status, False


def classify_wheels(
    items: list[WheelEntry] | list[dict[str, Any]],
    feed_status_health: bool = False,
) -> tuple[list[Any], list[Any], int]:
    """Проверяет список колёс через BetBoom API параллельно.

    Использует ThreadPoolExecutor с ACTIVE_CHECK_CONCURRENCY потоками.
    Кэширует expired-статусы на EXPIRED_CACHE_TTL_SECONDS.
    feed_status_health=False (по умолчанию) исключает результат из счётчика
    «проверка ослепла» (см. _note_status_health): /active — массовый обход
    колёс за сутки, и его сбои не должны говорить за рабочие потоки
    (parser, twitch).
    Возвращает кортеж (active_items, soon_items, unknown_count):
      - active_items  — колёса со статусом active (в исходном порядке);
      - soon_items    — колёса, розыгрыш которых ещё не начался (soon);
      - unknown_count — количество колёс с неопределённым статусом.
    """
    _prune_expired_cache()

    with ThreadLocalSession(session_factory=build_session) as worker_session:
        results: list[tuple[int, str]] = []  # (original_index, status)
        lock = threading.Lock()

        def check(index: int, item: dict[str, Any]) -> None:
            url = normalize_url(str(item.get("url", "")))
            if not url:
                with lock:
                    results.append((index, "unknown"))
                return
            if _is_cached_expired(url):
                log.info(
                    "active-check [cache]: %s → expired (кэш %sс)",
                    url,
                    EXPIRED_CACHE_TTL_SECONDS,
                )
                with lock:
                    results.append((index, "expired"))
                return
            try:
                info = fetch_wheel_info(url, worker_session())
            except WheelPageMissing:
                info = None
                status = "missing"
            else:
                status = "unknown" if info is None else api_info_to_status(info)
            if feed_status_health:
                _note_status_health(status)
            # Реф-флаг и дедлайн обновляются по свежему info: старые записи
            # (до появления этих полей) получают их прямо при /active.
            if not item.get("referral") and is_referral_wheel(url, info):
                item["referral"] = True
            ends_at = wheel_ends_at(info)
            if ends_at:
                item["ends_at"] = ends_at
            log.info("active-check [api]: %s → %s", url, status)
            if status == "expired":
                _cache_expired(url)
            with lock:
                results.append((index, status))

        with ThreadPoolExecutor(max_workers=ACTIVE_CHECK_CONCURRENCY) as pool:
            list(pool.map(lambda args: check(*args), enumerate(items)))

    results.sort(key=lambda pair: pair[0])
    active_items = [items[i] for i, status in results if status == "active"]
    soon_items = [items[i] for i, status in results if status == "soon"]
    unknown_count = sum(1 for _, status in results if status == "unknown")
    return active_items, soon_items, unknown_count
