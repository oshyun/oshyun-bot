#!/usr/bin/env python3
"""agy_wrapper.py — `src/bot/agy_wrapper.py` 실행 진입점."""

from __future__ import annotations

import os
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR = os.path.dirname(_SCRIPT_DIR)
_SRC_DIR = os.path.join(_ROOT_DIR, "src", "bot")

if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

try:
    import bot.core.paths as paths  # noqa: F401  # .env 로드 및 경로 해석 초기화
except ImportError:
    pass

from bot.engine.agy_wrapper import main

if __name__ == "__main__":
    sys.exit(main())
