"""Команда /active: проверка колёс через API и форматирование ответа.

Проверка идёт в фоновом потоке — поток бота не блокируется, результат
приходит отдельным сообщением. Одновременно выполняется не больше одной
проверки.
"""

from __future__ import annotations

import html
import threading
from typing import Any

from .betboom import classify_wheels
from .config import icon
from .logging_setup import log
from .telegram_api import background_bot_send, bot_send
from .timeutils import format_deadline
from .urls import normalize_url

# Один /active за раз (non-blocking acquire).
_active_check_lock = threading.Lock()

# Нумерация колёс из последнего ответа /active или /wheels: номер →
# канонический URL. По этим номерам работает /removewheel <номер> и
# кнопки ❌ под обеими командами. Обновляется из бот-потока (/wheels)
# и из фонового active-api-потока (/active) — доступ только под локом.
# Хранится в памяти: после рестарта нужно заново вызвать /active или
# /wheels, чтобы получить актуальные номера.
_last_active_lock = threading.Lock()
_last_active_numbers: dict[int, str] = {}


def remember_active_numbers(numbered: list[tuple[int, dict[str, Any]]]) -> None:
    with _last_active_lock:
        _last_active_numbers.clear()
        for number, item in numbered:
            _last_active_numbers[number] = normalize_url(str(item.get("url", "")))


def forget_active_numbers() -> None:
    with _last_active_lock:
        _last_active_numbers.clear()


def lookup_active_number(number: int) -> tuple[str | None, int]:
    """URL колеса по номеру из последнего /active и размер этой нумерации."""
    with _last_active_lock:
        return _last_active_numbers.get(number), len(_last_active_numbers)


def format_active_item(item: dict[str, Any], number: int) -> str:
    """Строка одного колеса в ответе /active."""
    found_at = str(item.get("found_at", ""))
    found_time = found_at[11:16] if len(found_at) >= 16 else found_at
    channel = html.escape(str(item.get("channel", "")))
    if item.get("source") == "twitch":
        channel_label = f"twitch.tv/{channel}"
    else:
        channel_label = f"@{channel}"
    # normalize_url: старые записи могли сохранить URL с &amp; и utm-хвостом.
    url = html.escape(normalize_url(str(item.get("url", ""))))
    referral_mark = " ⚠️ для рефералов" if item.get("referral") else ""
    # Дедлайн обновлён свежим info в classify_wheels, поэтому «осталось N мин»
    # считается от актуального конца розыгрыша, а не от момента находки.
    deadline = format_deadline(item.get("ends_at"))
    deadline_mark = f" · до {deadline}" if deadline else ""
    return (
        f"{number}. {found_time} — {channel_label}{referral_mark}{deadline_mark}\n{url}"
    )


_MENU_BUTTON_ROW = [{"text": "☰ Меню", "callback_data": "m:root"}]


def _menu_only_keyboard() -> dict[str, Any]:
    return {"inline_keyboard": [_MENU_BUTTON_ROW]}


def _removal_keyboard(numbered: list[tuple[int, dict[str, Any]]]) -> dict[str, Any]:
    """Клавиатура ❌ под /active: не через menu.py — тот импортирует этот
    модуль (lookup_active_number), обратный импорт создал бы цикл.
    Последней строкой всегда идёт кнопка «☰ Меню».
    """
    rows = [
        [{"text": f"❌ {number}", "callback_data": f"rmw:{number}"}]
        for number, _item in numbered
    ]
    rows.append(_MENU_BUTTON_ROW)
    return {"inline_keyboard": rows}


def format_active_result(
    active_items: list[dict[str, Any]] | None,
    total: int,
    unknown_count: int = 0,
) -> tuple[str, dict[str, Any] | None]:
    """Форматирует ответ команды /active для отправки в Telegram.

    Показываются только действительно активные колёса: ещё не начавшиеся
    (soon) и завершившиеся в ответ не попадают. Возвращает (текст,
    inline-клавиатура с кнопками ❌) — клавиатура None, если убирать нечего.
    unknown_count > 0 означает, что часть колёс не удалось проверить
    (таймаут или ошибка API) — результат может быть неполным.
    """
    if active_items is None:
        return (
            f"{icon('warn')} Не удалось проверить колёса через API BetBoom. "
            "Это ошибка проверки, а не «активных нет» — "
            "подробности в parser.log.",
            _menu_only_keyboard(),
        )
    # Все колёса вернули unknown — скорее всего сетевая ошибка.
    if not active_items and unknown_count > 0 and unknown_count == total:
        return (
            f"{icon('warn')} Не удалось определить статус {total} колёс "
            "(API не ответил).\n"
            "Попробуйте /active ещё раз через несколько секунд.",
            _menu_only_keyboard(),
        )
    suffix = (
        f"\n⚠️ {unknown_count} колёс не удалось проверить (таймаут) — "
        "результат может быть неполным."
        if unknown_count
        else ""
    )
    if not active_items:
        forget_active_numbers()
        return (
            f"{icon('warn')} Среди {total} колёс за сегодня "
            "активных не найдено.\n"
            f"Все розыгрыши уже завершились или ещё не начались.{suffix}",
            _menu_only_keyboard(),
        )
    lines = [
        f"{icon('link')} <b>Активные колёса ({len(active_items)} из "
        f"{total} за сегодня):</b>"
    ]
    numbered = list(enumerate(active_items, start=1))
    # Запоминаем нумерацию для /removewheel <номер>: номера действительны
    # до следующего ответа /active или /wheels.
    remember_active_numbers(numbered)
    lines.extend(format_active_item(item, number) for number, item in numbered)
    lines.append("\nУбрать колесо: /removewheel номер или кнопкой ❌ ниже.")
    if suffix:
        lines.append(suffix)
    return ("\n".join(lines), _removal_keyboard(numbered))


def fire_active_check(chat_id: str, unique_items: list[dict[str, Any]]) -> None:
    """Fire-and-forget: запускает API-проверку в daemon-потоке, немедленно возвращается.

    Поток бота не блокируется. Результат придёт отдельным сообщением через
    background_bot_send после завершения проверки в фоновом потоке.
    Если проверка уже идёт — бот сообщает об этом и возвращается.
    """
    if not _active_check_lock.acquire(blocking=False):
        bot_send(
            chat_id,
            f"{icon('warn')} Проверка уже выполняется, подождите…",
            reply_markup=_menu_only_keyboard(),
        )
        return

    total = len(unique_items)

    def _run_and_send() -> None:
        active_items: list[dict[str, Any]] | None = None
        unknown_count = 0
        try:
            # soon-колёса (ещё не начались) в /active не показываются.
            active_items, _soon_items, unknown_count = classify_wheels(unique_items)
        except Exception as error:
            log.error("active-check: проверка колёс не удалась: %s", error)
        finally:
            _active_check_lock.release()

        text, keyboard = format_active_result(active_items, total, unknown_count)
        background_bot_send(chat_id, text, reply_markup=keyboard)

    threading.Thread(target=_run_and_send, daemon=True, name="active-api").start()
