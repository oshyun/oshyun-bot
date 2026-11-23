#!/usr/bin/env python3
"""agy_wrapper.py — `bot.engine.agy_wrapper` 실행 진입점."""

from __future__ import annotations

import os
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR = os.path.dirname(_SCRIPT_DIR)
_SRC_DIR = os.path.join(_ROOT_DIR, "src")

if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

try:
    import bot.core.paths as paths  # noqa: F401  # .env 로드 및 경로 해석 초기화
except ImportError:
    # python-dotenv 미설정 환경(system python 등): .env 로드를 생략하고 진행하는 폴백.
    # paths 가 import 하는 load_dotenv 를 no-op 스텁으로 대체해 크래시를 막는다.
    import types
    _dotenv_stub = types.ModuleType("dotenv")
    _dotenv_stub.load_dotenv = lambda *args, **kwargs: False
    sys.modules.setdefault("dotenv", _dotenv_stub)
    import bot.core.paths as paths  # noqa: F401

from bot.engine.agy_wrapper import main

if __name__ == "__main__":
    sys.exit(main())
