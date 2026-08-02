# Комбинированное меню управления ботом — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить inline-меню `/menu` (навигация деревом через `editMessageText`) и контекстные кнопки ❌/«↩️ Отменить» под ответами существующих команд (`/channels`, `/twitch`, `/words`, `/active`, `/wheels`), не убирая ни одной текстовой команды.

**Architecture:** Новый модуль `wheelsparser/menu.py` (по образцу `active_report.py`) — сборка inline-клавиатур, роутинг callback'ов, in-memory undo-состояние (один слот на категорию, TTL 10 сек). `telegram_api.py` получает `reply_markup`, `edit_message_text`, `answer_callback_query`. `bot.py` разбирает `callback_query` в `bot_loop` наравне с `message` и держит 4 тривиальных обработчика («запустить существующую команду из меню»), делегируя всё остальное в `menu.handle_callback`. `storage.py` получает симметричную `unmark_wheel_removed`.

**Tech Stack:** Python 3, `requests`, `unittest` (`unittest.mock.patch`/`Mock`), файловый пакет `wheelsparser`.

## Global Constraints

- Все callback_data ≤ 64 байт (лимит Telegram Bot API) — имена TG-каналов ≤32 байт, Twitch ≤25 байт влезают целиком; слова (до 64 симв.) и URL колёс — только по индексу/номеру, не текстом.
- Каждый обработанный `callback_query` обязан закрываться `answer_callback_query` — иначе кнопка «крутится» до тайм-аута.
- Команды и callback'и принимаются только из `TELEGRAM_CHAT_ID` (см. `bot.py` текущую проверку) — то же самое для `callback_query`.
- Изменения `registry.CHANNELS`/`KEYWORDS`/`TWITCH_CHANNELS` — только под соответствующим локом (`CHANNELS_LOCK`, `KEYWORDS_LOCK`, `TWITCH_CHANNELS_LOCK`), запись файла (`save_*_file()`) — сразу после изменения списка, под тем же локом.
- JSON-состояние пишется только через `storage.atomic_write_json` (временный файл + replace).
- Пользовательский текст — по-русски, в стиле уже существующих сообщений бота (см. `bot.py`, `telegram_api.py`).
- Новый код без циклических импортов: `menu.py` импортирует из `active_report.py` (`lookup_active_number`), поэтому `active_report.py` не должен импортировать `menu.py` — там, где нужна клавиатура (`/active`), она строится локальным приватным хелпером внутри `active_report.py`, а не через `menu.py`.
- Тесты — `unittest.TestCase` + `unittest.mock.patch.object`, как в существующих `tests/test_bot.py`, `tests/test_telegram_api.py`, `tests/test_storage.py`. Запуск: `python -m pytest tests/<file> -v` (venv уже настроен в `.venv`).

---

## Файловая структура

- **Create** `wheelsparser/menu.py` — клавиатуры, undo-стор, роутинг callback'ов для навигации/списков/удаления каналов-Twitch-слов/колёс.
- **Create** `tests/test_menu.py` — тесты нового модуля.
- **Modify** `wheelsparser/telegram_api.py` — `reply_markup` в `_post_message`/`bot_send`/`background_bot_send`, новые `edit_message_text`, `answer_callback_query`.
- **Modify** `wheelsparser/storage.py` — `unmark_wheel_removed`.
- **Modify** `wheelsparser/active_report.py` — `format_active_result` возвращает `(текст, клавиатура)`, `fire_active_check` передаёт клавиатуру.
- **Modify** `wheelsparser/bot.py` — `BOT_COMMANDS`, `cmd_menu`, кнопка «☰ Меню» в `/start`/`/help`, `callback_query` в `bot_loop`, клавиатуры под `/channels`/`/twitch`/`/words`/`/wheels`.
- **Modify** `tests/test_telegram_api.py`, `tests/test_storage.py`, `tests/test_bot.py` — тесты на изменения выше.
- **Modify** `README.md` — строка `/menu` в таблице команд.

---

### Task 1: telegram_api — `reply_markup` в отправке сообщений

**Files:**
- Modify: `wheelsparser/telegram_api.py:82-97` (`_post_message`), `:252-257` (`bot_send`), `:260-270` (`background_bot_send`)
- Test: `tests/test_telegram_api.py`

**Interfaces:**
- Produces: `_post_message(session, chat_id, text, parse_mode=None, reply_markup=None)`, `bot_send(chat_id, text, reply_markup=None)`, `background_bot_send(chat_id, text, reply_markup=None)`.

- [ ] **Step 1: Write the failing tests**

Добавить в конец `tests/test_telegram_api.py` (перед `if __name__ == "__main__":`):

```python
class ReplyMarkupTests(unittest.TestCase):
    def test_bot_send_includes_reply_markup_when_given(self):
        session = fake_session()
        keyboard = {"inline_keyboard": [[{"text": "Меню", "callback_data": "m:root"}]]}
        with patch.object(telegram_api, "BOT_SESSION", session):
            telegram_api.bot_send("1", "text", reply_markup=keyboard)
        self.assertEqual(session.post.call_args.kwargs["json"]["reply_markup"], keyboard)

    def test_bot_send_omits_reply_markup_when_not_given(self):
        session = fake_session()
        with patch.object(telegram_api, "BOT_SESSION", session):
            telegram_api.bot_send("1", "text")
        self.assertNotIn("reply_markup", session.post.call_args.kwargs["json"])

    def test_background_bot_send_includes_reply_markup(self):
        session = fake_session()
        keyboard = {"inline_keyboard": [[{"text": "x", "callback_data": "y"}]]}
        with patch.object(telegram_api, "ACTIVE_CHECK_SESSION", session):
            telegram_api.background_bot_send("1", "text", reply_markup=keyboard)
        self.assertEqual(session.post.call_args.kwargs["json"]["reply_markup"], keyboard)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_telegram_api.py -k ReplyMarkupTests -v`
Expected: FAIL — `bot_send() got an unexpected keyword argument 'reply_markup'`

- [ ] **Step 3: Implement**

В `wheelsparser/telegram_api.py` заменить `_post_message`:

```python
def _post_message(
    session: requests.Session,
    chat_id: str,
    text: str,
    parse_mode: str | None = None,
    reply_markup: dict[str, Any] | None = None,
) -> requests.Response:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return session.post(
        f"{BOT_API}/sendMessage", json=payload, timeout=REQUEST_TIMEOUT
    )
```

Заменить `bot_send`:

```python
def bot_send(
    chat_id: str, text: str, reply_markup: dict[str, Any] | None = None
) -> None:
    """Ответ на команду — только из потока бота (BOT_SESSION)."""
    try:
        _post_message(
            BOT_SESSION, chat_id, text, parse_mode="HTML", reply_markup=reply_markup
        ).raise_for_status()
    except requests.RequestException as error:
        log.warning("Бот: не удалось ответить в чат %s: %s", chat_id, error)
```

Заменить `background_bot_send`:

```python
def background_bot_send(
    chat_id: str, text: str, reply_markup: dict[str, Any] | None = None
) -> None:
    """Ответ из фонового потока active-api (ACTIVE_CHECK_SESSION).

    Отдельная сессия нужна, чтобы не делить BOT_SESSION между потоками.
    """
    try:
        _post_message(
            ACTIVE_CHECK_SESSION,
            chat_id,
            text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        ).raise_for_status()
    except requests.RequestException as error:
        log.warning("Бот: не удалось ответить в чат %s: %s", chat_id, error)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_telegram_api.py -v`
Expected: PASS (все тесты файла, не только новые — regressions тоже проверяем)

- [ ] **Step 5: Commit**

```bash
git add wheelsparser/telegram_api.py tests/test_telegram_api.py
git commit -m "feat: support reply_markup in bot_send and background_bot_send"
```

---

### Task 2: telegram_api — `edit_message_text`

**Files:**
- Modify: `wheelsparser/telegram_api.py` (добавить функцию рядом с `bot_send`)
- Test: `tests/test_telegram_api.py`

**Interfaces:**
- Consumes: `BOT_API`, `BOT_SESSION`, `REQUEST_TIMEOUT`, `log` (уже импортированы в файле).
- Produces: `edit_message_text(chat_id: str, message_id: int, text: str, reply_markup: dict[str, Any] | None = None) -> None`.

- [ ] **Step 1: Write the failing tests**

Добавить в `tests/test_telegram_api.py`:

```python
class EditMessageTextTests(unittest.TestCase):
    def test_sends_chat_message_id_text_and_keyboard(self):
        session = fake_session()
        keyboard = {"inline_keyboard": [[{"text": "← Назад", "callback_data": "m:root"}]]}
        with patch.object(telegram_api, "BOT_SESSION", session):
            telegram_api.edit_message_text("1", 55, "<b>Раздел</b>", keyboard)
        payload = session.post.call_args.kwargs["json"]
        self.assertEqual(payload["chat_id"], "1")
        self.assertEqual(payload["message_id"], 55)
        self.assertEqual(payload["text"], "<b>Раздел</b>")
        self.assertEqual(payload["parse_mode"], "HTML")
        self.assertEqual(payload["reply_markup"], keyboard)
        session.post.assert_called_once_with(
            f"{telegram_api.BOT_API}/editMessageText",
            json=payload,
            timeout=telegram_api.REQUEST_TIMEOUT,
        )

    def test_omits_reply_markup_when_not_given(self):
        session = fake_session()
        with patch.object(telegram_api, "BOT_SESSION", session):
            telegram_api.edit_message_text("1", 55, "text")
        self.assertNotIn("reply_markup", session.post.call_args.kwargs["json"])

    def test_not_modified_error_is_not_logged_as_failure(self):
        response = Mock(
            status_code=400,
            text='{"description":"Bad Request: message is not modified"}',
        )
        error = requests.HTTPError("400", response=response)
        response.raise_for_status.side_effect = error
        session = Mock()
        session.post.return_value = response
        with patch.object(telegram_api, "BOT_SESSION", session), \
             patch.object(telegram_api.log, "warning") as warn:
            telegram_api.edit_message_text("1", 55, "text")
        warn.assert_not_called()

    def test_other_errors_are_logged(self):
        session = Mock()
        session.post.side_effect = requests.ConnectionError("boom")
        with patch.object(telegram_api, "BOT_SESSION", session), \
             patch.object(telegram_api.log, "warning") as warn:
            telegram_api.edit_message_text("1", 55, "text")
        warn.assert_called_once()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_telegram_api.py -k EditMessageTextTests -v`
Expected: FAIL — `AttributeError: module 'wheelsparser.telegram_api' has no attribute 'edit_message_text'`

- [ ] **Step 3: Implement**

Добавить в `wheelsparser/telegram_api.py` после `bot_send`:

```python
def edit_message_text(
    chat_id: str,
    message_id: int,
    text: str,
    reply_markup: dict[str, Any] | None = None,
) -> None:
    """Редактирует сообщение меню — только из потока бота (BOT_SESSION).

    «Message is not modified» (400) — не сбой: пользователь повторно
    открыл тот же раздел меню, текст и клавиатура не изменились.
    """
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        BOT_SESSION.post(
            f"{BOT_API}/editMessageText", json=payload, timeout=REQUEST_TIMEOUT
        ).raise_for_status()
    except requests.RequestException as error:
        response = getattr(error, "response", None)
        if (
            response is not None
            and response.status_code == 400
            and "message is not modified" in response.text.lower()
        ):
            return
        log.warning(
            "Бот: не удалось отредактировать сообщение %s в чате %s: %s",
            message_id, chat_id, error,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_telegram_api.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add wheelsparser/telegram_api.py tests/test_telegram_api.py
git commit -m "feat: add edit_message_text for inline-menu navigation"
```

---

### Task 3: telegram_api — `answer_callback_query`

**Files:**
- Modify: `wheelsparser/telegram_api.py`
- Test: `tests/test_telegram_api.py`

**Interfaces:**
- Produces: `answer_callback_query(callback_id: str, text: str = "", show_alert: bool = False) -> None`.

- [ ] **Step 1: Write the failing tests**

Добавить в `tests/test_telegram_api.py`:

```python
class AnswerCallbackQueryTests(unittest.TestCase):
    def test_sends_callback_id_and_text(self):
        session = fake_session()
        with patch.object(telegram_api, "BOT_SESSION", session):
            telegram_api.answer_callback_query("cb1", "Готово")
        payload = session.post.call_args.kwargs["json"]
        self.assertEqual(payload["callback_query_id"], "cb1")
        self.assertEqual(payload["text"], "Готово")
        self.assertNotIn("show_alert", payload)
        session.post.assert_called_once_with(
            f"{telegram_api.BOT_API}/answerCallbackQuery",
            json=payload,
            timeout=telegram_api.REQUEST_TIMEOUT,
        )

    def test_omits_text_when_empty(self):
        session = fake_session()
        with patch.object(telegram_api, "BOT_SESSION", session):
            telegram_api.answer_callback_query("cb1")
        self.assertNotIn("text", session.post.call_args.kwargs["json"])

    def test_show_alert_flag_is_included_when_true(self):
        session = fake_session()
        with patch.object(telegram_api, "BOT_SESSION", session):
            telegram_api.answer_callback_query("cb1", "Ошибка", show_alert=True)
        self.assertTrue(session.post.call_args.kwargs["json"]["show_alert"])

    def test_failure_is_logged_not_raised(self):
        session = Mock()
        session.post.side_effect = requests.ConnectionError("boom")
        with patch.object(telegram_api, "BOT_SESSION", session), \
             patch.object(telegram_api.log, "warning") as warn:
            telegram_api.answer_callback_query("cb1")
        warn.assert_called_once()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_telegram_api.py -k AnswerCallbackQueryTests -v`
Expected: FAIL — `AttributeError: ... no attribute 'answer_callback_query'`

- [ ] **Step 3: Implement**

Добавить в `wheelsparser/telegram_api.py` после `edit_message_text`:

```python
def answer_callback_query(
    callback_id: str, text: str = "", show_alert: bool = False
) -> None:
    """Закрывает «крутилку» на нажатой inline-кнопке.

    Вызывать на каждом обработанном callback'е — иначе кнопка у
    пользователя «крутится» до тайм-аута Telegram.
    """
    payload: dict[str, Any] = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
    if show_alert:
        payload["show_alert"] = True
    try:
        BOT_SESSION.post(
            f"{BOT_API}/answerCallbackQuery", json=payload, timeout=REQUEST_TIMEOUT
        ).raise_for_status()
    except requests.RequestException as error:
        log.warning("Бот: не удалось ответить на callback %s: %s", callback_id, error)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_telegram_api.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add wheelsparser/telegram_api.py tests/test_telegram_api.py
git commit -m "feat: add answer_callback_query to close inline button spinner"
```

---

### Task 4: storage — `unmark_wheel_removed`

**Files:**
- Modify: `wheelsparser/storage.py:191-206` (после `mark_wheel_removed`)
- Test: `tests/test_storage.py` (класс `RemovedWheelsTests`)

**Interfaces:**
- Consumes: `REMOVED_WHEELS_LOCK`, `_prune_removed_wheels_locked`, `_removed_wheels_locked`, `today_msk`, `atomic_write_json`, `REMOVED_WHEELS_FILE` (уже есть в файле).
- Produces: `unmark_wheel_removed(url: str) -> bool`.

- [ ] **Step 1: Write the failing test**

Добавить в `tests/test_storage.py` в класс `RemovedWheelsTests` (после `test_yesterdays_removals_are_pruned`):

```python
    def test_unmark_wheel_removed_restores_and_reports(self):
        with patch.object(storage, "today_msk", return_value="2026-07-30"):
            storage.mark_wheel_removed("https://x/one")
            self.assertTrue(storage.unmark_wheel_removed("https://x/one"))
            self.assertEqual(storage.removed_wheels_today(), set())
            self.assertFalse(storage.unmark_wheel_removed("https://x/one"))
        self.assertEqual(json.loads(self.file.read_text(encoding="utf-8")), {})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_storage.py -k test_unmark_wheel_removed_restores_and_reports -v`
Expected: FAIL — `AttributeError: module 'wheelsparser.storage' has no attribute 'unmark_wheel_removed'`

- [ ] **Step 3: Implement**

Добавить в `wheelsparser/storage.py` сразу после `mark_wheel_removed`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_storage.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add wheelsparser/storage.py tests/test_storage.py
git commit -m "feat: add unmark_wheel_removed for undoing /removewheel"
```

---

### Task 5: menu.py — клавиатуры и тексты разделов (скелет)

**Files:**
- Create: `wheelsparser/menu.py`
- Create: `tests/test_menu.py`

**Interfaces:**
- Consumes: `registry.channels_snapshot()`, `registry.twitch_channels_snapshot()`, `registry.keywords_snapshot()`, `config.icon(name)`.
- Produces: `BACK_BUTTON`, `root_open_keyboard()`, `root_menu_keyboard()`, `root_text()`, `wheels_section_keyboard()`, `wheels_section_text()`, `channels_list_keyboard()`, `channels_section_text()`, `twitch_list_keyboard()`, `twitch_section_text()`, `words_list_keyboard()`, `words_section_text()`, `wheel_removal_keyboard(rows: list[tuple[int, str]])`.

- [ ] **Step 1: Write the failing tests**

Создать `tests/test_menu.py`:

```python
import unittest
from unittest.mock import patch

from wheelsparser import menu, registry


class RootKeyboardTests(unittest.TestCase):
    def test_root_open_keyboard_has_single_menu_button(self):
        keyboard = menu.root_open_keyboard()
        self.assertEqual(
            keyboard, {"inline_keyboard": [[{"text": "☰ Меню", "callback_data": "m:root"}]]}
        )

    def test_root_menu_keyboard_has_four_sections(self):
        keyboard = menu.root_menu_keyboard()
        callbacks = [
            button["callback_data"]
            for row in keyboard["inline_keyboard"]
            for button in row
        ]
        self.assertEqual(
            callbacks, ["m:wheels", "m:channels", "m:twitch", "m:words"]
        )


class WheelsSectionTests(unittest.TestCase):
    def test_wheels_section_keyboard_has_four_actions_and_back(self):
        keyboard = menu.wheels_section_keyboard()
        callbacks = [
            button["callback_data"]
            for row in keyboard["inline_keyboard"]
            for button in row
        ]
        self.assertEqual(
            callbacks,
            ["m:do_wheels", "m:do_active", "m:do_status", "m:do_top", "m:root"],
        )


class ChannelsListKeyboardTests(unittest.TestCase):
    def test_lists_every_channel_with_remove_callback(self):
        with patch.object(registry, "CHANNELS", ["one", "two"]):
            keyboard = menu.channels_list_keyboard()
        rows = keyboard["inline_keyboard"]
        self.assertEqual(rows[0][0]["callback_data"], "ch:rm:one")
        self.assertEqual(rows[1][0]["callback_data"], "ch:rm:two")
        self.assertEqual(rows[-1][0]["callback_data"], "m:root")

    def test_empty_list_still_has_back_button(self):
        with patch.object(registry, "CHANNELS", []):
            keyboard = menu.channels_list_keyboard()
        self.assertEqual(keyboard["inline_keyboard"], [[menu.BACK_BUTTON]])

    def test_section_text_mentions_count(self):
        with patch.object(registry, "CHANNELS", ["one", "two"]):
            self.assertIn("2", menu.channels_section_text())

    def test_section_text_for_empty_list_points_to_add_command(self):
        with patch.object(registry, "CHANNELS", []):
            self.assertIn("/add", menu.channels_section_text())


class TwitchListKeyboardTests(unittest.TestCase):
    def test_lists_every_channel_with_remove_callback(self):
        with patch.object(registry, "TWITCH_CHANNELS", ["streamer"]):
            keyboard = menu.twitch_list_keyboard()
        self.assertEqual(
            keyboard["inline_keyboard"][0][0]["callback_data"], "tw:rm:streamer"
        )

    def test_section_text_for_empty_list_points_to_add_command(self):
        with patch.object(registry, "TWITCH_CHANNELS", []):
            self.assertIn("/addtwitch", menu.twitch_section_text())


class WordsListKeyboardTests(unittest.TestCase):
    def test_lists_every_word_with_index_based_remove_callback(self):
        with patch.object(registry, "KEYWORDS", ["колесо", "фрибет"]):
            keyboard = menu.words_list_keyboard()
        self.assertEqual(keyboard["inline_keyboard"][0][0]["callback_data"], "wd:rm:0")
        self.assertEqual(keyboard["inline_keyboard"][1][0]["callback_data"], "wd:rm:1")

    def test_section_text_for_empty_list_points_to_add_command(self):
        with patch.object(registry, "KEYWORDS", []):
            self.assertIn("/addword", menu.words_section_text())


class WheelRemovalKeyboardTests(unittest.TestCase):
    def test_builds_one_row_per_entry_with_rmw_callback(self):
        keyboard = menu.wheel_removal_keyboard([(1, "❌ 10:00 @demo"), (2, "❌ 10:05 @demo")])
        self.assertEqual(
            keyboard,
            {
                "inline_keyboard": [
                    [{"text": "❌ 10:00 @demo", "callback_data": "rmw:1"}],
                    [{"text": "❌ 10:05 @demo", "callback_data": "rmw:2"}],
                ]
            },
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_menu.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'wheelsparser.menu'`

- [ ] **Step 3: Implement**

Создать `wheelsparser/menu.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_menu.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add wheelsparser/menu.py tests/test_menu.py
git commit -m "feat: add menu.py with root/section keyboards for bot menu"
```

---

### Task 6: menu.py — undo-стор (один слот на категорию, TTL)

**Files:**
- Modify: `wheelsparser/menu.py`
- Test: `tests/test_menu.py`

**Interfaces:**
- Produces: `UNDO_WINDOW_SECONDS`, `remember_deletion(category: str, value: str) -> None`, `pop_deletion(category: str) -> str | None`, `forget_deletions() -> None`, `_with_undo(keyboard, undo_callback) -> dict`.

- [ ] **Step 1: Write the failing tests**

Добавить в `tests/test_menu.py`:

```python
class UndoStoreTests(unittest.TestCase):
    def setUp(self):
        menu.forget_deletions()
        self.addCleanup(menu.forget_deletions)

    def test_pop_within_window_returns_value_once(self):
        menu.remember_deletion("channel", "demo")
        self.assertEqual(menu.pop_deletion("channel"), "demo")
        self.assertIsNone(menu.pop_deletion("channel"))

    def test_pop_after_window_returns_none(self):
        with patch.object(menu.time, "time", return_value=1000.0):
            menu.remember_deletion("channel", "demo")
        with patch.object(menu.time, "time", return_value=1000.0 + menu.UNDO_WINDOW_SECONDS + 1):
            self.assertIsNone(menu.pop_deletion("channel"))

    def test_new_deletion_overwrites_previous_slot_in_same_category(self):
        menu.remember_deletion("channel", "first")
        menu.remember_deletion("channel", "second")
        self.assertEqual(menu.pop_deletion("channel"), "second")

    def test_categories_are_independent(self):
        menu.remember_deletion("channel", "demo")
        self.assertIsNone(menu.pop_deletion("twitch"))
        self.assertEqual(menu.pop_deletion("channel"), "demo")


class WithUndoTests(unittest.TestCase):
    def test_inserts_undo_row_before_last_row(self):
        base = menu._kb([[{"text": "x", "callback_data": "y"}], [menu.BACK_BUTTON]])
        combined = menu._with_undo(base, "undo:channel")
        rows = combined["inline_keyboard"]
        self.assertEqual(rows[0][0]["callback_data"], "y")
        self.assertEqual(rows[1][0]["callback_data"], "undo:channel")
        self.assertEqual(rows[2][0], menu.BACK_BUTTON)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_menu.py -k "UndoStoreTests or WithUndoTests" -v`
Expected: FAIL — `AttributeError: module 'wheelsparser.menu' has no attribute 'remember_deletion'`

- [ ] **Step 3: Implement**

В `wheelsparser/menu.py` добавить `import threading` и `import time` в блок импортов (после `from typing import Any`), затем добавить в конец файла:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_menu.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add wheelsparser/menu.py tests/test_menu.py
git commit -m "feat: add single-slot undo store with TTL to menu.py"
```

---

### Task 7: menu.py — навигация (роутинг разделов) + `handle_callback`

**Files:**
- Modify: `wheelsparser/menu.py`
- Test: `tests/test_menu.py`

**Interfaces:**
- Consumes: `telegram_api.answer_callback_query`, `telegram_api.edit_message_text`.
- Produces: `handle_callback(chat_id: str, message_id: int, callback_id: str, data: str) -> bool`.

- [ ] **Step 1: Write the failing tests**

Добавить в `tests/test_menu.py`:

```python
class NavigationCallbackTests(unittest.TestCase):
    def test_m_root_edits_message_and_answers(self):
        with patch.object(menu, "edit_message_text") as edit, \
             patch.object(menu, "answer_callback_query") as answer:
            handled = menu.handle_callback("1", 55, "cb1", "m:root")
        self.assertTrue(handled)
        answer.assert_called_once_with("cb1")
        edit.assert_called_once_with("1", 55, menu.root_text(), menu.root_menu_keyboard())

    def test_m_channels_shows_channel_list(self):
        with patch.object(registry, "CHANNELS", ["demo"]), \
             patch.object(menu, "edit_message_text") as edit, \
             patch.object(menu, "answer_callback_query"):
            menu.handle_callback("1", 55, "cb1", "m:channels")
        edit.assert_called_once_with(
            "1", 55, menu.channels_section_text(), menu.channels_list_keyboard()
        )

    def test_unknown_callback_is_not_handled_and_stays_silent(self):
        with patch.object(menu, "edit_message_text") as edit, \
             patch.object(menu, "answer_callback_query") as answer:
            handled = menu.handle_callback("1", 55, "cb1", "m:do_wheels")
        self.assertFalse(handled)
        edit.assert_not_called()
        answer.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_menu.py -k NavigationCallbackTests -v`
Expected: FAIL — `AttributeError: module 'wheelsparser.menu' has no attribute 'handle_callback'`

- [ ] **Step 3: Implement**

В `wheelsparser/menu.py` добавить импорт `from collections.abc import Callable` и `from .telegram_api import answer_callback_query, edit_message_text` в блок импортов, затем добавить в конец файла:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_menu.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add wheelsparser/menu.py tests/test_menu.py
git commit -m "feat: route menu navigation callbacks through handle_callback"
```

---

### Task 8: menu.py — удаление/восстановление TG- и Twitch-каналов

**Files:**
- Modify: `wheelsparser/menu.py`
- Test: `tests/test_menu.py`

**Interfaces:**
- Consumes: `registry.CHANNELS`, `registry.CHANNELS_LOCK`, `registry.save_channels_file`, `registry.TWITCH_CHANNELS`, `registry.TWITCH_CHANNELS_LOCK`, `registry.save_twitch_channels_file`, `registry.TWITCH_RELOAD`, `remember_deletion`, `pop_deletion`, `_with_undo` (из Task 6), `_STATIC_HANDLERS`/`_PREFIX_HANDLERS` (из Task 7).
- Produces: регистрирует `"ch:rm:"`, `"tw:rm:"` в `_PREFIX_HANDLERS` и `"undo:channel"`, `"undo:twitch"` в `_STATIC_HANDLERS`.

- [ ] **Step 1: Write the failing tests**

Добавить в `tests/test_menu.py`:

```python
class RemoveChannelCallbackTests(unittest.TestCase):
    def setUp(self):
        menu.forget_deletions()
        self.addCleanup(menu.forget_deletions)

    def test_removes_channel_saves_and_offers_undo(self):
        with patch.object(registry, "CHANNELS", ["demo", "other"]), \
             patch.object(registry, "save_channels_file") as save, \
             patch.object(menu, "edit_message_text") as edit, \
             patch.object(menu, "answer_callback_query") as answer:
            handled = menu.handle_callback("1", 55, "cb1", "ch:rm:demo")

        self.assertTrue(handled)
        self.assertEqual(registry.CHANNELS, ["other"])
        save.assert_called_once_with()
        answer.assert_called_once()
        self.assertIn("demo", answer.call_args.args[1])
        edited_keyboard = edit.call_args.args[3]
        callbacks = [b["callback_data"] for row in edited_keyboard["inline_keyboard"] for b in row]
        self.assertIn("undo:channel", callbacks)
        self.assertEqual(menu.pop_deletion("channel"), "demo")

    def test_removing_already_gone_channel_does_not_touch_undo_slot(self):
        with patch.object(registry, "CHANNELS", ["other"]), \
             patch.object(registry, "save_channels_file") as save, \
             patch.object(menu, "edit_message_text"), \
             patch.object(menu, "answer_callback_query"):
            menu.handle_callback("1", 55, "cb1", "ch:rm:demo")
        save.assert_not_called()
        self.assertIsNone(menu.pop_deletion("channel"))

    def test_undo_channel_restores_and_saves(self):
        menu.remember_deletion("channel", "demo")
        with patch.object(registry, "CHANNELS", ["other"]), \
             patch.object(registry, "save_channels_file") as save, \
             patch.object(menu, "edit_message_text"), \
             patch.object(menu, "answer_callback_query") as answer:
            handled = menu.handle_callback("1", 55, "cb1", "undo:channel")

        self.assertTrue(handled)
        self.assertIn("demo", registry.CHANNELS)
        save.assert_called_once_with()
        self.assertIn("demo", answer.call_args.args[1])

    def test_undo_channel_after_window_shows_alert_and_does_not_restore(self):
        with patch.object(registry, "CHANNELS", []), \
             patch.object(registry, "save_channels_file") as save, \
             patch.object(menu, "edit_message_text"), \
             patch.object(menu, "answer_callback_query") as answer:
            menu.handle_callback("1", 55, "cb1", "undo:channel")

        save.assert_not_called()
        self.assertEqual(registry.CHANNELS, [])
        self.assertTrue(answer.call_args.kwargs.get("show_alert"))


class RemoveTwitchCallbackTests(unittest.TestCase):
    def setUp(self):
        menu.forget_deletions()
        self.addCleanup(menu.forget_deletions)
        registry.TWITCH_RELOAD.clear()
        self.addCleanup(registry.TWITCH_RELOAD.clear)

    def test_removes_twitch_channel_signals_reload_and_offers_undo(self):
        with patch.object(registry, "TWITCH_CHANNELS", ["streamer"]), \
             patch.object(registry, "save_twitch_channels_file") as save, \
             patch.object(menu, "edit_message_text"), \
             patch.object(menu, "answer_callback_query"):
            menu.handle_callback("1", 55, "cb1", "tw:rm:streamer")

        self.assertEqual(registry.TWITCH_CHANNELS, [])
        save.assert_called_once_with()
        self.assertTrue(registry.TWITCH_RELOAD.is_set())
        self.assertEqual(menu.pop_deletion("twitch"), "streamer")

    def test_undo_twitch_restores_and_signals_reload(self):
        menu.remember_deletion("twitch", "streamer")
        with patch.object(registry, "TWITCH_CHANNELS", []), \
             patch.object(registry, "save_twitch_channels_file") as save, \
             patch.object(menu, "edit_message_text"), \
             patch.object(menu, "answer_callback_query"):
            menu.handle_callback("1", 55, "cb1", "undo:twitch")

        self.assertEqual(registry.TWITCH_CHANNELS, ["streamer"])
        save.assert_called_once_with()
        self.assertTrue(registry.TWITCH_RELOAD.is_set())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_menu.py -k "RemoveChannelCallbackTests or RemoveTwitchCallbackTests" -v`
Expected: FAIL — `handle_callback` возвращает `False` для `ch:rm:demo` (нет обработчика)

- [ ] **Step 3: Implement**

В `wheelsparser/menu.py` добавить в конец файла (перед секцией «Роутинг» — переместить `_STATIC_HANDLERS`/`_PREFIX_HANDLERS`/`handle_callback` в самый конец файла, если ещё не там; функции-обработчики должны быть определены до заполнения словарей):

```python
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
```

Дополнить регистрацию в `_STATIC_HANDLERS` и `_PREFIX_HANDLERS` (в конце файла):

```python
_STATIC_HANDLERS["undo:channel"] = _cb_undo_channel
_STATIC_HANDLERS["undo:twitch"] = _cb_undo_twitch
_PREFIX_HANDLERS["ch:rm:"] = _cb_remove_channel
_PREFIX_HANDLERS["tw:rm:"] = _cb_remove_twitch
```

Разместить новые функции-обработчики и эти четыре строки регистрации в
конце файла (после текущего `handle_callback` — порядок определений в
файле не влияет на работу: все строки верхнего уровня выполняются при
импорте модуля, до первого внешнего вызова `handle_callback`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_menu.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add wheelsparser/menu.py tests/test_menu.py
git commit -m "feat: add channel/twitch remove-undo callbacks to bot menu"
```

---

### Task 9: menu.py — удаление/восстановление ключевых слов (по индексу)

**Files:**
- Modify: `wheelsparser/menu.py`
- Test: `tests/test_menu.py`

**Interfaces:**
- Consumes: `registry.KEYWORDS`, `registry.KEYWORDS_LOCK`, `registry.save_keywords_file`.
- Produces: регистрирует `"wd:rm:"` в `_PREFIX_HANDLERS`, `"undo:word"` в `_STATIC_HANDLERS`.

- [ ] **Step 1: Write the failing tests**

Добавить в `tests/test_menu.py`:

```python
class RemoveWordCallbackTests(unittest.TestCase):
    def setUp(self):
        menu.forget_deletions()
        self.addCleanup(menu.forget_deletions)

    def test_removes_word_by_index_and_offers_undo(self):
        with patch.object(registry, "KEYWORDS", ["колесо", "фрибет"]), \
             patch.object(registry, "save_keywords_file") as save, \
             patch.object(menu, "edit_message_text"), \
             patch.object(menu, "answer_callback_query") as answer:
            handled = menu.handle_callback("1", 55, "cb1", "wd:rm:0")

        self.assertTrue(handled)
        self.assertEqual(registry.KEYWORDS, ["фрибет"])
        save.assert_called_once_with()
        self.assertIn("колесо", answer.call_args.args[1])
        self.assertEqual(menu.pop_deletion("word"), "колесо")

    def test_out_of_range_index_does_not_crash_and_refreshes_list(self):
        with patch.object(registry, "KEYWORDS", ["колесо"]), \
             patch.object(registry, "save_keywords_file") as save, \
             patch.object(menu, "edit_message_text") as edit, \
             patch.object(menu, "answer_callback_query") as answer:
            menu.handle_callback("1", 55, "cb1", "wd:rm:5")

        save.assert_not_called()
        self.assertEqual(registry.KEYWORDS, ["колесо"])
        self.assertTrue(answer.call_args.kwargs.get("show_alert"))
        edit.assert_called_once_with("1", 55, menu.words_section_text(), menu.words_list_keyboard())

    def test_undo_word_restores_and_saves(self):
        menu.remember_deletion("word", "колесо")
        with patch.object(registry, "KEYWORDS", ["фрибет"]), \
             patch.object(registry, "save_keywords_file") as save, \
             patch.object(menu, "edit_message_text"), \
             patch.object(menu, "answer_callback_query") as answer:
            menu.handle_callback("1", 55, "cb1", "undo:word")

        self.assertIn("колесо", registry.KEYWORDS)
        save.assert_called_once_with()
        self.assertIn("колесо", answer.call_args.args[1])

    def test_undo_word_does_not_duplicate_if_already_present(self):
        menu.remember_deletion("word", "колесо")
        with patch.object(registry, "KEYWORDS", ["колесо"]), \
             patch.object(registry, "save_keywords_file") as save, \
             patch.object(menu, "edit_message_text"), \
             patch.object(menu, "answer_callback_query"):
            menu.handle_callback("1", 55, "cb1", "undo:word")

        self.assertEqual(registry.KEYWORDS, ["колесо"])
        save.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_menu.py -k RemoveWordCallbackTests -v`
Expected: FAIL — `handle_callback` возвращает `False` для `wd:rm:0`

- [ ] **Step 3: Implement**

В `wheelsparser/menu.py` добавить:

```python
# ----------------------------------------------------------------------------
# Удаление / восстановление ключевых слов (по индексу — слово может быть
# длиннее, чем позволяет 64-байтный лимит callback_data)
# ----------------------------------------------------------------------------

def _cb_remove_word(chat_id: str, message_id: int, callback_id: str, raw_index: str) -> None:
    try:
        index = int(raw_index)
    except ValueError:
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
```

Дополнить регистрацию (там же, где для Task 8):

```python
_STATIC_HANDLERS["undo:word"] = _cb_undo_word
_PREFIX_HANDLERS["wd:rm:"] = _cb_remove_word
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_menu.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add wheelsparser/menu.py tests/test_menu.py
git commit -m "feat: add keyword remove-undo callbacks to bot menu"
```

---

### Task 10: menu.py — удаление/восстановление колеса из `/active`

**Files:**
- Modify: `wheelsparser/menu.py`
- Test: `tests/test_menu.py`

**Interfaces:**
- Consumes: `active_report.lookup_active_number(number: int) -> tuple[str | None, int]`, `storage.mark_wheel_removed(url: str) -> bool`, `storage.unmark_wheel_removed(url: str) -> bool`.
- Produces: регистрирует `"rmw:"` в `_PREFIX_HANDLERS`, `"undo:wheel"` в `_STATIC_HANDLERS`.

- [ ] **Step 1: Write the failing tests**

Добавить в `tests/test_menu.py` (добавить `from wheelsparser import active_report, storage` к импортам вверху файла):

```python
class RemoveWheelCallbackTests(unittest.TestCase):
    def setUp(self):
        menu.forget_deletions()
        self.addCleanup(menu.forget_deletions)
        active_report.forget_active_numbers()
        self.addCleanup(active_report.forget_active_numbers)

    def test_removes_wheel_by_number_and_offers_undo(self):
        active_report.remember_active_numbers(
            [(1, {"url": "https://betboom.ru/freestream/one"})]
        )
        with patch.object(storage, "mark_wheel_removed", return_value=True) as mark, \
             patch.object(menu, "answer_callback_query") as answer:
            handled = menu.handle_callback("1", 55, "cb1", "rmw:1")

        self.assertTrue(handled)
        mark.assert_called_once_with("https://betboom.ru/freestream/one")
        answer.assert_called_once()
        self.assertEqual(
            menu.pop_deletion("wheel"), "https://betboom.ru/freestream/one"
        )

    def test_unknown_number_shows_alert_without_marking(self):
        with patch.object(storage, "mark_wheel_removed") as mark, \
             patch.object(menu, "answer_callback_query") as answer:
            menu.handle_callback("1", 55, "cb1", "rmw:99")

        mark.assert_not_called()
        self.assertTrue(answer.call_args.kwargs.get("show_alert"))

    def test_already_removed_wheel_reports_without_touching_undo_slot(self):
        active_report.remember_active_numbers(
            [(1, {"url": "https://betboom.ru/freestream/one"})]
        )
        with patch.object(storage, "mark_wheel_removed", return_value=False), \
             patch.object(menu, "answer_callback_query"):
            menu.handle_callback("1", 55, "cb1", "rmw:1")

        self.assertIsNone(menu.pop_deletion("wheel"))

    def test_undo_wheel_calls_unmark(self):
        menu.remember_deletion("wheel", "https://betboom.ru/freestream/one")
        with patch.object(storage, "unmark_wheel_removed") as unmark, \
             patch.object(menu, "answer_callback_query") as answer:
            handled = menu.handle_callback("1", 55, "cb1", "undo:wheel")

        self.assertTrue(handled)
        unmark.assert_called_once_with("https://betboom.ru/freestream/one")
        answer.assert_called_once()

    def test_undo_wheel_after_window_shows_alert(self):
        with patch.object(storage, "unmark_wheel_removed") as unmark, \
             patch.object(menu, "answer_callback_query") as answer:
            menu.handle_callback("1", 55, "cb1", "undo:wheel")

        unmark.assert_not_called()
        self.assertTrue(answer.call_args.kwargs.get("show_alert"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_menu.py -k RemoveWheelCallbackTests -v`
Expected: FAIL — `handle_callback` возвращает `False` для `rmw:1`

- [ ] **Step 3: Implement**

В `wheelsparser/menu.py` добавить `from . import storage` и `from .active_report import lookup_active_number` в блок импортов, затем добавить:

```python
# ----------------------------------------------------------------------------
# Удаление / восстановление колеса из /active (кнопка ❌ под /active, /wheels)
# ----------------------------------------------------------------------------

def _cb_remove_wheel(chat_id: str, message_id: int, callback_id: str, raw_number: str) -> None:
    try:
        number = int(raw_number)
    except ValueError:
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
```

Дополнить регистрацию:

```python
_STATIC_HANDLERS["undo:wheel"] = _cb_undo_wheel
_PREFIX_HANDLERS["rmw:"] = _cb_remove_wheel
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_menu.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add wheelsparser/menu.py tests/test_menu.py
git commit -m "feat: add wheel remove-undo callbacks to bot menu"
```

---

### Task 11: active_report — `format_active_result` возвращает клавиатуру

**Files:**
- Modify: `wheelsparser/active_report.py:72-153` (`format_active_result`, `fire_active_check`)
- Test: `tests/test_bot.py` (класс `ActiveReportFormatTests`)

**Interfaces:**
- Produces: `format_active_result(active_items, total, unknown_count=0) -> tuple[str, dict[str, Any] | None]` (было `-> str`).
- Consumes (без изменений): `remember_active_numbers`, `forget_active_numbers`, `icon`.

**Важно:** этот модуль НЕ импортирует `menu.py` (иначе цикл: `menu` уже импортирует `active_report.lookup_active_number`). Клавиатура строится локальным приватным хелпером `_removal_keyboard`.

- [ ] **Step 1: Write the failing tests**

Добавить в `tests/test_bot.py` в класс `ActiveReportFormatTests`:

```python
    def test_format_active_result_returns_text_and_keyboard(self):
        items = [{
            "url": "https://betboom.ru/freestream/one",
            "found_at": "2026-07-30T20:40:31+03:00",
            "channel": "demo",
        }]
        text, keyboard = active_report.format_active_result(items, total=1)
        self.assertIn("Активные колёса", text)
        self.assertEqual(
            keyboard,
            {"inline_keyboard": [[{"text": "❌ 1", "callback_data": "rmw:1"}]]},
        )

    def test_format_active_result_keyboard_is_none_when_nothing_to_remove(self):
        text, keyboard = active_report.format_active_result([], total=2, unknown_count=0)
        self.assertIn("активных не найдено", text)
        self.assertIsNone(keyboard)

    def test_format_active_result_keyboard_is_none_on_check_failure(self):
        text, keyboard = active_report.format_active_result(None, total=0)
        self.assertIn("Не удалось проверить", text)
        self.assertIsNone(keyboard)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_bot.py -k test_format_active_result -v`
Expected: FAIL — `ValueError: not enough values to unpack (expected 2, got ...)` (текущая функция возвращает `str`)

- [ ] **Step 3: Implement**

В `wheelsparser/active_report.py` заменить сигнатуру и тело `format_active_result` (строки 72-122):

```python
def _removal_keyboard(numbered: list[tuple[int, dict[str, Any]]]) -> dict[str, Any]:
    """Клавиатура ❌ под /active: не через menu.py — тот импортирует этот
    модуль (lookup_active_number), обратный импорт создал бы цикл.
    """
    return {
        "inline_keyboard": [
            [{"text": f"❌ {number}", "callback_data": f"rmw:{number}"}]
            for number, _item in numbered
        ]
    }


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
            None,
        )
    # Все колёса вернули unknown — скорее всего сетевая ошибка.
    if not active_items and unknown_count > 0 and unknown_count == total:
        return (
            f"{icon('warn')} Не удалось определить статус {total} колёс "
            "(API не ответил).\n"
            "Попробуйте /active ещё раз через несколько секунд.",
            None,
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
            None,
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
```

Заменить тело `_run_and_send` внутри `fire_active_check` (строки 138-151):

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_bot.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add wheelsparser/active_report.py tests/test_bot.py
git commit -m "feat: attach removal keyboard to /active results"
```

---

### Task 12: bot.py — `/menu`, кнопка «☰ Меню» в `/start` и `/help`

**Files:**
- Modify: `wheelsparser/bot.py:1-64` (импорты, `BOT_COMMANDS`), `:214-234` (`cmd_start`, `cmd_help`), `:563-581` (`COMMAND_HANDLERS`)
- Test: `tests/test_bot.py`

**Interfaces:**
- Consumes: `menu.root_open_keyboard()`, `menu.root_text()`, `menu.root_menu_keyboard()` (из Task 5).
- Produces: `cmd_menu(chat_id: str, _argument: str) -> None`.

- [ ] **Step 1: Write the failing tests**

Добавить в `tests/test_bot.py` (в конец, перед `if __name__ == "__main__":`):

```python
class MenuCommandTests(unittest.TestCase):
    def test_menu_command_sends_root_keyboard(self):
        from wheelsparser import menu
        with patch.object(bot, "bot_send") as send:
            bot.handle_command("1", "/menu")
        send.assert_called_once_with("1", menu.root_text(), reply_markup=menu.root_menu_keyboard())

    def test_start_includes_menu_button(self):
        from wheelsparser import menu
        with patch.object(bot, "bot_send") as send:
            bot.handle_command("1", "/start")
        self.assertEqual(send.call_args.kwargs["reply_markup"], menu.root_open_keyboard())

    def test_help_includes_menu_button(self):
        from wheelsparser import menu
        with patch.object(bot, "bot_send") as send:
            bot.handle_command("1", "/help")
        self.assertEqual(send.call_args.kwargs["reply_markup"], menu.root_open_keyboard())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_bot.py -k MenuCommandTests -v`
Expected: FAIL — `/menu` не распознаётся (`bot_send` не вызван) и `bot_send` для `/start`/`/help` вызывается без `reply_markup`

- [ ] **Step 3: Implement**

В `wheelsparser/bot.py` добавить импорт после `from . import db, registry` (строка 18):

```python
from . import db, menu, registry
```

В список `BOT_COMMANDS` (строка 47) добавить пункт вторым (после `start`):

```python
BOT_COMMANDS = [
    {"command": "start", "description": "О боте"},
    {"command": "menu", "description": "Меню управления кнопками"},
    {"command": "wheels", "description": f"Колёса за последние {WHEELS_WINDOW_MINUTES} мин"},
    ...  # остальные пункты без изменений
]
```

Заменить `cmd_start` (строки 214-230), добавив `reply_markup`:

```python
def cmd_start(chat_id: str, _argument: str) -> None:
    total = len(registry.channels_snapshot())
    bot_send(
        chat_id,
        f"{icon('start')} <b>WheelsParser</b>\n"
        "Я мониторю Telegram-каналы стримеров и присылаю ссылки на "
        "фрибет-колёса BetBoom сразу после публикации — ничего "
        "запрашивать не нужно.\n\n"
        "Что я умею:\n"
        f"{icon('link')} ловлю ссылки на колёса в {total} каналах\n"
        f"{icon('scan')} проверяю каналы каждые {CHECK_INTERVAL} сек\n\n"
        "Самое полезное:\n"
        f"/wheels — колёса за последние {WHEELS_WINDOW_MINUTES} минут\n"
        "/active — живые колёса за сегодня\n"
        "/status — статистика находок\n\n"
        "Полный список команд — /help",
        reply_markup=menu.root_open_keyboard(),
    )


def cmd_help(chat_id: str, _argument: str) -> None:
    bot_send(chat_id, help_text(), reply_markup=menu.root_open_keyboard())


def cmd_menu(chat_id: str, _argument: str) -> None:
    bot_send(chat_id, menu.root_text(), reply_markup=menu.root_menu_keyboard())
```

(Заменяет старые `cmd_start` и `cmd_help`; `cmd_menu` — новая функция сразу после них.)

В `help_text()` добавить строку про `/menu` сразу после заголовка `"<b>Команды:</b>\n"`:

```python
def help_text() -> str:
    return (
        "<b>Команды:</b>\n"
        "/menu — то же самое, но кнопками\n"
        f"/wheels — колёса за последние {WHEELS_WINDOW_MINUTES} минут\n"
        ...  # остальное без изменений
    )
```

В `COMMAND_HANDLERS` (строка 563) добавить пункт после `"/start": cmd_start,`:

```python
COMMAND_HANDLERS: dict[str, Callable[[str, str], None]] = {
    "/start": cmd_start,
    "/menu": cmd_menu,
    "/help": cmd_help,
    ...  # остальные пункты без изменений
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_bot.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add wheelsparser/bot.py tests/test_bot.py
git commit -m "feat: add /menu command and menu button to /start and /help"
```

---

### Task 13: bot.py — `callback_query` в `bot_loop`

**Files:**
- Modify: `wheelsparser/bot.py:20-45` (импорты из `.telegram_api`), `:563-596` (`COMMAND_HANDLERS`, `handle_command`), `:602-661` (`bot_loop`)
- Test: `tests/test_bot.py`

**Interfaces:**
- Consumes: `menu.handle_callback(chat_id, message_id, callback_id, data) -> bool` (Task 7-10), `telegram_api.answer_callback_query`.
- Produces: `handle_callback(chat_id: str, message_id: int, callback_id: str, data: str) -> None`.

- [ ] **Step 1: Write the failing tests**

Добавить в `tests/test_bot.py`:

```python
class CallbackDispatchTests(unittest.TestCase):
    def test_do_wheels_runs_cmd_wheels_and_answers(self):
        with patch.object(bot, "cmd_wheels") as cmd, \
             patch.object(bot, "answer_callback_query") as answer:
            bot.handle_callback("1", 55, "cb1", "m:do_wheels")
        cmd.assert_called_once_with("1", "")
        answer.assert_called_once_with("cb1")

    def test_do_active_runs_cmd_active(self):
        with patch.object(bot, "cmd_active") as cmd, \
             patch.object(bot, "answer_callback_query"):
            bot.handle_callback("1", 55, "cb1", "m:do_active")
        cmd.assert_called_once_with("1", "")

    def test_do_status_runs_cmd_status(self):
        with patch.object(bot, "cmd_status") as cmd, \
             patch.object(bot, "answer_callback_query"):
            bot.handle_callback("1", 55, "cb1", "m:do_status")
        cmd.assert_called_once_with("1", "")

    def test_do_top_runs_cmd_top(self):
        with patch.object(bot, "cmd_top") as cmd, \
             patch.object(bot, "answer_callback_query"):
            bot.handle_callback("1", 55, "cb1", "m:do_top")
        cmd.assert_called_once_with("1", "")

    def test_unrecognized_data_falls_through_to_menu(self):
        from wheelsparser import menu
        with patch.object(menu, "handle_callback", return_value=True) as delegate:
            bot.handle_callback("1", 55, "cb1", "m:root")
        delegate.assert_called_once_with("1", 55, "cb1", "m:root")


class CallbackQueryLoopTests(unittest.TestCase):
    """update с callback_query обрабатывается в bot_loop наравне с message."""

    def test_bot_loop_dispatches_trusted_callback_and_advances_offset(self):
        update = {
            "update_id": 10,
            "callback_query": {
                "id": "cb1",
                "data": "m:root",
                "message": {"message_id": 77, "chat": {"id": 42}},
            },
        }
        response = Mock(status_code=200)
        response.raise_for_status.return_value = None
        response.json.return_value = {"result": [update]}
        session = Mock()
        session.get.return_value = response
        session.post.return_value = response

        with patch.object(bot, "BOT_SESSION", session), \
             patch.object(bot, "TELEGRAM_CHAT_ID", "42"), \
             patch.object(bot, "load_bot_offset", return_value=0), \
             patch.object(bot, "save_bot_offset") as save_offset, \
             patch.object(bot, "handle_callback") as handle, \
             patch.object(bot.STOP_EVENT, "is_set", side_effect=[False, True]):
            bot.bot_loop()

        handle.assert_called_once_with("42", 77, "cb1", "m:root")
        save_offset.assert_called_once_with(11)

    def test_bot_loop_ignores_callback_from_untrusted_chat(self):
        update = {
            "update_id": 10,
            "callback_query": {
                "id": "cb1",
                "data": "m:root",
                "message": {"message_id": 77, "chat": {"id": 999}},
            },
        }
        response = Mock(status_code=200)
        response.raise_for_status.return_value = None
        response.json.return_value = {"result": [update]}
        session = Mock()
        session.get.return_value = response
        session.post.return_value = response

        with patch.object(bot, "BOT_SESSION", session), \
             patch.object(bot, "TELEGRAM_CHAT_ID", "42"), \
             patch.object(bot, "load_bot_offset", return_value=0), \
             patch.object(bot, "save_bot_offset") as save_offset, \
             patch.object(bot, "handle_callback") as handle, \
             patch.object(bot.STOP_EVENT, "is_set", side_effect=[False, True]):
            bot.bot_loop()

        handle.assert_not_called()
        # offset всё равно продвигается, чтобы бэклог не повторялся:
        save_offset.assert_called_once_with(11)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_bot.py -k "CallbackDispatchTests or CallbackQueryLoopTests" -v`
Expected: FAIL — `AttributeError: <module 'wheelsparser.bot'> does not have the attribute 'handle_callback'`

- [ ] **Step 3: Implement**

В `wheelsparser/bot.py` заменить импорт из `.telegram_api` (строка 43), добавив `answer_callback_query`:

```python
from .telegram_api import answer_callback_query, bot_send
```

После определения `COMMAND_HANDLERS` (строка 581, перед `handle_command`) добавить:

```python
_MENU_RUN_HANDLERS: dict[str, Callable[[str, str], None]] = {
    "m:do_wheels": cmd_wheels,
    "m:do_active": cmd_active,
    "m:do_status": cmd_status,
    "m:do_top": cmd_top,
}


def handle_callback(chat_id: str, message_id: int, callback_id: str, data: str) -> None:
    """Разбирает callback inline-кнопки.

    Действия, запускающие существующую команду (m:do_*), обрабатываются
    здесь — им нужны cmd_wheels/cmd_active/cmd_status/cmd_top из этого
    модуля. Всё остальное (навигация, списки, undo) — в menu.handle_callback,
    который bot.py не трогает, чтобы не тащить его внутренности сюда.
    Неопознанный data молча игнорируется — как неизвестные команды.
    """
    run_command = _MENU_RUN_HANDLERS.get(data)
    if run_command is not None:
        answer_callback_query(callback_id)
        run_command(chat_id, "")
        return
    menu.handle_callback(chat_id, message_id, callback_id, data)
```

Заменить тело цикла обработки обновлений в `bot_loop` (строки 628-660):

```python
        offset_before = offset
        stale_count = 0
        for update in updates:
            offset = max(offset, int(update.get("update_id", 0)) + 1)
            callback = update.get("callback_query")
            if callback is not None:
                cb_message = callback.get("message") or {}
                chat_id = str((cb_message.get("chat") or {}).get("id", ""))
                if not chat_id or not TELEGRAM_CHAT_ID or chat_id != TELEGRAM_CHAT_ID:
                    # Тот же принцип, что и для команд: без доверенного
                    # chat_id callback'и не обрабатываются вовсе.
                    continue
                callback_id = str(callback.get("id", ""))
                message_id = int(cb_message.get("message_id", 0))
                data = str(callback.get("data", ""))
                try:
                    handle_callback(chat_id, message_id, callback_id, data)
                except Exception:
                    log.exception("Бот: ошибка обработки callback %r", data)
                continue
            message = update.get("message") or {}
            text = str(message.get("text") or "")
            chat_id = str((message.get("chat") or {}).get("id", ""))
            if not chat_id or not text.startswith("/"):
                continue
            if not TELEGRAM_CHAT_ID or chat_id != TELEGRAM_CHAT_ID:
                # Команды принимаем только из доверенного чата. Пустой
                # TELEGRAM_CHAT_ID означал бы «командовать может кто угодно»,
                # поэтому без него команды полностью отключены.
                continue
            # Устаревшие команды (например, отправленные, пока парсер лежал)
            # подтверждаем сдвигом offset, но не выполняем: отвечать на
            # команды суточной давности бессмысленно и создаёт спам в чате.
            message_date = int(message.get("date") or 0)
            if message_date and time.time() - message_date > STALE_COMMAND_SECONDS:
                stale_count += 1
                continue
            try:
                handle_command(chat_id, text)
            except Exception:
                log.exception("Бот: ошибка обработки команды %r", text)
        if offset > offset_before:
            save_bot_offset(offset)
        if stale_count:
            log.info(
                "%s Бот: пропущено устаревших команд из бэклога: %s",
                icon("bell"),
                stale_count,
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_bot.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add wheelsparser/bot.py tests/test_bot.py
git commit -m "feat: dispatch callback_query updates in bot_loop"
```

---

### Task 14: bot.py — клавиатуры под `/channels`, `/twitch`, `/words`

**Files:**
- Modify: `wheelsparser/bot.py:361-437` (`cmd_channels`, `cmd_words`, `cmd_twitch`)
- Test: `tests/test_bot.py`

**Interfaces:**
- Consumes: `menu.channels_list_keyboard()`, `menu.twitch_list_keyboard()`, `menu.words_list_keyboard()`.

- [ ] **Step 1: Write the failing tests**

Добавить в `tests/test_bot.py`:

```python
class ListCommandKeyboardTests(unittest.TestCase):
    def test_channels_command_attaches_removal_keyboard(self):
        from wheelsparser import menu
        with patch.object(registry, "CHANNELS", ["demo"]), \
             patch.object(bot, "bot_send") as send:
            bot.handle_command("1", "/channels")
        self.assertEqual(send.call_args.kwargs["reply_markup"], menu.channels_list_keyboard())

    def test_twitch_command_attaches_removal_keyboard(self):
        from wheelsparser import menu
        with patch.object(registry, "TWITCH_CHANNELS", ["streamer"]), \
             patch.object(bot, "bot_send") as send:
            bot.handle_command("1", "/twitch")
        self.assertEqual(send.call_args.kwargs["reply_markup"], menu.twitch_list_keyboard())

    def test_words_command_attaches_removal_keyboard(self):
        from wheelsparser import menu
        with patch.object(registry, "KEYWORDS", ["колесо"]), \
             patch.object(bot, "bot_send") as send:
            bot.handle_command("1", "/words")
        self.assertEqual(send.call_args.kwargs["reply_markup"], menu.words_list_keyboard())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_bot.py -k ListCommandKeyboardTests -v`
Expected: FAIL — `KeyError: 'reply_markup'` (текущий `bot_send` вызывается без этого kwarg)

- [ ] **Step 3: Implement**

Заменить `cmd_channels` (строки 361-364):

```python
def cmd_channels(chat_id: str, _argument: str) -> None:
    channels = registry.channels_snapshot()
    listing = "\n".join(f"• @{html.escape(channel)}" for channel in channels)
    bot_send(
        chat_id,
        f"<b>Каналы ({len(channels)}):</b>\n{listing}",
        reply_markup=menu.channels_list_keyboard(),
    )
```

Заменить `cmd_words` (строки 367-373):

```python
def cmd_words(chat_id: str, _argument: str) -> None:
    keywords = registry.keywords_snapshot()
    if not keywords:
        bot_send(chat_id, "Ключевых слов пока нет. Добавьте: /addword колесо")
        return
    listing = "\n".join(f"• {html.escape(keyword)}" for keyword in keywords)
    bot_send(
        chat_id,
        f"<b>Ключевые слова ({len(keywords)}):</b>\n{listing}",
        reply_markup=menu.words_list_keyboard(),
    )
```

Заменить `cmd_twitch` (строки 429-437):

```python
def cmd_twitch(chat_id: str, _argument: str) -> None:
    channels = registry.twitch_channels_snapshot()
    if not channels:
        bot_send(chat_id, "Twitch-каналов пока нет. Добавьте: /addtwitch channel")
        return
    listing = "\n".join(
        f"• twitch.tv/{html.escape(channel)}" for channel in channels
    )
    bot_send(
        chat_id,
        f"<b>Twitch-каналы ({len(channels)}):</b>\n{listing}",
        reply_markup=menu.twitch_list_keyboard(),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_bot.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add wheelsparser/bot.py tests/test_bot.py
git commit -m "feat: attach removal keyboards to /channels, /twitch, /words"
```

---

### Task 15: bot.py — нумерация и кнопки ❌ под `/wheels`

**Files:**
- Modify: `wheelsparser/bot.py:19` (импорт из `.active_report`), `:99-124` (`help_text`), `:256-274` (`cmd_wheels`), `:296-328` (`_resolve_wheel_to_remove`)
- Test: `tests/test_bot.py`

**Interfaces:**
- Consumes: `active_report.remember_active_numbers` (уже существует), `menu.wheel_removal_keyboard` (Task 5).

- [ ] **Step 1: Write the failing test**

Добавить в `tests/test_bot.py` в класс `HistoryReportTests`:

```python
    def test_wheels_command_attaches_removal_keyboard_and_shares_numbering(self):
        from wheelsparser import active_report, menu
        self.store(url="https://betboom.ru/freestream/only")
        self.addCleanup(active_report.forget_active_numbers)

        with patch.object(bot, "bot_send") as send:
            bot.cmd_wheels("1", "")

        keyboard = send.call_args.kwargs["reply_markup"]
        self.assertEqual(len(keyboard["inline_keyboard"]), 1)
        self.assertEqual(keyboard["inline_keyboard"][0][0]["callback_data"], "rmw:1")
        url, known = active_report.lookup_active_number(1)
        self.assertEqual(url, "https://betboom.ru/freestream/only")
        self.assertEqual(known, 1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_bot.py -k test_wheels_command_attaches_removal_keyboard_and_shares_numbering -v`
Expected: FAIL — `KeyError: 'reply_markup'`

- [ ] **Step 3: Implement**

В `wheelsparser/bot.py` заменить импорт из `.active_report` (строка 19):

```python
from .active_report import (
    fire_active_check,
    lookup_active_number,
    remember_active_numbers,
)
```

Заменить `cmd_wheels` (строки 256-274):

```python
def cmd_wheels(chat_id: str, _argument: str) -> None:
    wheels = recent_wheels()
    if not wheels:
        bot_send(
            chat_id,
            f"За последние {WHEELS_WINDOW_MINUTES} минут новых колёс не найдено. "
            "Как только появится ссылка — пришлю её сразу.",
        )
        return
    lines = [f"{icon('link')} <b>Колёса за последние {WHEELS_WINDOW_MINUTES} минут:</b>"]
    numbered = list(enumerate(wheels, start=1))
    keyboard_rows: list[tuple[int, str]] = []
    for number, item in numbered:
        found_at = str(item.get("found_at", ""))
        found_time = found_at[11:16] if len(found_at) >= 16 else found_at
        channel = html.escape(str(item.get("channel", "")))
        # normalize_url: старые записи могли сохранить URL с &amp; и utm-хвостом.
        url = html.escape(normalize_url(str(item.get("url", ""))))
        referral_mark = " ⚠️ для рефералов" if item.get("referral") else ""
        lines.append(f"• {found_time} — @{channel}{referral_mark}\n{url}")
        keyboard_rows.append((number, f"❌ {found_time} @{channel}"))
    # Общая нумерация с /active: /removewheel и кнопки ❌ работают по
    # номерам из ПОСЛЕДНЕГО показанного списка, каким бы он ни был.
    remember_active_numbers(numbered)
    bot_send(
        chat_id, "\n".join(lines), reply_markup=menu.wheel_removal_keyboard(keyboard_rows)
    )
```

В `help_text()` обновить строку про `/removewheel` (строка 104-105):

```python
        "/removewheel номер — убрать колесо из /active до конца суток\n"
        "    (номер — из последнего ответа /active или /wheels; можно "
        "ссылкой или кнопкой ❌)\n"
```

В `_resolve_wheel_to_remove` обновить подсказки (строки 305-311):

```python
    if raw.isdigit():
        number = int(raw)
        url, known = lookup_active_number(number)
        if not url:
            if known:
                hint = f"В последнем ответе /active или /wheels номера 1–{known}."
            else:
                hint = (
                    "Сначала вызовите /active или /wheels — номера колёс "
                    "берутся из последнего ответа."
                )
            bot_send(
                chat_id,
                f"{icon('warn')} Не нашёл колесо с номером {number}. {hint}",
            )
            return None
        return url
```

В `wheelsparser/active_report.py` уточнить комментарий у `_last_active_numbers` (строки 24-28), отразив, что нумерация теперь общая для `/active` и `/wheels`:

```python
# Нумерация колёс из последнего ответа /active или /wheels: номер →
# канонический URL. По этим номерам работает /removewheel <номер> и
# кнопки ❌ под обеими командами. Обновляется из бот-потока (/wheels)
# и из фонового active-api-потока (/active) — доступ только под локом.
# Хранится в памяти: после рестарта нужно заново вызвать /active или
# /wheels, чтобы получить актуальные номера.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_bot.py -v`
Expected: PASS

- [ ] **Step 5: Run the full test suite**

Run: `python -m pytest tests/ -v`
Expected: PASS (все файлы — regression по всему проекту)

- [ ] **Step 6: Commit**

```bash
git add wheelsparser/bot.py wheelsparser/active_report.py tests/test_bot.py
git commit -m "feat: number /wheels entries and attach removal keyboard"
```

---

### Task 16: README — строка `/menu` в таблице команд

**Files:**
- Modify: `README.md:20-32` (таблица «Команды бота»)

- [ ] **Step 1: Add the row**

В `README.md` в таблице команд бота добавить строку сразу после заголовка таблицы (перед строкой `/active`):

```markdown
| `/menu` | Меню управления кнопками (та же функциональность, что текстовые команды) |
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: document /menu command in README"
```

---

## Self-Review Notes

- **Покрытие спеки:** архитектура (menu.py + telegram_api + bot.py wiring) — Tasks 1-3, 5-13; корневое меню и разделы — Task 5, 7; callback-схема (`ch:rm:`, `tw:rm:`, `wd:rm:<idx>`, `rmw:<n>`) — Tasks 8-10; отмена 10 сек — Task 6, 8-10; обработка ошибок (answerCallbackQuery всегда, «not modified» не логируется, bounds-check для слов, untrusted/unknown молча игнорируются) — Tasks 2, 3, 9, 13; тесты — по одному классу на функцию в каждом task; вне рамок (FSM, reply-клавиатура, подтверждение «да/нет») — сознательно не реализовано, как и решили на брейнсторминге.
- **Циклические импорты:** проверено — `menu.py` импортирует `active_report` и `storage`, но не `bot.py`; `active_report.py` не импортирует `menu.py` (клавиатура строится локально в Task 11); `bot.py` импортирует `menu.py` — единственное направление зависимости.
- **Согласованность сигнатур:** `format_active_result` меняет тип возврата (`str` → `tuple[str, dict|None]`) только в Task 11, единственном месте, где функция вызывается (`fire_active_check`) — оба места правятся в одном task.
- **Расширение общей нумерации на `/wheels`** (Task 15) — сознательное решение: `/removewheel` и так был документирован как «номер из последнего ответа /active», распространение на «/active или /wheels» не меняет модель, только формулировку подсказок.
