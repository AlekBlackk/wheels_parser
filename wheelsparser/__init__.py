"""WheelsParser: мониторинг Telegram-каналов и Twitch-чатов на ссылки BetBoom.

Точка входа — :func:`wheelsparser.app.main`. Запуск из каталога проекта:
``python -m wheelsparser`` (в Windows — ``run.bat``).

Слои пакета (стрелка — направление зависимости, циклов нет):

    config → logging_setup → net/runtime/registry/storage/urls/timeutils
           → keywords/alerts/betboom/telegram_api
           → active_report → bot / twitch / parser → app

Файлы состояния (channels.txt, wheels.db, seen_ids.json и прочие)
лежат в корне репозитория, на уровень выше каталога пакета:
см. ``config.BASE_DIR``.
"""

__all__ = ["__version__"]

__version__ = "2.0.0"
