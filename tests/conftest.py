"""Изоляция тестов от реального состояния парсера.

config.py фиксирует пути при импорте, поэтому WHEELSPARSER_DATA_DIR
нужно подменить ДО первого импорта wheelsparser — на уровне модуля
conftest, а не в фикстуре. Все файлы состояния (seen_ids.json,
wheels.db и т.д.) уходят во временный каталог и не трогают data/.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile

_TEST_DATA_DIR = tempfile.mkdtemp(prefix="wheelsparser-tests-")
atexit.register(lambda: shutil.rmtree(_TEST_DATA_DIR, ignore_errors=True))
os.environ["WHEELSPARSER_DATA_DIR"] = _TEST_DATA_DIR
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["TELEGRAM_CHAT_ID"] = ""
os.environ["PRECHECK_WHEELS"] = "true"
os.environ["REALERT_COOLDOWN_MINUTES"] = "30"
os.environ["USE_COLORS"] = "false"
