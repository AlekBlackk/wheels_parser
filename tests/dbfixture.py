"""Изолированная база находок для тестов.

Каждый тест получает свой каталог: соединение кэшируется по пути, поэтому
подмены DB_FILE достаточно, чтобы прошлые тесты не влияли на текущий.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any
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


def entries_since(cutoff: datetime) -> list[dict[str, Any]]:
    """Все записи не старше cutoff, от старых к свежим (включая записи
    без url — посты с ключевыми словами).

    Только для тестов: прод-код всегда фильтрует чтение либо по url
    (db.wheels_since — колёса), либо по notified (db.pending_retry —
    недоставленные); «всё подряд» нужно только для проверки сырого
    round-trip'а хранения (миграция, порядок, обрезка).
    """
    rows = db.connection().execute(
        "SELECT * FROM wheels WHERE found_at >= ? ORDER BY found_at, id",
        (cutoff.isoformat(timespec="seconds"),),
    )
    return [db.row_to_entry(row) for row in rows]
