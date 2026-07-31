"""Изолированная база находок для тестов.

Каждый тест получает свой каталог: соединение кэшируется по пути, поэтому
подмены DB_FILE достаточно, чтобы прошлые тесты не влияли на текущий.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wheelsparser import db


def use_temp_db(case: unittest.TestCase, *, init: bool = True) -> Path:
    """Подключает тесту пустую базу и возвращает её каталог."""
    tmp = Path(tempfile.mkdtemp(prefix="wheelsparser-db-"))
    targets = {"DB_FILE": tmp / "wheels.db", "OUTPUT_FILE": tmp / "freebets.json"}
    for name, value in targets.items():
        patcher = patch.object(db, name, value)
        patcher.start()
        case.addCleanup(patcher.stop)
    case.addCleanup(db.close_connection)
    if init:
        db.init_db()
    return tmp
