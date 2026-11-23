"""cron — 최소 cron 표현식 매칭기 (외부 의존성 없음).

"분 시 일 월 요일" 5필드. 각 필드는 `*`, `*/n`, `a`, `a-b`, `a-b/n`, 그리고
이들의 콤마 목록(`a,b-c`)을 지원한다. 요일은 0-6(0=일요일)이며 7도 일요일로 받는다.
표준 cron 규칙대로 '일'과 '요일'이 둘 다 제한되면 둘 중 하나만 맞아도 매칭한다.

APScheduler 등 외부 의존성을 들이지 않으려고 직접 구현했다(cal-sync 와 같은 순수 파이썬 관례).
"""

from __future__ import annotations

from datetime import datetime


def _parse_field(spec: str, lo: int, hi: int) -> set[int]:
    """한 필드를 허용 값 집합으로 전개한다. 범위를 벗어난 값은 버린다."""
    values: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            term, step_text = part.split("/", 1)
            step = int(step_text)
        else:
            term = part
        if term == "*":
            start, end = lo, hi
        elif "-" in term:
            start, end = map(int, term.split("-", 1))
        else:
            start = end = int(term)
        for value in range(start, end + 1, step):
            if lo <= value <= hi:
                values.add(value)
    return values


class Cron:
    """파싱된 cron 표현식. ``matches(dt)`` 로 특정 시각(분 단위)이 맞는지 판정한다."""

    def __init__(self, expr: str):
        fields = expr.split()
        if len(fields) != 5:
            raise ValueError(f"cron 은 '분 시 일 월 요일' 5필드여야 합니다: {expr!r}")
        self.minute = _parse_field(fields[0], 0, 59)
        self.hour = _parse_field(fields[1], 0, 23)
        self.dom = _parse_field(fields[2], 1, 31)
        self.month = _parse_field(fields[3], 1, 12)
        dow = _parse_field(fields[4], 0, 7)
        if 7 in dow:  # 일요일을 7 로 쓴 표현 정규화(7→0).
            dow.discard(7)
            dow.add(0)
        self.dow = dow
        # 표준 cron: 일·요일이 둘 다 제한(* 아님)이면 OR 로 결합한다.
        self._dom_restricted = fields[2].strip() != "*"
        self._dow_restricted = fields[4].strip() != "*"
        if not (self.minute and self.hour and self.month):
            raise ValueError(f"매칭 가능한 값이 없는 cron 입니다: {expr!r}")

    def matches(self, dt: datetime) -> bool:
        if dt.minute not in self.minute:
            return False
        if dt.hour not in self.hour:
            return False
        if dt.month not in self.month:
            return False
        # datetime.weekday(): 월=0..일=6 → cron 요일(일=0..토=6)로 변환.
        cron_dow = (dt.weekday() + 1) % 7
        dom_ok = dt.day in self.dom
        dow_ok = cron_dow in self.dow
        if self._dom_restricted and self._dow_restricted:
            return dom_ok or dow_ok
        if self._dom_restricted:
            return dom_ok
        if self._dow_restricted:
            return dow_ok
        return True
