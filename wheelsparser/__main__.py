"""Запуск пакета: ``python -m wheelsparser``."""

from __future__ import annotations

import sys

from .app import main
from .logging_setup import log

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        log.exception("Критическая ошибка")
        sys.exit(1)
