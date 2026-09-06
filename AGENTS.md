# AGENTS.md

Инструкции для AI-агентов, работающих с репозиторием WheelsParser.

## Что это за проект

Мониторинг Telegram-каналов и Twitch-чатов на ссылки BetBoom (колёса фрибетов).
Находки уходят уведомлениями в Telegram, управление — через бота (команды и inline-меню).

Python 3.10+, без фреймворков. Зависимости: `requests`, `beautifulsoup4`,
`python-dotenv`, `colorama`. Всё остальное — стандартная библиотека.

## Язык

Докстринги, комментарии, README, ответы пользователю — **на русском**.
Английские — идентификаторы (имена функций, переменных, констант) и сообщения
коммитов.
`RUF001`/`RUF002`/`RUF003` в ruff отключены именно поэтому — не включать обратно.

## Команды

Установка окружения:

```bash
pip install -e .[dev]
```

Запуск:

```bash
python -m wheelsparser
```

В Windows — `run.bat` (сам создаёт `.venv` и ставит зависимости).

Проверки перед коммитом — те же три, что гоняет CI (`.github/workflows/ci.yml`,
матрица ubuntu/windows × 3.10/3.12):

```bash
ruff check . && mypy && pytest
```

Один тестовый файл:

```bash
pytest tests/test_parser.py
```

## Архитектура

Слои пакета `wheelsparser/`, стрелка — направление зависимости. **Циклов нет,
и их появление недопустимо:**

```
config → logging_setup → net/runtime/registry/storage/urls/timeutils
       → db/keywords/alerts/betboom/telegram_api
       → telegram_scrape/channel_health/retries
       → active_report/reports → menu → bot/twitch/predictive/parser → app
```

| Модуль | Роль |
|---|---|
| `config.py` | Пути, переменные окружения, константы. Ни от чего внутри пакета не зависит, читается первым. Значения фиксируются при импорте. |
| `logging_setup.py` | Общий `log`, ротация файла, маскировка токена. Обработчики ставятся только в `setup_logging()` из `app.main()`. |
| `net.py` | HTTP-сессии по потокам. `requests.Session` не потокобезопасна — у каждого потока своя сессия с фиксированным владельцем. `ThreadLocalSession` для пулов воркеров. |
| `runtime.py` | Стоп-флаг, сигналы, single instance, `supervise()` для перезапуска упавших потоков, хендшейк внепланового обхода каналов по `/active` (`request_rescan` / `wait_before_next_cycle` / `take_rescan_request` / `mark_rescan_done`). |
| `registry.py` | Списки под мониторингом (каналы, слова, Twitch). Источник правды — txt-файлы, меняются на лету командами бота. Атомарная запись через `atomic_write_text`. |
| `storage.py` | Мелкий JSON-стейт, атомарная запись (temp + replace). |
| `db.py` | История находок в SQLite (`data/wheels.db`), WAL, типизация `WheelEntry`, фабрика `make_wheel_entry`, соединение на поток. |
| `urls.py` | Канонизация ссылок и хэш поста — единая форма URL для дедупликации, кулдауна, кэшей. |
| `timeutils.py` | Всё время проекта — МСК, независимо от таймзоны сервера. |
| `keywords.py` | Поиск ключевых слов с учётом русской морфологии. |
| `alerts.py` | Кулдаун повторных уведомлений, общий для Telegram и Twitch. |
| `betboom.py` | Клиент API BetBoom: `active` / `soon` / `expired` / `unknown`. Общая функция `process_candidate_wheel`. |
| `telegram_api.py` | Отправка сообщений в Telegram Bot API. |
| `telegram_scrape.py` | BeautifulSoup-парсинг HTML публичных Telegram-каналов (`fetch_channel`, превью, форварды). |
| `channel_health.py` | Здоровье каналов: подсчёт пустых постов, серий сетевых ошибок, алерты об изменении вёрстки. |
| `retries.py` | Очереди и повторные попытки: доставка недошедших уведомлений и перепроверка `expired` колёс. |
| `reports.py` | Генерация форматированных текстов для команд бота (`/help`, `/status`, `/top`, списки каналов). |
| `menu.py` | Inline-меню бота, роутинг callback'ов, undo. |
| `active_report.py` | Команда `/active` — внеплановый обход каналов (`request_rescan`), затем параллельная проверка колёс в пуле потоков. |
| `predictive.py` | Сканер серий стримеров (перебор номеров слагов для обнаружения колёс до публикации). |
| `twitch.py` | Анонимный IRC-ридер чатов, два потока: `twitch-irc` и `twitch-worker`. |
| `parser.py` | Координатор обхода Telegram-каналов, сбор и сохранение находок. |
| `bot.py` | Команды бота, валидация отправителя, цикл `getUpdates`. |
| `app.py` | Точка входа, запуск и координация потоков. |

### Потоки

`parser`, `bot`, `twitch-irc`, `twitch-worker`, `active-api`, `predictive`. Все
рабочие потоки поднимаются через `runtime.supervise` — необработанное исключение
перезапускает поток и уходит сервисным уведомлением.

## Правила, которые легко нарушить

- **Сессии не делить между потоками.** Новый поток ходит в сеть — заводи ему
  свою сессию в `net.py`, не переиспользуй чужую.
- **Списки `registry` брать через модуль**: `registry.CHANNELS`, а не
  `from .registry import CHANNELS` — второе замораживает ссылку и ломает
  подмену в тестах. Чтение и изменение — под соответствующим локом.
- **`unknown` от API BetBoom — fail-open.** Лучше лишнее уведомление, чем
  пропущенное живое колесо. Не менять на fail-closed.
- **`menu.py` не импортирует `bot.py`** (цикл). Обратное направление —
  `bot.py` делегирует в `menu.handle_callback` — норма.
- **Ключевые слова в Twitch не ищутся.** Только ссылки `betboom.ru/freestream`
  и только от стримера, модераторов, VIP и известных ботов. Иначе спам.
- **Запись состояния — атомарная** (`storage.py`), а не `open(..., "w")`.
- **Команды бота — только из `TELEGRAM_CHAT_ID`.** Неизвестные команды
  игнорируются молча.

## Тесты

`unittest` через раннер `pytest`, каталог `tests/`.

`tests/conftest.py` подменяет `WHEELSPARSER_DATA_DIR` **на уровне модуля**, до
первого импорта `wheelsparser` — `config.py` фиксирует пути при импорте, в
фикстуре было бы поздно. Не переносить это в фикстуру.

Для тестов с базой — `tests/dbfixture.use_temp_db(self)`.

Сеть в тестах не трогать: HTTP-вызовы мокать.

## Файлы и секреты

Не коммитить и не читать в выдачу: `.env`, содержимое `data/`, `parser.log`,
`wheels.db*`. Токены и chat_id — только через `.env` (шаблон — `env.example`).
В логах токен маскируется `logging_setup.py`; при добавлении новых логов
следить, чтобы токен не утёк мимо маскировки.

Изменяемые на лету списки (`channels.txt`, `keywords.txt`,
`twitch_channels.txt`) — в `.gitignore`: правит их бот, не репозиторий.
