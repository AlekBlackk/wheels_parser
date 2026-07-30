"""Состояние на диске: JSON-файлы парсера.

Всё пишется атомарно (запись во временный файл + replace), чтобы падение
процесса посреди записи не оставило обрезанный JSON.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from . import registry
from .config import (
    BOT_STATE_FILE,
    MAX_RESULTS,
    MAX_SEEN_PER_CHANNEL,
    OUTPUT_FILE,
    REMOVED_WHEELS_FILE,
    SEEN_FILE,
)
from .logging_setup import log
from .timeutils import today_msk


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
# История находок (freebets.json)
# ----------------------------------------------------------------------------

def load_results() -> list[dict[str, Any]]:
    data = read_json(OUTPUT_FILE, [])
    return data if isinstance(data, list) else []


def save_results(results: list[dict[str, Any]]) -> None:
    """Сохраняет историю, обрезая её до MAX_RESULTS записей.

    Обрезка на месте (del, а не переприсваивание): список results общий
    между циклами, терять ссылку на него нельзя.
    """
    if len(results) > MAX_RESULTS:
        del results[: len(results) - MAX_RESULTS]
    atomic_write_json(OUTPUT_FILE, results)


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


REMOVED_WHEELS: dict[str, str] = load_removed_wheels()


def _prune_removed_wheels_locked(today: str) -> None:
    """Убирает записи прошлых суток. Вызывать только под REMOVED_WHEELS_LOCK."""
    stale = [url for url, day in REMOVED_WHEELS.items() if day != today]
    for url in stale:
        del REMOVED_WHEELS[url]


def removed_wheels_today() -> set[str]:
    """Канонические URL колёс, удалённых сегодня командой /removewheel."""
    today = today_msk()
    with REMOVED_WHEELS_LOCK:
        _prune_removed_wheels_locked(today)
        return set(REMOVED_WHEELS)


def mark_wheel_removed(url: str) -> bool:
    """Помечает колесо удалённым до конца текущих суток МСК.

    Возвращает True, если колесо помечено сейчас, и False, если оно уже
    было удалено сегодня. Файл записывается атомарно, как остальные JSON.
    """
    today = today_msk()
    with REMOVED_WHEELS_LOCK:
        _prune_removed_wheels_locked(today)
        if url in REMOVED_WHEELS:
            return False
        REMOVED_WHEELS[url] = today
        snapshot = dict(REMOVED_WHEELS)
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
