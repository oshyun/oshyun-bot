"""conftest.py — pytest 실행 시 sys.path 및 공통 fixture 설정."""

import os
import sys

# 리포 루트 및 src(패키지 루트) 경로를 sys.path 최우선으로 등록
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC_DIR = os.path.join(_REPO_ROOT, "src")

if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)
