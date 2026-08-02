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

from typing import Any

from . import registry
from .config import icon

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
    """
    return _kb([
        [{"text": label, "callback_data": f"rmw:{number}"}]
        for number, label in rows
    ])
