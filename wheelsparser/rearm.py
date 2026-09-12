"""Наблюдатель за известными адресами: ловит перезапуск старого колеса.

Перебор слагов (predictive.py) отвечает на вопрос «какой адрес появится
следующим». На проде это меньшая часть работы: из 816 колесо-дней за 58
суток 595 (73%) пришлись на адрес, который уже был в истории, и только
221 — на адрес, увиденный впервые. Слаг у стримера постоянный
(`aunkereref` отстрелял 29 разных дней, `MAGER01` — 26), а розыгрыш на
нём запускается заново.

Отсюда второй механизм: обходить известные адреса и замечать момент,
когда на адресе стартовал новый розыгрыш. Детектор дешёвый — страница
колеса отдаёт `props.pageProps.uid`, постоянный идентификатор действия.
Сменился uid — значит это другое действие, и только тогда имеет смысл
платить за запрос к API. Один запрос на адрес вместо трёх.

Наблюдаемые адреса берутся из истории находок, а не из списка руками:
новый стример подхватывается сам, как только его колесо нашли обычным
путём. К ним добавляется голый адрес каждой серии (`zonertg` при
известных `zonertg3…13`) — он существует и переиспользуется, но в
историю не попадает, пока кто-нибудь не выложит ссылку именно на него.
"""

from __future__ import annotations

import collections
import json
import queue
from datetime import datetime, timedelta
from typing import Any

import requests

from . import db
from .alerts import claim_url_alert, mark_url_alert
from .betboom import NEXT_DATA_RE, precheck_wheel
from .config import (
    PREDICTIVE_REQUEST_DELAY_SECONDS,
    REALERT_COOLDOWN_MINUTES,
    REARM_MAX_ADDRESSES,
    REARM_WINDOW_DAYS,
    REQUEST_TIMEOUT,
    icon,
)
from .db import WheelEntry, make_wheel_entry
from .logging_setup import log
from .runtime import STOP_EVENT
from .storage import load_streamer_frontier, load_watched_uids, save_watched_uids
from .telegram_api import send_telegram_notification
from .timeutils import now_msk, parse_msk
from .urls import normalize_url

WHEEL_URL_TEMPLATE = "https://betboom.ru/freestream/{slug}"


def _pause_between_requests() -> None:
    """Пауза между запросами: перебор чужих адресов не должен выглядеть
    как долбёжка — бан по IP убил бы весь парсер, а не только наблюдателя."""
    STOP_EVENT.wait(PREDICTIVE_REQUEST_DELAY_SECONDS)

# Исход проверки одного адреса. Общие с predictive.py: механизмы разные,
# но классы ответа betboom.ru одни и те же.
MISSING = "missing"  # 404 — адреса нет
BLOCKED = "blocked"  # 403/429 — нас притормаживают, немедленно замолкаем
FOUND = "found"      # 200 — страница есть, uid прочитан
ERROR = "error"      # сеть или неразборная страница

# Находки наблюдателя: уведомления уже отправлены, parser-поток забирает
# записи в начале цикла и пишет в базу (как у predictive и twitch).
REARM_NEW_ENTRIES: queue.Queue[WheelEntry] = queue.Queue(maxsize=1000)


def rank_known_addresses(window_days: int = REARM_WINDOW_DAYS) -> list[str]:
    """Известные адреса, от самых «горячих» к холодным.

    Горячесть — число РАЗНЫХ дней, в которые адрес отметился, а не число
    записей: одно болтливое колесо с десятком повторов за вечер иначе
    вытеснило бы из бюджета всех остальных. При равенстве вперёд идёт
    тот, кого видели позже.
    """
    cutoff = now_msk() - timedelta(days=window_days)
    days: dict[str, set[str]] = collections.defaultdict(set)
    last_seen: dict[str, str] = {}
    for entry in db.wheels_since(cutoff):
        url = normalize_url(str(entry.get("url", "")))
        if not url:
            continue
        found_at = str(entry.get("found_at", ""))
        days[url].add(found_at[:10])
        if found_at > last_seen.get(url, ""):
            last_seen[url] = found_at
    return sorted(days, key=lambda url: (-len(days[url]), _sort_key(last_seen[url])))


def _sort_key(found_at: str) -> float:
    """Отрицательная метка времени: свежие — вперёд."""
    moment = parse_msk(found_at)
    return -moment.timestamp() if moment is not None else 0.0


def series_base_addresses(frontier: dict[str, dict[str, object]]) -> list[str]:
    """Голые адреса серий: `zonertg` при известных `zonertg3…13`."""
    return [
        normalize_url(WHEEL_URL_TEMPLATE.format(slug=prefix))
        for prefix in sorted(frontier)
    ]


def watch_targets(
    frontier: dict[str, dict[str, object]], limit: int = REARM_MAX_ADDRESSES
) -> list[str]:
    """Что обходим в этот проход, в порядке приоритета.

    Голым адресам серий отводится не больше половины лимита. Их немного
    и они ничем другим не покрываются, но проверенные историей адреса
    приносят находки чаще, поэтому отдавать им весь бюджет нельзя.
    """
    known = rank_known_addresses()
    bases = [url for url in series_base_addresses(frontier) if url not in set(known)]
    targets = bases[: limit // 2]
    for url in known:
        if len(targets) >= limit:
            break
        targets.append(url)
    return targets


# Профиль суток по проду: из 816 колесо-дней за 58 суток 71% пришёлся на
# 14:00–21:00 МСК, 24% — на 10:00–13:00 и 22:00, и меньше 5% на остальные
# одиннадцать часов. Вес — во сколько раз час «дороже» самого пустого.
PEAK_HOURS = frozenset(range(14, 22))
DAY_HOURS = frozenset({10, 11, 12, 13, 22})
PEAK_WEIGHT = 9
DAY_WEIGHT = 5
NIGHT_WEIGHT = 1


def hour_weight(hour: int) -> int:
    """Во сколько раз этот час суток заслуживает больше запросов."""
    if hour in PEAK_HOURS:
        return PEAK_WEIGHT
    return DAY_WEIGHT if hour in DAY_HOURS else NIGHT_WEIGHT


_DAILY_WEIGHT = sum(hour_weight(hour) for hour in range(24))


def pass_allowance(daily_budget: int, scan_interval: int, moment: datetime) -> int:
    """Сколько запросов можно потратить в этот проход.

    Суточный потолок раскладывается по часам пропорционально весу часа,
    а внутри часа — поровну между проходами. Смысл не в экономии как
    таковой: прежний сканер тратил бюджет подряд от полуночи и замолкал
    в 14:02 — то есть был слеп ровно в те часы, когда колёса и запускают.

    Не меньше одного запроса даже ночью: ночные запуски редки, но
    полная слепота означала бы, что их не увидит никто.
    """
    passes_per_hour = max(1, 3600 // max(1, scan_interval))
    per_weight = daily_budget / _DAILY_WEIGHT / passes_per_hour
    return max(1, int(hour_weight(moment.hour) * per_weight))


def probe_action_uid(url: str, session: requests.Session) -> tuple[str, str]:
    """Идентификатор текущего розыгрыша на адресе: (исход, uid).

    Это весь дешёвый детектор. `props.pageProps.uid` со страницы колеса —
    постоянный идентификатор действия; пока он тот же, на адресе ничего
    не изменилось, и платить за запрос к API незачем.
    """
    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=False)
    except requests.RequestException as error:
        status_code = getattr(getattr(error, "response", None), "status_code", None)
        if status_code in (403, 429):
            return BLOCKED, ""
        log.debug("Наблюдатель: %s — ошибка запроса: %s", url, error)
        return ERROR, ""
    if response.status_code in (403, 429):
        return BLOCKED, ""
    if response.status_code == 404:
        return MISSING, ""
    if response.status_code != 200:
        return ERROR, ""
    match = NEXT_DATA_RE.search(response.text)
    if match is None:
        return ERROR, ""
    try:
        uid = json.loads(match.group(1))["props"]["pageProps"]["uid"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return ERROR, ""
    return (FOUND, str(uid)) if uid else (ERROR, "")


def notify_rearmed_wheel(
    url: str, status: str, referral: bool, ends_at: str, session: requests.Session
) -> None:
    """Уведомление о колесе, перезапущенном на известном адресе."""
    now = now_msk()
    if not claim_url_alert(url, now):
        log.info(
            "%s Наблюдатель: пропускаю %s — недавно уже оповещали (кулдаун %s мин)",
            icon("bell"),
            url,
            REALERT_COOLDOWN_MINUTES,
        )
        return
    slug = url.rsplit("/", 1)[-1]
    entry = make_wheel_entry(
        url=url,
        found_at=now.isoformat(timespec="seconds"),
        channel=slug,
        source="rearm",
        msg_id="",
        message_url=url,
        preview=f"Перезапуск колеса на адресе «{slug}»",
        edited=False,
        status=status,
        referral=referral,
        ends_at=ends_at,
        notified=False,
    )
    # Помечаем ДО отправки: при сбое уведомления повторной рассылки того же
    # колеса в течение кулдауна не будет (как в twitch.py и predictive.py).
    mark_url_alert(url, now)
    try:
        entry["notified"] = send_telegram_notification(entry, session)
    except Exception:
        log.exception("%s Наблюдатель: не удалось отправить уведомление о %s", icon("warn"), url)
        entry["notified"] = False
    try:
        REARM_NEW_ENTRIES.put_nowait(entry)
    except queue.Full:
        log.error(
            "%s Очередь находок наблюдателя переполнена — parser не читает; "
            "находка %s не попадёт в историю",
            icon("warn"),
            url,
        )
    log.info(
        "%s Колесо перезапущено: %s",
        icon("link"),
        url,
        extra={"highlight": True},
    )


def watch_address(
    url: str, known_uid: str | None, session: requests.Session
) -> tuple[str, str]:
    """Проверяет один известный адрес: (исход, актуальный uid).

    Успех здесь — «ничего не изменилось»: в подавляющем большинстве
    проверок uid тот же, и проверка стоит одного запроса. Смена uid
    означает новый розыгрыш — только тогда идём в API за статусом, и
    только подтверждённый `active` уходит в рассылку.

    Адрес, увиденный впервые (`known_uid is None`), молча запоминается:
    сравнивать не с чем, и считать это перезапуском нельзя — иначе первый
    же проход разослал бы всю историю разом.
    """
    outcome, uid = probe_action_uid(url, session)
    if outcome != FOUND or uid == known_uid:
        return outcome, uid
    if known_uid is None:
        return FOUND, uid
    status, referral, ends_at = precheck_wheel(
        url, session, use_cache=False, feed_status_health=False
    )
    if status == "active":
        notify_rearmed_wheel(url, status, referral, ends_at, session)
    return FOUND, uid


# Позиция в списке наблюдаемых адресов: проход берёт столько адресов,
# сколько разрешил планировщик, и следующий продолжает с этого места.
# Без неё бюджет уходил бы одним и тем же верхним адресам, а хвост списка
# не проверялся бы никогда. Живёт в памяти: после рестарта обход просто
# начинается сначала, терять тут нечего.
_CURSOR = 0


def reset_cursor() -> None:
    """Сбрасывает позицию обхода (нужно тестам и при смене списка)."""
    global _CURSOR
    _CURSOR = 0


def watch_once(budget: Any, session: requests.Session, allowance: int) -> bool:
    """Один проход наблюдателя. False — нас заблокировали, надо замолчать.

    Проверяет столько адресов, сколько разрешили планировщик и суточный
    бюджет, продолжая обход с прошлой позиции. Запомненные uid
    пересохраняются целиком по текущему списку адресов: выпавший из окна
    истории адрес уходит из файла вместе со своим uid.
    """
    global _CURSOR
    targets = watch_targets(load_streamer_frontier())
    if not targets:
        return True
    known = load_watched_uids()
    uids = {url: known[url] for url in targets if url in known}
    allowed = True
    for step in range(min(allowance, len(targets))):
        if STOP_EVENT.is_set() or not budget.take():
            break
        url = targets[(_CURSOR + step) % len(targets)]
        outcome, uid = watch_address(url, uids.get(url), session)
        if outcome == BLOCKED:
            allowed = False
            break
        if outcome == FOUND:
            uids[url] = uid
        elif outcome == MISSING:
            # Адреса нет — помнить его uid незачем; вернётся в историю,
            # если колесо по нему когда-нибудь снова появится.
            uids.pop(url, None)
        _pause_between_requests()
    _CURSOR = (_CURSOR + min(allowance, len(targets))) % len(targets)
    save_watched_uids(uids)
    return allowed


def drain_rearm_entries() -> list[WheelEntry]:
    """Забирает находки наблюдателя (вызывается parser-потоком)."""
    entries: list[WheelEntry] = []
    while True:
        try:
            entries.append(REARM_NEW_ENTRIES.get_nowait())
        except queue.Empty:
            return entries


__all__ = [
    "BLOCKED",
    "ERROR",
    "FOUND",
    "MISSING",
    "REARM_NEW_ENTRIES",
    "drain_rearm_entries",
    "notify_rearmed_wheel",
    "pass_allowance",
    "probe_action_uid",
    "rank_known_addresses",
    "reset_cursor",
    "series_base_addresses",
    "watch_address",
    "watch_once",
    "watch_targets",
]
