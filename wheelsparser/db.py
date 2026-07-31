"""История находок в SQLite (data/wheels.db).

До версии 3.x история жила в freebets.json: весь файл переписывался в конце
каждого цикла с находкой и целиком перечитывался на каждую команду бота.
При MAX_RESULTS=5000 это терпимо по скорости, но делает невозможной любую
аналитику («какой канал даёт колёса чаще») — данные приходилось фильтровать
в Python. Теперь новая находка — это один INSERT, а /status, /wheels,
/active и /top — запросы с WHERE и GROUP BY.

Потоки (parser, bot, twitch-worker, active-api) работают со своими
соединениями: sqlite3.Connection не переносится между потоками. Режим
WAL позволяет читать во время записи, busy_timeout переживает
одновременную запись parser- и twitch-потоков.

Записи истории — обычные dict'ы, как и раньше: остальной код работает с
ними через .get(), формат не изменился. Поля-списки (keywords,
author_roles) хранятся как JSON, флаги — как 0/1.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

from .config import DB_FILE, MAX_RESULTS, OUTPUT_FILE
from .logging_setup import log
from .timeutils import now_msk, parse_found_at

# Ждём освобождения базы вместо немедленного «database is locked»:
# запись parser-потока может совпасть с записью twitch-worker.
BUSY_TIMEOUT_SECONDS = 10

# Текстовые поля записи. found_at, channel и source читаются всегда,
# остальные попадают в dict, только если непустые — иначе записи по
# ключевым словам получили бы url="" и стали бы считаться колёсами.
TEXT_FIELDS = (
    "url",
    "found_at",
    "channel",
    "source",
    "author",
    "msg_id",
    "message_url",
    "preview",
    "preview_html",
    "status",
    "ends_at",
)
ALWAYS_READ = ("found_at", "channel", "source")
JSON_FIELDS = ("keywords", "author_roles")
FLAG_FIELDS = ("edited", "referral", "notified", "delivery_unknown")
COLUMNS = TEXT_FIELDS + JSON_FIELDS + FLAG_FIELDS

SCHEMA = """
CREATE TABLE IF NOT EXISTS wheels (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    url              TEXT    NOT NULL DEFAULT '',
    found_at         TEXT    NOT NULL DEFAULT '',
    channel          TEXT    NOT NULL DEFAULT '',
    source           TEXT    NOT NULL DEFAULT 'telegram',
    author           TEXT    NOT NULL DEFAULT '',
    msg_id           TEXT    NOT NULL DEFAULT '',
    message_url      TEXT    NOT NULL DEFAULT '',
    preview          TEXT    NOT NULL DEFAULT '',
    preview_html     TEXT    NOT NULL DEFAULT '',
    status           TEXT    NOT NULL DEFAULT '',
    ends_at          TEXT    NOT NULL DEFAULT '',
    keywords         TEXT    NOT NULL DEFAULT '',
    author_roles     TEXT    NOT NULL DEFAULT '',
    edited           INTEGER NOT NULL DEFAULT 0,
    referral         INTEGER NOT NULL DEFAULT 0,
    notified         INTEGER NOT NULL DEFAULT 0,
    delivery_unknown INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_wheels_found_at ON wheels(found_at);
CREATE INDEX IF NOT EXISTS idx_wheels_channel ON wheels(source, channel);
-- Частичный индекс под ретрай: недоставленных записей единицы, полный
-- индекс по notified здесь бесполезен.
CREATE INDEX IF NOT EXISTS idx_wheels_pending ON wheels(found_at)
    WHERE notified = 0 AND delivery_unknown = 0;
"""

INSERT_SQL = (
    f"INSERT INTO wheels ({', '.join(COLUMNS)}) "
    f"VALUES ({', '.join('?' * len(COLUMNS))})"
)

# Соединение на поток: sqlite3.Connection принадлежит потоку, который его
# открыл. Путь запоминается вместе с соединением — тесты подменяют DB_FILE,
# и закэшированное соединение к прошлой базе нужно закрыть.
_local = threading.local()


class WheelStats(NamedTuple):
    """Сводка для /status: всего колёс, за сегодня и последнее."""

    total: int
    today: int
    last: dict[str, Any] | None


class ChannelCount(NamedTuple):
    """Строка рейтинга каналов для /top."""

    source: str
    channel: str
    wheels: int


# ----------------------------------------------------------------------------
# Соединение
# ----------------------------------------------------------------------------

def connection() -> sqlite3.Connection:
    """Соединение текущего потока (открывается при первом обращении)."""
    path = Path(DB_FILE)
    existing = getattr(_local, "conn", None)
    if existing is not None:
        if getattr(_local, "path", None) == path:
            return existing
        existing.close()
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    # WAL: читатели (bot, active-api) не блокируют писателей и наоборот.
    conn.execute("PRAGMA journal_mode=WAL")
    # NORMAL вместо FULL: теряется максимум последний коммит при отказе
    # питания, зато нет fsync на каждую находку. Для истории находок
    # такой размен оправдан.
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_SECONDS * 1000}")
    _local.conn = conn
    _local.path = path
    return conn


def close_connection() -> None:
    """Закрывает соединение текущего потока (нужно тестам и на выходе)."""
    existing = getattr(_local, "conn", None)
    if existing is not None:
        existing.close()
    _local.conn = None
    _local.path = None


# ----------------------------------------------------------------------------
# Преобразование запись <-> строка
# ----------------------------------------------------------------------------

def _normalize_found_at(value: Any) -> str:
    """Приводит метку времени к единому виду ISO+03:00.

    В базе все found_at должны быть в одном формате: окна (/wheels,
    /active, ретрай, кулдаун) отбираются сравнением строк, а старые версии
    писали метки и без смещения. Неразбираемое значение сохраняется как
    есть — такие записи всё равно отсеиваются parse_found_at при чтении.
    """
    moment = parse_found_at(value)
    if moment is None:
        return str(value or "")
    return moment.isoformat(timespec="seconds")


def _dump_list(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, str):
        value = [value]
    return json.dumps(list(value), ensure_ascii=False)


def _load_list(value: Any) -> list[str]:
    if not value:
        return []
    try:
        loaded = json.loads(value)
    except (TypeError, ValueError):
        return []
    return [str(item) for item in loaded] if isinstance(loaded, list) else []


def entry_to_row(entry: dict[str, Any]) -> tuple[Any, ...]:
    values: list[Any] = []
    for field in TEXT_FIELDS:
        if field == "found_at":
            values.append(_normalize_found_at(entry.get("found_at")))
        elif field == "source":
            values.append(str(entry.get("source") or "telegram"))
        else:
            values.append(str(entry.get(field) or ""))
    values.extend(_dump_list(entry.get(field)) for field in JSON_FIELDS)
    values.extend(int(bool(entry.get(field))) for field in FLAG_FIELDS)
    return tuple(values)


def row_to_entry(row: sqlite3.Row) -> dict[str, Any]:
    entry: dict[str, Any] = {"id": row["id"]}
    for field in TEXT_FIELDS:
        value = row[field]
        if value or field in ALWAYS_READ:
            entry[field] = value
    for field in JSON_FIELDS:
        value = _load_list(row[field])
        if value:
            entry[field] = value
    for field in FLAG_FIELDS:
        entry[field] = bool(row[field])
    return entry


# ----------------------------------------------------------------------------
# Инициализация и перенос freebets.json
# ----------------------------------------------------------------------------

def init_db() -> None:
    """Создаёт схему и один раз переносит историю из freebets.json.

    Вызывается из app.main() после ensure_data_dir(): к этому моменту
    старый freebets.json из корня репозитория уже лежит в data/.
    """
    conn = connection()
    with conn:
        conn.executescript(SCHEMA)
    _migrate_legacy_json(conn)


def _migrate_legacy_json(conn: sqlite3.Connection) -> None:
    """Переносит freebets.json в базу и убирает файл с пути.

    Перенос идёт только в пустую таблицу: если базу уже наполнили, а
    рядом оказался старый файл (например, восстановили бэкап каталога),
    повторный импорт продублировал бы всю историю.
    """
    path = Path(OUTPUT_FILE)
    if not path.exists():
        return
    if conn.execute("SELECT 1 FROM wheels LIMIT 1").fetchone() is not None:
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        log.error(
            "Не удалось перенести %s в базу (%s) — файл переименован в %s, "
            "история начинается с нуля",
            path.name,
            error,
            path.name + ".broken",
        )
        _rename_legacy(path, ".json.broken")
        return
    entries = [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []
    if entries:
        with conn:
            conn.executemany(INSERT_SQL, [entry_to_row(entry) for entry in entries])
    log.info(
        "История перенесена из %s в %s: записей %s",
        path.name,
        Path(DB_FILE).name,
        len(entries),
    )
    _rename_legacy(path, ".json.migrated")


def _rename_legacy(path: Path, suffix: str) -> None:
    """Убирает старый файл с пути, не удаляя данные пользователя."""
    try:
        path.replace(path.with_suffix(suffix))
    except OSError as error:
        log.warning("Не удалось переименовать %s: %s", path.name, error)


# ----------------------------------------------------------------------------
# Запись
# ----------------------------------------------------------------------------

def insert_entries(entries: list[dict[str, Any]]) -> None:
    """Сохраняет находки и проставляет им id (нужен для update_delivery)."""
    if not entries:
        return
    conn = connection()
    with conn:
        for entry in entries:
            cursor = conn.execute(INSERT_SQL, entry_to_row(entry))
            entry["id"] = cursor.lastrowid


def update_delivery(entry: dict[str, Any]) -> None:
    """Сохраняет результат повторной отправки уведомления.

    Записи без id (созданные не из базы) молча пропускаются: обновлять
    в базе нечего, и это не ошибка.
    """
    entry_id = entry.get("id")
    if not entry_id:
        return
    conn = connection()
    with conn:
        conn.execute(
            "UPDATE wheels SET notified = ?, delivery_unknown = ? WHERE id = ?",
            (
                int(bool(entry.get("notified"))),
                int(bool(entry.get("delivery_unknown"))),
                entry_id,
            ),
        )


def prune(limit: int = MAX_RESULTS) -> int:
    """Оставляет только limit последних записей. Возвращает число удалённых."""
    conn = connection()
    with conn:
        cursor = conn.execute(
            "DELETE FROM wheels WHERE id NOT IN "
            "(SELECT id FROM wheels ORDER BY id DESC LIMIT ?)",
            (limit,),
        )
    return cursor.rowcount


# ----------------------------------------------------------------------------
# Чтение
# ----------------------------------------------------------------------------

def _cutoff_key(cutoff: datetime) -> str:
    return cutoff.isoformat(timespec="seconds")


def entries_since(cutoff: datetime) -> list[dict[str, Any]]:
    """Все записи не старше cutoff, от старых к свежим."""
    rows = connection().execute(
        "SELECT * FROM wheels WHERE found_at >= ? ORDER BY found_at, id",
        (_cutoff_key(cutoff),),
    )
    return [row_to_entry(row) for row in rows]


def wheels_since(cutoff: datetime) -> list[dict[str, Any]]:
    """Находки со ссылкой не старше cutoff, от старых к свежим.

    Записи по ключевым словам (url пуст) лежат в той же таблице ради
    ретрая уведомлений, но колёсами не являются.
    """
    rows = connection().execute(
        "SELECT * FROM wheels WHERE url != '' AND found_at >= ? "
        "ORDER BY found_at, id",
        (_cutoff_key(cutoff),),
    )
    return [row_to_entry(row) for row in rows]


def pending_retry(cutoff: datetime, limit: int) -> list[dict[str, Any]]:
    """Недоставленные уведомления не старше cutoff — кандидаты на повтор.

    delivery_unknown исключены: sendMessage не идемпотентен, и повтор
    отправки, статус которой неизвестен, продублировал бы сообщение.
    Записи без url и без keywords отсеиваются здесь же, а не у
    вызывающего: отправлять по ним нечего, и в пределах limit они
    вытесняли бы настоящие находки.
    """
    rows = connection().execute(
        "SELECT * FROM wheels WHERE notified = 0 AND delivery_unknown = 0 "
        "AND (url != '' OR keywords != '') "
        "AND found_at >= ? ORDER BY found_at, id LIMIT ?",
        (_cutoff_key(cutoff), limit),
    )
    return [row_to_entry(row) for row in rows]


def total_wheels() -> int:
    row = connection().execute(
        "SELECT COUNT(*) AS total FROM wheels WHERE url != ''"
    ).fetchone()
    return int(row["total"])


def wheel_stats() -> WheelStats:
    """Сводка для /status. «Сегодня» — сутки по МСК, как и found_at."""
    conn = connection()
    total = int(
        conn.execute("SELECT COUNT(*) AS n FROM wheels WHERE url != ''")
        .fetchone()["n"]
    )
    # Метки хранятся в МСК, поэтому «сегодня» — это префикс даты.
    today = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM wheels WHERE url != '' AND found_at LIKE ?",
            (now_msk().strftime("%Y-%m-%d") + "%",),
        ).fetchone()["n"]
    )
    last_row = conn.execute(
        "SELECT * FROM wheels WHERE url != '' ORDER BY found_at DESC, id DESC LIMIT 1"
    ).fetchone()
    return WheelStats(total, today, row_to_entry(last_row) if last_row else None)


def channel_counts(cutoff: datetime) -> list[ChannelCount]:
    """Рейтинг каналов по числу найденных колёс, от частых к редким."""
    rows = connection().execute(
        "SELECT source, channel, COUNT(*) AS wheels FROM wheels "
        "WHERE url != '' AND found_at >= ? "
        "GROUP BY source, channel ORDER BY wheels DESC, channel",
        (_cutoff_key(cutoff),),
    )
    return [
        ChannelCount(row["source"], row["channel"], int(row["wheels"]))
        for row in rows
    ]
