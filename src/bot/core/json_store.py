"""json_store — 상태 파일용 JSON 읽기·원자적 저장 공용 헬퍼.

jobs·schedules·restart_notice·bot_settings 이 같은 패턴(tmp 쓰기 → os.replace)으로
저장하던 것을 한 곳에 둔다 — 크래시 도중 반쯤 쓰인 파일이 생기지 않게 한다.
"""

from __future__ import annotations

import json
import os


def read_json(path: str, default, *, expect_type: type | None = None):
    """JSON 파일을 읽는다. 파일이 없거나 깨져 있으면 default 를 반환한다.

    expect_type 을 주면 파싱 결과가 그 타입이 아닐 때도 default 를 반환한다(타입 불일치 방어).
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError, OSError):
        return default
    return data if expect_type is None or isinstance(data, expect_type) else default


def write_json_atomic(path: str, data, *, indent: int | None = None) -> None:
    """원자적 저장(tmp 쓰기 → replace). 부모 폴더가 없으면 만든다."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=indent)
    os.replace(tmp, path)
