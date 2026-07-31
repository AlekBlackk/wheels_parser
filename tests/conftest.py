"""Изоляция тестов от реального состояния парсера.

config.py фиксирует пути при импорте, поэтому WHEELSPARSER_DATA_DIR
нужно подменить ДО первого импорта wheelsparser — на уровне модуля
conftest, а не в фикстуре. Все файлы состояния (seen_ids.json,
wheels.db и т.д.) уходят во временный каталог и не трогают data/.
"""

from __future__ import annotations

import os
import tempfile

_TEST_DATA_DIR = tempfile.mkdtemp(prefix="wheelsparser-tests-")
os.environ["WHEELSPARSER_DATA_DIR"] = _TEST_DATA_DIR
