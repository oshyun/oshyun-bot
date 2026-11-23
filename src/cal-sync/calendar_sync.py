"""calendar_sync — 구글 캘린더(비공개 iCal) → 볼트 노트 동기화 사이드카.

비공개 iCal(.ics) URL 을 SYNC_INTERVAL(기본 5분)마다 내려받아 반복일정(RRULE)을
전개하고, 과거~미래 N년치 일정을 '오늘/이번 주/이번 달/.../지난 일정' 버킷으로
묶어 단일 노트에 리스트로 기록한다. LLM 을 거치지 않는 순수 파이썬 — 빠르고 결정적이다.

매 주기마다 전체를 다시 받아 노트를 통째로 다시 쓴다(버킷은 표시 구분일 뿐 갱신 주기와
무관). 볼트에 트리거 파일(.cal-refresh)이 생기면 주기를 기다리지 않고 즉시 동기화한다.

봇(telegram_bot.py)과 별개의 프로세스로 돌린다(scripts/start.sh 가 함께 띄움).
"""

from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime, timedelta

import requests
from dotenv import load_dotenv

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 기본값을 먼저 채우고 .env 가 전부 덮어쓴다 — paths.py / 셸 common.sh 와 같은 규칙.
load_dotenv(os.path.join(_ROOT, ".env.defaults"))
load_dotenv(os.path.join(_ROOT, ".env"), override=True)

# 도메인 기반 환경변수(DOMAIN__KEY) 호환 동기화
import sys
_SRC_DIR = os.path.join(_ROOT, "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)
try:
    from bot.core.env_compat import sync_domain_env
    sync_domain_env()
except ImportError:
    pass

# 운영 산출물 디렉토리(로그). telegram-bot 의 paths.py / 셸 common.sh 과 같은 규칙
# (DATA_DIR 환경변수 또는 <루트>/data). cal-sync 는 별개 컴포넌트라 paths.py 를 쓰지 않는다.
_DATA_DIR = os.path.abspath(
    os.path.expanduser(os.getenv("DATA_DIR", "").strip() or os.path.join(_ROOT, "data"))
)
_LOGS_DIR = os.path.join(_DATA_DIR, "logs")  # paths.py 의 LOGS_DIR 과 동일 규칙
_LOG_FILE = os.path.join(_LOGS_DIR, "cal-sync.log")


class _CappedFileHandler(logging.Handler):
    """cal-sync.log 에 직접 기록하되 최근 100줄만 유지해 파일 크기를 묶는다.

    셸 리다이렉트가 무한정 append 하던 것을 데몬이 직접 관리한다(telegram-bot 과 동일 정책).
    셸은 크래시 stderr 만 별도 .err.log 로 받으므로 이 파일의 유일한 기록자다.
    """

    def __init__(self, path: str, max_lines: int = 100) -> None:
        super().__init__()
        self.path = path
        self.max_lines = max_lines

    def emit(self, record: logging.LogRecord) -> None:
        try:
            with open(self.path, "a+", encoding="utf-8") as f:
                f.write(self.format(record) + "\n")
                f.seek(0)
                lines = f.readlines()
            if len(lines) > self.max_lines:
                with open(self.path, "w", encoding="utf-8") as f:
                    f.writelines(lines[-self.max_lines:])
        except Exception:
            self.handleError(record)


os.makedirs(_LOGS_DIR, exist_ok=True)
_log_handler = _CappedFileHandler(_LOG_FILE)
_log_handler.setFormatter(
    logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
)
logging.basicConfig(level=logging.INFO, handlers=[_log_handler])
logger = logging.getLogger("oshyun-cal-sync")

ICAL_URL = os.getenv("GOOGLE_ICAL_URL", "").strip()
_raw_vault = ""
for _k in ("AGENT_WORKDIR", "VAULT_DIR", "CLAUDE_WORKDIR"):
    _v = (os.getenv(_k) or "").strip()
    if _v and _v != "~":
        _raw_vault = _v
        break
if not _raw_vault:
    _raw_vault = (os.getenv("AGENT_WORKDIR") or os.getenv("VAULT_DIR") or os.getenv("CLAUDE_WORKDIR") or "~").strip()
VAULT = os.path.expanduser(_raw_vault)
NOTE_PATH = os.path.join(VAULT, os.getenv("CAL_NOTE_PATH"))
YEARS_AHEAD = int(os.getenv("CAL_YEARS_AHEAD"))
YEARS_BACK = int(os.getenv("CAL_YEARS_BACK"))

# 수동 갱신 트리거 — 봇 에이전트(샌드박스)는 리포의 이 스크립트를 직접 실행할 수 없으므로,
# 볼트 안에 이 파일을 만들어 "지금 갱신"을 요청한다. 데몬이 감지하면 즉시 전체 동기화 후 삭제한다.
TRIGGER_PATH = os.path.join(VAULT, os.getenv("CAL_REFRESH_TRIGGER"))
# 전체 동기화 주기(초). 이 주기마다 iCal 을 다시 받아 노트를 통째로 다시 쓴다.
SYNC_INTERVAL = int(os.getenv("CAL_INTERVAL"))
# 트리거 감지 폴링 간격(초). 루프가 최소 이 주기로 깨어 트리거 파일을 확인한다.
REFRESH_POLL = int(os.getenv("CAL_REFRESH_POLL"))

# 시스템 로컬 타임존(KST 등) — 표시·"오늘" 판정에 쓴다.
LOCAL_TZ = datetime.now().astimezone().tzinfo

_WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]


def _format_date(d: date, ref_year: int) -> str:
    """date → 'M/D (요일)'. 올해가 아니면 연도를 앞에 붙여 모호함을 없앤다."""
    wd = _WEEKDAYS[d.weekday()]
    if d.year != ref_year:
        return f"{d.year}/{d.month}/{d.day} ({wd})"
    return f"{d.month}/{d.day} ({wd})"


class Tier:
    """표시 섹션(시간 버킷). ``contains`` 는 (이벤트일 - 오늘).days 로 판정."""

    def __init__(self, label: str, contains, reverse: bool = False):
        self.label = label
        self.contains = contains
        self.reverse = reverse  # True 면 섹션 내 최근 항목이 위로(역순)


def _tiers() -> list[Tier]:
    """노트에 표시할 시간 버킷(위→아래 순서). n = 이벤트일 - 오늘(일 단위)."""
    return [
        Tier("오늘", lambda n: n == 0),
        Tier("이번 주", lambda n: 1 <= n <= 7),
        Tier("이번 달", lambda n: 8 <= n <= 31),
        Tier("3개월 이내", lambda n: 32 <= n <= 90),
        Tier("6개월 이내", lambda n: 91 <= n <= 180),
        Tier("1년 이내", lambda n: 181 <= n <= 365),
        Tier("이후", lambda n: n > 365),
        # 지난 일정 — 최근 것이 위로 오도록 역순.
        Tier("지난 일정", lambda n: n < 0, reverse=True),
    ]


# ── 일정 모델 ──────────────────────────────────────────────────────────

class CalendarEvent:
    """전개된 단일 일정. 비교/표시에 필요한 최소 정보만 담는다."""

    def __init__(self, start: datetime, all_day: bool, title: str):
        self.start = start  # 로컬 타임존 aware datetime (all_day 면 00:00)
        self.all_day = all_day
        self.title = title

    @property
    def day(self) -> date:
        return self.start.date()

    def line(self) -> str:
        """리스트 한 줄: '- HH:MM 제목' / '- 종일 제목'."""
        when = "종일" if self.all_day else self.start.strftime("%H:%M")
        return f"- {when} {self.title}".rstrip()


def _to_local(dtstart) -> dict:
    """icalendar DTSTART 값(date 또는 datetime) → {"start": 로컬 datetime, "all_day": bool}."""
    if isinstance(dtstart, datetime):
        # tz 정보가 없으면 로컬로 간주.
        if dtstart.tzinfo is None:
            dtstart = dtstart.replace(tzinfo=LOCAL_TZ)
        return {"start": dtstart.astimezone(LOCAL_TZ), "all_day": False}
    # date (종일 일정)
    return {
        "start": datetime(dtstart.year, dtstart.month, dtstart.day, tzinfo=LOCAL_TZ),
        "all_day": True,
    }


def _fetch_events(now: datetime) -> list[CalendarEvent]:
    """iCal 을 내려받아 [-YEARS_BACK년, +YEARS_AHEAD년] 범위의 일정을 전개해 시간순으로 반환."""
    import icalendar
    import recurring_ical_events

    resp = requests.get(ICAL_URL, timeout=30)
    resp.raise_for_status()
    cal = icalendar.Calendar.from_ical(resp.content)

    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = today_start - timedelta(days=365 * YEARS_BACK)
    end = today_start + timedelta(days=365 * YEARS_AHEAD + 1)

    events: list[CalendarEvent] = []
    for comp in recurring_ical_events.of(cal).between(start, end):
        dtstart = comp.get("DTSTART")
        if dtstart is None:
            continue
        event_time = _to_local(dtstart.dt)
        title = str(comp.get("SUMMARY", "(제목 없음)")).strip()
        events.append(CalendarEvent(event_time["start"], event_time["all_day"], title))

    events.sort(key=lambda e: (e.start, e.title))
    return events


# ── 렌더링 ────────────────────────────────────────────────────────────

def _render_section(tier: Tier, events: list[CalendarEvent], today: date) -> str:
    """한 버킷을 마크다운 섹션 문자열로 렌더(헤더 + 날짜별 그룹)."""
    lines = [f"## {tier.label}"]
    bucket = [e for e in events if tier.contains((e.day - today).days)]
    if not bucket:
        lines.append("_일정 없음_")
        return "\n".join(lines)
    if tier.reverse:
        bucket = list(reversed(bucket))  # events 는 오름차순 → 역순 = 최근 먼저

    cur_day: date | None = None
    for e in bucket:
        if e.day != cur_day:
            cur_day = e.day
            lines.append(f"\n### {_format_date(e.day, today.year)}")
        lines.append(e.line())
    return "\n".join(lines)


def _assemble(sections: list[str], now: datetime) -> str:
    """헤더 + 모든 섹션 → 노트 전체 텍스트."""
    stamp = now.strftime("%Y-%m-%d %H:%M")
    header = (
        "# 📅 구글 캘린더\n\n"
        f"> 자동 생성 — 마지막 갱신 {stamp}. "
        "이 노트는 calendar_sync 가 덮어쓰므로 직접 편집하지 마세요.\n"
    )
    return header + "\n" + "\n\n".join(sections) + "\n"


def _write_note(text: str) -> None:
    """원자적 기록(tmp → replace). 부모 폴더가 없으면 만든다."""
    os.makedirs(os.path.dirname(NOTE_PATH) or ".", exist_ok=True)
    tmp = NOTE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, NOTE_PATH)


# ── 메인 루프 ──────────────────────────────────────────────────────────

def _take_trigger() -> bool:
    """수동 갱신 트리거 파일이 있으면 제거하고 True. (remove 가 존재 확인 겸 소비 — 경합 안전)"""
    try:
        os.remove(TRIGGER_PATH)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:  # 디렉토리이거나 권한 문제 등 — 무시하고 계속.
        logger.warning("트리거 파일 처리 실패(무시): %s", exc)
        return False


def main() -> None:
    if not ICAL_URL:
        logger.error("GOOGLE_ICAL_URL 미설정 — 사이드카를 종료합니다.")
        return

    tiers = _tiers()
    last = float("-inf")  # 마지막 동기화 시각(monotonic)

    logger.info(
        "calendar_sync 시작 — 노트=%s, %d년치, 트리거=%s, 주기=%d초",
        NOTE_PATH,
        YEARS_AHEAD,
        TRIGGER_PATH,
        SYNC_INTERVAL,
    )

    while True:
        mono = time.monotonic()
        # 수동 갱신 트리거: 파일이 있으면 제거하고 즉시 동기화한다.
        forced = _take_trigger()
        if forced:
            logger.info("수동 갱신 트리거 감지(%s) — 즉시 동기화", TRIGGER_PATH)

        if forced or mono - last >= SYNC_INTERVAL:
            now = datetime.now(LOCAL_TZ)
            today = now.date()
            try:
                events = _fetch_events(now)
                sections = [_render_section(t, events, today) for t in tiers]
                _write_note(_assemble(sections, now))
                last = mono
                logger.info("갱신 완료 (일정 %d건)", len(events))
            except Exception as exc:  # 네트워크/파싱 오류 — 기존 노트 유지하고 재시도.
                logger.warning("동기화 실패(다음 주기에 재시도): %s", exc)
                # last 를 갱신하지 않아 짧게(REFRESH_POLL 주기로) 재시도된다.

        # 다음 동기화까지 대기하되, 트리거를 빠르게 감지하도록 REFRESH_POLL 로 상한을 둔다.
        wait = SYNC_INTERVAL - (time.monotonic() - last)
        time.sleep(max(1, min(REFRESH_POLL, wait)))


if __name__ == "__main__":
    main()
