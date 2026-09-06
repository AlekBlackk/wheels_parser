"""Перебор слагов колёс: находит колесо до того, как стример его выложит.

Адреса колёс у стримера идут серией — `zonertg4`, `zonertg5`, … /
`LOLLY08`, `LOLLY09`, … Разведка по betboom.ru показала три вещи, на
которых держится весь модуль:

    1. страница колеса существует ДО публикации ссылки: свежесозданное,
       но не запущенное колесо отдаёт 200 и статус 'soon';
    2. несуществующий слаг честно отдаёт 404 — сигнал чистый, страница
       не генерируется на любой адрес вида «имя+цифры»;
    3. регистр значим: `LOLLY08` отдаёт 200, `lolly08` — 404.

Практическая ценность в том, что перебор находит и уже идущие колёса,
которые парсер прошляпил: на момент разведки у серии `zonertg`
существовали адреса 4…11, а в базе находок было три из них.

Серии не задаются руками: рубеж берётся из истории находок (что уже
нашли Telegram и Twitch) и из собственного файла состояния — сканер
подхватывает нового стримера сам, как только его первое колесо попало
в базу обычным путём.

Осторожность здесь не формальность: перебор по чужому адресному
пространству — ровно то, что фильтры вроде Cloudflare ловят по частоте
404, а бан по IP убьёт весь парсер, а не только сканер. Отсюда пауза
между запросами, остановка на первом 404, суточный потолок запросов и
немедленное молчание при 403/429.

Поток `predictive` работает под runtime.supervise, со своей сессией
(requests.Session не потокобезопасна — см. net.py). Находки уходят
parser-потоку очередью PREDICTIVE_NEW_ENTRIES: писать в базу из чужого
потока не запрещено, но так запись остаётся в одном месте и попадает
в окно кулдауна текущего цикла — как у twitch.TWITCH_NEW_ENTRIES.
"""

from __future__ import annotations

import queue
import time
from datetime import timedelta
from typing import Any

import requests

from . import db
from .alerts import claim_url_alert, mark_url_alert
from .betboom import precheck_wheel
from .config import (
    PREDICTIVE_BLOCK_COOLDOWN_MINUTES,
    PREDICTIVE_DAILY_BUDGET,
    PREDICTIVE_LOOKAHEAD,
    PREDICTIVE_MAX_EMPTY_SCANS,
    PREDICTIVE_PREFIX_RE,
    PREDICTIVE_REQUEST_DELAY_SECONDS,
    PREDICTIVE_SCAN_INTERVAL,
    REALERT_COOLDOWN_MINUTES,
    REQUEST_TIMEOUT,
    SLUG_SERIES_RE,
    icon,
)
from .db import WheelEntry, make_wheel_entry
from .logging_setup import log
from .net import build_session
from .runtime import STOP_EVENT
from .storage import (
    load_retired_series,
    load_streamer_frontier,
    save_retired_series,
    save_streamer_frontier,
)
from .telegram_api import send_service_notification, send_telegram_notification
from .timeutils import now_msk, today_msk
from .urls import normalize_url

# Серии, завершённые после PREDICTIVE_MAX_EMPTY_SCANS пустых проверок: prefix -> index.
# Предотвращает их повторное добавление из истории находок при том же или меньшем индексе.
# Переживает перезапуск сервиса благодаря сохранению в retired_streamers.json.
_RETIRED_SERIES: dict[str, int] = load_retired_series()

# Находки сканера: уведомления по ним уже отправлены, parser-поток
# забирает записи в начале цикла и пишет в базу (см. модульную докстроку).
PREDICTIVE_NEW_ENTRIES: queue.Queue[WheelEntry] = queue.Queue(maxsize=1000)

WHEEL_URL_TEMPLATE = "https://betboom.ru/freestream/{slug}"

# Исход проверки одного адреса.
MISSING = "missing"  # 404 — адреса нет, серия закончилась
BLOCKED = "blocked"  # 403/429 — нас притормаживают, немедленно замолкаем
FOUND = "found"      # 200 — адрес существует, статус смотрели через API
ERROR = "error"      # сеть/прочее — этот цикл для серии закончен


def split_slug(slug: str) -> tuple[str, int, int] | None:
    """Разбирает слаг серии на (префикс, индекс, ширина числа).

    Ширина обязательна для обратной сборки: `LOLLY08` дополнен нулём до
    двух знаков, а `zonertg4` — нет, и `LOLLY8` вместо `LOLLY08` отдал бы
    404. Слаг без числового хвоста (`aunkereref`) серией не считается.
    """
    match = SLUG_SERIES_RE.match(slug)
    if match is None:
        return None
    digits = match.group("index")
    return match.group("prefix"), int(digits), len(digits)


def build_slug(prefix: str, index: int, width: int) -> str:
    return f"{prefix}{index:0{width}d}"


def frontier_from_history(cutoff_days: int = 120) -> dict[str, dict[str, Any]]:
    """Рубеж серий по истории находок: что уже нашли Telegram и Twitch.

    Благодаря этому список серий не нужно вести руками — новый стример
    попадает в перебор сам, как только его первое колесо найдено обычным
    путём. Окно ограничено: серия, о которой год ничего не слышно, скорее
    закрыта, и перебирать её каждый цикл незачем.
    """
    frontier: dict[str, dict[str, Any]] = {}
    try:
        rows = db.wheels_since(now_msk() - timedelta(days=cutoff_days))
    except Exception:
        # База — не критичный для сканера источник: без неё он отработает
        # по собственному файлу состояния.
        log.exception("%s Сканер: не удалось прочитать историю находок", icon("warn"))
        return frontier
    for row in rows:
        url = normalize_url(str(row.get("url", "")))
        if not url:
            continue
        parsed = split_slug(url.rsplit("/", 1)[-1])
        if parsed is None:
            continue
        prefix, index, width = parsed
        if not PREDICTIVE_PREFIX_RE.match(prefix):
            continue
        known = frontier.get(prefix)
        if known is None or index > known["index"]:
            frontier[prefix] = {"index": index, "width": width, "pending": []}
    return frontier


def merge_frontiers(
    stored: dict[str, dict[str, Any]], history: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Объединяет рубеж из файла и из истории, беря больший индекс.

    Источники дополняют друг друга: в истории есть серии, которых сканер
    ещё не видел, а в файле — адреса, до которых он дошёл сам (давно
    завершившиеся колёса в базу не попадают, помнить их больше негде).
    """
    merged = {prefix: dict(info) for prefix, info in stored.items()}
    for prefix, info in history.items():
        if prefix in _RETIRED_SERIES:
            if info["index"] <= _RETIRED_SERIES[prefix]:
                continue
            _RETIRED_SERIES.pop(prefix, None)
            save_retired_series(_RETIRED_SERIES)
        known = merged.get(prefix)
        if known is None:
            merged[prefix] = dict(info)
        elif info["index"] > known["index"]:
            # Индекс берём больший, а список ожидающих старта сохраняем:
            # история о нём ничего не знает, но эти адреса всё ещё ждут.
            merged[prefix] = {**info, "pending": known.get("pending", [])}
    return merged


class Budget:
    """Суточный потолок запросов сканера (по МСК, как остальные счётчики)."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.day = today_msk()
        self.used = 0

    def take(self) -> bool:
        """Списывает один запрос. False — на сегодня лимит исчерпан."""
        today = today_msk()
        if today != self.day:
            self.day = today
            self.used = 0
        if self.used >= self.limit:
            return False
        self.used += 1
        return True


def probe_slug(
    slug: str, session: requests.Session
) -> tuple[str, str, bool, str]:
    """Проверяет один адрес: (исход, статус колеса, реф-флаг, дедлайн).

    Сначала обычный GET страницы — он и отвечает на вопрос «существует ли
    адрес» (404), и позволяет увидеть 403/429, по которым сканер обязан
    замолчать. Только для существующего адреса идёт precheck_wheel: он
    тянет ту же страницу второй раз, и это осознанный размен — попаданий
    единицы в сутки, а лезть в кэш подписи betboom.py ради одного запроса
    значило бы завязать сканер на его внутренности.
    """
    url = WHEEL_URL_TEMPLATE.format(slug=slug)
    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=False)
    except requests.RequestException as error:
        error_response = getattr(error, "response", None)
        status_code = getattr(error_response, "status_code", None)
        if status_code in (403, 429):
            return BLOCKED, "", False, ""
        log.debug("Сканер: %s — ошибка запроса: %s", url, error)
        return ERROR, "", False, ""
    if response.status_code in (403, 429):
        return BLOCKED, "", False, ""
    if response.status_code != 200:
        return MISSING, "", False, ""
    # feed_status_health=False: сбои и 404 для сканера — норма (он намеренно
    # ходит по чужому адресному пространству), и его результаты не должны
    # выдавать поломку API там, где её нет (см. betboom._note_status_health).
    status, referral, ends_at = precheck_wheel(
        url, session, use_cache=False, feed_status_health=False
    )
    return FOUND, status, referral, ends_at


def notify_found_wheel(
    prefix: str, slug: str, status: str, referral: bool, ends_at: str
) -> None:
    """Уведомление о колесе, найденном перебором, и запись в историю."""
    url = normalize_url(WHEEL_URL_TEMPLATE.format(slug=slug))
    now = now_msk()
    if not claim_url_alert(url, now):
        log.info(
            "%s Сканер: пропускаю %s — недавно уже оповещали (кулдаун %s мин)",
            icon("bell"),
            url,
            REALERT_COOLDOWN_MINUTES,
        )
        return
    entry = make_wheel_entry(
        url=url,
        found_at=now.isoformat(timespec="seconds"),
        channel=prefix,
        source="predictive",
        msg_id="",
        message_url=url,
        preview=f"Найдено перебором серии «{prefix}»",
        edited=False,
        status=status,
        referral=referral,
        ends_at=ends_at,
        notified=False,
    )
    # Помечаем ДО отправки: даже при сбое уведомления повторной рассылки
    # того же колеса в течение кулдауна не будет (как в twitch.py).
    mark_url_alert(url, now)
    try:
        entry["notified"] = send_telegram_notification(entry, _session())
    except Exception:
        log.exception("%s Сканер: не удалось отправить уведомление о %s", icon("warn"), url)
        entry["notified"] = False
    try:
        PREDICTIVE_NEW_ENTRIES.put_nowait(entry)
    except queue.Full:
        # Уведомление ушло, но запись в историю потеряна — parser не читает очередь.
        log.error(
            "%s Очередь находок сканера переполнена — parser не читает; "
            "находка %s не попадёт в историю",
            icon("warn"),
            url,
        )
    log.info(
        "%s Колесо найдено перебором: %s (серия %s)",
        icon("link"),
        url,
        prefix,
        extra={"highlight": True},
    )


# Сессия потока сканера: своя, потому что requests.Session не
# потокобезопасна (см. net.py). Создаётся лениво — модуль импортируется
# и при выключенном сканере.
PREDICTIVE_SESSION: requests.Session | None = None


def _session() -> requests.Session:
    global PREDICTIVE_SESSION
    if PREDICTIVE_SESSION is None:
        # 429 намеренно исключён из status_forcelist: при получении 429 сканер
        # обязан сразу зафиксировать BLOCKED и уйти в кулдаун
        # PREDICTIVE_BLOCK_COOLDOWN_MINUTES, а не повторять запросы через Retry
        # и падать в RetryError / ERROR (см. probe_slug).
        PREDICTIVE_SESSION = build_session(status_forcelist=(500, 502, 503, 504))
    return PREDICTIVE_SESSION


def _pause_between_requests() -> None:
    STOP_EVENT.wait(PREDICTIVE_REQUEST_DELAY_SECONDS)


# Потолок списка ожидающих старта адресов одной серии. Не настройка, а
# страховка: список пополняется только реально существующими адресами со
# статусом 'soon', но колесо, созданное и заброшенное навсегда, осталось
# бы в нём вечно и каждый цикл тратило запрос. Держим свежие — у них
# больше шансов запуститься.
MAX_PENDING_PER_SERIES = 5


def scan_series(
    prefix: str, info: dict[str, Any], budget: Budget, session: requests.Session
) -> tuple[dict[str, Any], bool]:
    """Проверяет одну серию. Возвращает (новый рубеж, не_заблокированы_ли).

    Сначала перепроверяются адреса, ждущие старта ('soon' — колесо создано,
    но не запущено): именно ради них сканер и нужен, уведомление уходит в
    тот же цикл, когда колесо стало активным. Ждущих может быть несколько
    сразу — на живом прогоне у серии zonertw таких оказалось два подряд,
    и хранить статус только последнего адреса значило бы потерять
    остальные. Затем перебор идёт вперёд до первого 404.
    """
    index, width = info["index"], info["width"]
    pending = sorted(set(info.get("pending", [])))
    updated = {"index": index, "width": width, "pending": list(pending)}
    found_any = False

    for waiting in pending:
        if STOP_EVENT.is_set() or not budget.take():
            return updated, True
        slug = build_slug(prefix, waiting, width)
        outcome, status, referral, ends_at = probe_slug(slug, session)
        if outcome == BLOCKED:
            return updated, False
        if outcome == FOUND:
            found_any = True
            if status != "soon":
                # Дождались: колесо либо запустилось, либо успело закончиться —
                # в обоих случаях ждать его больше незачем.
                updated["pending"] = [item for item in updated["pending"] if item != waiting]
                if status == "active":
                    notify_found_wheel(prefix, slug, status, referral, ends_at)
        _pause_between_requests()

    completed_normally = False
    for step in range(1, PREDICTIVE_LOOKAHEAD + 1):
        if STOP_EVENT.is_set():
            break
        if not budget.take():
            log.info(
                "%s Сканер: суточный лимит запросов исчерпан (%s)",
                icon("bell"),
                budget.limit,
            )
            break
        next_index = index + step
        slug = build_slug(prefix, next_index, width)
        outcome, status, referral, ends_at = probe_slug(slug, session)
        if outcome == BLOCKED:
            return updated, False
        if outcome in (MISSING, ERROR):
            # Реальные серии сплошные (проверено на zonertg4…11 и
            # LOLLY08…15), поэтому 404 — это конец серии, а не пропуск.
            completed_normally = (outcome == MISSING)
            break
        found_any = True
        completed_normally = True
        updated["index"] = next_index
        log.info("Сканер: %s существует, статус %s", slug, status)
        if status == "soon":
            updated["pending"] = sorted(
                set(updated["pending"]) | {next_index}
            )[-MAX_PENDING_PER_SERIES:]
        elif status == "active":
            notify_found_wheel(prefix, slug, status, referral, ends_at)
        _pause_between_requests()

    if not found_any and not updated.get("pending") and completed_normally:
        empty_scans = int(info.get("empty_scans", 0)) + 1
        if empty_scans > 0:
            updated["empty_scans"] = empty_scans

    return updated, True


def scan_once(budget: Budget, session: requests.Session) -> bool:
    """Один проход по всем сериям. False — нас заблокировали, надо замолчать."""
    stored = load_streamer_frontier()
    frontier = merge_frontiers(stored, frontier_from_history())
    if not frontier:
        log.info(
            "%s Сканер: серий пока нет — ждём первую находку с числовым слагом",
            icon("bell"),
        )
        return True
    allowed = True
    for prefix in sorted(frontier):
        if STOP_EVENT.is_set():
            break
        info = frontier[prefix]
        if info.get("empty_scans", 0) >= PREDICTIVE_MAX_EMPTY_SCANS:
            _RETIRED_SERIES[prefix] = info["index"]
            save_retired_series(_RETIRED_SERIES)
            del frontier[prefix]
            continue
        updated, allowed = scan_series(prefix, info, budget, session)
        if updated.get("empty_scans", 0) >= PREDICTIVE_MAX_EMPTY_SCANS:
            log.info(
                "Сканер: серия «%s» удалена после %d пустых проходов",
                prefix,
                updated["empty_scans"],
            )
            _RETIRED_SERIES[prefix] = updated["index"]
            save_retired_series(_RETIRED_SERIES)
            del frontier[prefix]
        else:
            frontier[prefix] = updated
        if not allowed:
            break
    save_streamer_frontier(frontier)
    return allowed


def _notify_blocked() -> None:
    log.error(
        "%s Сканер: betboom.ru ответил 403/429 — перебор остановлен на %s мин",
        icon("warn"),
        PREDICTIVE_BLOCK_COOLDOWN_MINUTES,
    )
    send_service_notification(
        f"{icon('warn')} Перебор слагов остановлен: betboom.ru ответил "
        f"403/429. Пауза {PREDICTIVE_BLOCK_COOLDOWN_MINUTES} мин. Если это "
        "повторяется, уменьшите PREDICTIVE_LOOKAHEAD или выключите сканер "
        "(PREDICTIVE_ENABLED=false) — бан по IP заденет весь парсер.",
        _session(),
    )


def predictive_loop() -> None:
    """Поток сканера: цикл перебора с паузой PREDICTIVE_SCAN_INTERVAL."""
    budget = Budget(PREDICTIVE_DAILY_BUDGET)
    session = _session()
    while not STOP_EVENT.is_set():
        started = time.monotonic()
        allowed = scan_once(budget, session)
        if not allowed:
            _notify_blocked()
            STOP_EVENT.wait(PREDICTIVE_BLOCK_COOLDOWN_MINUTES * 60)
            continue
        elapsed = time.monotonic() - started
        STOP_EVENT.wait(max(5.0, PREDICTIVE_SCAN_INTERVAL - elapsed))


def drain_predictive_entries() -> list[WheelEntry]:
    """Забирает находки сканера (вызывается parser-потоком)."""
    entries: list[WheelEntry] = []
    while True:
        try:
            entries.append(PREDICTIVE_NEW_ENTRIES.get_nowait())
        except queue.Empty:
            return entries


__all__ = [
    "PREDICTIVE_NEW_ENTRIES",
    "Budget",
    "build_slug",
    "drain_predictive_entries",
    "frontier_from_history",
    "merge_frontiers",
    "predictive_loop",
    "probe_slug",
    "scan_once",
    "scan_series",
    "split_slug",
]
