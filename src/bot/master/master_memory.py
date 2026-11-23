"""master_memory — 마스터 에이전트 4계층 메모리(Hot / Warm / Cold / Freeze) 및 캐스케이드 강등 엔진.

마스터 에이전트(`__core__`)가 대화와 하위 작업 모니터링을 수행하며 축적하는 기억을 4개 영역으로 계층화하여
`data/state/master_memory.json` 에 영속 관리한다:

1. `hot`    — 즉각 참조하는 활성 컨텍스트(최근 턴, 방금 위임/보고된 핵심 작업).
2. `warm`   — hot 길이/개수 한도를 초과해 밀려난 최근 작업 요약 및 완료 이력.
3. `cold`   — warm 한도를 초과해 압축 강등된 중기 이력.
4. `freeze` — cold 한도를 초과해 장기 동결 보관되는 아카이브.

요약 정제 및 중복 병합 정책:
- 자동 적재 항목(`dispatch`, `notify`, `task_event`, `turn`)은 마크다운 장식·상투어·
  원문 지시문·UI 복붙 노이즈를 제거하고 핵심 사실·결론·수치·방ID/작업ID만 남겨
  `AUTO_SUMMARY_MAX_CHARS`(기본 180자) 이내로 압축 저장한다.
- 동일 `taskId` 의 완료 보고(`notify` / `task_event` / `task_report` 턴)나 직전 `dispatch` 에
  대응하는 단순 위임 응답은 중복 적재하지 않고 기존 항목에 병합한다.

하위 캐시 이동 전략(Cascading Overflow Demotion):
- 새 항목은 기본적으로 `hot` 영역에 FIFO(가장 오래된 항목이 index 0, 최신이 끝)로 적재된다.
- 어떤 계층이든 총 문자 길이(`maxChars`) 또는 최대 항목 수(`maxItems`)를 초과하면,
  가장 오래된 항목부터 꺼내 하위 계층의 `itemMaxChars` 규격에 맞게 압축 클리핑한 뒤
  하위 계층(`hot → warm → cold → freeze`)으로 연쇄 이동(demote)시킨다.
- 최하위 `freeze` 영역마저 한도를 초과하면 가장 오래된 항목부터 방출(drop)한다.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from typing import Any

from json_store import read_json, write_json_atomic
from paths import MASTER_MEMORY_FILE

logger = logging.getLogger(__name__)

CURRENT_VERSION = 3
AUTO_SUMMARY_MAX_CHARS = 180
AUTO_SUMMARY_TITLE_CHARS = 44
AUTO_SUMMARY_KINDS: frozenset[str] = frozenset({"dispatch", "notify", "task_event", "turn"})

LLM_SUMMARY_MODEL = os.environ.get("OSHYUN_BOT_MEMORY_LLM_MODEL", "gemini-3.8-flash")
LLM_SUMMARY_EFFORT = os.environ.get("OSHYUN_BOT_MEMORY_LLM_EFFORT", "low")
_llm_env = os.environ.get("OSHYUN_BOT_LLM_MEMORY_SUMMARY", "").strip().lower()
if _llm_env in ("1", "true", "yes", "on"):
    LLM_SUMMARY_ENABLED = True
elif _llm_env in ("0", "false", "no", "off"):
    LLM_SUMMARY_ENABLED = False
else:
    LLM_SUMMARY_ENABLED = "unittest" not in sys.modules

_bg_refine_tasks: set[asyncio.Task[Any]] = set()

TIERS: tuple[str, ...] = ("hot", "warm", "cold", "freeze")
NEXT_TIER: dict[str, str | None] = {
    "hot": "warm",
    "warm": "cold",
    "cold": "freeze",
    "freeze": None,
}
PREV_TIER: dict[str, str | None] = {
    "hot": None,
    "warm": "hot",
    "cold": "warm",
    "freeze": "cold",
}
TIER_LABELS: dict[str, str] = {
    "hot": "HOT · 활성",
    "warm": "WARM · 최근",
    "cold": "COLD · 압축",
    "freeze": "FREEZE · 동결",
}

VALID_AGENT_IDS: tuple[str, ...] = ("master", "second_brain", "bot_dev")
AGENT_LABELS: dict[str, str] = {
    "master": "마스터 에이전트",
    "second_brain": "세컨브레인 에이전트",
    "bot_dev": "봇개발 에이전트",
}
AGENT_DOMAIN_TAGS: dict[str, str] = {
    "master": "[마스터]",
    "second_brain": "[세컨브레인]",
    "bot_dev": "[봇개발]",
}
_AGENT_ALIASES: dict[str, str] = {
    "": "master",
    "master": "master",
    "__core__": "master",
    "core": "master",
    "second_brain": "second_brain",
    "__expert_vault__": "second_brain",
    "expert_vault": "second_brain",
    "vault": "second_brain",
    "bot_dev": "bot_dev",
    "__expert_coding__": "bot_dev",
    "expert_coding": "bot_dev",
    "coding": "bot_dev",
}


def normalize_agent_id(agent_id: str | None = None) -> str:
    """에이전트 식별자(또는 룸 ID)를 정규 메모리 스코프('master', 'second_brain', 'bot_dev', 또는 'custom_<slug>')로 변환한다."""
    raw = str(agent_id or "").strip().lower()
    if raw in _AGENT_ALIASES:
        return _AGENT_ALIASES[raw]
    if raw.startswith("__custom_") and raw.endswith("__") and len(raw) > 11:
        slug = re.sub(r"[^a-z0-9_\-]", "", raw[9:-2]).strip("_-")
        if slug:
            return f"custom_{slug}"
    if raw.startswith("custom_"):
        slug = re.sub(r"[^a-z0-9_\-]", "", raw[7:]).strip("_-")
        if slug:
            return f"custom_{slug}"
    try:
        import agent_configs
        cfg = agent_configs.get_agent_config(raw)
        if cfg and cfg.get("memoryScope"):
            return str(cfg["memoryScope"])
    except Exception:
        pass
    raise ValueError(f"지원하지 않는 에이전트 메모리 식별자입니다: {agent_id}")


def _get_memory_file(agent_id: str = "master") -> str:
    """에이전트 스코프별 4계층 메모리 JSON 파일 경로를 반환한다."""
    norm = normalize_agent_id(agent_id)
    if norm == "master":
        return MASTER_MEMORY_FILE
    base, ext = os.path.splitext(MASTER_MEMORY_FILE)
    return f"{base}_{norm}{ext or '.json'}"

DEFAULT_TIER_LIMITS: dict[str, dict[str, int]] = {
    "hot": {"maxChars": 1500, "maxItems": 8, "itemMaxChars": 220},
    "warm": {"maxChars": 3000, "maxItems": 16, "itemMaxChars": 180},
    "cold": {"maxChars": 6000, "maxItems": 32, "itemMaxChars": 160},
    "freeze": {"maxChars": 15000, "maxItems": 100, "itemMaxChars": 140},
}

_LEGACY_DEFAULT_ITEM_MAX: dict[str, int] = {
    "hot": 400,
    "warm": 300,
    "cold": 220,
    "freeze": 180,
}

_lock = threading.Lock()
_PROMPT_CHECKPOINT_MAX_SESSIONS = 32
_prompt_checkpoints: dict[str, dict[str, tuple[float, str]]] = {}

# ── 텍스트 정제 및 요약 추출 정규식 ───────────────────────────────────────
_RE_FENCED_CODE = re.compile(r"```[\s\S]*?```")
_RE_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_RE_MD_LINK = re.compile(r"\[(?:link:)?([^\]]+)\]\([^)]+\)")
_RE_WIKI_ALIAS = re.compile(r"\[\[([^\]|]+)\|([^\]]+)\]\]")
_RE_WIKI_LINK = re.compile(r"\[\[([^\]]+)\]\]")
_RE_HTML_TAG = re.compile(r"<[^>]+>")
_RE_URL = re.compile(r"https?://\S+")
_RE_LONG_ABS_PATH = re.compile(r"(?:/Users|/tmp|/var)/[^\s,)\]'\"`]+/([^\s,)\]'\"`/]+)")
_RE_MD_HEADING = re.compile(r"(?:^|\s)#{1,6}(?=\s)")
_RE_MD_RULE = re.compile(r"(?:^|\s)[-=─━]{3,}(?=\s|$)")
_RE_MD_QUOTE = re.compile(r"(?m)^[ \t]*>[ \t]*")
_RE_MD_BOLD = re.compile(r"\*\*([^*]+)\*\*|__([^_]+)__|~~([^~]+)~~")
_RE_MD_ITALIC_STAR = re.compile(r"(?<!\S)\*([^*\n]+)\*(?!\S)")
_RE_EMOJI_DECOR = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u26FF\u2700-\u27BF]+")
_RE_REPEATED_NON_DIGIT = re.compile(r"([^\d\s])\1{11,}")
_RE_DEDUP_STRIP = re.compile(r"[\s·.,:;!?]+")

_RE_TASK_ID = re.compile(r"\b(mt-[0-9a-f]{6,12}|task_[0-9a-zA-Z_]+)\b")
_RE_ROOM_ID = re.compile(r"\b(room-\d+-[0-9a-f]+)\b")

_RE_DISPATCH_HEADER = re.compile(
    r"^\[(?:디스패치|위임):([^\|\]]+?)(?:\|[^|\]]+)*\]\s*(.+?)\s*\(([^)]+)\)\s*(?:—|-|:)\s*(.*)$",
    re.DOTALL,
)
_RE_DISPATCH_TAG_PREFIX = re.compile(r"^\[(?:디스패치|위임):([^\]]+)\]\s*")
_RE_NOTIFY_HEADER = re.compile(
    r"^\[(?:(성공|실패)\s*보고\s*[·|]\s*([^\]|]+)(?:\|([^\]|]+))?|보고:([^\|\]]+)(?:\|([^\]|]+))*)\]\s*(.*)$",
    re.DOTALL,
)
_RE_TASK_EVENT_HEADER = re.compile(
    r"^\[([A-Z_]+):([^\|\]\s]+)(?:\|([^\]]+))?\]\s*(.*)$",
    re.DOTALL,
)

# 지시문 내 장황한 세부 섹션 절단 마커
_DISPATCH_CUT_MARKERS = (
    "[세부 지시]",
    "[요청 작업]",
    "[세부 수정",
    "[원본 데이터 파일 경로]",
    "[첨부 이미지 경로]",
    "[첨부 파일 경로]",
    "[참고:",
    "■ 조치 지침",
    "※ 마스터 지침",
)

_RE_UI_CHROME_START = re.compile(
    r"(?:네이버페이\s*수입\s*지출\s*뒤로가기|현재\s*\d+월\s*날짜\s*선택하기|날짜\s*선택하기\s*이전\s*달\s*보기|결제수단\s*선택\s*전체\s*카드)"
)

_RE_DELEGATION_REPLY = re.compile(
    r"(?:하위\s*에이전트\(([^)]+)\)\s*에게\s*(.+?)\s*(?:작업을\s*)?(?:비동기로\s*)?(?:위임|전달|재지시)했습니다\.?|"
    r"(.+?)\s*작업을\s*하위\s*에이전트(?:\(([^)]+)\))?에게\s*(?:비동기로\s*)?(?:위임|전달|재지시)했습니다\.?)"
)

_RE_TABLE_SEP_LINE = re.compile(r"^\s*\|?(?:\s*:?-{2,}:?\s*\|)+(?:\s*:?-{2,}:?\s*)?\|?\s*$")
_RE_INLINE_TABLE_SEP = re.compile(r"\|\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)+\|?")
_RE_GENERIC_TABLE_HDR = re.compile(
    r"^\s*(?:항목|구분|종류|분류)\s*[:/|]\s*(?:상세\s*내용|내용|값|설명|비고|상태)\s*$"
)
_RE_INLINE_GENERIC_HDR = re.compile(
    r"\|\s*(?:항목|구분|종류)\s*\|\s*(?:상세\s*내용|내용|값|설명|비고)\s*\|"
)

_RE_LOW_SIGNAL_SEGMENT = re.compile(
    r"^(?:"
    r"(?:발생\s*상황\s*요약|원인\s*분석|원인|요약|조치\s*결과|적용된\s*안전장치|주요\s*변경\s*내역|"
    r"대상\s*방|작업\s*ID|상태|결론|핵심\s*요약|화면\s*증상|분석\s*결과|참고\s*사항)(?:\s*[:：]?\s*)?"
    r"|.*(?:정밀\s*분석한\s*결과입니다|조사\s*결과를\s*브리핑드립니다|조치\s*결과를\s*보고드립니다|"
    r"완료\s*보고\s*확인했습니다|요약입니다|브리핑드립니다|보고드립니다)\.?"
    r"|.*(?:해\s*드릴까요|드릴까요|진행할까요|알려드릴까요|반영할까요)\??"
    r"|(?:왜\s+이렇게|무엇이\s+바뀌었는가|어떻게\s+동작하는가)[^?]{0,40}\?"
    r"|.*AGENTS\.md\s*워크트리\s*규칙에\s*따라\s*작업을\s*완료하고.*반영했습니다\.?"
    r")$"
)

_BOILERPLATE_FILLERS = (
    re.compile(r"현재\s*날짜\([^)]*\)\s*기준으로\s*"),
    re.compile(r"oshyun-bot\s*리포지토리(?:\([^)]*\))?의\s*AGENTS\.md\s*워크트리\s*규칙을\s*준수하여[,:\s]*"),
    re.compile(r"oshyun-bot\s*리포지토리(?:\([^)]*\))?의\s*AGENTS\.md\s*워크트리\s*규칙에\s*따라\s*"),
    re.compile(r"(?:먼저\s*)?첨부\s*이미지를\s*read_file로\s*확인한\s*뒤[,:\s]*"),
    re.compile(r"첨부해주신\s*스크린샷(?:\([^)]*\))?(?:과\s*[^\n.]*?)?(?:을|를)\s*(?:분석하여\s*|정밀\s*분석한\s*결과입니다\.?\s*)"),
    re.compile(r"첨부해주신\s*스크린샷과\s*[^\n.]*?정밀\s*분석한\s*결과입니다\.?\s*"),
    re.compile(r"하위\s*에이전트\([^)]+\)의\s*조사\s*결과를\s*브리핑드립니다\.?\s*"),
    re.compile(r"방금\s*턴이\s*빈\s*응답으로\s*종료되었습니다\.?[·\s]*(?:다시\s*한\s*번\s*)?"),
    re.compile(r"(?:웹\s*서버\s*재시작으로\s*인해\s*)?이전\s*턴이\s*(?:보고\s*전에\s*|웹\s*서버\s*재시작으로\s*)?중단(?:\([^)]*\))?되었습니다\.?[·\s]*(?:(?:이어서|다시)\s*(?:작업을\s*진행해\s*주세요[:：\s]*)?)?"),
    re.compile(r"^(?:이어서|다시)\s*(?:한\s*번\s*)?작업을\s*(?:진행해|재개해)\s*주세요[:：\s]*"),
    re.compile(r"원인\s*분석이\s*매우\s*정확합니다!?\s*(?:이제\s*)?"),
    re.compile(r"(?:네,\s*)?(?:정확합니다|말씀하신\s*대로(?:\s*확인해\s*보니)?|지적해주신\s*대로)[!.,:\s]*"),
    re.compile(r"조치\s*결과를\s*보고드립니다\.?\s*"),
    re.compile(r"완료\s*보고가\s*도착하는\s*대로\s*(?:정리해서\s*)?브리핑\s*드리겠습니다\.?"),
    re.compile(r"(?:단위\s*테스트\s*)?검증\s*후\s*main에\s*merge\s*및\s*웹\s*서버(?:\([^)]*\))?에\s*반영한\s*뒤\s*결과를\s*보고해\s*주세요\.?"),
    re.compile(r"단위\s*테스트\s*검증\s*후\s*main에\s*merge\s*및\s*웹\s*서버[^\n·]*"),
    re.compile(r"지금\s*바로\s*실행해\s*주세요\.?"),
    re.compile(r"지금\s*바로\s*실행해주세요\.?"),
    re.compile(r"※\s*마스터\s*지침:.*$", re.DOTALL),
    re.compile(r"•\s*산출물\s*포인터.*?(?=(?:•\s*마스터\s*개입|$))", re.DOTALL),
    re.compile(r"•\s*마스터\s*개입\s*필요\s*여부:.*$"),
)

_PRIORITY_KEYWORDS = (
    "완료", "수정", "구현", "해결", "원인", "실패", "중단", "오류", "에러",
    "검증", "정산", "동기화", "위임", "승인", "합계", "수입", "지출", "실수지",
    "혜택", "기간", "금액", "개선", "추가", "삭제", "병합", "통과", "할인",
    "적립", "확정", "반영", "원", "건", "%", ".py", ".html", ".md",
)


def _now() -> float:
    return time.time()


def _clip_text(text: Any, limit: int) -> str:
    """개행·중복 공백을 단일 공백으로 정규화하고 지정 길이 이내로 클리핑한다."""
    s = " ".join(str(text or "").split())
    if limit <= 0 or not s:
        return ""
    if len(s) <= limit:
        return s
    if limit == 1:
        return s[:1]
    return s[: limit - 1] + "…"


def _smart_clip(text: str, limit: int) -> str:
    """단어/구분자 경계를 가급적 유지하면서 지정 길이 이내로 절단한다."""
    s = " ".join(str(text or "").split())
    if limit <= 0 or not s:
        return ""
    if len(s) <= limit:
        return s
    if limit <= 2:
        return _clip_text(s, limit)
    cut = s[: limit - 1]
    # 뒤쪽 25% 구간에 공백이나 구분자가 있으면 경계에서 자른다.
    min_pos = max(1, int((limit - 1) * 0.7))
    for sep in (" · ", ", ", " ", "·"):
        pos = cut.rfind(sep)
        if pos >= min_pos:
            return cut[:pos].rstrip(" ·,;:-…—") + "…"
    return cut.rstrip(" ·,;:-…—") + "…"


_RE_MEMORY_TAG_PREFIX = re.compile(r"^\[(?:보고|위임|완료응답|정기점검|[A-Z_]+):[^\]]+\]\s*")


def _clean_markdown_tables(text: str) -> str:
    """마크다운 표(| ... |) 및 정렬 구분선(| :--- |)을 평문 키-값 요약으로 변환한다."""
    # 한 줄로 평탄화된 표 행 경계(| |)를 줄바꿈으로 복원하되, [보고:성공|room|task] 태그는 보호
    protected_tag = ""
    work = text
    m_tag = _RE_MEMORY_TAG_PREFIX.match(work)
    if m_tag:
        protected_tag = m_tag.group(0)
        work = work[m_tag.end() :]

    work = _RE_INLINE_TABLE_SEP.sub("\n", work)
    work = _RE_INLINE_GENERIC_HDR.sub("\n", work)
    work = re.sub(r"\|\s*\|", "\n|", work)

    out_lines: list[str] = []
    for raw_line in work.splitlines():
        ln = raw_line.strip()
        if not ln:
            continue
        if _RE_TABLE_SEP_LINE.match(ln):
            continue
        if ln.startswith("|") or (ln.count("|") >= 2):
            cells = [c.strip() for c in ln.strip("|").split("|")]
            cells = [c for c in cells if c and not re.fullmatch(r"[:\-\s]+", c)]
            if not cells:
                continue
            joined_row = " / ".join(cells)
            if len(cells) == 2 and _RE_GENERIC_TABLE_HDR.match(f"{cells[0]}: {cells[1]}"):
                continue
            if _RE_GENERIC_TABLE_HDR.match(joined_row):
                continue
            out_lines.append(f"{cells[0]}: {cells[1]}" if len(cells) == 2 else " / ".join(cells))
            continue
        out_lines.append(raw_line)

    res = "\n".join(out_lines)
    res = re.sub(r"\s*\|\s*", ": ", res)
    return f"{protected_tag}{res}" if protected_tag else res


def clean_memory_markdown(text: Any) -> str:
    """마크다운 장식·코드펜스·표·링크·장황한 절대경로·장식용 이모지를 걷어낸다."""
    s = str(text or "").strip()
    if not s:
        return ""

    s = _RE_FENCED_CODE.sub(" ", s)
    s = _RE_INLINE_CODE.sub(r"\1", s)
    s = _RE_MD_LINK.sub(r"\1", s)
    s = _RE_WIKI_ALIAS.sub(r"\2", s)
    s = _RE_WIKI_LINK.sub(r"\1", s)
    s = _RE_HTML_TAG.sub(" ", s)
    s = _RE_LONG_ABS_PATH.sub(r"\1", s)
    s = _RE_URL.sub("", s)
    s = _RE_MD_BOLD.sub(lambda m: next((g for g in m.groups() if g is not None), ""), s)
    s = _RE_MD_ITALIC_STAR.sub(r"\1", s)
    s = s.replace("**", "").replace("~~", "")
    s = _clean_markdown_tables(s)
    s = _RE_MD_RULE.sub(" ", s)
    s = _RE_MD_HEADING.sub(" ", s)
    s = _RE_MD_QUOTE.sub("", s)
    s = _RE_EMOJI_DECOR.sub(" ", s)
    s = _RE_REPEATED_NON_DIGIT.sub(lambda m: f"{m.group(1)}×{len(m.group(0))}", s)
    return s


def _strip_ui_paste_noise(text: str) -> str:
    """웹 화면(네이버페이 등) 복붙 시 따라붙는 내비게이션 크롬 노이즈를 제거한다."""
    s = str(text or "").strip()
    if not s:
        return ""
    m = _RE_UI_CHROME_START.search(s)
    if not m:
        return s
    head = s[: m.start()].strip(" -·:,")
    tail_chunk = s[m.start() :]
    month_m = re.search(r"현재\s*(\d{1,2}월)", tail_chunk)
    month_str = month_m.group(1) if month_m else ""
    if head:
        if month_str and month_str not in head:
            return f"{head} ({month_str})"
        return head
    if month_str:
        return f"네이버페이 {month_str} 내역"
    return "네이버페이 내역"


def _strip_report_template(text: str) -> tuple[str, str, str, str]:
    """`[하위 작업 완료 보고]` 템플릿에서 (room_title, room_id, task_id, 본문)을 추출한다."""
    s = str(text or "").strip()
    if not s:
        return "", "", "", ""

    room_title = ""
    room_id = ""
    task_id = ""

    header_m = re.search(r"^\[하위 작업 (?:완료|중단/오류) 보고\]\s*(?:\[([^\]]+)\])?", s)
    if header_m and header_m.group(1):
        room_title = header_m.group(1).strip()

    rid_m = re.search(r"Room ID:\s*([A-Za-z0-9_-]+)", s)
    if rid_m:
        room_id = rid_m.group(1).strip()
    if not room_id:
        m2 = _RE_ROOM_ID.search(s)
        if m2:
            room_id = m2.group(1)

    tid_matches = _RE_TASK_ID.findall(s)
    if tid_matches:
        # 8자리 이상 완전한 작업ID(예: mt-93c1530c)를 절단된 ID(mt-93c153)보다 우선 선택
        task_id = max(tid_matches, key=len)
    if not task_id:
        tid_m = re.search(r"작업 ID:\s*([A-Za-z0-9_-]+)", s)
        if tid_m:
            task_id = tid_m.group(1).strip()

    # 응답 요약 본문 우선 추출
    body = s
    summary_split = re.split(r"•\s*(?:하위 에이전트 응답 요약|내용)\s*:\s*", s, maxsplit=1)
    if len(summary_split) == 2:
        body = summary_split[1]
    else:
        # 템플릿 헤더 라인 제거
        cleaned_lines: list[str] = []
        for raw_line in s.splitlines():
            ln = raw_line.strip()
            if not ln:
                continue
            if ln.startswith(("[하위 작업 완료 보고]", "[하위 작업 중단/오류 보고]")):
                continue
            if re.match(r"^[•\-]?\s*(?:대상 방|작업 ID|상태)\s*:", ln):
                continue
            cleaned_lines.append(ln)
        body = "\n".join(cleaned_lines)

    body = re.sub(r"※\s*마스터\s*지침:.*$", "", body, flags=re.DOTALL).strip()
    return room_title, room_id, task_id, body


def _strip_boilerplate(text: str, *, kind: str = "note") -> str:
    """상투어·원문 지시문·템플릿 안내 문구를 걷어낸다."""
    s = str(text or "").strip()
    if not s:
        return ""

    for pat in _BOILERPLATE_FILLERS:
        s = pat.sub("", s)

    if kind == "dispatch":
        # "[버그 내용 및 첨부 이미지] - 내용: ..." 패턴이면 내용 우선 추출
        bug_m = re.search(r"\[버그\s*내용[^\]]*\]\s*[-•]?\s*(?:내용|증상)\s*:\s*(.+?)(?=\n|\[요청|첨부|$)", s, re.DOTALL)
        if bug_m and len(bug_m.group(1).strip()) >= 8:
            s = bug_m.group(1).strip()
        else:
            # "[10월 전체본 파일 경로]" 등 경로 섹션 앞의 "추가 요청 (주제): 사용자가 ... 전달했습니다." 보존
            add_req_m = re.match(r"^추가\s*요청\s*\(([^)]+)\)\s*[:：]\s*(.*)$", s, re.DOTALL)
            if add_req_m:
                topic = add_req_m.group(1).strip()
                rest = re.sub(r"사용자가\s+.+?전달했습니다\.?\s*", "", add_req_m.group(2)).strip()
                s = f"{topic} — {rest}" if rest else topic

            # "사용자가 '...'라고 요청했습니다." / "사용자가 다음과 같이 요청했습니다: '...'" 래퍼에서 핵심 요청문 추출
            quoted_req_m = re.match(
                r"^(?:사용자가\s+(?:추가로\s+)?(?:다음과\s*같이\s*)?)?['\"]([^'\"]{8,180})['\"](?:라고|이라고)?\s*(?:요청|지시|문의)했습니다[.:：\s]*(.*)$",
                s,
                re.DOTALL,
            )
            if not quoted_req_m:
                quoted_req_m = re.match(
                    r"^사용자가\s+(?:추가로\s+)?(?:다음과\s*같이\s*)?(?:요청|지시|개선을\s*요청)했습니다[:：\s]*['\"]([^'\"]{8,180})['\"](.*)$",
                    s,
                    re.DOTALL,
                )
            if quoted_req_m:
                quoted_core = quoted_req_m.group(1).strip()
                after_q = quoted_req_m.group(2).strip(" ·.:")
                # 뒤따르는 "다음 작업을 수행하세요: 1)..." 등은 버리고 핵심만 유지
                after_q = re.sub(r"^(?:다음\s*(?:작업을\s*수행하세요|순서로[^:：]*)[.:：\s]*)", "", after_q).strip()
                if not after_q or re.match(r"^(?:1[.)]|①|\d+\))", after_q):
                    s = quoted_core
                else:
                    s = f"{quoted_core} · {after_q}"

            # 파일 경로 블록([10월 전체본 파일 경로] 등) 절단
            path_sec_m = re.search(r"\[[^\]]*파일\s*경로\]", s)
            if path_sec_m and path_sec_m.start() >= 10:
                s = s[: path_sec_m.start()].strip()
            for marker in _DISPATCH_CUT_MARKERS:
                idx = s.find(marker)
                if idx >= 14:
                    s = s[:idx].strip()
            s = re.sub(r"(?:다음\s*(?:작업을\s*수행하세요|순서로\s*(?:빠르게\s*)?(?:조사해\s*)?요약해주세요))\s*[:：]?\s*", "", s)
            # 번호 지시 목록("1. 먼저 ...", "1) ...", "① ...")이 앞에 충분한 요약문이 있을 때만 절단
            num_m = re.search(r"(?:\s|^)(?:1[.)]|①)\s+", s)
            if num_m and num_m.start() >= 18:
                s = s[: num_m.start()].strip()

        s = re.sub(r"^(?:추가\s*요청(?:\s*\([^)]+\))?|사용자\s*추가\s*지시)\s*[:：]\s*", "", s)
        s = re.sub(r"사용자가\s+.+?(?:추가로\s*전달하며|제보했습니다\.?|전달했습니다\.?|요청했습니다\.?)\s*", "", s)
        s = re.sub(r"방금\s*전달된\s*추가\s*지시\(([^)]+)\)를\s*", r"\1 ", s)
        s = re.sub(r"^(?:1[.)]|①)\s+", "", s)
        s = re.sub(r"(?:을|를)\s*(?:웹\s*검색으로\s*)?(조사|정리|분석|작성|수정|구현|검증|파악|실행)해\s*주세요\.?", r" \1", s)
        s = re.sub(r"(?:해\s*주세요|해주세요)\.?$", "", s).strip()

    elif kind == "notify":
        if s.startswith(("[하위 작업 완료 보고]", "[하위 작업 중단/오류 보고]")):
            _, _, _, s = _strip_report_template(s)
        s = re.sub(r"^\[System Event\][^\n]*\n?", "", s)
        s = re.sub(r"(?m)^•\s*(?:판정\(Verdict\)|핵심 요약)\s*:\s*", "", s)
        s = re.sub(r"요청하신\s+", "", s)
        s = re.sub(r"작업을\s*완료하여\s*main\s*브랜치에\s*병합하고\s*웹\s*서버에\s*(?:배포·?)?반영했습니다\.?", "완료·배포", s)
        s = re.sub(r"작업을\s*완료하고\s*웹\s*서버에\s*(?:성공적으로\s*)?반영했습니다\.?", "완료·반영", s)

    elif kind == "turn":
        # 단순 위임 확인 응답 압축
        del_m = _RE_DELEGATION_REPLY.search(s)
        if del_m:
            prefix_part = s[: del_m.start()].strip()
            raw_desc = del_m.group(2) if del_m.group(1) is not None else del_m.group(3)
            task_desc = clean_memory_markdown(raw_desc or "").strip()
            task_desc = re.sub(r"^(?:비동기로|작업을)\s*", "", task_desc).strip()
            if not task_desc or task_desc in ("비동기로", "작업을"):
                # "위임 내용" 이후 줄에서 핵심 요약 추출
                after_del = s[del_m.end() :]
                after_del = re.sub(r"^.*?위임\s*내용\s*", "", after_del).strip(" -·:")
                task_desc = clean_memory_markdown(after_del)[:70] or "작업 위임"
            # 위임 이전 본문에 실질 답변이 없으면 간결한 위임 요약으로 치환
            if len(clean_memory_markdown(prefix_part)) < 25:
                s = f"위임: {task_desc}"
            else:
                s = f"{prefix_part} (위임: {task_desc})"
        else:
            for marker in ("### 📌 위임 내용", "위임 내용"):
                idx = s.find(marker)
                if idx >= 15:
                    s = s[:idx].strip()
        s = re.sub(r"요청하신\s+", "", s)
        s = re.sub(r"완료\s*보고\s*확인했습니다\.?\s*", "", s)
        s = re.sub(r"확인해\s*보니\s*", "", s)

    return s.strip()


def _norm_plain(text: str) -> str:
    return _RE_DEDUP_STRIP.sub("", text).lower()


def _norm_dedup_key(kind: str, text: str) -> str:
    return f"{kind}::{_norm_plain(text)}"


def _split_meaningful_segments(text: str) -> list[str]:
    """줄바꿈/불릿/문장 경계를 기준으로 중복 없는 의미 단편 목록을 만든다."""
    if not text:
        return []
    raw_parts = re.split(r"[\n]+|\s+·\s+|(?<=[.!?다])\s+(?=[가-힣A-Za-z0-9•\-])", text)
    segments: list[str] = []
    seen_norm: set[str] = set()
    for part in raw_parts:
        seg = re.sub(r"^[\s•\-*·\d.)①②③④]+(?=\s)", "", part).strip(" ·|-:;")
        seg = re.sub(r"^[•\-*]\s*", "", seg).strip()
        seg = " ".join(seg.split())
        if not seg or len(seg) < 2:
            continue
        # 제목 반복이나 구분선 잔재 제외
        if re.fullmatch(r"[\d.\-:;/\s]+", seg):
            continue
        norm = _norm_plain(seg)
        if norm in seen_norm:
            continue
        seen_norm.add(norm)
        segments.append(seg)
    return segments


def _condense_segments(text: str, max_chars: int) -> str:
    """핵심 결론·수치·동사 위주로 문장을 압축 결합한다."""
    if max_chars <= 0:
        return ""
    md_cleaned = clean_memory_markdown(text)
    segs = _split_meaningful_segments(md_cleaned)
    if not segs:
        flat = " ".join(line.strip() for line in md_cleaned.splitlines() if line.strip())
        return _smart_clip(flat, max_chars)

    # 메타 헤더·상투 인사말만 있는 세그먼트는 실질 내용이 있을 때 제외
    high_signal = [seg for seg in segs if not _RE_LOW_SIGNAL_SEGMENT.match(seg)]
    candidates = high_signal if high_signal else segs

    if len(candidates) == 1:
        return _smart_clip(candidates[0], max_chars)

    def _score_seg(idx_seg: tuple[int, str]) -> tuple[int, int]:
        idx, seg = idx_seg
        score = 0
        if _RE_LOW_SIGNAL_SEGMENT.match(seg):
            score -= 10
        if re.search(r"\d", seg):
            score += 3
        if any(kw in seg for kw in _PRIORITY_KEYWORDS):
            score += 2
        if re.search(r"[A-Za-z_][A-Za-z0-9_]*\(\)|\.(?:py|html|md|json|sh)\b", seg):
            score += 2
        if 6 <= len(seg) <= 65:
            score += 1
        return (-score, idx)

    ranked = sorted(enumerate(candidates), key=_score_seg)
    anchor_idx, anchor_seg = ranked[0]
    # 첫 문장이 저신호가 아니고 앵커보다 크게 뒤지지 않으면 자연스런 첫 문장 우선
    if anchor_idx != 0:
        first_score = -_score_seg((0, candidates[0]))[0]
        anchor_score = -_score_seg((anchor_idx, anchor_seg))[0]
        if first_score >= anchor_score - 1 and first_score > 0:
            anchor_idx, anchor_seg = 0, candidates[0]

    selected: list[str] = [anchor_seg]
    used_len = len(anchor_seg)

    for idx, seg in ranked:
        if idx == anchor_idx:
            continue
        if used_len >= max_chars - 10:
            break
        # 이미 선택된 문장의 부분 문자열이면 건너뜀
        if seg in " ".join(selected):
            continue
        sep = " · "
        if used_len + len(sep) + len(seg) <= max_chars:
            selected.append(seg)
            used_len += len(sep) + len(seg)
        elif used_len < max_chars * 0.65:
            avail = max_chars - used_len - len(sep)
            if avail >= 18:
                selected.append(_smart_clip(seg, avail))
            break

    joined = " · ".join(selected)
    return _smart_clip(joined, max_chars)


def summarize_user_query(user_text: str, *, max_chars: int = 60) -> str:
    """사용자 질문에서 UI 복붙 노이즈와 서식을 제거하고 핵심 의도만 추출한다."""
    s = str(user_text or "").strip()
    if not s:
        return ""
    if s.startswith(("[하위 작업 완료 보고]", "[하위 작업 중단/오류 보고]")):
        r_title, _, t_id, _ = _strip_report_template(s)
        tag = t_id or r_title or "하위작업"
        return _smart_clip(f"보고({tag})", max_chars)
    if s.startswith("[마스터 에이전트 정기 점검"):
        return "정기점검"

    s = _strip_ui_paste_noise(s)
    s = clean_memory_markdown(s)
    first_line = next((ln.strip() for ln in s.splitlines() if ln.strip()), s)
    return _smart_clip(first_line, max_chars)


def summarize_memory_title(
    title: str,
    *,
    kind: str = "note",
    room_id: str = "",
    task_id: str = "",
    max_chars: int = AUTO_SUMMARY_TITLE_CHARS,
) -> str:
    """메모리 항목 제목에서 보고 템플릿/UI 복붙 잡음을 제거해 짧게 정제한다."""
    raw = str(title or "").strip()
    if not raw:
        if task_id:
            return f"작업:{task_id}"[:max_chars]
        return ""

    if kind == "note":
        return _clip_text(raw, 60)

    if raw.startswith(("[하위 작업 완료 보고]", "[하위 작업 중단/오류 보고]")):
        r_title, _, t_id, _ = _strip_report_template(raw)
        if t_id or task_id:
            return _smart_clip(f"보고: {t_id or task_id}", max_chars)
        if r_title:
            return _smart_clip(r_title, max_chars)
        if room_id:
            return _smart_clip(f"보고: {room_id}", max_chars)

    cleaned = _strip_ui_paste_noise(raw)
    cleaned = clean_memory_markdown(cleaned)
    cleaned = " ".join(cleaned.split())
    return _smart_clip(cleaned or raw, max_chars)


def _format_id_tag(prefix: str, *parts: str) -> str:
    clean_parts = [str(p).strip() for p in parts if str(p or "").strip()]
    if clean_parts:
        return f"[{prefix}|{'|'.join(clean_parts)}]"
    return f"[{prefix}]"


def _extract_delegation_info(reply_text: str) -> tuple[str, str, bool]:
    """마스터 응답이 하위 에이전트 위임 안내인지 판별해 (room_id, 위임요약, 추가답변여부)를 반환한다."""
    s = str(reply_text or "").strip()
    if not s:
        return "", "", False
    m = _RE_DELEGATION_REPLY.search(s)
    if not m:
        return "", "", True

    if m.group(1) is not None:
        agent_meta = m.group(1)
        raw_desc = m.group(2) or ""
    else:
        agent_meta = m.group(4) or ""
        raw_desc = m.group(3) or ""

    rid_m = _RE_ROOM_ID.search(agent_meta) or _RE_ROOM_ID.search(s)
    room_id = rid_m.group(1) if rid_m else ""
    delegated_desc = clean_memory_markdown(raw_desc).strip()
    if not delegated_desc and agent_meta:
        delegated_desc = clean_memory_markdown(agent_meta).strip()

    prefix_part = clean_memory_markdown(s[: m.start()]).strip()
    # "지적해주신 대로 ~ 문제가 있습니다." 등 서론성 안내 외의 독립적 사실 답변 여부 체크
    has_extra = len(prefix_part) >= 45 and not any(
        filler in prefix_part for filler in ("지적해주신", "확인했습니다", "요청하신")
    )
    return room_id, delegated_desc, has_extra


def format_dispatch_memory(
    *,
    action: str,
    room_title: str,
    room_id: str,
    task_id: str = "",
    message: str,
    max_chars: int = AUTO_SUMMARY_MAX_CHARS,
) -> str:
    """디스패치(spawn/send) 내역을 ID·핵심 목표 중심의 짧은 요약문으로 생성한다."""
    act = str(action or "spawn").strip().lower() or "spawn"
    tag = _format_id_tag(f"위임:{act}", room_id, task_id)
    body = _strip_boilerplate(message, kind="dispatch")
    avail = max(24, max_chars - len(tag) - 1)
    condensed = _condense_segments(body, avail).rstrip(" ·,;:-—")
    if not condensed:
        condensed = _smart_clip(room_title or "작업 위임", avail)
    return _smart_clip(f"{tag} {condensed}", max_chars)


def format_notify_memory(
    *,
    status_label: str = "성공",
    room_id: str = "",
    task_id: str = "",
    result_text: str = "",
    max_chars: int = AUTO_SUMMARY_MAX_CHARS,
) -> str:
    """하위 작업 보고 내역을 상태·ID·핵심 결과/수치 중심으로 압축 생성한다."""
    label = str(status_label or "보고").strip() or "보고"
    body = str(result_text or "").strip()
    if body.startswith(("[하위 작업 완료 보고]", "[하위 작업 중단/오류 보고]")):
        _, r_id, t_id, extracted_body = _strip_report_template(body)
        room_id = room_id or r_id
        task_id = task_id or t_id
        body = extracted_body
        if "중단/오류" in result_text:
            label = "실패"
    body = _strip_boilerplate(body, kind="notify")
    tag = _format_id_tag(f"보고:{label}", room_id, task_id)
    avail = max(20, max_chars - len(tag) - 1)
    condensed = _condense_segments(body, avail)
    return _smart_clip(f"{tag} {condensed}" if condensed else tag, max_chars)


def format_task_event_memory(
    *,
    event_type: str,
    verdict: str = "-",
    task_id: str = "",
    summary: str = "",
    missing_reason: str | None = None,
    max_chars: int = AUTO_SUMMARY_MAX_CHARS,
) -> str:
    """래퍼 콜백(`task_event`) 이벤트를 간결하게 정제한다."""
    ev = str(event_type or "EVENT").strip().upper()
    vd = str(verdict or "-").strip().upper() or "-"
    tag = _format_id_tag(f"{ev}:{vd}", task_id)
    raw = str(summary or "").strip()
    if missing_reason and missing_reason not in raw:
        raw = f"{raw} · 사유: {missing_reason}" if raw else f"사유: {missing_reason}"
    avail = max(20, max_chars - len(tag) - 1)
    condensed = _condense_segments(raw, avail)
    return _smart_clip(f"{tag} {condensed}" if condensed else tag, max_chars)


def summarize_memory_entry(
    text: str,
    *,
    kind: str = "note",
    title: str = "",
    room_id: str = "",
    task_id: str = "",
    max_chars: int = AUTO_SUMMARY_MAX_CHARS,
) -> str:
    """입력 텍스트를 계층/유형에 맞춰 핵심 사실·결론·수치·ID만 남기도록 요약 정제한다."""
    raw = str(text or "").strip()
    if not raw:
        return ""
    clean_kind = str(kind or "note").strip() or "note"
    limit = max(1, int(max_chars))

    if clean_kind == "note":
        return _clip_text(raw, limit)

    # 1. dispatch
    if clean_kind == "dispatch":
        m = _RE_DISPATCH_HEADER.match(raw)
        if m:
            act = m.group(1).strip()
            r_title = m.group(2).strip()
            r_id = room_id or m.group(3).strip()
            body = m.group(4).strip()
            return format_dispatch_memory(
                action=act,
                room_title=r_title,
                room_id=r_id,
                task_id=task_id,
                message=body,
                max_chars=limit,
            )
        m2 = _RE_DISPATCH_TAG_PREFIX.match(raw)
        if m2:
            inner_tag = m2.group(1)
            act = inner_tag.split("|", 1)[0].strip()
            body = raw[m2.end() :].strip()
            if title and body.startswith(title):
                body = body[len(title) :].lstrip(" —:-")
            return format_dispatch_memory(
                action=act,
                room_title=title,
                room_id=room_id,
                task_id=task_id,
                message=body,
                max_chars=limit,
            )
        return format_dispatch_memory(
            action="spawn",
            room_title=title,
            room_id=room_id,
            task_id=task_id,
            message=raw,
            max_chars=limit,
        )

    # 2. notify
    if clean_kind == "notify":
        m = _RE_NOTIFY_HEADER.match(raw)
        if m:
            if m.group(1):  # [성공 보고 · room-...]
                lbl = m.group(1).strip()
                r_id = room_id or m.group(2).strip()
                t_id = task_id or (m.group(3) or "").strip()
                body = m.group(6).strip()
            else:  # [보고:channel]
                lbl = (m.group(4) or "보고").strip()
                if lbl == "task_report":
                    lbl = "완료"
                r_id = room_id
                t_id = task_id or (m.group(5) or "").strip()
                body = m.group(6).strip()
            return format_notify_memory(
                status_label=lbl,
                room_id=r_id,
                task_id=t_id,
                result_text=body,
                max_chars=limit,
            )
        return format_notify_memory(
            status_label="보고",
            room_id=room_id,
            task_id=task_id,
            result_text=raw,
            max_chars=limit,
        )

    # 3. task_event
    if clean_kind == "task_event":
        m = _RE_TASK_EVENT_HEADER.match(raw)
        if m:
            ev = m.group(1).strip()
            vd = m.group(2).strip()
            t_id = task_id or (m.group(3) or "").strip()
            body = m.group(4).strip()
            return format_task_event_memory(
                event_type=ev,
                verdict=vd,
                task_id=t_id,
                summary=body,
                max_chars=limit,
            )
        return _condense_segments(raw, limit)

    # 4. turn ("Q: ... → A: ..." 또는 "[완료응답|...] ...")
    if clean_kind == "turn":
        if raw.startswith(("[완료응답", "[정기점검", "[보고:")):
            tag_m = re.match(r"^(\[[^\]]+\])\s*(.*)$", raw, re.DOTALL)
            if tag_m:
                tag_str = tag_m.group(1)
                body = _strip_boilerplate(tag_m.group(2), kind="turn")
                avail = max(16, limit - len(tag_str) - 1)
                condensed = _condense_segments(body, avail)
                return _smart_clip(f"{tag_str} {condensed}" if condensed else tag_str, limit)

        qa_m = re.match(r"^Q:\s*(.+?)\s*(?:→|->)\s*A:\s*(.+)$", raw, re.DOTALL)
        if qa_m:
            q_raw = qa_m.group(1).strip()
            a_raw = qa_m.group(2).strip()

            # 하위 작업 완료 보고 턴인 경우 템플릿 제거 후 완료응답 형식으로 압축
            if q_raw.startswith(("[하위 작업 완료 보고]", "[하위 작업 중단/오류 보고]")):
                _, r_id, t_id, _ = _strip_report_template(q_raw)
                r_id = room_id if (room_id and room_id != "__core__") else r_id
                t_id = task_id or t_id
                tag = _format_id_tag("완료응답", r_id, t_id)
                a_clean = _strip_boilerplate(a_raw, kind="turn")
                avail = max(20, limit - len(tag) - 1)
                condensed_a = _condense_segments(a_clean, avail)
                return _smart_clip(f"{tag} {condensed_a}" if condensed_a else tag, limit)

            if q_raw.startswith("[마스터 에이전트 정기 점검"):
                a_clean = _strip_boilerplate(a_raw, kind="turn")
                avail = max(20, limit - len("[정기점검] "))
                return _smart_clip(f"[정기점검] {_condense_segments(a_clean, avail)}", limit)

            q_budget = min(44, max(20, int(limit * 0.26)))
            q_clean = summarize_user_query(q_raw, max_chars=q_budget)
            a_clean = _strip_boilerplate(a_raw, kind="turn")
            prefix = f"Q: {q_clean} → A: " if q_clean else ""
            a_budget = max(24, limit - len(prefix))
            condensed_a = _condense_segments(a_clean, a_budget)
            if not condensed_a:
                condensed_a = _smart_clip(clean_memory_markdown(a_raw), a_budget)
            return _smart_clip(f"{prefix}{condensed_a}", limit)

        # Q/A 구분이 없는 일반 turn 응답
        return _condense_segments(_strip_boilerplate(raw, kind="turn"), limit)

    return _condense_segments(raw, limit)


def _default_limits() -> dict[str, dict[str, int]]:
    return copy.deepcopy(DEFAULT_TIER_LIMITS)


def _normalize_limits(raw_limits: Any, *, migrate_legacy: bool = False) -> dict[str, dict[str, int]]:
    limits = _default_limits()
    if not isinstance(raw_limits, dict):
        return limits
    for tier in TIERS:
        patch_val = raw_limits.get(tier)
        if not isinstance(patch_val, dict):
            continue
        for key in ("maxChars", "maxItems", "itemMaxChars"):
            if key in patch_val and patch_val[key] is not None:
                try:
                    val = int(patch_val[key])
                    if val > 0:
                        if migrate_legacy and key == "itemMaxChars" and val == _LEGACY_DEFAULT_ITEM_MAX.get(tier):
                            val = DEFAULT_TIER_LIMITS[tier]["itemMaxChars"]
                        limits[tier][key] = val
                except (TypeError, ValueError):
                    pass
    return limits


def _merge_texts(existing_text: str, incoming_text: str, *, max_chars: int) -> str:
    """동일 작업의 중복 보고/후속 응답을 1개 요약문으로 병합한다."""
    ext_m = re.match(r"^(\[[^\]]+\])\s*(.*)$", existing_text.strip(), re.DOTALL)
    inc_m = re.match(r"^(\[[^\]]+\])\s*(.*)$", incoming_text.strip(), re.DOTALL)

    tag = ext_m.group(1) if ext_m else (inc_m.group(1) if inc_m else "")
    ext_body = ext_m.group(2).strip() if ext_m else existing_text.strip()
    inc_body = inc_m.group(2).strip() if inc_m else incoming_text.strip()

    if not inc_body or inc_body in ext_body:
        return _smart_clip(existing_text, max_chars)
    if not ext_body or ext_body in inc_body:
        return _smart_clip(f"{tag} {inc_body}".strip() if tag else inc_body, max_chars)

    # 태그 내 작업ID/방ID 보강
    if ext_m and inc_m:
        # 보고(성공/실패) 태그를 우선 유지하되 누락된 ID 보강
        tag = ext_m.group(1)
        if "[완료응답" in tag and "[보고:" in inc_m.group(1):
            tag = inc_m.group(1)

    ext_segs = _split_meaningful_segments(ext_body)
    inc_segs = _split_meaningful_segments(inc_body)
    combined: list[str] = []
    seen: set[str] = set()
    for seg in ext_segs + inc_segs:
        norm = _norm_plain(seg)
        # 기존 문장에 상당 부분 포함되면 중복으로 간주
        if not norm or norm in seen or any(norm in s or s in norm for s in seen if len(s) >= 8 and len(norm) >= 8):
            continue
        seen.add(norm)
        combined.append(seg)

    body_budget = max(20, max_chars - (len(tag) + 1 if tag else 0))
    merged_body = _condense_segments(" · ".join(combined), body_budget)
    return _smart_clip(f"{tag} {merged_body}".strip() if tag else merged_body, max_chars)


def _task_ids_match(tid_a: str, tid_b: str, room_a: str = "", room_b: str = "") -> bool:
    """두 taskId 가 동일하거나(또는 동일 방의 6자 이상 prefix 관계인지) 판별한다."""
    a = str(tid_a or "").strip()
    b = str(tid_b or "").strip()
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) >= 8 and len(b) >= 8 and (a.startswith(b) or b.startswith(a)):
        ra = str(room_a or "").strip()
        rb = str(room_b or "").strip()
        if not ra or not rb or ra == "__core__" or rb == "__core__" or ra == rb:
            return True
    return False


def _is_generic_report_title(title: str) -> bool:
    t = str(title or "").strip()
    return (not t) or t.startswith(("보고:", "Task:", "작업:", "하위보고"))


# ── 내부 시스템 잡음 vs 가치 있는 기억 판정 정규식 ─────────────────────────
_RE_VALUABLE_USER_RULE = re.compile(
    r"(?:^\[(?:규칙|선호|지시|원칙)\]|작업\s*방식을?\s*개선|지양하며|지양하고|지양하|원칙으로\s*삼|항상\s+[^\s]+하|"
    r"앞으로\s+[^\s]+하|영구\s*삭제하고|남발을\s*지양)"
)
_RE_VALUABLE_VAULT_DOMAIN = re.compile(
    r"(?:가계부|네이버페이|수입[·/\s]*지출|실수지|정산|차계부|주유|자동차(?:보험)?|마일리지\s*특약|주행거리|"
    r"진료|병원|처방|의약품|약\s*복용|실손보험?|건강검진|카드\s*(?:혜택|보유|할인|실적)|할인카드|"
    r"연저펀|연금저축|주식|배당|계좌|식재료|레시피|캘린더|볼트\s*(?:동기화|확인|반영|기록)|개인/금융)"
)
_RE_ACTIVE_TASK_HINT = re.compile(
    r"(?:진행\s*중|미완결|후속\s*검증\s*필요|확인\s*필요|대기\s*중(?!.*패스)|보류\s*중)"
)
_RE_META_SCHEDULER_NOISE = re.compile(
    r"(?:토큰\s*버닝.*(?:패스|미달|스킵|생략)|주간\s*리셋까지.*남아.*발동\s*기준|"
    r"(?:4계층\s*)?메모리\s*리밸런싱.*(?:삭제|재배치|정제|완료)|memory-rebalance\s*스킬|"
    r"정기\s*점검.*이상\s*없음)"
)
_RE_DEV_CODE_SIGNAL = re.compile(
    r"(?:\b[a-zA-Z0-9_]+\.(?:py|html|js|ts|css|sb)\b|SKILL\.md|"
    r"\b(?:ReferenceError|TypeError|SyntaxError|ValueError|KeyError|AttributeError)\b|"
    r"\b(?:session-ctl|chat_db|web_execution|web_orchestrator|master_memory|agy_wrapper|burn_runner)\b|"
    r"\b[a-zA-Z_][a-zA-Z0-9_]{2,}\(\)|\btest_[a-zA-Z0-9_]+\b|"
    r"단위\s*테스트|테스트\s*통과|전수\s*통과|정적\s*자산\s*테스트|단언문|passed|"
    r"버그\s*수정|렌더링\s*(?:실패|수정)|함수\s*수정|로직\s*(?:수정|검증)|방어\s*로직|안전장치\s*적용|"
    r"유실\s*원인|재부착\s*구조|유사\s*(?:방|대화방)\s*매칭|TF-IDF|유사도\s*임계값|내부\s*리셋|"
    r"워크트리|main\s*(?:에\s*)?(?:merge|병합)|restart\.sh|workdir|"
    r"웹\s*UI|칩\s*UI|모달\s*(?:카드|드래그)|드래그\s*앤\s*드롭|슬라이더|닫기\s*버튼|팝오버|뱃지|배지|"
    r"스킬\s*(?:신설|개편|화)|자동화\s*(?:탭|통합\s*탭)|토큰\s*버닝\s*탭|토큰\s*보호\s*단계|"
    r"스케줄러\s*(?:개편|전환)|schedule_trigger|하위\s*에이전트\s*(?:점검|칩)|"
    r"\[done\]\s*일괄\s*표기|4계층\s*메모리\s*(?:요약\s*저장|모달))"
)
_RE_DEV_ACTION_OR_STATE = re.compile(
    r"(?:완료|수정|개편|통과|배포|적용|구현|해결|규명|개선|간소화|전환|검증|위임함|위임\]|마이그레이션|리팩토링|추가|제거)"
)


def is_internal_system_noise(
    text: str,
    *,
    title: str = "",
    kind: str = "note",
    agent_id: str = "master",
) -> tuple[bool, str]:
    """시스템 내부 개발·버그픽스·UI 변경·테스트·스케줄 패스 등 재참조 가치가 없는 잡음인지 판별한다."""
    combined = f"{title} {text}".strip()
    if not combined:
        return True, "empty"

    # 1. 무의미한 스케줄러 패스 / 리밸런싱 메타 로그는 최우선 배제
    if _RE_META_SCHEDULER_NOISE.search(combined):
        return True, "meta_scheduler_noise"

    # 2. 봇개발 에이전트(bot_dev) 및 마스터 상향 보고 태그([봇개발]/[세컨브레인])는 개발 컨텍스트를 보존
    norm_agent = normalize_agent_id(agent_id)
    if norm_agent == "bot_dev" or combined.startswith(("[봇개발]", "[세컨브레인]")):
        return False, ""

    # 3. 사용자의 명시적 선호/지시/규칙은 보존
    if _RE_VALUABLE_USER_RULE.search(combined):
        return False, ""

    # 4. 옵시디언 볼트 지식 및 개인 기록(가계부, 건강, 차량, 금융 등)은 보존
    if _RE_VALUABLE_VAULT_DOMAIN.search(combined):
        return False, ""

    # 5. 현재 진행 중/미완결/후속 검증 필요 작업은 보존
    if _RE_ACTIVE_TASK_HINT.search(combined):
        return False, ""

    # 6. 내부 개발·코드·UI·테스트·프레임워크 변경 히스토리는 잡음으로 배제
    if _RE_DEV_CODE_SIGNAL.search(combined) and _RE_DEV_ACTION_OR_STATE.search(combined):
        return True, "internal_dev_history"

    return False, ""


def _compact_and_dedup_unlocked(
    data: dict[str, Any],
    *,
    now: float | None = None,
    agent_id: str = "master",
) -> dict[str, int]:
    """모든 계층의 비대 항목을 정제·요약하고 동일 작업/유사 중복 항목을 병합한다."""
    ts = float(now if now is not None else _now())
    limits = data["limits"]
    compacted = 0
    merged = 0
    cleaned = 0
    purged = 0
    seen_texts: set[str] = set()
    # taskId 기반 중복 보고 통합 인덱스: list[(task_id, room_id, tier, item_dict)]
    report_entries: list[tuple[str, str, str, dict[str, Any]]] = []
    # dispatch 항목 인덱스(이미 완료 보고가 있으면 완료 보고로 흡수): task_id -> item_dict
    seen_dispatch_by_task: list[tuple[str, str, str, dict[str, Any]]] = []

    def _find_matching_report(tid: str, rid: str) -> tuple[str, dict[str, Any]] | None:
        for r_tid, r_rid, r_tier, r_it in report_entries:
            if _task_ids_match(tid, r_tid, rid, r_rid):
                return r_tier, r_it
        return None

    for t in TIERS:
        items = data["tiers"][t]
        lim = limits[t]
        item_max = int(lim["itemMaxChars"])
        new_items: list[dict[str, Any]] = []

        # 최신 항목 우선으로 훑되 원래 순서를 보존하기 위해 역순 순회 후 뒤집는다.
        for raw_it in reversed(items):
            it = dict(raw_it)
            kind = str(it.get("kind") or "note").strip() or "note"
            raw_text = str(it.get("text") or "").strip()
            if not raw_text:
                cleaned += 1
                continue

            room_id = str(it.get("roomId") or "").strip()
            task_id = str(it.get("taskId") or "").strip()
            all_tids = _RE_TASK_ID.findall(raw_text) + _RE_TASK_ID.findall(str(it.get("title") or ""))
            if all_tids:
                longest_tid = max(all_tids, key=len)
                if not task_id or (longest_tid.startswith(task_id) and len(longest_tid) > len(task_id)):
                    task_id = longest_tid
                    it["taskId"] = task_id
            if not room_id or room_id == "__core__":
                m_rid = _RE_ROOM_ID.search(raw_text)
                if m_rid and (kind != "turn" or "[하위 작업" in raw_text or "[완료응답" in raw_text):
                    room_id = m_rid.group(1)
                    if kind != "turn":
                        it["roomId"] = room_id

            target_cap = min(item_max, AUTO_SUMMARY_MAX_CHARS) if kind in AUTO_SUMMARY_KINDS else item_max
            new_title = summarize_memory_title(
                str(it.get("title") or ""),
                kind=kind,
                room_id=room_id,
                task_id=task_id,
            )
            if kind in AUTO_SUMMARY_KINDS:
                new_text = summarize_memory_entry(
                    raw_text,
                    kind=kind,
                    title=new_title,
                    room_id=room_id,
                    task_id=task_id,
                    max_chars=target_cap,
                )
            else:
                new_text = _clip_text(raw_text, target_cap)

            if not new_text:
                cleaned += 1
                continue

            is_noise, _ = is_internal_system_noise(new_text, title=new_title, kind=kind, agent_id=agent_id)
            if is_noise:
                purged += 1
                cleaned += 1
                continue

            if new_text != raw_text or new_title != str(it.get("title") or ""):
                compacted += 1

            it["title"] = new_title
            it["text"] = new_text
            it["chars"] = len(new_text)
            it["tier"] = t

            # 완료 보고(notify / task_event / [완료응답])가 이미 존재하면 동일 task_id 의 dispatch 는 흡수·제거
            if kind == "dispatch" and task_id:
                rep_match = _find_matching_report(task_id, room_id)
                if rep_match is not None:
                    _, keeper = rep_match
                    if _is_generic_report_title(keeper.get("title") or "") and not _is_generic_report_title(new_title):
                        keeper["title"] = new_title
                    merged += 1
                    continue

            # 동일 task_id 의 중복 보고(notify / task_event / task_report turn) 병합
            is_report_like = bool(
                task_id
                and (
                    kind in ("notify", "task_event")
                    or (kind == "turn" and new_text.startswith(("[완료응답", "[보고:")))
                )
            )
            if is_report_like:
                rep_match = _find_matching_report(task_id, room_id)
                if rep_match is not None:
                    _, keeper = rep_match
                    keeper_cap = min(int(limits[keeper["tier"]]["itemMaxChars"]), AUTO_SUMMARY_MAX_CHARS)
                    keeper["text"] = _merge_texts(keeper["text"], new_text, max_chars=keeper_cap)
                    keeper["chars"] = len(keeper["text"])
                    keeper["updatedAt"] = max(float(keeper.get("updatedAt") or 0), float(it.get("updatedAt") or ts))
                    if len(task_id) > len(str(keeper.get("taskId") or "")):
                        keeper["taskId"] = task_id
                    if _is_generic_report_title(keeper.get("title") or "") and not _is_generic_report_title(new_title):
                        keeper["title"] = new_title
                    merged += 1
                    continue

                # 이전에 먼저 순회된 dispatch 가 있었다면 제거하고 제목 상속
                for d_idx in range(len(seen_dispatch_by_task) - 1, -1, -1):
                    d_tid, d_rid, d_tier, d_it = seen_dispatch_by_task[d_idx]
                    if _task_ids_match(task_id, d_tid, room_id, d_rid):
                        if _is_generic_report_title(it.get("title") or "") and not _is_generic_report_title(d_it.get("title") or ""):
                            it["title"] = d_it["title"]
                        target_list = new_items if d_tier == t else data["tiers"][d_tier]
                        if d_it in target_list:
                            target_list.remove(d_it)
                        seen_dispatch_by_task.pop(d_idx)
                        merged += 1

            norm_key = _norm_dedup_key(kind, new_text)
            if norm_key in seen_texts:
                merged += 1
                continue
            seen_texts.add(norm_key)

            if is_report_like:
                report_entries.append((task_id, room_id, t, it))
            elif kind == "dispatch" and task_id:
                seen_dispatch_by_task.append((task_id, room_id, t, it))

            new_items.append(it)

        data["tiers"][t] = list(reversed(new_items))

    data["version"] = CURRENT_VERSION
    return {"compacted": compacted, "merged": merged, "cleaned": cleaned, "purged": purged}


def _normalize_data(raw: Any, *, agent_id: str = "master") -> tuple[dict[str, Any], bool]:
    """디스크 JSON을 정규화하고 v1 레거시 포맷이면 자동 압축 마이그레이션 여부를 반환한다."""
    norm_agent = normalize_agent_id(agent_id)
    data: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
    raw_version = int(data.get("version") or 0) if isinstance(raw, dict) and raw else CURRENT_VERSION
    is_legacy = bool(isinstance(raw, dict) and raw and raw_version < CURRENT_VERSION)

    raw_tiers = data.get("tiers")
    tiers: dict[str, list[dict[str, Any]]] = {}
    for tier in TIERS:
        items = raw_tiers.get(tier) if isinstance(raw_tiers, dict) else None
        if isinstance(items, list):
            tiers[tier] = [dict(it) for it in items if isinstance(it, dict) and str(it.get("text") or "").strip()]
        else:
            tiers[tier] = []
    data["tiers"] = tiers
    data["limits"] = _normalize_limits(data.get("limits"), migrate_legacy=is_legacy)

    raw_stats = data.get("stats") if isinstance(data.get("stats"), dict) else {}
    data["stats"] = {
        "totalAdded": int(raw_stats.get("totalAdded") or 0),
        "cascadeCount": int(raw_stats.get("cascadeCount") or 0),
        "droppedCount": int(raw_stats.get("droppedCount") or 0),
        "lastResetAt": raw_stats.get("lastResetAt"),
        "lastCascadeAt": raw_stats.get("lastCascadeAt"),
    }
    data["version"] = CURRENT_VERSION
    data["agentId"] = norm_agent

    migrated = False
    if is_legacy and any(tiers[t] for t in TIERS):
        _compact_and_dedup_unlocked(data, agent_id=norm_agent)
        _cascade_overflow_unlocked(data)
        migrated = True

    return data, migrated


def _load_unlocked(agent_id: str = "master") -> dict[str, Any]:
    norm_agent = normalize_agent_id(agent_id)
    mem_file = _get_memory_file(norm_agent)
    raw = read_json(mem_file, {}, expect_type=dict)
    data, migrated = _normalize_data(raw, agent_id=norm_agent)
    if migrated:
        _save_unlocked(data, agent_id=norm_agent)
    return data


def _save_unlocked(data: dict[str, Any], *, now: float | None = None, agent_id: str = "master") -> None:
    norm_agent = normalize_agent_id(agent_id or data.get("agentId"))
    data["agentId"] = norm_agent
    data["updatedAt"] = float(now if now is not None else _now())
    write_json_atomic(_get_memory_file(norm_agent), data)


def _tier_chars(items: list[dict[str, Any]]) -> int:
    return sum(len(str(it.get("text") or "")) for it in items)


def _demote_item_payload(
    item: dict[str, Any],
    *,
    from_tier: str,
    to_tier: str,
    item_max_chars: int,
    now: float,
) -> dict[str, Any]:
    moved = dict(item)
    clipped = _smart_clip(moved.get("text") or "", item_max_chars)
    moved["text"] = clipped
    moved["chars"] = len(clipped)
    moved["tier"] = to_tier
    moved["fromTier"] = from_tier
    moved["movedAt"] = now
    moved["updatedAt"] = now
    moved["demoteCount"] = int(moved.get("demoteCount") or 0) + 1
    return moved


def _cascade_overflow_unlocked(data: dict[str, Any], *, now: float | None = None) -> dict[str, int]:
    """모든 계층(hot → warm → cold → freeze)의 길이/개수 초과분을 하위 캐시로 연쇄 이동한다."""
    ts = float(now if now is not None else _now())
    limits = data["limits"]
    tiers = data["tiers"]
    stats = data["stats"]
    moved_counts: dict[str, int] = {t: 0 for t in TIERS}
    dropped = 0

    for tier in TIERS:
        items = tiers[tier]
        tier_lim = limits[tier]
        max_chars = int(tier_lim["maxChars"])
        max_items = int(tier_lim["maxItems"])
        next_tier = NEXT_TIER[tier]

        while len(items) > max_items or _tier_chars(items) > max_chars:
            oldest = items.pop(0)
            if next_tier is not None:
                next_lim = limits[next_tier]
                demoted = _demote_item_payload(
                    oldest,
                    from_tier=tier,
                    to_tier=next_tier,
                    item_max_chars=int(next_lim["itemMaxChars"]),
                    now=ts,
                )
                if demoted["text"]:
                    tiers[next_tier].append(demoted)
                moved_counts[tier] += 1
                stats["cascadeCount"] = int(stats.get("cascadeCount") or 0) + 1
                stats["lastCascadeAt"] = ts
            else:
                dropped += 1
                stats["droppedCount"] = int(stats.get("droppedCount") or 0) + 1

    return {
        "cascaded": sum(moved_counts.values()),
        "dropped": dropped,
    }


def _snapshot_unlocked(
    data: dict[str, Any],
    *,
    tier: str | None = None,
    limit_per_tier: int = 50,
    agent_id: str = "master",
) -> dict[str, Any]:
    target_tiers = (tier,) if tier in TIERS else TIERS
    limits = data["limits"]
    tiers_out: dict[str, dict[str, Any]] = {}
    total_items = 0
    total_chars = 0

    for t in TIERS:
        items = data["tiers"][t]
        char_count = _tier_chars(items)
        lim = limits[t]
        max_chars = int(lim["maxChars"])
        max_items = int(lim["maxItems"])
        total_items += len(items)
        total_chars += char_count
        if t not in target_tiers:
            continue
        # UI/CLI 표시용으로는 최신 항목이 위로 오도록 역순 슬라이싱 제공
        recent_items = [dict(it) for it in reversed(items[-max(1, int(limit_per_tier)) :])]
        usage_ratio = round(char_count / max_chars, 4) if max_chars > 0 else 0.0
        tiers_out[t] = {
            "tier": t,
            "label": TIER_LABELS[t],
            "itemCount": len(items),
            "maxItems": max_items,
            "charCount": char_count,
            "maxChars": max_chars,
            "itemMaxChars": int(lim["itemMaxChars"]),
            "usageRatio": usage_ratio,
            "usagePct": min(100, round(usage_ratio * 100)),
            "items": recent_items,
        }

    norm_agent = normalize_agent_id(data.get("agentId") or agent_id)
    return {
        "version": CURRENT_VERSION,
        "agentId": norm_agent,
        "agentLabel": AGENT_LABELS.get(norm_agent, norm_agent),
        "tierOrder": list(TIERS),
        "tiers": tiers_out,
        "limits": copy.deepcopy(limits),
        "stats": dict(data["stats"]),
        "totalItems": total_items,
        "totalChars": total_chars,
        "updatedAt": data.get("updatedAt"),
    }


def _pop_matching_dispatch_unlocked(
    data: dict[str, Any],
    *,
    clean_task: str,
    clean_room: str,
) -> dict[str, Any] | None:
    """hot/warm 에서 동일 taskId 의 대기 중 dispatch 항목을 찾아 꺼낸다."""
    if not clean_task:
        return None
    for search_tier in ("hot", "warm"):
        items = data["tiers"][search_tier]
        for idx in range(len(items) - 1, -1, -1):
            cand = items[idx]
            if cand.get("kind") != "dispatch":
                continue
            if _task_ids_match(cand.get("taskId") or "", clean_task, cand.get("roomId") or "", clean_room):
                return items.pop(idx)
    return None


def _try_merge_report_unlocked(
    data: dict[str, Any],
    *,
    clean_kind: str,
    clean_text: str,
    clean_title: str,
    clean_room: str,
    clean_task: str,
    target_tier: str,
    summary_limit: int,
    now: float,
) -> dict[str, Any] | None:
    """동일 taskId 를 가진 완료 보고/후속 응답이 있으면 기존 항목 또는 대기 dispatch 에 병합한다."""
    if not clean_task:
        return None
    is_report_item = clean_kind in ("notify", "task_event") or (
        clean_kind == "turn" and clean_text.startswith("[완료응답")
    )
    if not is_report_item:
        return None

    for search_tier in ("hot", "warm"):
        items = data["tiers"][search_tier]
        for idx in range(len(items) - 1, -1, -1):
            cand = items[idx]
            if not _task_ids_match(cand.get("taskId") or "", clean_task, cand.get("roomId") or "", clean_room):
                continue
            cand_kind = str(cand.get("kind") or "")
            if cand_kind not in ("notify", "task_event") and not (
                cand_kind == "turn" and str(cand.get("text") or "").startswith("[완료응답")
            ):
                continue

            # INTERIM -> COMPLETED 등 상태가 바뀐 task_event 끼리는 최신 이벤트 내용 우선
            if clean_kind == "task_event" and cand_kind == "task_event":
                cand["text"] = clean_text
            else:
                cand["text"] = _merge_texts(str(cand.get("text") or ""), clean_text, max_chars=summary_limit)
            cand["chars"] = len(cand["text"])
            cand["updatedAt"] = now
            if len(clean_task) > len(str(cand.get("taskId") or "")):
                cand["taskId"] = clean_task
            if clean_title and (_is_generic_report_title(cand.get("title") or "") or not _is_generic_report_title(clean_title)):
                cand["title"] = clean_title

            # 혹시 남아있는 동일 taskId dispatch 가 있으면 정리하고 제목 보강
            stale_disp = _pop_matching_dispatch_unlocked(data, clean_task=clean_task, clean_room=clean_room)
            if stale_disp is not None and _is_generic_report_title(cand.get("title") or ""):
                d_title = str(stale_disp.get("title") or "").strip()
                if not _is_generic_report_title(d_title):
                    cand["title"] = d_title

            # warm 에 있던 기존 보고가 방금 마스터 응답으로 갱신됐다면 hot 끝으로 승격
            if search_tier != target_tier and target_tier == "hot":
                items.pop(idx)
                cand["tier"] = target_tier
                cand["movedAt"] = now
                data["tiers"][target_tier].append(cand)
            return cand

    # 기존 보고는 없지만 완료/중단 보고가 도착했다면 동일 taskId 의 dispatch 항목을 대체·병합
    if clean_kind == "notify" or (clean_kind == "turn" and clean_text.startswith("[완료응답")):
        disp = _pop_matching_dispatch_unlocked(data, clean_task=clean_task, clean_room=clean_room)
        if disp is not None:
            resolved_title = clean_title
            d_title = str(disp.get("title") or "").strip()
            if _is_generic_report_title(resolved_title) and not _is_generic_report_title(d_title):
                resolved_title = d_title

            final_text = clean_text
            # 실패/추적 끊김 등 빈약한 오류 보고면 dispatch 주제를 간략히 앞에 보존
            if ("실행 추적이 끊겼고" in final_text or "작업 중단됨" in final_text) and d_title and d_title not in final_text:
                m_tag = re.match(r"^(\[[^\]]+\])\s*(.*)$", final_text, re.DOTALL)
                if m_tag:
                    final_text = _smart_clip(f"{m_tag.group(1)} {d_title} — {m_tag.group(2)}", summary_limit)

            folded = {
                "id": disp.get("id") or f"mm-{uuid.uuid4().hex[:8]}",
                "tier": target_tier,
                "kind": clean_kind,
                "title": resolved_title,
                "text": final_text,
                "chars": len(final_text),
                "roomId": clean_room or disp.get("roomId") or "",
                "taskId": clean_task,
                "createdAt": disp.get("createdAt") or now,
                "updatedAt": now,
                "movedAt": now if disp.get("tier") != target_tier else disp.get("movedAt"),
                "fromTier": disp.get("tier") if disp.get("tier") != target_tier else disp.get("fromTier"),
                "demoteCount": int(disp.get("demoteCount") or 0),
            }
            data["tiers"][target_tier].append(folded)
            return folded

    return None


async def _run_flash_low_summary(raw_text: str, *, kind: str, title: str) -> str:
    """경량 모델(flash low)로 원문을 개행 없는 1문장 핵심 요약으로 압축한다."""
    from agy_wrapper import run_agy_cli

    prompt = (
        "다음 마스터 에이전트 기억을 메모리 저장용으로 핵심 팩트·결론·수치·파일명만 남겨 "
        "개행 없는 평문 1문장(최대 100자)으로 요약하라.\n"
        "규칙: 마크다운·이모지·번호목록·인사말·상투어('확인했습니다', '진행할까요?' 등)·"
        "room-/mt- ID 금지. 제목의 질문을 반복하지 말 것. 요약문만 출력.\n\n"
        f"유형: {kind}\n"
        f"제목: {title}\n"
        f"원문:\n{str(raw_text or '')[:1500]}"
    )
    extra = ["--model", LLM_SUMMARY_MODEL, "--effort", LLM_SUMMARY_EFFORT, "--sandbox"]
    res = await run_agy_cli(prompt, timeout_sec=20, extra_args=extra)
    if res.exit_status != "NORMAL" or not (res.stdout or "").strip():
        return ""
    cleaned = clean_memory_markdown(res.stdout.strip())
    cleaned = " ".join(cleaned.split()).strip(" \"'·-:;")
    return _smart_clip(cleaned, AUTO_SUMMARY_MAX_CHARS)


def update_memory_item_text(
    item_id: str,
    new_text: str,
    *,
    llm_summarized: bool = True,
    agent_id: str = "master",
) -> bool:
    """저장된 메모리 항목의 본문을 비동기 LLM 요약본으로 제자리 갱신한다."""
    target_id = str(item_id or "").strip()
    cleaned = _smart_clip(new_text, AUTO_SUMMARY_MAX_CHARS)
    if not target_id or not cleaned:
        return False
    norm_agent = normalize_agent_id(agent_id)
    now = _now()
    with _lock:
        data = _load_unlocked(norm_agent)
        for t in TIERS:
            for it in data["tiers"][t]:
                if it.get("id") == target_id:
                    it["text"] = cleaned
                    it["chars"] = len(cleaned)
                    if llm_summarized:
                        it["llmSummarized"] = True
                    _save_unlocked(data, now=now, agent_id=norm_agent)
                    return True
    return False


async def _refine_memory_item_bg(
    item_id: str,
    raw_text: str,
    *,
    kind: str,
    title: str,
    existing_text: str,
    agent_id: str = "master",
) -> None:
    try:
        summarized = await _run_flash_low_summary(raw_text, kind=kind, title=title)
        if not summarized or len(summarized) < 6:
            return
        short_tag, _ = _compact_prompt_tag(existing_text)
        final_text = f"{short_tag} {summarized}".strip() if (short_tag and not summarized.startswith("[")) else summarized
        update_memory_item_text(item_id, final_text, llm_summarized=True, agent_id=agent_id)
    except Exception as exc:
        logger.debug("[master_memory] LLM 백그라운드 요약 생략 (id=%s): %s", item_id, exc)


def _schedule_llm_refine(
    item_id: str,
    raw_text: str,
    *,
    kind: str,
    title: str,
    existing_text: str,
    agent_id: str = "master",
) -> None:
    if not LLM_SUMMARY_ENABLED or kind not in AUTO_SUMMARY_KINDS:
        return
    if len(str(raw_text or "").strip()) < 45:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if len(_bg_refine_tasks) >= 4:
        return
    task = loop.create_task(
        _refine_memory_item_bg(
            item_id,
            raw_text,
            kind=kind,
            title=title,
            existing_text=existing_text,
            agent_id=agent_id,
        )
    )
    _bg_refine_tasks.add(task)
    task.add_done_callback(_bg_refine_tasks.discard)


def add_memory(
    text: str,
    *,
    title: str = "",
    kind: str = "note",
    room_id: str = "",
    task_id: str = "",
    tier: str = "hot",
    limits_override: dict[str, dict[str, int]] | None = None,
    summarize: bool | None = None,
    raw_text_for_llm: str | None = None,
    filter_noise: bool | None = None,
    agent_id: str = "master",
) -> dict[str, Any]:
    """새 기억 항목을 지정 계층(기본 hot)에 추가하고 초과분을 하위 캐시로 연쇄 이동한다."""
    target_tier = str(tier or "hot").strip().lower()
    if target_tier not in TIERS:
        raise ValueError(f"지원하지 않는 메모리 계층입니다: {tier}")
    norm_agent = normalize_agent_id(agent_id)

    now = _now()
    with _lock:
        data = _load_unlocked(norm_agent)
        if limits_override:
            for t_name, patch_vals in limits_override.items():
                if t_name in TIERS and isinstance(patch_vals, dict):
                    data["limits"][t_name].update(
                        {k: int(v) for k, v in patch_vals.items() if k in ("maxChars", "maxItems", "itemMaxChars") and int(v) > 0}
                    )

        clean_kind = str(kind or "note").strip() or "note"
        clean_room = str(room_id or "").strip()
        clean_task = str(task_id or "").strip()
        item_limit = int(data["limits"][target_tier]["itemMaxChars"])
        should_summarize = (clean_kind in AUTO_SUMMARY_KINDS) if summarize is None else bool(summarize)

        summary_limit = min(item_limit, AUTO_SUMMARY_MAX_CHARS) if should_summarize else item_limit
        clean_title = summarize_memory_title(title, kind=clean_kind, room_id=clean_room, task_id=clean_task)

        if should_summarize:
            clean_text = summarize_memory_entry(
                text,
                kind=clean_kind,
                title=clean_title,
                room_id=clean_room,
                task_id=clean_task,
                max_chars=summary_limit,
            )
        else:
            clean_text = _clip_text(text, item_limit)

        if not clean_text:
            raise ValueError("저장할 메모리 내용이 비어 있습니다.")

        should_filter = should_summarize if filter_noise is None else bool(filter_noise)
        if should_filter:
            is_noise, noise_reason = is_internal_system_noise(
                clean_text, title=clean_title, kind=clean_kind, agent_id=norm_agent
            )
            if is_noise:
                removed_disp = False
                if clean_task and clean_kind in ("notify", "task_event"):
                    removed_disp = _pop_matching_dispatch_unlocked(
                        data, clean_task=clean_task, clean_room=clean_room
                    ) is not None
                if removed_disp:
                    _save_unlocked(data, now=now, agent_id=norm_agent)
                snap = _snapshot_unlocked(data, agent_id=norm_agent)
                snap["addedItem"] = None
                snap["lastOperation"] = {
                    "action": "skip",
                    "reason": noise_reason,
                    "tier": target_tier,
                    "cascaded": 0,
                    "dropped": 0,
                }
                return snap

        merged_item = _try_merge_report_unlocked(
            data,
            clean_kind=clean_kind,
            clean_text=clean_text,
            clean_title=clean_title,
            clean_room=clean_room,
            clean_task=clean_task,
            target_tier=target_tier,
            summary_limit=summary_limit,
            now=now,
        )
        if merged_item is not None:
            cascade_info = _cascade_overflow_unlocked(data, now=now)
            _save_unlocked(data, now=now, agent_id=norm_agent)
            snap = _snapshot_unlocked(data, agent_id=norm_agent)
            snap["addedItem"] = dict(merged_item)
            snap["lastOperation"] = {"action": "add", "tier": target_tier, "merged": True, **cascade_info}
            return snap

        tier_list = data["tiers"][target_tier]
        norm_new = _norm_dedup_key(clean_kind, clean_text)
        duplicate_item = next(
            (
                it
                for it in reversed(tier_list[-3:])
                if _norm_dedup_key(str(it.get("kind") or "note"), str(it.get("text") or "")) == norm_new
            ),
            None,
        )
        if duplicate_item is not None:
            duplicate_item["updatedAt"] = now
            if clean_title:
                duplicate_item["title"] = clean_title
            item = dict(duplicate_item)
            cascade_info = {"cascaded": 0, "dropped": 0}
        else:
            item = {
                "id": f"mm-{uuid.uuid4().hex[:8]}",
                "tier": target_tier,
                "kind": clean_kind,
                "title": clean_title,
                "text": clean_text,
                "chars": len(clean_text),
                "roomId": clean_room,
                "taskId": clean_task,
                "createdAt": now,
                "updatedAt": now,
                "movedAt": None,
                "fromTier": None,
                "demoteCount": 0,
            }
            tier_list.append(item)
            data["stats"]["totalAdded"] = int(data["stats"].get("totalAdded") or 0) + 1
            cascade_info = _cascade_overflow_unlocked(data, now=now)

        _save_unlocked(data, now=now, agent_id=norm_agent)
        snap = _snapshot_unlocked(data, agent_id=norm_agent)
        snap["addedItem"] = item
        snap["lastOperation"] = {"action": "add", "tier": target_tier, **cascade_info}

    if duplicate_item is None and (summarize if summarize is not None else clean_kind in AUTO_SUMMARY_KINDS):
        llm_src = str(raw_text_for_llm if raw_text_for_llm is not None else text or "").strip()
        _schedule_llm_refine(
            item["id"],
            llm_src,
            kind=clean_kind,
            title=clean_title,
            existing_text=clean_text,
            agent_id=norm_agent,
        )
    return snap


def record_orchestrator_turn_memory(
    *,
    user_text: str,
    reply_text: str,
    channel: str = "web",
    task_id: str = "",
    room_id: str = "__core__",
    agent_id: str | None = None,
    upward_report_to_master: bool = True,
) -> dict[str, Any] | None:
    """마스터 또는 전문가 에이전트 턴 결과를 맥락에 맞게 정제·병합하여 메모리에 저장한다."""
    reply_clean = str(reply_text or "").strip()
    if not reply_clean or reply_clean.startswith("⚠️"):
        return None

    resolved_agent = normalize_agent_id(agent_id or room_id)
    user_str = str(user_text or "").strip()
    ch = str(channel or "web").strip()

    def _maybe_upward_report(summary_text: str, title_str: str) -> None:
        """하위 전문가 에이전트의 중요 결과를 마스터 메모리 WARM 티어로 1줄 상향 보고한다."""
        if not upward_report_to_master or resolved_agent == "master":
            return
        tag = AGENT_DOMAIN_TAGS.get(resolved_agent, f"[{resolved_agent}]")
        upward_text = f"{tag} {summary_text}".strip()
        try:
            add_memory(
                upward_text,
                title=f"{tag} {title_str}".strip(),
                kind="turn",
                room_id=room_id,
                task_id=task_id,
                tier="warm",
                summarize=True,
                filter_noise=False,
                agent_id="master",
            )
        except Exception as exc:
            logger.debug("[master_memory] 마스터 상향 보고 생략: %s", exc)

    def _return_if_added(snap: dict[str, Any]) -> dict[str, Any] | None:
        if snap.get("addedItem") is None:
            return None
        added = snap["addedItem"]
        if ch == "task_report" or user_str.startswith(("[하위 작업 완료 보고]", "[하위 작업 중단/오류 보고]")):
            _maybe_upward_report(str(added.get("text") or ""), str(added.get("title") or ""))
        return snap

    # 1. 정기 점검 턴: 시스템 프롬프트 대신 핵심 이상 징후/조치 결과만 기록
    if ch == "master_patrol" or user_str.startswith("[마스터 에이전트 정기 점검"):
        a_summary = _condense_segments(_strip_boilerplate(reply_clean, kind="turn"), AUTO_SUMMARY_MAX_CHARS - 8)
        if not a_summary or ("이상 없음" in a_summary and len(a_summary) < 40):
            return None
        return _return_if_added(add_memory(
            f"[정기점검] {a_summary}",
            title="정기점검",
            kind="turn",
            room_id=room_id,
            task_id=task_id,
            summarize=True,
            agent_id=resolved_agent,
        ))

    # 1-2. 정기 스케줄 트리거 턴: 프롬프트 템플릿 제거 + 스케줄 실행 요약만 기록
    if ch == "schedule_trigger" or user_str.startswith("[정기 스케줄 실행 요청]"):
        desc_match = re.search(r"^- 설명:\s*(.+)$", user_str, re.MULTILINE)
        sched_desc = (desc_match.group(1).strip() if desc_match else "") or "정기 스케줄"
        a_body = _strip_boilerplate(reply_clean, kind="turn")
        a_condensed = _condense_segments(a_body, max(24, AUTO_SUMMARY_MAX_CHARS - len(sched_desc) - 8))
        return _return_if_added(add_memory(
            f"[스케줄:{sched_desc}] {a_condensed}" if a_condensed else f"[스케줄:{sched_desc}] 실행 완료",
            title=f"스케줄: {sched_desc[:32]}",
            kind="turn",
            room_id=room_id,
            task_id=task_id,
            summarize=True,
            agent_id=resolved_agent,
        ))

    # 2. 하위 작업 완료 보고 응답 턴: 보고 템플릿 제거 + 기존 notify 항목과 taskId 기준 병합
    if ch == "task_report" or user_str.startswith(("[하위 작업 완료 보고]", "[하위 작업 중단/오류 보고]")):
        r_title, extracted_rid, extracted_tid, _ = _strip_report_template(user_str)
        resolved_tid = str(task_id or extracted_tid or "").strip()
        resolved_rid = extracted_rid or ""
        tag = _format_id_tag("완료응답", resolved_rid, resolved_tid)
        a_body = _strip_boilerplate(reply_clean, kind="turn")
        a_condensed = _condense_segments(a_body, max(24, AUTO_SUMMARY_MAX_CHARS - len(tag) - 1))
        title_str = r_title or (f"보고: {resolved_tid}" if resolved_tid else "하위보고")
        return _return_if_added(add_memory(
            f"{tag} {a_condensed}" if a_condensed else tag,
            title=title_str,
            kind="turn",
            room_id=resolved_rid or room_id,
            task_id=resolved_tid,
            summarize=True,
            agent_id=resolved_agent,
        ))

    # 3. 일반 사용자 Q&A 턴
    q_summary = summarize_user_query(user_str, max_chars=44)
    delegated_rid, delegated_desc, has_extra_answer = _extract_delegation_info(reply_clean)

    # 직전 dispatch 항목과 동일한 방(또는 방금 생성된 위임 작업)에 대한 단순 위임 안내 턴이면 dispatch 항목에 병합해 중복 적재 방지
    if (delegated_rid or delegated_desc) and not has_extra_answer:
        now = _now()
        with _lock:
            data = _load_unlocked(resolved_agent)
            hot_items = data["tiers"]["hot"]
            recent_dispatches = [
                it
                for it in hot_items
                if it.get("kind") == "dispatch" and now - float(it.get("createdAt") or 0) <= 300
            ]
            matched_cand: dict[str, Any] | None = None
            if delegated_rid:
                for cand in reversed(recent_dispatches):
                    if cand.get("roomId") == delegated_rid:
                        matched_cand = cand
                        break
            elif recent_dispatches:
                desc_norm = _norm_plain(delegated_desc)
                for cand in reversed(recent_dispatches):
                    if now - float(cand.get("createdAt") or 0) > 120:
                        continue
                    title_norm = _norm_plain(str(cand.get("title") or ""))
                    if (title_norm and desc_norm and (title_norm in desc_norm or desc_norm in title_norm)):
                        matched_cand = cand
                        break
                if matched_cand is None and len(recent_dispatches) == 1 and (now - float(recent_dispatches[0].get("createdAt") or 0) <= 90):
                    matched_cand = recent_dispatches[0]

            if matched_cand is not None:
                if q_summary and q_summary not in str(matched_cand.get("text") or ""):
                    cand_limit = min(int(data["limits"]["hot"]["itemMaxChars"]), AUTO_SUMMARY_MAX_CHARS)
                    merged_txt = _merge_texts(
                        str(matched_cand.get("text") or ""),
                        f"요청: {q_summary}",
                        max_chars=cand_limit,
                    )
                    matched_cand["text"] = merged_txt
                    matched_cand["chars"] = len(merged_txt)
                matched_cand["updatedAt"] = now
                _save_unlocked(data, now=now, agent_id=resolved_agent)
                snap = _snapshot_unlocked(data, agent_id=resolved_agent)
                snap["addedItem"] = dict(matched_cand)
                snap["lastOperation"] = {"action": "add", "tier": "hot", "merged": True, "cascaded": 0, "dropped": 0}
                return snap

    a_clean = _strip_boilerplate(reply_clean, kind="turn")
    if (delegated_rid or delegated_desc) and not has_extra_answer:
        del_label = f"위임({delegated_rid})" if delegated_rid else "위임"
        a_summary = _condense_segments(f"{del_label} {delegated_desc}", 95)
    else:
        a_budget = max(40, AUTO_SUMMARY_MAX_CHARS - (len(q_summary) + 8 if q_summary else 0))
        a_summary = _condense_segments(a_clean, a_budget)

    turn_text = f"Q: {q_summary} → A: {a_summary}" if q_summary else a_summary
    snap = add_memory(
        turn_text,
        title=q_summary[:AUTO_SUMMARY_TITLE_CHARS] if q_summary else f"{AGENT_LABELS.get(resolved_agent, '에이전트')} 응답",
        kind="turn",
        room_id=room_id,
        task_id=task_id,
        summarize=True,
        raw_text_for_llm=reply_clean,
        agent_id=resolved_agent,
    )
    if snap.get("addedItem") is None:
        return None
    return snap


def demote_tier(from_tier: str = "hot", *, agent_id: str = "master") -> dict[str, Any]:
    """지정 계층의 모든 항목을 즉시 하위 계층으로 내리고 초과분을 연쇄 이동한다."""
    src_tier = str(from_tier or "hot").strip().lower()
    if src_tier not in TIERS:
        raise ValueError(f"지원하지 않는 메모리 계층입니다: {from_tier}")
    norm_agent = normalize_agent_id(agent_id)
    dst_tier = NEXT_TIER[src_tier]
    now = _now()

    with _lock:
        data = _load_unlocked(norm_agent)
        if dst_tier is None:
            snap = _snapshot_unlocked(data, agent_id=norm_agent)
            snap["lastOperation"] = {"action": "demote", "fromTier": src_tier, "movedCount": 0, "cascaded": 0, "dropped": 0}
            return snap

        src_items = data["tiers"][src_tier]
        moved_count = 0
        if src_items:
            dst_item_limit = int(data["limits"][dst_tier]["itemMaxChars"])
            while src_items:
                oldest = src_items.pop(0)
                demoted = _demote_item_payload(
                    oldest,
                    from_tier=src_tier,
                    to_tier=dst_tier,
                    item_max_chars=dst_item_limit,
                    now=now,
                )
                if demoted["text"]:
                    data["tiers"][dst_tier].append(demoted)
                moved_count += 1
            data["stats"]["cascadeCount"] = int(data["stats"].get("cascadeCount") or 0) + moved_count
            data["stats"]["lastCascadeAt"] = now

        cascade_info = _cascade_overflow_unlocked(data, now=now)
        _save_unlocked(data, now=now, agent_id=norm_agent)
        snap = _snapshot_unlocked(data, agent_id=norm_agent)
        snap["lastOperation"] = {
            "action": "demote",
            "fromTier": src_tier,
            "toTier": dst_tier,
            "movedCount": moved_count,
            "cascaded": cascade_info["cascaded"] + moved_count,
            "dropped": cascade_info["dropped"],
        }
        return snap


def promote_tier(from_tier: str = "warm", *, agent_id: str = "master") -> dict[str, Any]:
    """지정 계층의 모든 항목을 즉시 상위 계층으로 올리고 초과분을 연쇄 정리한다."""
    src_tier = str(from_tier or "warm").strip().lower()
    if src_tier not in TIERS:
        raise ValueError(f"지원하지 않는 메모리 계층입니다: {from_tier}")
    dst_tier = PREV_TIER[src_tier]
    if dst_tier is None:
        raise ValueError(f"{src_tier} 계층은 최상위 계층이므로 상위로 이동할 수 없습니다.")
    norm_agent = normalize_agent_id(agent_id)
    now = _now()
    with _lock:
        data = _load_unlocked(norm_agent)
        src_items = data["tiers"][src_tier]
        moved_count = 0
        if src_items:
            dst_item_limit = int(data["limits"][dst_tier]["itemMaxChars"])
            while src_items:
                item = src_items.pop(0)
                promoted = dict(item)
                clean_text = _smart_clip(promoted.get("text") or "", dst_item_limit)
                promoted["text"] = clean_text
                promoted["chars"] = len(clean_text)
                promoted["tier"] = dst_tier
                promoted["fromTier"] = src_tier
                promoted["movedAt"] = now
                promoted["updatedAt"] = now
                promoted["promoteCount"] = int(promoted.get("promoteCount") or 0) + 1
                if clean_text:
                    data["tiers"][dst_tier].append(promoted)
                    moved_count += 1
        cascade_info = _cascade_overflow_unlocked(data, now=now)
        _save_unlocked(data, now=now, agent_id=norm_agent)
        snap = _snapshot_unlocked(data, agent_id=norm_agent)
        snap["lastOperation"] = {
            "action": "promote",
            "fromTier": src_tier,
            "toTier": dst_tier,
            "movedCount": moved_count,
            "cascaded": cascade_info["cascaded"],
            "dropped": cascade_info["dropped"],
        }
        return snap


def move_memory_item(item_id: str, to_tier: str, *, agent_id: str = "master") -> dict[str, Any]:
    """특정 메모리 항목(item_id)을 원하는 대상 계층(to_tier)으로 자유롭게 이동한다."""
    target_id = str(item_id or "").strip()
    dst_tier = str(to_tier or "hot").strip().lower()
    if not target_id:
        raise ValueError("item_id 가 필요합니다.")
    if dst_tier not in TIERS:
        raise ValueError(f"지원하지 않는 메모리 계층입니다: {to_tier}")
    norm_agent = normalize_agent_id(agent_id)
    now = _now()
    with _lock:
        data = _load_unlocked(norm_agent)
        found_item: dict[str, Any] | None = None
        src_tier: str | None = None
        for t in TIERS:
            for idx, it in enumerate(data["tiers"][t]):
                if it.get("id") == target_id:
                    found_item = data["tiers"][t].pop(idx)
                    src_tier = t
                    break
            if found_item is not None:
                break
        if found_item is None or src_tier is None:
            raise ValueError(f"해당 메모리 항목을 찾을 수 없습니다: {item_id}")

        dst_item_limit = int(data["limits"][dst_tier]["itemMaxChars"])
        moved = dict(found_item)
        clipped = _smart_clip(moved.get("text") or "", dst_item_limit)
        moved["text"] = clipped
        moved["chars"] = len(clipped)
        moved["tier"] = dst_tier
        moved["fromTier"] = src_tier
        moved["movedAt"] = now
        moved["updatedAt"] = now
        tier_indices = {t: idx for idx, t in enumerate(TIERS)}
        if tier_indices[dst_tier] < tier_indices[src_tier]:
            moved["promoteCount"] = int(moved.get("promoteCount") or 0) + 1
        else:
            moved["demoteCount"] = int(moved.get("demoteCount") or 0) + 1

        if clipped:
            data["tiers"][dst_tier].append(moved)
        cascade_info = _cascade_overflow_unlocked(data, now=now)
        _save_unlocked(data, now=now, agent_id=norm_agent)
        snap = _snapshot_unlocked(data, agent_id=norm_agent)
        snap["movedItem"] = moved
        snap["lastOperation"] = {
            "action": "move",
            "itemId": target_id,
            "fromTier": src_tier,
            "toTier": dst_tier,
            **cascade_info,
        }
        return snap


def delete_memory_item(item_id: str, *, agent_id: str = "master") -> dict[str, Any]:
    """특정 메모리 항목(item_id)을 삭제한다."""
    target_id = str(item_id or "").strip()
    if not target_id:
        raise ValueError("item_id 가 필요합니다.")
    norm_agent = normalize_agent_id(agent_id)
    now = _now()
    with _lock:
        data = _load_unlocked(norm_agent)
        deleted = False
        src_tier = None
        for t in TIERS:
            for idx, it in enumerate(data["tiers"][t]):
                if it.get("id") == target_id:
                    data["tiers"][t].pop(idx)
                    deleted = True
                    src_tier = t
                    break
            if deleted:
                break
        if not deleted:
            raise ValueError(f"해당 메모리 항목을 찾을 수 없습니다: {item_id}")
        _save_unlocked(data, now=now, agent_id=norm_agent)
        snap = _snapshot_unlocked(data, agent_id=norm_agent)
        snap["lastOperation"] = {
            "action": "delete",
            "itemId": target_id,
            "fromTier": src_tier,
        }
        return snap


def compact_memory(*, agent_id: str = "master") -> dict[str, Any]:
    """기존 4계층 메모리의 비대한 항목을 일괄 요약 정제하고 중복 보고를 병합한다."""
    norm_agent = normalize_agent_id(agent_id)
    now = _now()
    with _lock:
        data = _load_unlocked(norm_agent)
        compact_info = _compact_and_dedup_unlocked(data, now=now, agent_id=norm_agent)
        cascade_info = _cascade_overflow_unlocked(data, now=now)
        _save_unlocked(data, now=now, agent_id=norm_agent)
        snap = _snapshot_unlocked(data, agent_id=norm_agent)
        snap["lastOperation"] = {
            "action": "compact",
            **compact_info,
            "cascaded": cascade_info["cascaded"],
            "dropped": cascade_info["dropped"],
        }
        return snap


def rebalance_memory(*, agent_id: str = "master") -> dict[str, Any]:
    """전체 4계층 메모리의 요약 압축·중복 병합·용량 한도 리밸런싱을 수행한다."""
    norm_agent = normalize_agent_id(agent_id)
    now = _now()
    with _lock:
        data = _load_unlocked(norm_agent)
        compact_info = _compact_and_dedup_unlocked(data, now=now, agent_id=norm_agent)
        cascade_info = _cascade_overflow_unlocked(data, now=now)
        _save_unlocked(data, now=now, agent_id=norm_agent)
        snap = _snapshot_unlocked(data, agent_id=norm_agent)
        snap["lastOperation"] = {
            "action": "rebalance",
            "purged": compact_info.get("purged", 0),
            "deduped": compact_info["merged"],
            "compacted": compact_info["compacted"],
            "cleaned": compact_info["cleaned"],
            "cascaded": cascade_info["cascaded"],
            "dropped": cascade_info["dropped"],
        }
        return snap


def _format_age_label(ts: float, now: float) -> str:
    diff = max(0, int(now - float(ts or now)))
    if diff < 3600:
        return f"{max(1, diff // 60)}분 전"
    if diff < 86400:
        return f"{diff // 3600}시간 전"
    return f"{diff // 86400}일 전"


async def _run_flash_low_rebalance_batch(
    candidates: list[dict[str, Any]],
    *,
    now: float,
) -> dict[str, dict[str, str]]:
    """flash low 모델 1회 호출로 후보 항목들의 핵심 요약 및 적정 계층(hot/warm/cold/freeze)을 판단한다."""
    if not candidates:
        return {}
    from agy_wrapper import run_agy_cli

    compact_payload = [
        {
            "id": it["id"],
            "tier": it["tier"],
            "age": _format_age_label(float(it.get("updatedAt") or it.get("createdAt") or now), now),
            "kind": it.get("kind") or "note",
            "title": str(it.get("title") or "")[:40],
            "text": str(it.get("text") or "")[:240],
            "needsSummary": not bool(it.get("llmSummarized")),
        }
        for it in candidates
    ]
    prompt = (
        "너는 마스터 에이전트 4계층 메모리(hot→warm→cold→freeze) 리밸런싱 관리자다.\n"
        "아래 항목 목록을 보고 각 항목의 (1) 보존 가치 및 적정 계층(`tier`: `hot|warm|cold|freeze|delete`)을 판단하고, "
        "(2) 유지할 항목 중 `needsSummary=true`이거나 원문이 지저분하면 핵심 팩트·결론·수치·파일명만 남긴 "
        "개행 없는 평문 1문장(최대 100자, 마크다운/이모지/ID/질문반복/상투어 금지)으로 `summary`를 작성하라.\n\n"
        "엄격한 삭제(`delete`) 기준 — 다음에 해당하면 무조건 `\"tier\": \"delete\"`로 분류하라:\n"
        "- 시스템 내부 개발·코드 수정·버그픽스 히스토리 (예: 함수/파일 수정, 렌더링 오류 수정, UI/버튼/슬라이더 개편, 단위 테스트 통과, 내부 리셋/재부착/유사도 로직, 스킬/프레임워크 설정 변경 완료 이력)\n"
        "- 무의미한 정기점검·스케줄 패스 로그 (예: 토큰 버닝 기준 미달 패스, 이상 없음, 메모리 리밸런싱 자체 실행 완료 보고)\n"
        "- 단순 인사, 일회성 확인, 다른 항목과 내용이 겹치는 중복 기억\n\n"
        "보존할 가치 있는 기억 및 계층 배치 기준:\n"
        "- hot (최대 8건): **현재 진행 중·미완결 사용자 작업**, **사용자의 선호·지시·규칙**, **최근 핵심 의사결정**\n"
        "- warm (최대 16건): 최근 완료된 **옵시디언 볼트 지식/개인 기록**(가계부, 건강, 진료, 보험, 차량, 주유, 카드, 주식, 일정 등) 중 단기 참조 맥락\n"
        "- cold (최대 32건): 완전히 종결된 과거 볼트 작업, 일회성 유용한 조회 결과\n"
        "- freeze (최대 100건): 오래되었으나 장기 보존 가치가 있는 개인 기록·아카이브\n\n"
        "반드시 JSON 배열만 출력하라. 형식:\n"
        '[{"id":"...","tier":"hot|warm|cold|freeze|delete","summary":"...(변경 불필요 또는 delete 시 빈 문자열)"}]\n\n'
        f"항목 목록:\n{json.dumps(compact_payload, ensure_ascii=False)}"
    )
    extra = ["--model", LLM_SUMMARY_MODEL, "--effort", LLM_SUMMARY_EFFORT, "--sandbox"]
    res = await run_agy_cli(prompt, timeout_sec=30, extra_args=extra)
    if res.exit_status != "NORMAL" or not (res.stdout or "").strip():
        return {}

    raw_out = res.stdout.strip()
    m = re.search(r"\[[\s\S]*\]", raw_out)
    if not m:
        return {}
    try:
        parsed = json.loads(m.group(0))
    except Exception:
        return {}
    if not isinstance(parsed, list):
        return {}

    decisions: dict[str, dict[str, str]] = {}
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        eid = str(entry.get("id") or "").strip()
        if not eid:
            continue
        new_tier = str(entry.get("tier") or "").strip().lower()
        action = str(entry.get("action") or "").strip().lower()
        if action in ("delete", "remove") or new_tier in ("delete", "remove"):
            resolved_tier = "delete"
        elif new_tier in TIERS:
            resolved_tier = new_tier
        else:
            resolved_tier = ""
        summary = str(entry.get("summary") or "").strip()
        decisions[eid] = {
            "tier": resolved_tier,
            "summary": summary,
        }
    return decisions


async def rebalance_memory_with_llm(*, agent_id: str = "master") -> dict[str, Any]:
    """flash low 요약 + 계층 승격/강등 판단을 수행한 뒤 중복 병합 및 용량 리밸런싱을 완료한다."""
    norm_agent = normalize_agent_id(agent_id)
    now = _now()
    candidates: list[dict[str, Any]] = []
    if LLM_SUMMARY_ENABLED:
        with _lock:
            data = _load_unlocked(norm_agent)
            # hot, warm, cold 전체 + freeze 최신 12건을 합쳐 최대 60건 평가
            pool: list[dict[str, Any]] = []
            for t in ("hot", "warm", "cold"):
                pool.extend(data["tiers"][t])
            pool.extend(data["tiers"]["freeze"][-12:])
            candidates = [dict(it) for it in pool[:60]]

    decisions: dict[str, dict[str, str]] = {}
    if candidates:
        try:
            decisions = await _run_flash_low_rebalance_batch(candidates, now=now)
        except Exception as exc:
            logger.debug("[master_memory] LLM 리밸런싱 배치 생략: %s", exc)
            decisions = {}

    tier_rank = {t: idx for idx, t in enumerate(TIERS)}
    summarized_cnt = 0
    promoted_cnt = 0
    demoted_cnt = 0
    purged_cnt = 0

    with _lock:
        data = _load_unlocked(norm_agent)
        if decisions:
            id_to_item: dict[str, tuple[str, dict[str, Any]]] = {}
            for t in TIERS:
                for it in data["tiers"][t]:
                    if it.get("id"):
                        id_to_item[it["id"]] = (t, it)

            for eid, dec in decisions.items():
                found = id_to_item.get(eid)
                if not found:
                    continue
                cur_tier, it = found
                target_tier = dec.get("tier") or ""
                if target_tier == "delete":
                    if it in data["tiers"][cur_tier]:
                        data["tiers"][cur_tier].remove(it)
                        purged_cnt += 1
                    continue

                new_sum = dec.get("summary") or ""
                if new_sum:
                    cleaned = clean_memory_markdown(new_sum)
                    cleaned = " ".join(cleaned.split()).strip(" \"'·-:;")
                    if len(cleaned) >= 6:
                        short_tag, _ = _compact_prompt_tag(str(it.get("text") or ""))
                        final_text = (
                            f"{short_tag} {cleaned}".strip()
                            if (short_tag and not cleaned.startswith("["))
                            else cleaned
                        )
                        final_text = _smart_clip(final_text, AUTO_SUMMARY_MAX_CHARS)
                        if final_text and final_text != it.get("text"):
                            it["text"] = final_text
                            it["chars"] = len(final_text)
                            it["llmSummarized"] = True
                            summarized_cnt += 1

                if target_tier in TIERS and target_tier != cur_tier:
                    if it in data["tiers"][cur_tier]:
                        data["tiers"][cur_tier].remove(it)
                        it["fromTier"] = cur_tier
                        it["movedAt"] = now
                        it["tier"] = target_tier
                        data["tiers"][target_tier].append(it)
                        if tier_rank[target_tier] < tier_rank[cur_tier]:
                            promoted_cnt += 1
                        else:
                            demoted_cnt += 1

        compact_info = _compact_and_dedup_unlocked(data, now=now, agent_id=norm_agent)
        cascade_info = _cascade_overflow_unlocked(data, now=now)
        _save_unlocked(data, now=now, agent_id=norm_agent)
        snap = _snapshot_unlocked(data, agent_id=norm_agent)
        snap["lastOperation"] = {
            "action": "rebalance",
            "purged": purged_cnt + compact_info.get("purged", 0),
            "summarized": summarized_cnt,
            "promoted": promoted_cnt,
            "demoted": demoted_cnt,
            "deduped": compact_info["merged"],
            "compacted": compact_info["compacted"],
            "cleaned": compact_info["cleaned"],
            "cascaded": cascade_info["cascaded"],
            "dropped": cascade_info["dropped"],
        }
        return snap


def apply_memory_batch(operations: list[dict[str, Any]], *, agent_id: str = "master") -> dict[str, Any]:
    """마스터 또는 전문가 에이전트가 전달한 delete/update/move 배치 작업을 원자적으로 적용한다."""
    if not isinstance(operations, list):
        raise ValueError("operations 리스트가 필요합니다.")
    norm_agent = normalize_agent_id(agent_id)
    now = _now()
    deleted_cnt = 0
    updated_cnt = 0
    moved_cnt = 0

    with _lock:
        data = _load_unlocked(norm_agent)

        def _locate(target_id: str) -> tuple[str, int, dict[str, Any]] | None:
            for t in TIERS:
                for idx, it in enumerate(data["tiers"][t]):
                    if it.get("id") == target_id:
                        return t, idx, it
            return None

        for op in operations:
            if not isinstance(op, dict):
                continue
            act = str(op.get("action") or "").strip().lower()
            eid = str(op.get("id") or op.get("itemId") or "").strip()
            if not eid:
                continue
            loc = _locate(eid)
            if loc is None:
                continue
            cur_tier, idx, it = loc

            if act in ("delete", "remove"):
                data["tiers"][cur_tier].pop(idx)
                deleted_cnt += 1
                continue

            if act == "update":
                new_text = op.get("text")
                if new_text is not None:
                    cleaned = _smart_clip(clean_memory_markdown(str(new_text)), AUTO_SUMMARY_MAX_CHARS)
                    if cleaned:
                        it["text"] = cleaned
                        it["chars"] = len(cleaned)
                        it["llmSummarized"] = True
                new_title = op.get("title")
                if new_title is not None and str(new_title).strip():
                    it["title"] = _clip_text(str(new_title).strip(), AUTO_SUMMARY_TITLE_CHARS)
                it["updatedAt"] = now
                updated_cnt += 1

            target_tier = str(op.get("tier") or op.get("toTier") or "").strip().lower()
            if act in ("move", "update") and target_tier in TIERS and target_tier != cur_tier:
                data["tiers"][cur_tier].pop(idx)
                it["fromTier"] = cur_tier
                it["movedAt"] = now
                it["tier"] = target_tier
                data["tiers"][target_tier].append(it)
                moved_cnt += 1

        cascade_info = _cascade_overflow_unlocked(data, now=now)
        _save_unlocked(data, now=now, agent_id=norm_agent)
        snap = _snapshot_unlocked(data, agent_id=norm_agent)
        snap["lastOperation"] = {
            "action": "batch",
            "deleted": deleted_cnt,
            "updated": updated_cnt,
            "moved": moved_cnt,
            "cascaded": cascade_info["cascaded"],
            "dropped": cascade_info["dropped"],
        }
        return snap


def clear_memory(tier: str | None = None, *, agent_id: str = "master") -> dict[str, Any]:
    """특정 계층 또는 4계층 메모리 전체를 초기화한다."""
    raw_tier = str(tier or "all").strip().lower()
    norm_agent = normalize_agent_id(agent_id)
    now = _now()
    with _lock:
        data = _load_unlocked(norm_agent)
        removed_count = 0
        if raw_tier in ("", "all"):
            for t in TIERS:
                removed_count += len(data["tiers"][t])
                data["tiers"][t] = []
            data["stats"]["lastResetAt"] = now
            _prompt_checkpoints.clear()
        elif raw_tier in TIERS:
            removed_count = len(data["tiers"][raw_tier])
            data["tiers"][raw_tier] = []
            data["stats"]["lastResetAt"] = now
            if raw_tier == "hot":
                _prompt_checkpoints.clear()
        else:
            raise ValueError(f"지원하지 않는 메모리 계층입니다: {tier}")

        _save_unlocked(data, now=now, agent_id=norm_agent)
        snap = _snapshot_unlocked(data, agent_id=norm_agent)
        snap["lastOperation"] = {"action": "clear", "tier": raw_tier or "all", "removedCount": removed_count}
        return snap


def update_tier_limits(limits_patch: dict[str, Any], *, agent_id: str = "master") -> dict[str, Any]:
    """계층별 길이 제한을 변경하고 즉시 하위 캐시 캐스케이드를 재실행한다."""
    if not isinstance(limits_patch, dict):
        raise ValueError("limits 객체가 필요합니다.")
    norm_agent = normalize_agent_id(agent_id)
    now = _now()
    with _lock:
        data = _load_unlocked(norm_agent)
        for tier in TIERS:
            patch_vals = limits_patch.get(tier)
            if not isinstance(patch_vals, dict):
                continue
            for key in ("maxChars", "maxItems", "itemMaxChars"):
                if key in patch_vals and patch_vals[key] is not None:
                    val = int(patch_vals[key])
                    if val <= 0:
                        raise ValueError(f"{tier}.{key} 는 1 이상이어야 합니다.")
                    data["limits"][tier][key] = val
        cascade_info = _cascade_overflow_unlocked(data, now=now)
        _save_unlocked(data, now=now, agent_id=norm_agent)
        snap = _snapshot_unlocked(data, agent_id=norm_agent)
        snap["lastOperation"] = {"action": "set_limits", **cascade_info}
        return snap


def get_memory_snapshot(*, tier: str | None = None, limit_per_tier: int = 50, agent_id: str = "master") -> dict[str, Any]:
    """현재 4계층 메모리 스냅샷(각 계층 사용량, 항목 목록, 통계)을 반환한다."""
    target_tier = str(tier).strip().lower() if tier and str(tier).strip().lower() != "all" else None
    if target_tier is not None and target_tier not in TIERS:
        raise ValueError(f"지원하지 않는 메모리 계층입니다: {tier}")
    norm_agent = normalize_agent_id(agent_id)
    with _lock:
        data = _load_unlocked(norm_agent)
        return _snapshot_unlocked(data, tier=target_tier, limit_per_tier=limit_per_tier, agent_id=norm_agent)


_RE_PROMPT_TAG = re.compile(
    r"^\[(?:"
    r"보고:([^\]|]+)(?:\|[^\]]*)?"
    r"|완료응답(?:\|[^\]]*)?"
    r"|위임:[^\]|]+(?:\|[^\]]*)?"
    r"|정기점검"
    r"|[A-Z_]+:([^\]|]+)(?:\|[^\]]*)?"
    r")\]\s*"
)
_RE_PROMPT_NOISE_ID = re.compile(
    r"\(\s*(?:room-\d+-[0-9a-f]+|mt-[0-9a-f]{6,12})\s*\)"
    r"|\b(?:room-\d+-[0-9a-f]+|mt-[0-9a-f]{6,12})\b"
)


def _compact_prompt_tag(text: str) -> tuple[str, str]:
    """프롬프트 주입용으로 긴 메타태그(`[보고:성공|room-...|mt-...]`)를 짧은 상태 태그로 축약한다."""
    m = _RE_PROMPT_TAG.match(text)
    if not m:
        return "", text
    raw_tag = m.group(0)
    rest = text[m.end() :].strip()
    if m.group(1):
        lbl = m.group(1).strip()
        return f"[{lbl}]", rest
    if raw_tag.startswith("[완료응답"):
        return "[완료]", rest
    if raw_tag.startswith("[위임:"):
        return "[위임]", rest
    if raw_tag.startswith("[정기점검"):
        return "[점검]", rest
    if m.group(2):
        return f"[{m.group(2).strip()}]", rest
    return "", rest


def _format_prompt_memory_line(
    item: dict[str, Any],
    *,
    max_text_chars: int,
    max_title_chars: int = 28,
) -> str:
    """메모리 항목 1건을 중복·내부ID·개행 없는 컴팩트 1줄 프롬프트 라인으로 포맷한다."""
    raw_title = " ".join(str(item.get("title") or "").split())
    raw_text = " ".join(str(item.get("text") or "").split())
    if not raw_text:
        return ""

    short_tag, body = _compact_prompt_tag(raw_text)

    # Q: ... → A: ... 형태는 title과 Q가 중복되므로 A 본문만 추출한다.
    qa_m = re.match(r"^Q:\s*(.+?)\s*(?:→|->)\s*A:\s*(.+)$", body)
    if qa_m:
        if not raw_title:
            raw_title = qa_m.group(1).strip()
        body = qa_m.group(2).strip()

    # 본문 내 남아있는 긴 room-/mt- ID 및 장식 이모지 제거
    body = _RE_PROMPT_NOISE_ID.sub("", body)
    body = _RE_EMOJI_DECOR.sub(" ", body)
    body = re.sub(r"\s{2,}", " ", body).strip(" ·,;:-")

    # 저신호 세그먼트(제안형 꼬리 질문 등) 필터링
    if " · " in body:
        kept_segs = [
            seg.strip()
            for seg in body.split(" · ")
            if seg.strip() and not _RE_LOW_SIGNAL_SEGMENT.match(seg.strip())
        ]
        if kept_segs:
            body = " · ".join(kept_segs)

    short_title = _smart_clip(raw_title, max_title_chars) if raw_title else ""
    if short_title and body.startswith(short_title):
        trimmed = body[len(short_title) :].lstrip(" ·,:—-")
        if trimmed:
            body = trimmed

    clipped_body = _smart_clip(body or raw_text, max_text_chars)
    title_part = f"[{short_title}] " if short_title else ""
    tag_part = f"{short_tag} " if short_tag else ""
    return f"  - {title_part}{tag_part}{clipped_body}".rstrip()


def _item_fingerprint(item: dict[str, Any]) -> tuple[float, str]:
    return (float(item.get("updatedAt") or 0), str(item.get("text") or ""))


def commit_memory_prompt_checkpoint(session_key: str | None, *, max_hot: int = 6, agent_id: str = "master") -> None:
    """해당 세션에 현재 HOT 컨텍스트가 주입·반영되었음을 기록해 다음 턴부터 변경분만 주입하게 한다."""
    skey = str(session_key or "").strip()
    if not skey:
        return
    norm_agent = normalize_agent_id(agent_id)
    with _lock:
        data = _load_unlocked(norm_agent)
        hot_items = data["tiers"]["hot"][-max(0, int(max_hot)) :]
        snap = {
            str(it.get("id") or ""): _item_fingerprint(it)
            for it in hot_items
            if it.get("id")
        }
        _prompt_checkpoints.pop(skey, None)
        _prompt_checkpoints[skey] = snap
        while len(_prompt_checkpoints) > _PROMPT_CHECKPOINT_MAX_SESSIONS:
            oldest_key = next(iter(_prompt_checkpoints))
            _prompt_checkpoints.pop(oldest_key, None)


def build_memory_prompt_context(
    *,
    max_hot: int = 6,
    max_warm: int = 3,
    session_key: str | None = None,
    agent_id: str = "master",
) -> str:
    """마스터 또는 전문가 에이전트 턴 프롬프트에 주입할 컴팩트 4계층 메모리 컨텍스트를 생성한다."""
    skey = str(session_key or "").strip()
    norm_agent = normalize_agent_id(agent_id)
    with _lock:
        data = _load_unlocked(norm_agent)
        seen_hot = _prompt_checkpoints.get(skey) if skey else None

    tiers = data["tiers"]
    hot_items = tiers["hot"][-max(0, int(max_hot)) :]
    warm_items = tiers["warm"][-max(0, int(max_warm)) :]
    cold_count = len(tiers["cold"])
    freeze_count = len(tiers["freeze"])

    if not any(tiers[t] for t in TIERS):
        return ""

    label = AGENT_LABELS.get(norm_agent, "에이전트")

    # 이미 같은 세션에 초기 컨텍스트를 주입한 적이 있으면 HOT 변경분만 주입 (변경 없으면 빈 문자열)
    if seen_hot is not None:
        changed_hot = [
            it
            for it in hot_items
            if seen_hot.get(str(it.get("id") or "")) != _item_fingerprint(it)
        ]
        if not changed_hot:
            return ""
        delta_lines = [f"[{label} 메모리 갱신 (HOT 변경분)]"]
        for it in changed_hot:
            line = _format_prompt_memory_line(it, max_text_chars=115, max_title_chars=28)
            if line:
                delta_lines.append(line)
        return "\n".join(delta_lines) if len(delta_lines) > 1 else ""

    lines = [
        f"[{label} 4계층 메모리]",
        f"• 현황: HOT {len(tiers['hot'])} | WARM {len(tiers['warm'])} | COLD {cold_count} | FREEZE {freeze_count}",
    ]
    if hot_items:
        lines.append("• HOT (즉각 활성 컨텍스트):")
        for it in hot_items:
            line = _format_prompt_memory_line(it, max_text_chars=115, max_title_chars=28)
            if line:
                lines.append(line)
    if warm_items:
        lines.append("• WARM (최근 하위 이동 요약):")
        for it in warm_items:
            line = _format_prompt_memory_line(it, max_text_chars=75, max_title_chars=24)
            if line:
                lines.append(line)
    if cold_count or freeze_count:
        lines.append(f"• 장기(`cold`/`freeze`) 조회: `session-ctl memory --agent {norm_agent} --tier cold` 또는 `--tier freeze`")
    return "\n".join(lines)
