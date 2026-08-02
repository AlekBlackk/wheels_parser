"""Inline-меню управления ботом: клавиатуры, роутинг callback'ов, undo.

Корневое меню открывается командой /menu (и кнопкой «☰ Меню» в /start,
/help). Разделы переключаются через editMessageText — одно сообщение,
не плодит новые. Списки каналов/Twitch/слов получают те же кнопки ❌
и при обычном текстовом выводе команд (/channels, /twitch, /words), не
только через /menu.

Обработчики здесь не знают о bot.py (иначе циклический импорт): запуск
существующих команд из меню (/wheels, /active, /status, /top) остаётся
в bot.py, который делегирует сюда всё остальное через handle_callback.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from . import registry, storage
from .active_report import lookup_active_number
from .config import icon
from .telegram_api import answer_callback_query, edit_message_text

BACK_BUTTON = {"text": "← Назад", "callback_data": "m:root"}


def _kb(rows: list[list[dict[str, str]]]) -> dict[str, Any]:
    return {"inline_keyboard": rows}


def root_open_keyboard() -> dict[str, Any]:
    """Кнопка «☰ Меню» под /start и /help."""
    return _kb([[{"text": "☰ Меню", "callback_data": "m:root"}]])


def root_menu_keyboard() -> dict[str, Any]:
    return _kb([
        [{"text": "📊 Находки", "callback_data": "m:wheels"}],
        [{"text": "📡 Каналы", "callback_data": "m:channels"}],
        [{"text": "🎮 Twitch", "callback_data": "m:twitch"}],
        [{"text": "🔑 Слова", "callback_data": "m:words"}],
    ])


def root_text() -> str:
    return "☰ <b>Меню WheelsParser</b>\nВыберите раздел:"


def wheels_section_keyboard() -> dict[str, Any]:
    return _kb([
        [{"text": f"{icon('link')} Колёса", "callback_data": "m:do_wheels"}],
        [{"text": f"{icon('bell')} Активные", "callback_data": "m:do_active"}],
        [{"text": "📈 Статус", "callback_data": "m:do_status"}],
        [{"text": "📊 Топ каналов", "callback_data": "m:do_top"}],
        [BACK_BUTTON],
    ])


def wheels_section_text() -> str:
    return "📊 <b>Находки</b>\nВыберите действие:"


def channels_list_keyboard() -> dict[str, Any]:
    rows = [
        [{"text": f"@{channel} ❌", "callback_data": f"ch:rm:{channel}"}]
        for channel in registry.channels_snapshot()
    ]
    rows.append([BACK_BUTTON])
    return _kb(rows)


def channels_section_text() -> str:
    channels = registry.channels_snapshot()
    if not channels:
        return "📡 <b>Каналы</b>\nСписок пуст. Добавьте: /add @channel"
    return f"📡 <b>Каналы ({len(channels)}):</b>\nНажмите ❌, чтобы убрать канал."


def twitch_list_keyboard() -> dict[str, Any]:
    rows = [
        [{"text": f"twitch.tv/{channel} ❌", "callback_data": f"tw:rm:{channel}"}]
        for channel in registry.twitch_channels_snapshot()
    ]
    rows.append([BACK_BUTTON])
    return _kb(rows)


def twitch_section_text() -> str:
    channels = registry.twitch_channels_snapshot()
    if not channels:
        return "🎮 <b>Twitch</b>\nСписок пуст. Добавьте: /addtwitch channel"
    return f"🎮 <b>Twitch-каналы ({len(channels)}):</b>\nНажмите ❌, чтобы убрать канал."


def words_list_keyboard() -> dict[str, Any]:
    rows = [
        [{"text": f"{word} ❌", "callback_data": f"wd:rm:{index}"}]
        for index, word in enumerate(registry.keywords_snapshot())
    ]
    rows.append([BACK_BUTTON])
    return _kb(rows)


def words_section_text() -> str:
    words = registry.keywords_snapshot()
    if not words:
        return "🔑 <b>Слова</b>\nСписок пуст. Добавьте: /addword слово"
    return f"🔑 <b>Ключевые слова ({len(words)}):</b>\nНажмите ❌, чтобы убрать слово."


def wheel_removal_keyboard(rows: list[tuple[int, str]]) -> dict[str, Any]:
    """Клавиатура ❌ под списком колёс (/active, /wheels).

    rows — пары (номер из общей нумерации /removewheel, подпись кнопки).
    Последней строкой всегда идёт кнопка «☰ Меню».
    """
    kb_rows = [
        [{"text": label, "callback_data": f"rmw:{number}"}]
        for number, label in rows
    ]
    kb_rows.append([{"text": "☰ Меню", "callback_data": "m:root"}])
    return _kb(kb_rows)


# ----------------------------------------------------------------------------
# Отмена последнего удаления (один слот на категорию, TTL)
# ----------------------------------------------------------------------------
# Как в Gmail — «Отменить» относится к последнему действию в категории,
# не к полной истории: новое удаление в той же категории перезаписывает
# предыдущий слот отмены.

UNDO_WINDOW_SECONDS = 10

_undo_lock = threading.Lock()
_last_deletion: dict[str, tuple[str, float]] = {}


def remember_deletion(category: str, value: str) -> None:
    with _undo_lock:
        _last_deletion[category] = (value, time.time())


def pop_deletion(category: str) -> str | None:
    """Значение для отмены, если оно ещё в пределах окна.

    Слот освобождается в любом случае (и при успехе, и при просрочке) —
    повторный тап «Отменить» не должен восстановить то же самое дважды.
    """
    with _undo_lock:
        entry = _last_deletion.pop(category, None)
    if entry is None:
        return None
    value, deleted_at = entry
    if time.time() - deleted_at > UNDO_WINDOW_SECONDS:
        return None
    return value


def forget_deletions() -> None:
    """Только для тестов — сбрасывает все ожидающие отмены."""
    with _undo_lock:
        _last_deletion.clear()


def _with_undo(keyboard: dict[str, Any], undo_callback: str) -> dict[str, Any]:
    """Вставляет кнопку «↩️ Отменить» перед последней строкой (обычно «← Назад»)."""
    rows = [list(row) for row in keyboard["inline_keyboard"]]
    undo_row = [{"text": "↩️ Отменить", "callback_data": undo_callback}]
    if rows:
        rows.insert(len(rows) - 1, undo_row)
    else:
        rows.append(undo_row)
    return _kb(rows)


# ----------------------------------------------------------------------------
# Навигация
# ----------------------------------------------------------------------------

def _cb_root(chat_id: str, message_id: int, callback_id: str) -> None:
    answer_callback_query(callback_id)
    edit_message_text(chat_id, message_id, root_text(), root_menu_keyboard())


def _cb_wheels_section(chat_id: str, message_id: int, callback_id: str) -> None:
    answer_callback_query(callback_id)
    edit_message_text(chat_id, message_id, wheels_section_text(), wheels_section_keyboard())


def _cb_channels_section(chat_id: str, message_id: int, callback_id: str) -> None:
    answer_callback_query(callback_id)
    edit_message_text(chat_id, message_id, channels_section_text(), channels_list_keyboard())


def _cb_twitch_section(chat_id: str, message_id: int, callback_id: str) -> None:
    answer_callback_query(callback_id)
    edit_message_text(chat_id, message_id, twitch_section_text(), twitch_list_keyboard())


def _cb_words_section(chat_id: str, message_id: int, callback_id: str) -> None:
    answer_callback_query(callback_id)
    edit_message_text(chat_id, message_id, words_section_text(), words_list_keyboard())


# ----------------------------------------------------------------------------
# Роутинг
# ----------------------------------------------------------------------------

_STATIC_HANDLERS: dict[str, Callable[[str, int, str], None]] = {
    "m:root": _cb_root,
    "m:wheels": _cb_wheels_section,
    "m:channels": _cb_channels_section,
    "m:twitch": _cb_twitch_section,
    "m:words": _cb_words_section,
}

_PREFIX_HANDLERS: dict[str, Callable[[str, int, str, str], None]] = {}


def handle_callback(chat_id: str, message_id: int, callback_id: str, data: str) -> bool:
    """Обрабатывает callback меню. Возвращает True, если данные распознаны.

    False означает «не мой callback» — bot.py пробует свои обработчики
    (m:do_wheels и т.п.) перед тем, как молча проигнорировать.
    """
    handler = _STATIC_HANDLERS.get(data)
    if handler is not None:
        handler(chat_id, message_id, callback_id)
        return True
    for prefix, prefix_handler in _PREFIX_HANDLERS.items():
        if data.startswith(prefix):
            prefix_handler(chat_id, message_id, callback_id, data[len(prefix):])
            return True
    return False


# ----------------------------------------------------------------------------
# Удаление / восстановление Telegram- и Twitch-каналов
# ----------------------------------------------------------------------------

def _cb_remove_channel(chat_id: str, message_id: int, callback_id: str, channel: str) -> None:
    with registry.CHANNELS_LOCK:
        if channel not in registry.CHANNELS:
            answer_callback_query(callback_id, "Уже удалён")
            edit_message_text(chat_id, message_id, channels_section_text(), channels_list_keyboard())
            return
        registry.CHANNELS.remove(channel)
        registry.save_channels_file()
    remember_deletion("channel", channel)
    answer_callback_query(callback_id, f"@{channel} удалён")
    edit_message_text(
        chat_id, message_id, channels_section_text(),
        _with_undo(channels_list_keyboard(), "undo:channel"),
    )


def _cb_undo_channel(chat_id: str, message_id: int, callback_id: str) -> None:
    channel = pop_deletion("channel")
    if channel is None:
        answer_callback_query(
            callback_id, "Время отмены истекло. Добавьте: /add @channel", show_alert=True
        )
        return
    with registry.CHANNELS_LOCK:
        if channel not in registry.CHANNELS:
            registry.CHANNELS.append(channel)
            registry.save_channels_file()
    answer_callback_query(callback_id, f"@{channel} восстановлен")
    edit_message_text(chat_id, message_id, channels_section_text(), channels_list_keyboard())


def _cb_remove_twitch(chat_id: str, message_id: int, callback_id: str, channel: str) -> None:
    with registry.TWITCH_CHANNELS_LOCK:
        if channel not in registry.TWITCH_CHANNELS:
            answer_callback_query(callback_id, "Уже удалён")
            edit_message_text(chat_id, message_id, twitch_section_text(), twitch_list_keyboard())
            return
        registry.TWITCH_CHANNELS.remove(channel)
        registry.save_twitch_channels_file()
    registry.TWITCH_RELOAD.set()
    remember_deletion("twitch", channel)
    answer_callback_query(callback_id, f"twitch.tv/{channel} удалён")
    edit_message_text(
        chat_id, message_id, twitch_section_text(),
        _with_undo(twitch_list_keyboard(), "undo:twitch"),
    )


def _cb_undo_twitch(chat_id: str, message_id: int, callback_id: str) -> None:
    channel = pop_deletion("twitch")
    if channel is None:
        answer_callback_query(
            callback_id, "Время отмены истекло. Добавьте: /addtwitch channel", show_alert=True
        )
        return
    with registry.TWITCH_CHANNELS_LOCK:
        if channel not in registry.TWITCH_CHANNELS:
            registry.TWITCH_CHANNELS.append(channel)
            registry.save_twitch_channels_file()
    registry.TWITCH_RELOAD.set()
    answer_callback_query(callback_id, f"twitch.tv/{channel} восстановлен")
    edit_message_text(chat_id, message_id, twitch_section_text(), twitch_list_keyboard())


_STATIC_HANDLERS["undo:channel"] = _cb_undo_channel
_STATIC_HANDLERS["undo:twitch"] = _cb_undo_twitch
_PREFIX_HANDLERS["ch:rm:"] = _cb_remove_channel
_PREFIX_HANDLERS["tw:rm:"] = _cb_remove_twitch


# ----------------------------------------------------------------------------
# Удаление / восстановление ключевых слов (по индексу — слово может быть
# длиннее, чем позволяет 64-байтный лимит callback_data)
# ----------------------------------------------------------------------------

def _cb_remove_word(chat_id: str, message_id: int, callback_id: str, raw_index: str) -> None:
    try:
        index = int(raw_index)
    except ValueError:
        answer_callback_query(callback_id, "Некорректный номер", show_alert=True)
        return
    with registry.KEYWORDS_LOCK:
        if index < 0 or index >= len(registry.KEYWORDS):
            answer_callback_query(callback_id, "Список изменился — обновляю", show_alert=True)
            edit_message_text(chat_id, message_id, words_section_text(), words_list_keyboard())
            return
        word = registry.KEYWORDS.pop(index)
        registry.save_keywords_file()
    remember_deletion("word", word)
    answer_callback_query(callback_id, f"«{word}» удалено")
    edit_message_text(
        chat_id, message_id, words_section_text(),
        _with_undo(words_list_keyboard(), "undo:word"),
    )


def _cb_undo_word(chat_id: str, message_id: int, callback_id: str) -> None:
    word = pop_deletion("word")
    if word is None:
        answer_callback_query(
            callback_id, "Время отмены истекло. Добавьте: /addword слово", show_alert=True
        )
        return
    with registry.KEYWORDS_LOCK:
        already_present = any(
            existing.casefold() == word.casefold() for existing in registry.KEYWORDS
        )
        if not already_present:
            registry.KEYWORDS.append(word)
            registry.save_keywords_file()
    answer_callback_query(callback_id, f"«{word}» восстановлено")
    edit_message_text(chat_id, message_id, words_section_text(), words_list_keyboard())


_STATIC_HANDLERS["undo:word"] = _cb_undo_word
_PREFIX_HANDLERS["wd:rm:"] = _cb_remove_word


# ----------------------------------------------------------------------------
# Удаление / восстановление колеса из /active (кнопка ❌ под /active, /wheels)
# ----------------------------------------------------------------------------

def _cb_remove_wheel(chat_id: str, message_id: int, callback_id: str, raw_number: str) -> None:
    try:
        number = int(raw_number)
    except ValueError:
        answer_callback_query(callback_id, "Некорректный номер", show_alert=True)
        return
    url, _known = lookup_active_number(number)
    if not url:
        answer_callback_query(
            callback_id, "Список устарел — вызовите /active заново", show_alert=True
        )
        return
    if storage.mark_wheel_removed(url):
        remember_deletion("wheel", url)
        answer_callback_query(callback_id, "Колесо убрано из /active")
    else:
        answer_callback_query(callback_id, "Уже убрано")


def _cb_undo_wheel(chat_id: str, message_id: int, callback_id: str) -> None:
    url = pop_deletion("wheel")
    if url is None:
        answer_callback_query(
            callback_id,
            "Время отмены истекло. Колесо останется скрытым из /active до 00:00 МСК.",
            show_alert=True,
        )
        return
    storage.unmark_wheel_removed(url)
    answer_callback_query(callback_id, "Колесо возвращено в /active")


_STATIC_HANDLERS["undo:wheel"] = _cb_undo_wheel
_PREFIX_HANDLERS["rmw:"] = _cb_remove_wheel
