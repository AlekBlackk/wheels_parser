"""Состояние на диске: JSON-файлы парсера.

Всё пишется атомарно (запись во временный файл + replace), чтобы падение
процесса посреди записи не оставило обрезанный JSON.

Здесь живут только небольшие файлы, которые дёшево переписывать целиком:
обработанные сообщения, удалённые вручную колёса и offset бота. История
находок переехала в SQLite — см. db.py.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from . import registry
from .config import (
    BASE_DIR,
    BOT_STATE_FILE,
    DATA_DIR,
    LOG_FILE,
    MAX_SEEN_PER_CHANNEL,
    OUTPUT_FILE,
    PENDING_EXPIRED_FILE,
    REMOVED_WHEELS_FILE,
    SEEN_FILE,
)
from .logging_setup import log
from .timeutils import parse_msk, today_msk


def ensure_data_dir() -> list[str]:
    """Создаёт DATA_DIR и переносит туда файлы состояния из старых версий.

    До версии 2.x состояние лежало в корне репозитория. Без переноса
    обновившийся пользователь потерял бы seen_ids.json — и получил бы
    шквал «новых» уведомлений по всем старым постам. Возвращает имена
    перенесённых файлов (для лога).
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    targets = [OUTPUT_FILE, SEEN_FILE, BOT_STATE_FILE, REMOVED_WHEELS_FILE, LOG_FILE]
    # Бэкапы ротации лога (parser.log.1 ...) переносим вместе с логом.
    targets.extend(
        LOG_FILE.with_suffix(LOG_FILE.suffix + f".{index}") for index in range(1, 4)
    )
    for target in targets:
        legacy = BASE_DIR / target.name
        if legacy == target or target.exists() or not legacy.exists():
            continue
        try:
            legacy.replace(target)
            moved.append(target.name)
        except OSError as error:
            log.warning("Не удалось перенести %s в %s: %s", legacy.name, DATA_DIR, error)
    return moved


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


# ----------------------------------------------------------------------------
# Обработанные сообщения (seen_ids.json)
# ----------------------------------------------------------------------------

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
    for channel in registry.channels_snapshot():
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
    active = set(registry.channels_snapshot())
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


# ----------------------------------------------------------------------------
# Удалённые вручную колёса (/removewheel)
# ----------------------------------------------------------------------------
# url -> "YYYY-MM-DD" (день удаления по МСК). Удалённое колесо скрывается из
# /active до конца суток; сам /active и так сбрасывается в 00:00 МСК, поэтому
# записи прошлых дней вычищаются автоматически. Файл переживает рестарт:
# удалённое колесо не «воскресает» в /active после перезапуска парсера.

REMOVED_WHEELS_LOCK = threading.Lock()


def load_removed_wheels() -> dict[str, str]:
    data = read_json(REMOVED_WHEELS_FILE, {})
    if not isinstance(data, dict):
        return {}
    return {
        str(url): str(day)
        for url, day in data.items()
        if isinstance(url, str) and isinstance(day, str)
    }


# Ленивая загрузка (а не при импорте): файл читается после ensure_data_dir(),
# иначе перенос removed_wheels.json из корня прошёл бы мимо уже загруженного
# пустого словаря и удалённые колёса «воскресли» бы после обновления.
REMOVED_WHEELS: dict[str, str] | None = None


def _removed_wheels_locked() -> dict[str, str]:
    """Возвращает словарь, загружая при первом обращении. Только под локом."""
    global REMOVED_WHEELS
    if REMOVED_WHEELS is None:
        REMOVED_WHEELS = load_removed_wheels()
    return REMOVED_WHEELS


def _prune_removed_wheels_locked(today: str) -> None:
    """Убирает записи прошлых суток. Вызывать только под REMOVED_WHEELS_LOCK."""
    removed = _removed_wheels_locked()
    stale = [url for url, day in removed.items() if day != today]
    for url in stale:
        del removed[url]


def removed_wheels_today() -> set[str]:
    """Канонические URL колёс, удалённых сегодня командой /removewheel."""
    today = today_msk()
    with REMOVED_WHEELS_LOCK:
        _prune_removed_wheels_locked(today)
        return set(_removed_wheels_locked())


def mark_wheel_removed(url: str) -> bool:
    """Помечает колесо удалённым до конца текущих суток МСК.

    Возвращает True, если колесо помечено сейчас, и False, если оно уже
    было удалено сегодня. Файл записывается атомарно, как остальные JSON.
    """
    today = today_msk()
    with REMOVED_WHEELS_LOCK:
        _prune_removed_wheels_locked(today)
        removed = _removed_wheels_locked()
        if url in removed:
            return False
        removed[url] = today
        snapshot = dict(removed)
    atomic_write_json(REMOVED_WHEELS_FILE, snapshot)
    return True


def unmark_wheel_removed(url: str) -> bool:
    """Снимает пометку ручного удаления колеса (отмена /removewheel).

    Возвращает True, если колесо было отмечено удалённым сегодня и
    пометка снята, и False, если снимать было нечего.
    """
    today = today_msk()
    with REMOVED_WHEELS_LOCK:
        _prune_removed_wheels_locked(today)
        removed = _removed_wheels_locked()
        if url not in removed:
            return False
        del removed[url]
        snapshot = dict(removed)
    atomic_write_json(REMOVED_WHEELS_FILE, snapshot)
    return True


# ----------------------------------------------------------------------------
# Offset Telegram-бота (bot_state.json)
# ----------------------------------------------------------------------------

def load_bot_offset() -> int:
    """Читает сохранённый offset getUpdates (последний update_id + 1).

    Благодаря этому после рестарта парсера уже обработанные команды
    не выполняются повторно.
    """
    raw = read_json(BOT_STATE_FILE, {})
    if isinstance(raw, dict):
        try:
            return max(0, int(raw.get("offset", 0)))
        except (TypeError, ValueError):
            pass
    return 0


def save_bot_offset(offset: int) -> None:
    """Атомарно сохраняет offset getUpdates в bot_state.json."""
    try:
        atomic_write_json(BOT_STATE_FILE, {"offset": offset})
    except OSError as error:
        log.warning("Бот: не удалось сохранить %s: %s", BOT_STATE_FILE.name, error)


# ----------------------------------------------------------------------------
# Ссылки, ожидающие перепроверки после expired (pending_expired.json)
# ----------------------------------------------------------------------------
# url -> {channel, msg_id, message_url, preview, post_text, first_seen}.
# Без этого файла PENDING_EXPIRED_RETRY (см. parser.py) жил только в
# памяти: пост, чья ссылка ошибочно признана expired, уже помечен
# «увиденным» в seen_ids.json, и обычная правка поста его больше не
# перепроверит — рестарт процесса терял такую находку навсегда вместо
# повторной проверки на следующих циклах (см. parser.retry_expired_links).
# Доступ и запись — только из parser-потока (как у самого
# PENDING_EXPIRED_RETRY), поэтому лок не нужен.

def load_pending_expired() -> dict[str, dict[str, Any]]:
    """Читает список ссылок на перепроверку, накопленный до рестарта.

    Записи с нечитаемым first_seen отбрасываются: без валидной метки
    времени retry_expired_links не сможет сравнить её с окном ретрая
    (NOTIFY_RETRY_WINDOW_MINUTES), а восстановить её нечем.
    """
    raw = read_json(PENDING_EXPIRED_FILE, {})
    if not isinstance(raw, dict):
        return {}
    pending: dict[str, dict[str, Any]] = {}
    for url, info in raw.items():
        if not isinstance(url, str) or not isinstance(info, dict):
            continue
        first_seen = parse_msk(info.get("first_seen"))
        if first_seen is None:
            continue
        pending[url] = {
            "channel": str(info.get("channel", "")),
            "msg_id": str(info.get("msg_id", "")),
            "message_url": str(info.get("message_url", "")),
            "preview": str(info.get("preview", "")),
            "post_text": str(info.get("post_text", "")),
            "first_seen": first_seen,
        }
    return pending


def save_pending_expired(pending: dict[str, dict[str, Any]]) -> None:
    """Атомарно сохраняет список ссылок на перепроверку.

    Вызывается на каждое изменение (добавление/снятие одной ссылки) —
    записей единицы (только реальные «хвосты» с ошибочным is_ended), полная
    перезапись дёшева. Сбой записи не должен ронять цикл парсинга: при
    ошибке диска парсер продолжает работать по памяти в пределах текущего
    запуска, просто без гарантии пережить следующий рестарт.
    """
    try:
        serializable = {
            url: {
                "channel": info["channel"],
                "msg_id": info["msg_id"],
                "message_url": info["message_url"],
                "preview": info["preview"],
                "post_text": info["post_text"],
                "first_seen": info["first_seen"].isoformat(timespec="seconds"),
            }
            for url, info in pending.items()
        }
        atomic_write_json(PENDING_EXPIRED_FILE, serializable)
    except OSError as error:
        log.warning(
            "Не удалось сохранить %s: %s", PENDING_EXPIRED_FILE.name, error
        )
