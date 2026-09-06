"""WheelsParser: мониторинг Telegram-каналов и Twitch-чатов на ссылки BetBoom.

Точка входа — :func:`wheelsparser.app.main`. Запуск из каталога проекта:
``python -m wheelsparser`` (в Windows — ``run.bat``).

Слои пакета (стрелка — направление зависимости, циклов нет):

    config → logging_setup → net/runtime/registry/storage/urls/timeutils
           → db/keywords/alerts/betboom/telegram_api
           → telegram_scrape/channel_health/retries
           → active_report/reports → menu → bot/twitch/predictive/parser → app

Файлы машинного состояния (wheels.db, seen_ids.json и прочие)
лежат в каталоге data/ (см. ``config.DATA_DIR``), а списки-источники
правды (channels.txt, keywords.txt, twitch_channels.txt) — в корне (``config.BASE_DIR``).
"""

__all__ = ["__version__"]

__version__ = "2.0.0"
