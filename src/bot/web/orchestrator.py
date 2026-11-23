"""web_orchestrator — 총괄 에이전트(Core Orchestrator) 및 마스터 슈퍼바이저 세션 제어, 지시 디스패치 및 REST API 모듈."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import threading
import time
from typing import Any
import uuid

from aiohttp import web

from bot.engine.agy_wrapper import cap_summary_text
import bot.core.chat_db as chat_db
from bot.engine.agent_engine import clear_session, get_session
from bot.core.bot_logger import logger, vault_log
from bot.core.json_store import read_json, write_json_atomic
from bot.master.master_supervisor import CORE_ROOM_ID, MASTER_PATROL_CHANNEL, MasterSupervisor, build_graph as build_master_graph
import bot.core.agent_configs as agent_configs
import bot.master.master_memory as master_memory
import bot.master.master_tasks as master_tasks
from bot.core.paths import MASTER_STATE_FILE, WORKDIR, child_default_workdir
from bot.web.runtime_state import _get_mod_attr, get_master_supervisor

AGY_BRAIN_DIR = os.path.expanduser("~/.gemini/antigravity/brain")
CORE_ORCHESTRATOR_ROOM_ID = "__core__"
CORE_ORCHESTRATOR_TITLE = "마스터 에이전트"
EXPERT_GENERAL_ROOM_ID = "__expert_general__"
EXPERT_GENERAL_TITLE = "일반 대화"
EXPERT_VAULT_ROOM_ID = "__expert_vault__"
EXPERT_VAULT_TITLE = "세컨브레인 에이전트"
EXPERT_CODING_ROOM_ID = "__expert_coding__"
EXPERT_CODING_TITLE = "봇개발 에이전트"
ORCHESTRATOR_ROOM_IDS = (
    CORE_ORCHESTRATOR_ROOM_ID,
    EXPERT_GENERAL_ROOM_ID,
    EXPERT_VAULT_ROOM_ID,
    EXPERT_CODING_ROOM_ID,
)
_master_state_lock = threading.Lock()


def _all_orchestrator_room_ids() -> tuple[str, ...]:
    try:
        return agent_configs.get_orchestrator_room_ids()
    except Exception:
        return ORCHESTRATOR_ROOM_IDS


def _is_orchestrator_room_id(room_id: str | None) -> bool:
    if not room_id:
        return False
    clean = str(room_id).strip()
    return clean in ORCHESTRATOR_ROOM_IDS or clean in _all_orchestrator_room_ids()



class _WebServerSupervisorHost:
    """MasterSupervisor 와 web_server 내부 상태를 잇는 어댑터."""

    def snapshot(self) -> dict[str, Any]:
        from bot.web.rooms import _load_chat_rooms_index
        from bot.web.execution import build_active_runs_payload
        load_index_fn = _get_mod_attr("_load_chat_rooms_index", _load_chat_rooms_index)
        build_payload_fn = _get_mod_attr("build_active_runs_payload", build_active_runs_payload)
        return {
            "rooms": load_index_fn(),
            "liveRuns": build_payload_fn(),
        }

    def core_busy(self) -> bool:
        from bot.web.execution import _active_watch_tasks, _active_agent_pids, _room_queues
        task = _active_watch_tasks.get(CORE_ORCHESTRATOR_ROOM_ID)
        is_running = (task is not None and not task.done()) or (CORE_ORCHESTRATOR_ROOM_ID in _active_agent_pids)
        q = _room_queues.get(CORE_ORCHESTRATOR_ROOM_ID)
        has_queue = (q is not None and not q.empty())
        return is_running or has_queue

    async def enqueue_patrol(self, prompt: str) -> asyncio.Future:
        from bot.web.execution import enqueue_room_turn, _room_session_ids
        enqueue_fn = _get_mod_attr("enqueue_room_turn", enqueue_room_turn)
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        await enqueue_fn(
            room_id=CORE_ORCHESTRATOR_ROOM_ID,
            user_text=prompt,
            is_voice_mode=False,
            client_session_id=_room_session_ids.get(CORE_ORCHESTRATOR_ROOM_ID) or get_session(ns="core_"),
            is_new_session=False,
            channel=MASTER_PATROL_CHANNEL,
            result_future=fut,
        )
        return fut

    async def broadcast(self, message: dict) -> None:
        from bot.web.execution import broadcast_ws_json
        broadcast_fn = _get_mod_attr("broadcast_ws_json", broadcast_ws_json)
        await broadcast_fn(message)

    async def notify_task(self, task: dict) -> None:
        from bot.web.execution import _notify_master_agent
        notify_fn = _get_mod_attr("_notify_master_agent", _notify_master_agent)
        ok = task.get("status") == "done"
        text = (task.get("resultPreview") if ok else task.get("error")) or task.get("status") or ""
        await notify_fn(str(task.get("roomId") or ""), task["id"], str(text), is_success=ok)

    def latest_reply(self, room_id: str | None, since_ts: float) -> str | None:
        if not room_id:
            return None
        current_chat_db = _get_mod_attr("chat_db", chat_db)
        try:
            msgs = current_chat_db.get_messages(room_id, limit=5)
            for m in reversed(msgs):
                if m.get("role") == "assistant" and m.get("text"):
                    return str(m["text"])
        except Exception:
            pass
        return None


def _format_updated_at(ts: Any) -> str:
    from bot.web.rooms import _normalize_room_ts
    norm = _normalize_room_ts(ts)
    if norm <= 0:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(norm))


def _is_room_currently_running(room_id: str, session_id: str | None = None) -> tuple[bool, int, str]:
    """해당 방(또는 세션 ID)이 현재 실행 중인지, 대기열 수, 실시간 피드 문자열을 반환한다."""
    from bot.web.execution import _active_watch_tasks, _active_agent_pids, _room_queues, _room_live_feeds
    keys = [k for k in (room_id, session_id) if k]
    for k in keys:
        t = _active_watch_tasks.get(k)
        q = _room_queues.get(k)
        q_size = q.qsize() if q is not None else 0
        is_run = (t is not None and not t.done()) or (k in _active_agent_pids)
        if is_run or q_size > 0:
            return is_run, q_size, _room_live_feeds.get(k, "실행 중…")
    return False, 0, ""


_TOPIC_STOPWORDS = frozenset({
    "새", "신규", "대화", "대화방", "채팅", "채팅방", "작업", "작업방", "방", "세션",
    "에이전트", "하위", "마스터", "슬레이브", "테스트", "test", "샘플", "sample",
    "room", "session", "agent", "질문", "요청", "지시", "확인", "정리", "조사",
    "분석", "점검", "작성", "보고", "보고서", "가이드", "요약", "브리핑", "안내",
    "목록", "현황", "내역", "내용", "결과", "상태", "관련", "대한", "대해서", "통한",
    "위한", "대해", "최근", "현재", "전체", "주요", "상세", "간략히", "빠르게",
    "해줘", "해주세요", "알려줘", "알려주세요", "보여줘", "찾아줘", "만들어줘",
    "수정", "추가", "삭제", "업데이트", "개선", "적용", "검증", "진행", "실행",
    "완료", "처리", "방법", "기준", "규칙", "문제", "해결", "다시",
})

_GENERIC_SINGLE_TOKENS = frozenset({
    "웹", "ui", "ux", "앱", "봇", "코드", "버그", "기능", "설정", "화면",
    "노트", "볼트", "옵시디언", "문서", "파일", "시스템", "서버", "오류", "에러",
    "패널", "버튼", "체크리스트",
})

_KO_MULTI_SUFFIXES = (
    "해주세요", "알려주세요", "확인해줘", "정리해줘", "해결해줘",
    "으로는", "에서는", "에서", "에게", "한테", "으로", "부터", "까지",
    "처럼", "보다", "마다", "이랑", "하고", "에는", "에도", "해줘",
    "알려줘", "입니다", "합니다",
)

_SIMILAR_ROOM_THRESHOLD = 0.68


def _strip_ko_suffix(token: str) -> str:
    """한국어 조사·어미를 제거하되, 받침 조건과 최소 길이를 검사해 원형 훼손을 막는다."""
    for suffix in _KO_MULTI_SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 2:
            return token[: -len(suffix)]
    if len(token) < 3:
        return token
    last = token[-1]
    prev = token[-2]
    if "가" <= prev <= "힣":
        jong = (ord(prev) - 0xAC00) % 28
        if last in ("은", "을") and jong != 0:
            return token[:-1]
        if last in ("는", "가", "를") and jong == 0:
            return token[:-1]
        if last == "로" and jong in (0, 8):
            return token[:-1]
        if last in ("에", "의"):
            return token[:-1]
    return token


def _extract_ordered_tokens(text: str) -> list[str]:
    """텍스트에서 불용어와 조사를 제거한 핵심 주제 토큰 리스트(순서 유지)를 추출한다."""
    if not text:
        return []
    raw_matches = re.findall(r"[가-힣a-z0-9]+", str(text).lower())
    expanded: list[str] = []
    for tok in raw_matches:
        m = re.match(r"^([a-z0-9]{2,})([가-힣]{2,})$", tok)
        if m:
            expanded.append(m.group(1))
            expanded.append(m.group(2))
        else:
            expanded.append(tok)

    tokens: list[str] = []
    for raw in expanded:
        if raw in _TOPIC_STOPWORDS:
            continue
        stemmed = _strip_ko_suffix(raw)
        if stemmed in _TOPIC_STOPWORDS or len(stemmed) < 2:
            continue
        if stemmed.isdigit() and len(stemmed) < 4:
            continue
        tokens.append(stemmed)
    return tokens


def _extract_char_bigrams(tokens: set[str], *, exclude_generic: bool = True) -> set[str]:
    """한국어 복합어 매칭을 위해 토큰에서 2글자 바이그램 집합을 생성한다."""
    bigrams: set[str] = set()
    for tok in tokens:
        if exclude_generic and tok in _GENERIC_SINGLE_TOKENS:
            continue
        if len(tok) == 2:
            bigrams.add(tok)
        elif len(tok) >= 3:
            for idx in range(len(tok) - 1):
                bigrams.add(tok[idx : idx + 2])
    return bigrams


def _compute_set_similarity(
    set_a: set[str],
    set_b: set[str],
    bigrams_a: set[str],
    bigrams_b: set[str],
) -> float:
    """토큰 Overlap·Jaccard와 문자 바이그램 유사도를 결합해 띄어쓰기 변형에도 강건한 유사도를 반환한다."""
    if not set_a or not set_b:
        return 0.0
    common = set_a & set_b
    union = set_a | set_b
    overlap = len(common) / min(len(set_a), len(set_b))
    jaccard = len(common) / len(union) if union else 0.0
    if bigrams_a and bigrams_b:
        common_bg = bigrams_a & bigrams_b
        bg_overlap = len(common_bg) / min(len(bigrams_a), len(bigrams_b))
        bg_dice = 2.0 * len(common_bg) / (len(bigrams_a) + len(bigrams_b))
        bigram_sim = 0.5 * bg_overlap + 0.5 * bg_dice
    else:
        bigram_sim = 0.0
    token_sim = 0.65 * overlap + 0.35 * jaccard
    return max(0.55 * token_sim + 0.45 * bigram_sim, 0.85 * bigram_sim)


def _score_query_history_similarity(
    req_all_tokens: set[str],
    req_all_bg: set[str],
    room_summary: dict | None,
) -> float:
    """요청 토큰 집합과 채팅방 요약본(`queries`·`summary`) 간의 최대 유사도를 계산한다."""
    if not isinstance(room_summary, dict) or not req_all_tokens:
        return 0.0
    history_texts: list[str] = [str(q) for q in (room_summary.get("queries") or []) if q]
    if room_summary.get("summary"):
        history_texts.append(str(room_summary["summary"]))

    best_q_sim = 0.0
    for hist in history_texts:
        hist_tokens = set(_extract_ordered_tokens(hist))
        if not hist_tokens:
            continue
        hist_bg = _extract_char_bigrams(hist_tokens)
        common_q = (req_all_tokens & hist_tokens) - _GENERIC_SINGLE_TOKENS
        common_q_bg = req_all_bg & hist_bg
        if len(common_q) < 2 and len(common_q_bg) < 2:
            continue
        q_sim = _compute_set_similarity(req_all_tokens, hist_tokens, req_all_bg, hist_bg)
        if q_sim > best_q_sim:
            best_q_sim = q_sim
    return best_q_sim


def _score_room_similarity(
    req_title: str,
    req_message: str,
    room: dict,
    room_tasks: list[dict] | None = None,
    room_summary: dict | None = None,
) -> float:
    """요청(제목+메시지)과 기존 채팅방(제목+프리뷰+요약본+작업이력) 간의 주제 유사도(0.0~1.0)를 계산한다."""
    req_title_list = _extract_ordered_tokens(req_title)
    room_title_raw = str(room.get("title") or "")
    room_title_list = _extract_ordered_tokens(room_title_raw)

    req_title_tokens = set(req_title_list)
    room_title_tokens = set(room_title_list)

    req_all_tokens = req_title_tokens | set(_extract_ordered_tokens(str(req_message or "")[:300]))
    room_context_parts = [room_title_raw, str(room.get("preview") or "")[:240]]
    if isinstance(room_summary, dict):
        if room_summary.get("title"):
            room_context_parts.append(str(room_summary["title"]))
        if room_summary.get("summary"):
            room_context_parts.append(str(room_summary["summary"]))
        if room_summary.get("lastResult"):
            room_context_parts.append(str(room_summary["lastResult"])[:240])
        if room_summary.get("lastNote"):
            room_context_parts.append(str(room_summary["lastNote"])[:160])
        for q in room_summary.get("queries") or []:
            if q:
                room_context_parts.append(str(q))
    for t in (room_tasks or [])[:3]:
        if isinstance(t, dict):
            room_context_parts.append(str(t.get("title") or ""))
            room_context_parts.append(str(t.get("instruction") or "")[:200])
    room_all_tokens = set(_extract_ordered_tokens(" ".join(room_context_parts)))

    if not req_all_tokens or not room_all_tokens:
        return 0.0

    common_title = req_title_tokens & room_title_tokens
    common_all = req_all_tokens & room_all_tokens
    non_generic_title = common_title - _GENERIC_SINGLE_TOKENS
    non_generic_all = common_all - _GENERIC_SINGLE_TOKENS

    req_title_bg = _extract_char_bigrams(req_title_tokens)
    room_title_bg = _extract_char_bigrams(room_title_tokens)
    common_title_bg = req_title_bg & room_title_bg

    req_all_bg = _extract_char_bigrams(req_all_tokens)
    room_all_bg = _extract_char_bigrams(room_all_tokens)
    query_history_sim = _score_query_history_similarity(req_all_tokens, req_all_bg, room_summary)

    # 오탐(False Positive) 방지 가드: 범용 단어 또는 2글자 단어 1개만 겹치는 경우 차단
    if len(non_generic_all) < 2 and len(common_title_bg) < 2:
        return 0.0
    if (
        req_title_tokens
        and room_title_tokens
        and not non_generic_title
        and len(common_title_bg) < 2
        and query_history_sim < 0.58
    ):
        if len(non_generic_all) < 3:
            return 0.0

    norm_req_title = " ".join(req_title_list)
    norm_room_title = " ".join(room_title_list)
    compact_req_title = "".join(req_title_list)
    compact_room_title = "".join(room_title_list)
    has_title_substance = len(non_generic_title) >= 2 or len(common_title_bg) >= 2
    if norm_req_title and norm_room_title:
        if norm_req_title == norm_room_title and (non_generic_title or len(common_title_bg) >= 2):
            return 1.0
        if (
            len(compact_req_title) >= 4
            and len(compact_room_title) >= 4
            and (compact_req_title in compact_room_title or compact_room_title in compact_req_title)
            and has_title_substance
        ):
            return 0.85

    title_sim = _compute_set_similarity(
        req_title_tokens, room_title_tokens, req_title_bg, room_title_bg
    )
    context_sim = _compute_set_similarity(
        req_all_tokens, room_all_tokens, req_all_bg, room_all_bg
    )

    if not req_title_tokens or not room_title_tokens:
        base_score = context_sim
    else:
        blended = 0.55 * title_sim + 0.45 * context_sim
        if title_sim >= 0.62:
            base_score = max(title_sim, blended)
        else:
            base_score = max(blended, 0.35 * title_sim + 0.65 * context_sim)

    return min(1.0, max(base_score, query_history_sim * 0.95))


def _collect_orchestrator_candidate_rooms() -> list[dict]:
    """마스터 에이전트를 제외한 모든 후보 채팅방 목록을 중복 없이 모은다."""
    from bot.web.rooms import _load_chat_rooms_index
    load_index_fn = _get_mod_attr("_load_chat_rooms_index", _load_chat_rooms_index)
    current_chat_db = _get_mod_attr("chat_db", chat_db)

    rooms = [
        r for r in load_index_fn()
        if str(r.get("id") or r.get("roomId") or "") != CORE_ORCHESTRATOR_ROOM_ID
    ]
    seen_ids = {str(r.get("id") or r.get("roomId") or "") for r in rooms}
    try:
        for db_r in current_chat_db.get_rooms(limit=200):
            rid = str(db_r.get("id") or db_r.get("roomId") or "")
            if rid and rid != CORE_ORCHESTRATOR_ROOM_ID and rid not in seen_ids:
                rooms.append(db_r)
                seen_ids.add(rid)
    except Exception:
        pass
    return rooms


def _load_room_summaries_map(limit: int = 300) -> dict[str, dict]:
    """채팅방 ID → 관리 중인 요약본(`room_summaries`) 맵을 안전하게 조회한다."""
    try:
        return master_tasks.list_room_summaries_map(limit=limit)
    except Exception:
        return {}


def _find_similar_orchestrator_room(
    title: str,
    message: str = "",
    *,
    threshold: float = _SIMILAR_ROOM_THRESHOLD,
    include_running: bool = False,
    rooms: list[dict] | None = None,
) -> tuple[dict | None, float]:
    """기존 활성 채팅방 중 질문/주제가 가장 유사한 방과 유사도 점수를 반환한다."""
    from bot.web.rooms import _normalize_room_ts
    candidate_rooms = rooms if rooms is not None else _collect_orchestrator_candidate_rooms()
    if not candidate_rooms:
        return None, 0.0

    tasks_by_room: dict[str, list[dict]] = {}
    try:
        for t in master_tasks.list_tasks(limit=200):
            t_rid = str(t.get("roomId") or "").strip()
            if t_rid:
                tasks_by_room.setdefault(t_rid, []).append(t)
    except Exception:
        pass

    room_summaries_map = _load_room_summaries_map(limit=300)

    best_room: dict | None = None
    best_score = 0.0
    best_ts = -1.0

    for r in candidate_rooms:
        rid = str(r.get("id") or r.get("roomId") or "").strip()
        if not rid or rid == CORE_ORCHESTRATOR_ROOM_ID:
            continue
        if r.get("isArchived"):
            continue

        sid = str(r.get("sessionId") or "").strip() or None
        score = _score_room_similarity(
            title,
            message,
            r,
            tasks_by_room.get(rid),
            room_summary=room_summaries_map.get(rid),
        )
        if score < threshold:
            continue

        if not include_running:
            is_run, q_size, _ = _is_room_currently_running(rid, sid)
            # 병렬 팬아웃 중 다른 작업 방에 잘못 합쳐지지 않도록 실행 중인 방은 완전 일치만 허용
            if (is_run or q_size > 0) and score < 0.95:
                continue

        ts = _normalize_room_ts(r.get("updatedAt") or r.get("createdAt"))
        if score > best_score + 1e-6 or (abs(score - best_score) <= 1e-6 and ts > best_ts):
            best_room = r
            best_score = score
            best_ts = ts

    return best_room, best_score


def _resolve_orchestrator_target_room(target: str) -> dict | None:
    """총괄 에이전트가 지정한 식별자(roomId, sessionId, 접두사, 방 제목 키워드, 또는 유사 주제)로 대상 대화방을 찾는다."""
    clean = str(target or "").strip()
    if not clean:
        return None
    current_chat_db = _get_mod_attr("chat_db", chat_db)
    rooms = _collect_orchestrator_candidate_rooms()

    # 1. 정확 일치 (id / roomId / sessionId)
    for r in rooms:
        rid = str(r.get("id") or r.get("roomId") or "").strip()
        sid = str(r.get("sessionId") or "").strip()
        if clean in (rid, sid):
            return r
    # 2. 접두사 일치 (UUID 앞자리 등 6자 이상)
    if len(clean) >= 6:
        for r in rooms:
            rid = str(r.get("id") or r.get("roomId") or "").strip()
            sid = str(r.get("sessionId") or "").strip()
            if (rid and rid.startswith(clean)) or (sid and sid.startswith(clean)):
                return r
    # 3. 제목 대소문자 무시 부분 일치 (최신 방 우선)
    clean_lower = clean.lower()
    for r in rooms:
        title = str(r.get("title") or "").strip().lower()
        if title and clean_lower in title:
            return r
    # 4. chat_db 직접 조회 폴백
    db_room = current_chat_db.get_room(clean)
    if db_room and db_room.get("id") != CORE_ORCHESTRATOR_ROOM_ID:
        return db_room
    # 5. 유사 주제 채팅방 탐색 폴백
    similar_room, _ = _find_similar_orchestrator_room(
        clean, "", include_running=True, rooms=rooms
    )
    return similar_room


def _inspect_agy_brain(session_id: str | None) -> dict | None:
    """AGY 세션의 ~/.gemini/antigravity/brain/<session_id> 아티팩트 및 최근 트랜스크립트를 요약한다."""
    if not session_id:
        return None
    safe_sid = re.sub(r"[^a-zA-Z0-9_\-]", "", str(session_id).strip())
    if not safe_sid:
        return None
    brain_path = os.path.join(AGY_BRAIN_DIR, safe_sid)
    if not os.path.isdir(brain_path):
        return None

    artifacts: list[str] = []
    try:
        for name in sorted(os.listdir(brain_path)):
            if name.startswith("."):
                continue
            artifacts.append(name)
    except OSError:
        pass

    recent_steps: list[dict] = []
    transcript_path = os.path.join(brain_path, ".system_generated", "logs", "transcript.jsonl")
    if os.path.isfile(transcript_path):
        try:
            with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()[-15:]
            for ln in lines:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    step_obj = json.loads(ln)
                    tool_names = [
                        tc.get("name") or tc.get("tool_name") or ""
                        for tc in (step_obj.get("tool_calls") or [])
                        if isinstance(tc, dict)
                    ]
                    recent_steps.append({
                        "stepIndex": step_obj.get("step_index"),
                        "type": step_obj.get("type"),
                        "status": step_obj.get("status"),
                        "contentPreview": str(step_obj.get("content") or "")[:200],
                        "tools": [t for t in tool_names if t],
                    })
                except Exception:
                    pass
        except OSError:
            pass

    return {
        "sessionId": safe_sid,
        "brainPath": brain_path,
        "artifacts": artifacts,
        "recentSteps": recent_steps,
    }


async def handle_orchestrator_sessions(request: web.Request) -> web.Response:
    """GET /api/orchestrator/sessions — 전체 활성 채팅방(AGY 세션) 목록과 실시간 실행 상태를 반환한다."""
    from bot.web.rooms import _load_chat_rooms_index, _normalize_room_ts
    from bot.web.execution import _room_session_ids
    load_index_fn = _get_mod_attr("_load_chat_rooms_index", _load_chat_rooms_index)

    try:
        limit = max(1, min(200, int(request.query.get("limit", "30"))))
    except ValueError:
        limit = 30

    all_rooms = load_index_fn()
    room_summaries_map = _load_room_summaries_map(limit=300)
    sessions: list[dict] = []
    for r in all_rooms:
        rid = str(r.get("id") or r.get("roomId") or "").strip()
        if not rid or rid == CORE_ORCHESTRATOR_ROOM_ID:
            continue
        sid = str(r.get("sessionId") or "").strip() or None
        is_run, q_size, live_feed = _is_room_currently_running(rid, sid)
        rsum = room_summaries_map.get(rid) or {}
        sessions.append({
            "id": rid,
            "roomId": rid,
            "sessionId": sid,
            "title": r.get("title") or "새 대화",
            "preview": r.get("preview") or "",
            "summary": rsum.get("summary") or (r.get("preview") or ""),
            "queries": list(rsum.get("queries") or []),
            "lastResult": rsum.get("lastResult") or "",
            "taskCount": int(rsum.get("taskCount") or 0),
            "messageCount": r.get("messageCount") or 0,
            "updatedAt": _normalize_room_ts(r.get("updatedAt") or r.get("createdAt")),
            "updatedAtFormatted": _format_updated_at(r.get("updatedAt") or r.get("createdAt")),
            "isRunning": is_run,
            "queueSize": q_size,
            "liveFeed": live_feed,
            "agyCmd": f"agy --conversation {sid} --add-dir {WORKDIR}" if sid else f"agy -i --add-dir {WORKDIR}",
        })
        if len(sessions) >= limit:
            break

    core_is_run, core_q_size, core_feed = _is_room_currently_running(
        CORE_ORCHESTRATOR_ROOM_ID,
        _room_session_ids.get(CORE_ORCHESTRATOR_ROOM_ID) or get_session(ns="core_"),
    )
    return web.json_response({
        "ok": True,
        "total": len(sessions),
        "runningCount": sum(1 for s in sessions if s.get("isRunning")),
        "coreOrchestrator": {
            "roomId": CORE_ORCHESTRATOR_ROOM_ID,
            "sessionId": _room_session_ids.get(CORE_ORCHESTRATOR_ROOM_ID) or get_session(ns="core_"),
            "isRunning": core_is_run,
            "queueSize": core_q_size,
            "liveFeed": core_feed,
        },
        "sessions": sessions,
    })


async def handle_orchestrator_read(request: web.Request) -> web.Response:
    """GET /api/orchestrator/session/read?target=...&limit=20&brain=0 — 특정 채팅방(AGY 세션)의 대화 내역 및 브레인 상태를 반환한다."""
    target = request.query.get("target", "").strip()
    if not target:
        return web.json_response({"ok": False, "error": "target 파라미터가 필요합니다."}, status=400)

    try:
        limit = max(1, min(100, int(request.query.get("limit", "20"))))
    except ValueError:
        limit = 20
    include_brain = request.query.get("brain", "0").strip() in ("1", "true", "yes")

    resolve_fn = _get_mod_attr("_resolve_orchestrator_target_room", _resolve_orchestrator_target_room)
    matched = resolve_fn(target)
    if not matched:
        return web.json_response(
            {"ok": False, "error": f"대상 채팅방(세션)을 찾을 수 없습니다: {target}"},
            status=404,
        )

    from bot.web.rooms import _load_room_messages, _normalize_room_ts
    load_msgs_fn = _get_mod_attr("_load_room_messages", _load_room_messages)

    rid = str(matched.get("id") or matched.get("roomId") or "").strip()
    sid = str(matched.get("sessionId") or "").strip() or None
    messages = load_msgs_fn(rid)
    if not messages and sid and sid != rid:
        messages = load_msgs_fn(sid)

    is_run, q_size, live_feed = _is_room_currently_running(rid, sid)
    rsum = master_tasks.get_room_summary(rid)
    session_info = {
        "id": rid,
        "roomId": rid,
        "sessionId": sid,
        "title": matched.get("title") or "대화방",
        "preview": matched.get("preview") or "",
        "summary": (rsum or {}).get("summary") or (matched.get("preview") or ""),
        "messageCount": len(messages) or matched.get("messageCount") or 0,
        "updatedAt": _normalize_room_ts(matched.get("updatedAt") or matched.get("createdAt")),
        "updatedAtFormatted": _format_updated_at(matched.get("updatedAt") or matched.get("createdAt")),
        "isRunning": is_run,
        "queueSize": q_size,
        "liveFeed": live_feed,
    }

    resp_data: dict[str, Any] = {
        "ok": True,
        "session": session_info,
        "roomSummary": rsum,
        "messages": messages[-limit:],
    }
    if include_brain:
        resp_data["brain"] = _inspect_agy_brain(sid or rid)

    return web.json_response(resp_data)


def _merge_room_titles(existing_title: str, incoming_title: str, max_len: int = 36) -> str:
    """기존 방 제목과 새 요청 제목이 다를 경우 두 주제를 자연스럽게 아우르는 통합 제목을 생성한다."""
    t1 = str(existing_title or "").strip()
    t2 = str(incoming_title or "").strip()
    if not t1:
        return t2[:max_len]
    if not t2:
        return t1[:max_len]

    n1 = re.sub(r"\s+", " ", t1)
    n2 = re.sub(r"\s+", " ", t2)
    if n1 == n2 or n2 in n1:
        return t1[:max_len]
    if n1 in n2:
        return t2[:max_len]

    sep = " · "
    merged = f"{t1}{sep}{t2}"
    if len(merged) <= max_len:
        return merged

    # 36자 초과 시 두 제목의 핵심부를 균형있게 분할
    avail = max_len - len(sep) - 1
    half = max(6, avail // 2)
    p1 = t1[:half].rstrip()
    p2 = t2[:(avail - len(p1))].rstrip()
    candidate = f"{p1}…{sep}{p2}"
    if len(candidate) <= max_len:
        return candidate
    return merged[:max_len - 1] + "…"


def _bind_existing_room_for_dispatch(
    matched: dict,
    current_chat_db: Any,
    clean_model: str | None,
    clean_effort: str | None,
) -> tuple[str, str | None, str]:
    """기존 채팅방(세션) 메타데이터를 추출하고 필요 시 모델/effort를 갱신한다."""
    room_id = str(matched.get("id") or matched.get("roomId") or "").strip()
    session_id = str(matched.get("sessionId") or "").strip() or None
    room_title = str(matched.get("title") or room_id)
    if clean_model is not None or clean_effort is not None:
        try:
            current_chat_db.update_room(room_id, model=clean_model, effort=clean_effort)
        except Exception as exc:
            logger.warning("[orchestrator] 대화방 모델/effort 갱신 실패: %s", exc)
    return room_id, session_id, room_title


async def handle_orchestrator_dispatch(request: web.Request) -> web.Response:
    """POST /api/orchestrator/dispatch — 특정 채팅방(AGY 세션)에 지시 메시지를 전송하거나 새 채팅방 세션을 스폰한다."""
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "유효하지 않은 JSON 요청입니다."}, status=400)

    if not isinstance(payload, dict):
        return web.json_response({"ok": False, "error": "JSON 객체가 필요합니다."}, status=400)

    action = str(payload.get("action") or "send").strip().lower()
    message = str(payload.get("message") or payload.get("prompt") or payload.get("text") or "").strip()
    wait_for_result = bool(payload.get("wait", False))
    force_new = bool(payload.get("forceNew") or payload.get("force_new"))

    req_model = payload.get("model")
    clean_model = str(req_model).strip() if (isinstance(req_model, str) and req_model.strip()) else None

    req_effort = payload.get("effort")
    clean_effort = str(req_effort).strip() if (isinstance(req_effort, str) and req_effort.strip()) else None

    # --model 사전 검증: 표시명("Claude Sonnet 5.5 (Medium)") 정규화, 해석 불가 시 허용 목록과 함께 400
    if clean_model:
        import bot.core.child_error_report as child_error_report
        import bot.engine.engine_settings as engine_settings
        known = engine_settings.get_all_model_labels()
        norm_model, norm_effort = child_error_report.normalize_model(clean_model, known)
        if norm_model is None:
            return web.json_response(
                {
                    "ok": False,
                    "error": f"invalid model selection (--model \"{clean_model}\"): 허용 모델 목록: "
                    + child_error_report.format_allowed_models(known),
                    "allowedModels": list(known),
                },
                status=400,
            )
        clean_model = norm_model
        if clean_effort is None:
            clean_effort = norm_effort

    req_parent = payload.get("parentAgentId") or payload.get("parent_agent_id") or payload.get("parent")
    parent_agent_id = str(req_parent).strip() if (isinstance(req_parent, str) and req_parent.strip()) else CORE_ORCHESTRATOR_ROOM_ID
    if not _is_orchestrator_room_id(parent_agent_id):
        parent_agent_id = CORE_ORCHESTRATOR_ROOM_ID

    parent_cfg = agent_configs.get_agent_config(parent_agent_id)
    if clean_model is None and parent_cfg and parent_cfg.get("childModel"):
        clean_model = str(parent_cfg["childModel"]).strip() or None
    if clean_effort is None and parent_cfg and parent_cfg.get("childEffort"):
        clean_effort = str(parent_cfg["childEffort"]).strip() or None

    raw_prompt = bool(payload.get("rawPrompt") or payload.get("raw_prompt"))
    slash_commands = bool(payload.get("slashCommands") or payload.get("slash_commands"))
    req_orchestrator = payload.get("orchestrator")
    orchestrator_flag: bool | None = bool(req_orchestrator) if req_orchestrator is not None else None

    if not message:
        return web.json_response({"ok": False, "error": "전송할 message가 비어 있습니다."}, status=400)

    effective_message = (
        message
        if (raw_prompt or slash_commands)
        else agent_configs.compose_child_dispatch_prompt(parent_agent_id, message, raw_prompt=raw_prompt)
    )

    from bot.web.rooms import _append_room_message, _update_room_metadata
    from bot.web.execution import is_room_busy, enqueue_room_turn, broadcast_ws_json
    current_chat_db = _get_mod_attr("chat_db", chat_db)
    current_broadcast = _get_mod_attr("broadcast_ws_json", broadcast_ws_json)
    current_enqueue = _get_mod_attr("enqueue_room_turn", enqueue_room_turn)

    reused_existing = False
    matched_similarity: float | None = None
    matched_summary: str = ""
    user_msg_id: int | None = None
    dispatch_client_msg_id = f"orch-{uuid.uuid4().hex[:12]}"

    if action == "spawn":
        raw_title = str(payload.get("title") or "").strip()
        room_title = (raw_title or message.splitlines()[0].strip())[:36] or "새 에이전트 세션"

        matched_room: dict | None = None
        sim_score = 0.0
        if not force_new:
            find_similar_fn = _get_mod_attr(
                "_find_similar_orchestrator_room", _find_similar_orchestrator_room
            )
            matched_room, sim_score = find_similar_fn(room_title, message)

        if matched_room is not None:
            room_id, session_id, existing_room_title = _bind_existing_room_for_dispatch(
                matched_room, current_chat_db, clean_model, clean_effort
            )
            # 기존 방 제목과 새 요청 제목을 아우르는 통합 제목 산출 및 갱신
            merged_title = _merge_room_titles(existing_room_title, room_title)
            room_title = merged_title
            if merged_title != existing_room_title:
                try:
                    current_chat_db.update_room(
                        room_id=room_id,
                        title=merged_title,
                        session_id=session_id,
                        force_title=True,
                    )
                except Exception as exc:
                    logger.warning("[orchestrator] 유사 방 재사용 시 통합 제목 DB 갱신 실패: %s", exc)
                try:
                    master_tasks.upsert_room_summary(
                        room_id=room_id,
                        title=merged_title,
                        session_id=session_id,
                    )
                except Exception as exc:
                    logger.warning("[orchestrator] 유사 방 재사용 시 room_summaries 제목 갱신 실패: %s", exc)
                try:
                    await current_broadcast({
                        "type": "room_renamed",
                        "roomId": room_id,
                        "sessionId": session_id,
                        "title": merged_title,
                    })
                except Exception:
                    pass

            is_new_session = False
            reused_existing = True
            matched_similarity = round(sim_score, 3)
            rsum = master_tasks.get_room_summary(room_id)
            matched_summary = str((rsum or {}).get("summary") or matched_room.get("preview") or "").strip()
            logger.info(
                "[orchestrator] 유사 채팅방 재사용: 요청='%s' -> 기존 방='%s' (통합제목='%s', roomId=%s, score=%.2f)",
                raw_title or room_title,
                existing_room_title,
                room_title,
                room_id,
                sim_score,
            )
        else:
            room_id = f"room-{int(time.time() * 1000)}-{uuid.uuid4().hex[:4]}"
            session_id = None
            is_new_session = True

            try:
                current_chat_db.get_or_create_room(
                    room_id,
                    title=room_title,
                    channel="web",
                    model=clean_model,
                    effort=clean_effort,
                    workdir=child_default_workdir(parent_agent_id),
                    parent_agent_id=parent_agent_id,
                )
            except Exception as exc:
                logger.warning("[orchestrator] 새 대화방 DB 생성 실패: %s", exc)
            user_msg_id = _append_room_message(
                room_id,
                {
                    "type": "user",
                    "role": "user",
                    "text": effective_message,
                    "channel": "orchestrator",
                    "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                },
            )
            _update_room_metadata(
                room_id,
                title=room_title,
                preview=message[:60],
                delta_msgs=1,
            )
            await current_broadcast({
                "type": "room_created",
                "room": {
                    "id": room_id,
                    "roomId": room_id,
                    "title": room_title,
                    "preview": message[:60],
                    "messageCount": 1,
                    "updatedAt": time.time(),
                    "sessionId": None,
                    "isNewSession": True,
                    "model": clean_model,
                    "effort": clean_effort,
                    "workdir": child_default_workdir(parent_agent_id),
                    "byOrchestrator": True,
                    "isOrchestratorChild": True,
                    "parentAgentId": parent_agent_id,
                    "channel": "orchestrator",
                },
                "byOrchestrator": True,
            })
    elif action == "send":
        target = str(payload.get("target") or payload.get("roomId") or payload.get("sessionId") or "").strip()
        if not target:
            return web.json_response({"ok": False, "error": "target 식별자가 필요합니다."}, status=400)
        if _is_orchestrator_room_id(target):
            if payload.get("allowCore") or payload.get("channel") in ("task_report", "master_patrol", "schedule_trigger"):
                return await handle_orchestrator_notify(request)
            return web.json_response(
                {"ok": False, "error": f"오케스트레이터 에이전트({target})에게는 일반 지시를 재귀 전송할 수 없습니다. 보고/알림은 /api/orchestrator/notify 를 사용하세요."},
                status=400,
            )
        resolve_fn = _get_mod_attr("_resolve_orchestrator_target_room", _resolve_orchestrator_target_room)
        matched = resolve_fn(target)
        if not matched:
            return web.json_response(
                {"ok": False, "error": f"대상 채팅방(세션)을 찾을 수 없습니다: {target}"},
                status=404,
            )
        room_id, session_id, room_title = _bind_existing_room_for_dispatch(
            matched, current_chat_db, clean_model, clean_effort
        )
        is_new_session = False
    else:
        return web.json_response({"ok": False, "error": f"지원하지 않는 action입니다: {action}"}, status=400)

    will_be_queued = is_room_busy(room_id)

    dispatch_event: dict[str, Any] = {
        "type": "orchestrator_dispatch",
        "action": action,
        "roomId": room_id,
        "sessionId": session_id,
        "title": room_title,
        "text": effective_message,
        "msgId": user_msg_id,
        "clientMsgId": dispatch_client_msg_id,
        "queued": will_be_queued,
        "isOrchestratorChild": True,
        "parentAgentId": parent_agent_id,
        "model": clean_model,
        "effort": clean_effort,
        "wait": wait_for_result,
        "reused": reused_existing,
    }
    if matched_similarity is not None:
        dispatch_event["matchedSimilarity"] = matched_similarity
    await current_broadcast(dispatch_event)

    task_rec = master_tasks.create_task(
        room_id=room_id,
        instruction=message,
        session_id=session_id,
        title=room_title,
        origin="dispatch",
        wait=wait_for_result,
    )
    task_id = task_rec["id"]

    try:
        dispatch_summary = master_memory.format_dispatch_memory(
            action=action,
            room_title=room_title,
            room_id=room_id,
            task_id=task_id,
            message=message,
        )
        master_memory.add_memory(
            dispatch_summary,
            title=room_title,
            kind="dispatch",
            room_id=room_id,
            task_id=task_id,
            summarize=True,
            agent_id=master_memory.normalize_agent_id(parent_agent_id),
        )
    except Exception:
        pass

    loop = asyncio.get_running_loop()
    result_future: asyncio.Future | None = loop.create_future() if wait_for_result else None

    enqueue_kwargs: dict[str, Any] = {
        "room_id": room_id,
        "user_text": effective_message,
        "is_voice_mode": False,
        "client_session_id": session_id,
        "is_new_session": is_new_session,
        "channel": "orchestrator",
        "model": clean_model,
        "effort": clean_effort,
        "result_future": result_future,
        "task_id": task_id,
        "user_msg_id": user_msg_id,
        "client_msg_id": dispatch_client_msg_id,
    }
    if raw_prompt:
        enqueue_kwargs["raw_prompt"] = True
    if slash_commands:
        enqueue_kwargs["slash_commands"] = True
    if orchestrator_flag is not None:
        enqueue_kwargs["orchestrator"] = orchestrator_flag

    enqueue_result = await current_enqueue(**enqueue_kwargs)
    is_already_running = enqueue_result["already_running"]
    q_size = enqueue_result["queue_size"]

    if not wait_for_result or result_future is None:
        resp_body: dict[str, Any] = {
            "ok": True,
            "status": "queued" if is_already_running else "started",
            "action": action,
            "reused": reused_existing,
            "taskId": task_id,
            "roomId": room_id,
            "sessionId": session_id,
            "parentAgentId": parent_agent_id,
            "title": room_title,
            "queuePosition": q_size,
            "serverOnComplete": True,
        }
        if matched_similarity is not None:
            resp_body["matchedSimilarity"] = matched_similarity
        if matched_summary:
            resp_body["matchedSummary"] = matched_summary
        return web.json_response(resp_body)

    try:
        turn_res = await result_future
    except asyncio.CancelledError:
        return web.json_response(
            {
                "ok": False,
                "error": "대상 세션의 실행이 취소되었습니다.",
                "taskId": task_id,
                "roomId": room_id,
                "sessionId": session_id,
                "title": room_title,
            },
            status=409,
        )
    except Exception as exc:
        return web.json_response(
            {
                "ok": False,
                "error": f"대상 세션 실행 중 오류 발생: {exc}",
                "taskId": task_id,
                "roomId": room_id,
                "sessionId": session_id,
                "title": room_title,
            },
            status=500,
        )

    done_body: dict[str, Any] = {
        "ok": bool(turn_res.get("ok", True)),
        "status": "completed",
        "action": action,
        "reused": reused_existing,
        "taskId": task_id,
        "roomId": room_id,
        "sessionId": turn_res.get("sessionId") or session_id,
        "title": room_title,
        "model": clean_model,
        "effort": clean_effort,
        "reply": turn_res.get("reply") or "",
        "elapsedSeconds": turn_res.get("elapsedSeconds") or 0,
        "steps": turn_res.get("steps") or [],
    }
    if matched_similarity is not None:
        done_body["matchedSimilarity"] = matched_similarity
    if matched_summary:
        done_body["matchedSummary"] = matched_summary
    return web.json_response(done_body)


async def handle_orchestrator_summaries(request: web.Request) -> web.Response:
    """GET /api/orchestrator/summaries?limit=50&includeArchived=0 — 마스터가 관리 중인 하위 채팅 요약본 목록을 반환한다."""
    from bot.web.rooms import _normalize_room_ts

    try:
        limit = max(1, min(200, int(request.query.get("limit", "50"))))
    except ValueError:
        limit = 50
    inc_raw = request.query.get("includeArchived") or request.query.get("include_archived") or "0"
    include_archived = inc_raw.strip().lower() in ("1", "true", "yes")

    rooms_by_id: dict[str, dict] = {}
    for r in _collect_orchestrator_candidate_rooms():
        rid = str(r.get("id") or r.get("roomId") or "").strip()
        if rid:
            rooms_by_id[rid] = r

    summaries: list[dict] = []
    for item in master_tasks.list_room_summaries(limit=300):
        rid = str(item.get("roomId") or "").strip()
        if not rid or rid == CORE_ORCHESTRATOR_ROOM_ID:
            continue
        meta = rooms_by_id.get(rid)
        if meta is None:
            continue
        is_archived = bool(meta.get("isArchived"))
        if is_archived and not include_archived:
            continue

        sid = str(meta.get("sessionId") or item.get("sessionId") or "").strip() or None
        is_run, q_size, live_feed = _is_room_currently_running(rid, sid)
        updated_raw = meta.get("updatedAt") or item.get("updatedAt")
        summaries.append({
            "roomId": rid,
            "sessionId": sid,
            "title": meta.get("title") or item.get("title") or rid,
            "summary": item.get("summary") or "",
            "customSummary": item.get("customSummary") or "",
            "queries": list(item.get("queries") or []),
            "lastResult": item.get("lastResult") or "",
            "lastNote": item.get("lastNote") or "",
            "taskCount": int(item.get("taskCount") or 0),
            "lastTaskId": item.get("lastTaskId") or "",
            "lastStatus": item.get("lastStatus") or "",
            "busy": bool(is_run or q_size > 0),
            "isRunning": is_run,
            "queueSize": q_size,
            "liveFeed": live_feed,
            "archived": is_archived,
            "updatedAt": _normalize_room_ts(updated_raw),
            "updatedAtFormatted": _format_updated_at(updated_raw),
        })
        if len(summaries) >= limit:
            break

    return web.json_response({
        "ok": True,
        "total": len(summaries),
        "summaries": summaries,
    })


async def handle_orchestrator_summary_update(request: web.Request) -> web.Response:
    """POST /api/orchestrator/session/summary — 특정 하위 채팅방의 요약본(`room_summaries`)을 명시적으로 갱신한다."""
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "유효하지 않은 JSON 요청입니다."}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"ok": False, "error": "JSON 객체가 필요합니다."}, status=400)

    target = str(payload.get("target") or payload.get("roomId") or payload.get("sessionId") or "").strip()
    if not target:
        return web.json_response({"ok": False, "error": "target 식별자가 필요합니다."}, status=400)

    resolve_fn = _get_mod_attr("_resolve_orchestrator_target_room", _resolve_orchestrator_target_room)
    matched = resolve_fn(target)
    if not matched:
        return web.json_response(
            {"ok": False, "error": f"대상 채팅방(세션)을 찾을 수 없습니다: {target}"},
            status=404,
        )

    rid = str(matched.get("id") or matched.get("roomId") or "").strip()
    sid = str(matched.get("sessionId") or "").strip() or None
    summary_val = payload.get("summary")
    title_val = payload.get("title")
    query_val = payload.get("query")
    result_val = payload.get("resultSummary") if "resultSummary" in payload else payload.get("result_summary")
    note_val = payload.get("note")
    status_val = payload.get("status")

    if all(v is None for v in (summary_val, title_val, query_val, result_val, note_val, status_val)):
        return web.json_response(
            {"ok": False, "error": "갱신할 summary, query, resultSummary, note, title 또는 status 필드가 필요합니다."},
            status=400,
        )

    updated = master_tasks.upsert_room_summary(
        rid,
        title=str(title_val).strip() if title_val is not None else (matched.get("title") or rid),
        session_id=sid,
        summary=str(summary_val) if summary_val is not None else None,
        query=str(query_val) if query_val is not None else None,
        result_summary=str(result_val) if result_val is not None else None,
        note=str(note_val) if note_val is not None else None,
        status=str(status_val).strip() if status_val is not None else None,
    )

    supervisor = get_master_supervisor()
    if supervisor:
        supervisor.poke()

    return web.json_response({
        "ok": True,
        "roomId": rid,
        "summary": updated.get("summary") or "",
        "roomSummary": updated,
    })


async def handle_orchestrator_graph(request: web.Request) -> web.Response:
    """GET /api/orchestrator/graph — 실시간 마스터-슬레이브 관계도 및 감독 상태 반환."""
    supervisor = get_master_supervisor()
    if supervisor is None:
        return web.json_response({"ok": False, "error": "Supervisor not initialized"}, status=503)
    graph = supervisor.current_graph()
    return web.json_response({"ok": True, "graph": graph})


async def handle_orchestrator_tasks(request: web.Request) -> web.Response:
    """GET /api/orchestrator/tasks?limit=50&include_closed=1 — 작업 원장 목록 반환."""
    try:
        limit = max(1, min(200, int(request.query.get("limit", "50"))))
    except ValueError:
        limit = 50
    include_closed = request.query.get("include_closed", "1").strip() not in ("0", "false", "no")
    tasks = master_tasks.list_tasks(include_closed=include_closed, limit=limit)
    return web.json_response({"ok": True, "tasks": tasks, "total": len(tasks)})


async def handle_orchestrator_task_update(request: web.Request) -> web.Response:
    """POST /api/orchestrator/task/update — 작업에 검토 메모 추가 또는 종결 처리."""
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "유효하지 않은 JSON 요청입니다."}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"ok": False, "error": "JSON 객체가 필요합니다."}, status=400)

    task_id = str(payload.get("taskId") or payload.get("id") or "").strip()
    room_id = str(payload.get("roomId") or "").strip()
    if not task_id and room_id:
        marked = master_tasks.mark_room_reviewed(room_id)
        supervisor = get_master_supervisor()
        if supervisor:
            supervisor.poke()
        return web.json_response({"ok": True, "roomId": room_id, "marked": marked})
    if not task_id:
        return web.json_response({"ok": False, "error": "taskId 가 필요합니다."}, status=400)

    note = payload.get("note")
    close = bool(payload.get("close", False))
    status = payload.get("status")
    reviewed = payload.get("reviewed")

    fields = {}
    if close:
        fields["status"] = "closed"
        fields["reviewed"] = True
    elif status:
        fields["status"] = str(status).strip()
    if reviewed is not None:
        fields["reviewed"] = bool(reviewed)

    updated = master_tasks.update_task(task_id, note=note, **fields)
    if not updated:
        return web.json_response({"ok": False, "error": f"작업을 찾을 수 없습니다: {task_id}"}, status=404)

    supervisor = get_master_supervisor()
    if supervisor:
        supervisor.poke()
    return web.json_response({"ok": True, "task": updated})


async def handle_orchestrator_room_mark_read(request: web.Request) -> web.Response:
    """POST /api/orchestrator/room/mark-read — 특정 방(roomId)에 속한 모든 위임 작업을 읽음/검토 완료(reviewed=True) 처리."""
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "유효하지 않은 JSON 요청입니다."}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"ok": False, "error": "JSON 객체가 필요합니다."}, status=400)

    room_id = str(payload.get("roomId") or payload.get("id") or "").strip()
    if not room_id:
        return web.json_response({"ok": False, "error": "roomId 가 필요합니다."}, status=400)

    marked = master_tasks.mark_room_reviewed(room_id)

    supervisor = get_master_supervisor()
    if supervisor:
        supervisor.poke()

    return web.json_response({"ok": True, "roomId": room_id, "marked": marked})


async def handle_orchestrator_patrol_trigger(request: web.Request) -> web.Response:
    """POST /api/orchestrator/patrol/trigger — 정기 점검 턴을 즉시 수동 트리거."""
    supervisor = get_master_supervisor()
    if supervisor is None:
        return web.json_response({"ok": False, "error": "Supervisor not initialized"}, status=503)
    res = await supervisor.start_patrol(reason="사용자 수동 점검", force=True)
    return web.json_response(res)


async def handle_orchestrator_archive_session(request: web.Request) -> web.Response:
    """POST /api/orchestrator/session/archive — 마스터 모니터에서 하위 세션 아카이브 처리."""
    from bot.web.rooms import handle_archive_chat_room
    handler = _get_mod_attr("handle_archive_chat_room", handle_archive_chat_room)
    return await handler(request)


async def handle_orchestrator_notify(request: web.Request) -> web.Response:
    """POST /api/orchestrator/notify — 하위 작업 완료 또는 상태 변경 시 마스터 에이전트(__core__)에게 보고 턴을 전달하여 후속 작업을 트리거."""
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "유효하지 않은 JSON 요청입니다."}, status=400)

    if not isinstance(payload, dict):
        return web.json_response({"ok": False, "error": "JSON 객체가 필요합니다."}, status=400)

    message = str(payload.get("message") or payload.get("prompt") or payload.get("text") or "").strip()
    req_parent = payload.get("parentAgentId") or payload.get("parent_agent_id") or payload.get("parent")
    parent_agent_id = str(req_parent).strip() if (isinstance(req_parent, str) and req_parent.strip()) else CORE_ORCHESTRATOR_ROOM_ID
    if not _is_orchestrator_room_id(parent_agent_id):
        parent_agent_id = CORE_ORCHESTRATOR_ROOM_ID

    if not message:
        return web.json_response({"ok": False, "error": "전송할 message가 비어 있습니다."}, status=400)

    task_id = str(payload.get("taskId") or payload.get("task_id") or "").strip() or None
    room_id = str(payload.get("roomId") or payload.get("room_id") or "").strip() or None
    channel = str(payload.get("channel") or "task_report").strip()

    from bot.web.execution import _room_session_ids, broadcast_ws_json, enqueue_room_turn, is_room_completion_recently_reported

    # 서버 완료 훅/안전망이 이미 보고한 작업이면 클라이언트 감시자(session-ctl watch/notify)의 중복 보고는 무시한다.
    if task_id and channel == "task_report" and master_tasks.get_task(task_id) and not master_tasks.claim_report(task_id):
        return web.json_response({"ok": True, "status": "duplicate", "taskId": task_id})
    if not task_id and channel == "task_report" and room_id and is_room_completion_recently_reported(room_id):
        return web.json_response({"ok": True, "status": "duplicate", "roomId": room_id})

    current_enqueue = _get_mod_attr("enqueue_room_turn", enqueue_room_turn)
    current_broadcast = _get_mod_attr("broadcast_ws_json", broadcast_ws_json)

    parent_cfg = agent_configs.get_agent_config(parent_agent_id)
    parent_ns = (
        str(parent_cfg.get("sessionNamespace"))
        if parent_cfg and parent_cfg.get("sessionNamespace")
        else (
            "core_" if parent_agent_id == CORE_ORCHESTRATOR_ROOM_ID
            else ("general_" if parent_agent_id == EXPERT_GENERAL_ROOM_ID
            else ("vault_" if parent_agent_id == EXPERT_VAULT_ROOM_ID else "coding_"))
        )
    )
    target_sid = _room_session_ids.get(parent_agent_id) or get_session(ns=parent_ns)

    enqueue_result = await current_enqueue(
        room_id=parent_agent_id,
        user_text=message,
        is_voice_mode=False,
        client_session_id=target_sid,
        is_new_session=False,
        channel=channel,
        task_id=task_id,
    )
    is_already_running = enqueue_result["already_running"]
    q_size = enqueue_result["queue_size"]

    try:
        notify_summary = master_memory.format_notify_memory(
            status_label=("완료" if channel == "task_report" else channel),
            room_id=room_id or "",
            task_id=task_id or "",
            result_text=message,
        )
        notify_title = ""
        if room_id:
            try:
                current_chat_db = _get_mod_attr("chat_db", chat_db)
                r_info = current_chat_db.get_room(room_id)
                if r_info and r_info.get("title"):
                    notify_title = str(r_info["title"]).strip()
            except Exception:
                pass
        if not notify_title:
            notify_title = f"보고: {task_id or room_id}" if (task_id or room_id) else "하위보고"
        master_memory.add_memory(
            notify_summary,
            title=notify_title,
            kind="notify",
            room_id=room_id or "",
            task_id=task_id or "",
            summarize=True,
            agent_id=master_memory.normalize_agent_id(parent_agent_id),
        )
    except Exception:
        pass

    await current_broadcast({
        "type": "orchestrator_notify",
        "roomId": parent_agent_id,
        "taskId": task_id,
        "sourceRoomId": room_id,
        "text": message[:120],
        "queueSize": q_size,
    })

    logger.info("[orchestrator] 부모 에이전트(%s) 완료보고 턴 적재 (taskId=%s, sourceRoom=%s, q_size=%s)", parent_agent_id, task_id, room_id, q_size)
    return web.json_response({
        "ok": True,
        "status": "queued" if is_already_running else "started",
        "target": parent_agent_id,
        "taskId": task_id,
        "sourceRoomId": room_id,
        "queuePosition": q_size,
    })


def _get_master_state_file() -> str:
    """테스트 패치 호환성을 보장하면서 master_state.json 경로를 반환한다."""
    from bot.core.paths import MASTER_STATE_FILE as _DEFAULT_STATE_FILE

    if MASTER_STATE_FILE != _DEFAULT_STATE_FILE:
        return MASTER_STATE_FILE
    return _get_mod_attr("MASTER_STATE_FILE", MASTER_STATE_FILE)


def _record_master_state_event(event_record: dict[str, Any]) -> None:
    """래퍼 콜백 이벤트를 `master_state.json` 에 원자적으로 영속 기록한다."""
    state_file = _get_master_state_file()
    task_id = str(event_record.get("task_id") or "")
    with _master_state_lock:
        data = read_json(state_file, {}, expect_type=dict)
        if not isinstance(data.get("tasks"), dict):
            data["tasks"] = {}
        if not isinstance(data.get("events"), list):
            data["events"] = []
        data["tasks"][task_id] = event_record
        data["events"] = (data["events"] + [event_record])[-100:]
        write_json_atomic(state_file, data)


async def handle_task_events_callback(request: web.Request) -> web.Response:
    """POST /v1/callbacks/task-events — `agy_wrapper.py` 가 보내는 서브 태스크 보고를 수신한다."""
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "유효하지 않은 JSON 요청입니다."}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"ok": False, "error": "JSON 객체가 필요합니다."}, status=400)

    task_id = str(payload.get("task_id") or payload.get("taskId") or "").strip()
    event_type = str(payload.get("event_type") or payload.get("eventType") or "").strip().upper()
    if not task_id or not event_type:
        return web.json_response(
            {"ok": False, "error": "task_id 및 event_type 필드가 필요합니다."},
            status=400,
        )

    verdict = str(payload.get("verdict") or "").strip().upper()
    try:
        attempt = max(1, int(payload.get("attempt") or 1))
    except (TypeError, ValueError):
        attempt = 1

    summary = cap_summary_text(str(payload.get("summary") or ""))
    missing_raw = payload.get("missing_reason") if "missing_reason" in payload else payload.get("missingReason")
    missing_reason = str(missing_raw).strip() if missing_raw is not None and str(missing_raw).strip() else None
    next_action_taken = str(payload.get("next_action_taken") or payload.get("nextActionTaken") or "").strip()
    requires_intervention = bool(
        payload.get("requires_master_intervention")
        if "requires_master_intervention" in payload
        else payload.get("requiresMasterIntervention", False)
    )

    raw_pointers = payload.get("pointers") if isinstance(payload.get("pointers"), dict) else {}
    pointers = {
        "artifact_path": str(raw_pointers.get("artifact_path") or "").strip(),
        "raw_log_path": str(raw_pointers.get("raw_log_path") or "").strip(),
    }

    now_ts = time.time()
    event_record = {
        "task_id": task_id,
        "event_type": event_type,
        "verdict": verdict,
        "attempt": attempt,
        "summary": summary,
        "missing_reason": missing_reason,
        "next_action_taken": next_action_taken,
        "requires_master_intervention": requires_intervention,
        "pointers": pointers,
        "updated_at": now_ts,
    }
    _record_master_state_event(event_record)

    # master_tasks 원장 동기화 (없으면 래퍼 작업으로 등록 후 상태 업데이트)
    if master_tasks.get_task(task_id) is None:
        master_tasks.create_task(
            room_id=CORE_ORCHESTRATOR_ROOM_ID,
            instruction=summary or task_id,
            title=f"Wrapper:{task_id}",
            origin="wrapper",
            task_id=task_id,
        )

    ledger_fields: dict[str, Any] = {}
    if event_type == "COMPLETED":
        ledger_fields.update(status="done", finishedAt=now_ts, resultPreview=summary[:400], feed="완료")
    elif event_type == "INTERIM_REPORT_AND_RETRY":
        ledger_fields.update(status="running", feed=f"보강 실행 중 (Attempt #{attempt + 1})")
    elif event_type in ("PARTIAL_SUCCESS", "FAILED"):
        ledger_fields.update(
            status="failed",
            finishedAt=now_ts,
            resultPreview=summary[:400],
            error=str(missing_reason or summary or event_type)[:300],
            feed="중단/미완료",
        )
    note_text = f"[{event_type} #{attempt}] {summary[:200]}" if summary else f"[{event_type} #{attempt}]"
    if ledger_fields:
        master_tasks.update_task(task_id, note=note_text, **ledger_fields)
    else:
        master_tasks.update_task(task_id, note=note_text)

    if event_type == "COMPLETED" and not requires_intervention:
        master_tasks.claim_report(task_id)

    try:
        event_summary = master_memory.format_task_event_memory(
            event_type=event_type,
            verdict=verdict or "-",
            task_id=task_id,
            summary=summary or task_id,
            missing_reason=missing_reason,
        )
        master_memory.add_memory(
            event_summary,
            title=f"Task:{task_id}",
            kind="task_event",
            task_id=task_id,
            summarize=True,
        )
    except Exception:
        pass

    from bot.web.execution import _room_session_ids, broadcast_ws_json, enqueue_room_turn

    current_enqueue = _get_mod_attr("enqueue_room_turn", enqueue_room_turn)
    current_broadcast = _get_mod_attr("broadcast_ws_json", broadcast_ws_json)

    should_enqueue = not (event_type == "INTERIM_REPORT_AND_RETRY" and not requires_intervention)
    is_already_running = False
    q_size = 0

    if should_enqueue:
        # 마스터 에이전트(__core__) 에게는 압축 Summary + 파일 포인터만 전달 (Lazy Loading)
        event_lines = [
            f"[System Event] 서브 태스크 {task_id}의 보고가 도착했습니다 ({event_type}, Attempt #{attempt})",
            f"• 판정(Verdict): {verdict or '-'}",
            f"• 핵심 요약:\n{summary or '(요약 없음)'}",
        ]
        if missing_reason:
            event_lines.append(f"• 미흡/중단 사유: {missing_reason}")
        if next_action_taken:
            event_lines.append(f"• Wrapper 조치: {next_action_taken}")
        if pointers["artifact_path"] or pointers["raw_log_path"]:
            event_lines.append(
                "• 산출물 포인터 (세부 데이터 필요 시에만 열람):\n"
                f"  - artifact_path: {pointers['artifact_path'] or '-'}\n"
                f"  - raw_log_path: {pointers['raw_log_path'] or '-'}"
            )
        event_lines.append(
            f"• 마스터 개입 필요 여부: {'예 (확인 및 후속 조치 필요)' if requires_intervention else '아니오 (정보성 보고)'}"
        )
        system_prompt = "\n".join(event_lines)

        core_sid = _room_session_ids.get(CORE_ORCHESTRATOR_ROOM_ID) or get_session(ns="core_")

        enqueue_result = await current_enqueue(
            room_id=CORE_ORCHESTRATOR_ROOM_ID,
            user_text=system_prompt,
            is_voice_mode=False,
            client_session_id=core_sid,
            is_new_session=False,
            channel="task_report",
            task_id=task_id,
        )
        is_already_running = enqueue_result["already_running"]
        q_size = enqueue_result["queue_size"]

    await current_broadcast({
        "type": "orchestrator_task_event",
        "roomId": CORE_ORCHESTRATOR_ROOM_ID,
        "taskId": task_id,
        "eventType": event_type,
        "verdict": verdict,
        "attempt": attempt,
        "summary": summary,
        "pointers": pointers,
        "requiresMasterIntervention": requires_intervention,
        "queueSize": q_size,
    })

    supervisor = get_master_supervisor()
    if supervisor is not None:
        supervisor.poke()

    logger.info(
        "[orchestrator] 래퍼 태스크 콜백 수신 (taskId=%s, event=%s, verdict=%s, attempt=%s, enqueued=%s)",
        task_id,
        event_type,
        verdict,
        attempt,
        should_enqueue,
    )
    status_str = ("queued" if is_already_running else "started") if should_enqueue else "recorded"
    return web.json_response({
        "ok": True,
        "status": status_str,
        "task_id": task_id,
        "event_type": event_type,
        "verdict": verdict,
        "attempt": attempt,
        "pointers": pointers,
        "queuePosition": q_size,
    })


def ensure_orchestrator_room(
    room_id: str,
    current_chat_db: Any = None,
    *,
    model: str | None = None,
    effort: str | None = None,
) -> dict[str, Any]:
    """총괄 에이전트 및 전문가/커스텀 에이전트 대화방이 chat_db 에 항상 존재하도록 보장한다."""
    db = current_chat_db or _get_mod_attr("chat_db", chat_db)
    clean_rid = str(room_id or "").strip()
    cfg = agent_configs.get_agent_config(clean_rid)
    if cfg:
        title = str(cfg.get("title") or cfg.get("name") or clean_rid)
        rid = str(cfg["id"])
        workdir = str(cfg.get("workdir") or "").strip() or None
        eff_model = model if model is not None else cfg.get("model")
        eff_effort = effort if effort is not None else cfg.get("effort")
    elif clean_rid == EXPERT_GENERAL_ROOM_ID:
        title = EXPERT_GENERAL_TITLE
        rid = EXPERT_GENERAL_ROOM_ID
        workdir = None
        eff_model, eff_effort = model, effort
    elif clean_rid == EXPERT_VAULT_ROOM_ID:
        title = EXPERT_VAULT_TITLE
        rid = EXPERT_VAULT_ROOM_ID
        workdir = None
        eff_model, eff_effort = model, effort
    elif clean_rid == EXPERT_CODING_ROOM_ID:
        title = EXPERT_CODING_TITLE
        rid = EXPERT_CODING_ROOM_ID
        workdir = None
        eff_model, eff_effort = model, effort
    else:
        title = CORE_ORCHESTRATOR_TITLE
        rid = CORE_ORCHESTRATOR_ROOM_ID
        workdir = None
        eff_model, eff_effort = model, effort
    return db.get_or_create_room(
        rid,
        session_id=None,
        title=title,
        channel="web",
        model=eff_model,
        effort=eff_effort,
        workdir=workdir,
    )


def ensure_core_orchestrator_room(
    current_chat_db: Any = None,
    *,
    model: str | None = None,
    effort: str | None = None,
) -> dict[str, Any]:
    """총괄 에이전트(`__core__`) 대화방이 chat_db 에 항상 존재하도록 보장한다."""
    return ensure_orchestrator_room(CORE_ORCHESTRATOR_ROOM_ID, current_chat_db, model=model, effort=effort)


async def reset_core_orchestrator_state(
    *,
    target_room_id: str = CORE_ORCHESTRATOR_ROOM_ID,
    memory_mode: str = "keep",
    clear_done_tasks: bool = False,
) -> dict[str, Any]:
    """마스터 에이전트(`__core__`) 또는 전문가/커스텀 에이전트 대화·세션을 초기화하고 4계층 메모리 정리 정책을 적용한다."""
    target_rid = str(target_room_id or CORE_ORCHESTRATOR_ROOM_ID).strip()
    if not _is_orchestrator_room_id(target_rid):
        target_rid = CORE_ORCHESTRATOR_ROOM_ID

    cfg = agent_configs.get_agent_config(target_rid)
    if target_rid == EXPERT_GENERAL_ROOM_ID:
        ns = "general_"
        agent_id = "general"
        agent_name = (cfg.get("name") if cfg else None) or "일반 대화"
    elif target_rid == EXPERT_VAULT_ROOM_ID:
        ns = "vault_"
        agent_id = "vault"
        agent_name = (cfg.get("name") if cfg else None) or "세컨브레인"
    elif target_rid == EXPERT_CODING_ROOM_ID:
        ns = "coding_"
        agent_id = "coding"
        agent_name = (cfg.get("name") if cfg else None) or "봇개발"
    elif target_rid == CORE_ORCHESTRATOR_ROOM_ID:
        ns = "core_"
        agent_id = "master"
        agent_name = (cfg.get("name") if cfg else None) or "총괄"
    else:
        ns = str((cfg or {}).get("sessionNamespace") or f"{target_rid.strip('_')}_")
        agent_id = str((cfg or {}).get("memoryScope") or f"custom_{target_rid.strip('_')}")
        agent_name = str((cfg or {}).get("name") or target_rid)

    mode = str(memory_mode or "keep").strip().lower()
    if mode == "none":
        mode = "keep"
    if mode not in ("cascade", "clear", "keep"):
        raise ValueError(f"지원하지 않는 memoryMode 입니다: {memory_mode}")

    from bot.engine.agent_engine import kill_job
    from bot.engine.engine_settings import delete_session_files
    from bot.web.execution import _load_web_jobs, _remove_web_job, _room_session_ids, broadcast_ws_json, cancel_active_run

    current_chat_db = _get_mod_attr("chat_db", chat_db)
    cancel_run_fn = _get_mod_attr("cancel_active_run", cancel_active_run)
    load_jobs_fn = _get_mod_attr("_load_web_jobs", _load_web_jobs)
    remove_job_fn = _get_mod_attr("_remove_web_job", _remove_web_job)
    kill_job_fn = _get_mod_attr("kill_job", kill_job)
    clear_session_fn = _get_mod_attr("clear_session", clear_session)
    broadcast_fn = _get_mod_attr("broadcast_ws_json", broadcast_ws_json)
    vault_log_fn = _get_mod_attr("vault_log", vault_log)
    supervisor = get_master_supervisor()

    # 1. 기존 모델/effort 및 세션 ID 확보 (대화방 재생성·CLI 세션 파일 정리용)
    old_row: dict[str, Any] | None = None
    try:
        old_row = current_chat_db.get_room(target_rid)
    except Exception:
        old_row = None

    old_model = (old_row or {}).get("model")
    old_effort = (old_row or {}).get("effort")

    mod = sys.modules.get("bot.web.server")
    ws_session_map = getattr(mod, "_room_session_ids", None) if mod else None
    old_sids: list[str] = []
    for cand in (
        (old_row or {}).get("sessionId"),
        _room_session_ids.get(target_rid),
        ws_session_map.get(target_rid) if isinstance(ws_session_map, dict) else None,
        get_session(ns=ns),
    ):
        sid = str(cand or "").strip()
        if sid and sid not in old_sids:
            old_sids.append(sid)

    # 2. 실행 중인 작업/큐/백그라운드 잡 즉시 중단
    if cancel_run_fn:
        cancel_run_fn(target_rid)

    _room_session_ids.pop(target_rid, None)
    if isinstance(ws_session_map, dict):
        ws_session_map.pop(target_rid, None)

    if load_jobs_fn and remove_job_fn:
        try:
            jobs_data = load_jobs_fn()
        except Exception:
            jobs_data = {}
        old_sid_set = set(old_sids)
        for jid, jrec in list(jobs_data.items()):
            if not isinstance(jrec, dict):
                continue
            j_rid = str(jrec.get("room_id") or "").strip()
            j_sid = str(jrec.get("session_id") or "").strip()
            if j_rid == target_rid or (j_sid and j_sid in old_sid_set):
                j_pid = jrec.get("pid")
                if j_pid and kill_job_fn:
                    kill_job_fn(j_pid)
                remove_job_fn(jid)

    # 3. 네임스페이스 세션 초기화 및 CLI 세션 파일 정리
    clear_session_fn(ns=ns)
    for sid in old_sids:
        try:
            delete_session_files(sid)
        except Exception:
            pass

    # 4. 대화방 메시지 완전 비우기 후 빈 룸 보장
    try:
        if hasattr(current_chat_db, "clear_room_messages"):
            current_chat_db.clear_room_messages(target_rid)
        else:
            current_chat_db.delete_room(target_rid, hard_delete=True)
    except Exception as exc:
        logger.warning("[orchestrator] %s 초기화 중 DB 처리 실패: %s", target_rid, exc)
    room_row = ensure_orchestrator_room(target_rid, current_chat_db, model=old_model, effort=old_effort)

    # 5. 4계층 메모리 처리 ("cascade": hot→warm 연쇄 이동, "clear": 전 계층 삭제, "keep": 유지)
    if mode == "cascade":
        memory_snap = master_memory.demote_tier("hot", agent_id=agent_id)
    elif mode == "clear":
        memory_snap = master_memory.clear_memory("all", agent_id=agent_id)
    else:
        memory_snap = master_memory.get_memory_snapshot(limit_per_tier=25, agent_id=agent_id)

    cleared_tasks = (
        master_tasks.clear_terminal_tasks()
        if (clear_done_tasks and target_rid == CORE_ORCHESTRATOR_ROOM_ID)
        else 0
    )
    vault_log_fn(
        f"🧹 {agent_name} 초기화",
        f"[{agent_name} 에이전트] 대화 초기화 완료 (memoryMode={mode}, clearedTasks={cleared_tasks})",
    )

    if target_rid == CORE_ORCHESTRATOR_ROOM_ID and supervisor is not None:
        supervisor.poke()
        try:
            snap = supervisor.host.snapshot()
            graph = build_master_graph(
                rooms=snap.get("rooms") or [],
                live_runs=snap.get("liveRuns") or [],
                tasks=master_tasks.list_tasks(include_closed=True, limit=100),
                patrol=master_tasks.get_patrol_state(),
                now=time.time(),
            )
        except Exception:
            graph = {"memory": memory_snap}
    else:
        graph = {"memory": memory_snap}

    if broadcast_fn:
        await broadcast_fn({
            "type": "session_reset",
            "roomId": target_rid,
            "memoryMode": mode,
            "clearedTasks": cleared_tasks,
            "memory": memory_snap,
            "room": room_row,
        })
        if target_rid == CORE_ORCHESTRATOR_ROOM_ID:
            await broadcast_fn({
                "type": "master_graph",
                "graph": graph if isinstance(graph, dict) else {"memory": memory_snap},
            })
            build_status_fn = _get_mod_attr("build_status_payload", None)
            if build_status_fn:
                await broadcast_fn(build_status_fn())

    return {
        "ok": True,
        "roomId": target_rid,
        "memoryMode": mode,
        "clearedTasks": cleared_tasks,
        "room": room_row,
        "memory": memory_snap,
    }


async def handle_orchestrator_memory_get(request: web.Request) -> web.Response:
    """GET /api/orchestrator/memory — 마스터 또는 전문가 에이전트 4계층 메모리 현황 조회."""
    tier_raw = request.query.get("tier")
    tier = tier_raw.strip().lower() if isinstance(tier_raw, str) and tier_raw.strip() else None
    agent_id = request.query.get("agentId") or request.query.get("agent_id") or request.query.get("agent") or "master"
    try:
        limit = max(1, min(200, int(request.query.get("limit", "50"))))
    except ValueError:
        limit = 50
    try:
        snap = master_memory.get_memory_snapshot(tier=tier, limit_per_tier=limit, agent_id=agent_id)
    except ValueError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400)
    return web.json_response({"ok": True, **snap})


async def handle_orchestrator_memory_post(request: web.Request) -> web.Response:
    """POST /api/orchestrator/memory — 메모리 추가/하위이동/초기화/한도 변경 (자동 cascade 포함)."""
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    agent_id = str(payload.get("agentId") or payload.get("agent_id") or payload.get("agent") or "master").strip()
    action = str(payload.get("action") or "add").strip().lower()
    try:
        if action == "add":
            text = str(payload.get("text") or "").strip()
            if not text:
                return web.json_response({"ok": False, "error": "text 필드가 필요합니다."}, status=400)
            limits_override = payload.get("limits") if isinstance(payload.get("limits"), dict) else None
            snap = master_memory.add_memory(
                text,
                title=str(payload.get("title") or "").strip(),
                kind=str(payload.get("kind") or "note").strip() or "note",
                room_id=str(payload.get("roomId") or payload.get("room_id") or "").strip(),
                task_id=str(payload.get("taskId") or payload.get("task_id") or "").strip(),
                tier=str(payload.get("tier") or "hot").strip().lower(),
                limits_override=limits_override,
                agent_id=agent_id,
            )
        elif action == "demote":
            from_tier = str(payload.get("fromTier") or payload.get("from_tier") or payload.get("tier") or "hot").strip().lower()
            snap = master_memory.demote_tier(from_tier, agent_id=agent_id)
        elif action == "promote":
            from_tier = str(payload.get("fromTier") or payload.get("from_tier") or payload.get("tier") or "warm").strip().lower()
            snap = master_memory.promote_tier(from_tier, agent_id=agent_id)
        elif action == "move":
            item_id = str(payload.get("itemId") or payload.get("id") or "").strip()
            to_tier = str(payload.get("toTier") or payload.get("to_tier") or payload.get("tier") or "hot").strip().lower()
            snap = master_memory.move_memory_item(item_id, to_tier, agent_id=agent_id)
        elif action in ("delete", "remove"):
            item_id = str(payload.get("itemId") or payload.get("id") or "").strip()
            snap = master_memory.delete_memory_item(item_id, agent_id=agent_id)
        elif action == "update":
            item_id = str(payload.get("itemId") or payload.get("id") or "").strip()
            snap = master_memory.apply_memory_batch([{
                "action": "update",
                "id": item_id,
                "text": payload.get("text"),
                "title": payload.get("title"),
                "tier": payload.get("tier") or payload.get("toTier"),
            }], agent_id=agent_id)
        elif action == "batch":
            operations = payload.get("operations")
            if not isinstance(operations, list):
                return web.json_response({"ok": False, "error": "operations 리스트가 필요합니다."}, status=400)
            snap = master_memory.apply_memory_batch(operations, agent_id=agent_id)
        elif action in ("rebalance", "rebalance_memory"):
            snap = await master_memory.rebalance_memory_with_llm(agent_id=agent_id)
        elif action in ("compact", "compact_memory"):
            snap = master_memory.compact_memory(agent_id=agent_id)
        elif action == "clear":
            target_tier = payload.get("tier")
            snap = master_memory.clear_memory(str(target_tier).strip().lower() if target_tier else "all", agent_id=agent_id)
        elif action in ("set_limits", "limits"):
            limits_patch = payload.get("limits")
            if not isinstance(limits_patch, dict):
                return web.json_response({"ok": False, "error": "limits 객체가 필요합니다."}, status=400)
            snap = master_memory.update_tier_limits(limits_patch, agent_id=agent_id)
        else:
            return web.json_response({"ok": False, "error": f"지원하지 않는 action 입니다: {action}"}, status=400)
    except ValueError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400)

    supervisor = get_master_supervisor()
    if supervisor is not None:
        supervisor.poke()
    return web.json_response({"ok": True, **snap})


_bg_skill_tasks: set[asyncio.Task[Any]] = set()


async def handle_orchestrator_skill_post(request: web.Request) -> web.Response:
    """POST /api/orchestrator/skill — 추가 프롬프트 주입 없이 `/<skill>` 만 순수 실행한다."""
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "유효한 JSON 본문이 필요합니다."}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"ok": False, "error": "JSON 객체가 필요합니다."}, status=400)

    raw_skill = str(payload.get("skill") or payload.get("name") or "").strip().lstrip("/")
    if not raw_skill or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", raw_skill):
        return web.json_response(
            {"ok": False, "error": "유효한 skill 이름(영문·숫자·-·_)이 필요합니다."},
            status=400,
        )

    args_str = str(payload.get("args") or "").strip()
    prompt = f"/{raw_skill} {args_str}".strip() if args_str else f"/{raw_skill}"
    model = str(payload.get("model") or "gemini-3.8-flash").strip()
    effort_raw = payload.get("effort")
    effort = str(effort_raw).strip() if effort_raw is not None and str(effort_raw).strip() else "low"
    wait_sync = bool(payload.get("wait"))

    import bot.engine.agent_engine as agent_engine
    from bot.web.execution import broadcast_ws_json

    current_run_agent = _get_mod_attr("run_agent", agent_engine.run_agent)
    current_broadcast = _get_mod_attr("broadcast_ws_json", broadcast_ws_json)

    async def _execute_skill() -> dict[str, Any]:
        out = await current_run_agent(
            prompt,
            None,
            workdir=WORKDIR,
            model=model,
            effort=effort,
            slash_commands=True,
            orchestrator=True,
            timeout=180,
        )
        result_text = str((out or {}).get("result") or "").strip()
        mem_snap = master_memory.get_memory_snapshot(limit_per_tier=50)
        supervisor = get_master_supervisor()
        if supervisor is not None:
            supervisor.poke()
        return {
            "ok": True,
            "skill": raw_skill,
            "prompt": prompt,
            "result": result_text,
            "model": (out or {}).get("model") or model,
            "memory": mem_snap,
        }

    if wait_sync:
        res_data = await _execute_skill()
        return web.json_response(res_data)

    async def _run_bg() -> None:
        try:
            res_data = await _execute_skill()
            await current_broadcast({"type": "orchestrator_skill_done", **res_data})
        except Exception as exc:
            logger.warning("[skill] 백그라운드 스킬 실행 실패 (%s): %s", raw_skill, exc)
            try:
                await current_broadcast({
                    "type": "orchestrator_skill_done",
                    "ok": False,
                    "skill": raw_skill,
                    "error": str(exc),
                })
            except Exception:
                pass

    task = asyncio.create_task(_run_bg())
    _bg_skill_tasks.add(task)
    task.add_done_callback(_bg_skill_tasks.discard)

    return web.json_response({
        "ok": True,
        "status": "started",
        "skill": raw_skill,
        "prompt": prompt,
    })


async def handle_orchestrator_reset(request: web.Request) -> web.Response:
    """POST /api/orchestrator/reset — 마스터 또는 전문가 에이전트 대화/세션 초기화 및 메모리 정리."""
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    target_room_id = str(
        payload.get("roomId") or payload.get("room_id")
        or (EXPERT_VAULT_ROOM_ID if str(payload.get("agentId") or payload.get("agent_id") or "").strip() in ("vault", "expert_vault", "second_brain") else
            EXPERT_CODING_ROOM_ID if str(payload.get("agentId") or payload.get("agent_id") or "").strip() in ("coding", "expert_coding", "bot_dev") else
            payload.get("agentId") or payload.get("agent_id") or CORE_ORCHESTRATOR_ROOM_ID)
    ).strip()
    if not _is_orchestrator_room_id(target_room_id):
        target_room_id = CORE_ORCHESTRATOR_ROOM_ID

    if payload.get("clearMemory") is True and "memoryMode" not in payload and "memory_mode" not in payload:
        memory_mode = "clear"
    else:
        memory_mode = str(payload.get("memoryMode") or payload.get("memory_mode") or "keep").strip().lower()
    clear_done_tasks = bool(payload.get("clearDoneTasks") or payload.get("clear_done_tasks"))

    try:
        res = await reset_core_orchestrator_state(
            target_room_id=target_room_id,
            memory_mode=memory_mode,
            clear_done_tasks=clear_done_tasks,
        )
    except ValueError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400)
    return web.json_response(res)


def extract_delegated_subagents(
    steps: list[dict] | None,
    text: str = "",
    *,
    chat_db_module: Any = None,
    master_tasks_module: Any = None,
    web_exec_module: Any = None,
) -> list[dict[str, Any]]:
    """마스터/전문가 에이전트의 실행 단계(steps) 및 본문에서 위임된 하위 에이전트 목록을 추출한다.

    각 반환 항목:
    {
        "roomId": str,     # 하위 채팅방 ID (예: 'room-17412345-abcd', 확인 불가 시 '')
        "title": str,      # 하위 채팅방 제목 (예: '오늘 일정 조사')
        "status": str,     # 'running' | 'done' | 'error'
        "reason": str,     # 상태 사유 ('정상 완료', '백그라운드 실행 중' 등)
    }
    """
    raw_steps = steps if isinstance(steps, list) else []
    if not raw_steps:
        return []

    db = chat_db_module or _get_mod_attr("chat_db", chat_db)
    tasks_mod = master_tasks_module or _get_mod_attr("master_tasks", master_tasks)
    try:
        import bot.web.execution as web_execution
        exec_mod = web_exec_module or web_execution
    except Exception:
        exec_mod = web_exec_module

    def _is_valid_room_id(rid: str) -> bool:
        if not rid:
            return False
        clean = rid.strip()
        if _is_orchestrator_room_id(clean):
            return False
        return bool(re.match(r"^room-[a-zA-Z0-9_\-]+$", clean))

    def _normalize_title(t: str) -> str:
        s = str(t or "").strip()
        s = re.sub(r"^[\"\'\[\]]+|[\"\'\[\]]+$", "", s)
        s = re.sub(r"\s+", " ", s).strip()
        return s.lower()

    # 1. steps 순회하며 후보 항목(candidate) 추출
    candidates: list[dict[str, Any]] = []

    for idx, s in enumerate(raw_steps):
        if not isinstance(s, dict):
            continue
        title = str(s.get("title") or "")
        detail = str(s.get("detail") or "")
        output = str(s.get("output") or "")

        # 다음 step이 tool_result인 경우 결과 출력을 함께 결합
        next_result_text = ""
        for j in range(idx + 1, min(idx + 3, len(raw_steps))):
            next_s = raw_steps[j]
            if isinstance(next_s, dict) and next_s.get("kind") == "tool_result":
                next_result_text += "\n" + str(next_s.get("detail") or next_s.get("output") or "")

        combined = f"{title}\n{detail}\n{output}\n{next_result_text}"

        # 실제 실행된 커맨드라인 추출 ('$ ...' 줄 우선)
        cmd_line = ""
        for line in detail.splitlines():
            stripped = line.strip()
            if stripped.startswith("$ "):
                cmd_line = stripped[2:].strip()
                break
        if not cmd_line:
            cmd_line = detail.splitlines()[0].strip() if detail.strip() else title

        # --help / -h 조회 명령은 위임이 아니므로 즉시 제외
        if re.search(r"(?:^|\s)(?:--help|-h)(?:\s|$)", f"{title}\n{cmd_line}"):
            continue

        is_spawn = "session-ctl spawn" in combined or title.startswith("session-ctl spawn")
        is_send = "session-ctl send" in combined or title.startswith("session-ctl send")

        if not (is_spawn or is_send):
            continue

        extracted_room_id = ""
        extracted_title = ""

        # A. 결과 출력 패턴 1: [방제목] ((Room )?ID: room-...)
        # 예: 🚀 새 채팅방(AGY 세션) [일정 조사] (Room ID: room-12345)을 생성하고...
        # 예: ✅ [데이터 정리] (ID: room-12345) 세션에 작업을 비동기로 지시했습니다.
        m_bracket = re.search(
            r'\[([^\]]+)\]\s*\((?:Room\s+)?ID:\s*(room-[a-zA-Z0-9_\-]+)',
            combined,
            re.IGNORECASE,
        )
        if m_bracket:
            extracted_title = m_bracket.group(1).strip()
            extracted_room_id = m_bracket.group(2).strip()

        # B. 결과 출력 패턴 2: Room ID: room-... 또는 roomId: "room-..."
        if not extracted_room_id:
            m_rid = re.search(r'(?:Room\s+ID|roomId|room_id)["\']?\s*[:=]\s*["\']?(room-[a-zA-Z0-9_\-]+)', combined, re.IGNORECASE)
            if m_rid:
                extracted_room_id = m_rid.group(1).strip()

        # C. JSON 모드 패턴: "roomId": "...", "title": "..."
        if not extracted_title:
            m_jtitle = re.search(r'"title"\s*:\s*"([^"]+)"', combined)
            if m_jtitle:
                extracted_title = m_jtitle.group(1).strip()

        # D. 명령어 인자에서 target / title 추출 (명령줄에서만 파싱하여 출력 메타데이터 오탐 방지)
        cmd_source = cmd_line if ("session-ctl" in cmd_line) else title
        if is_spawn and not extracted_title:
            m_spawn_cmd = re.search(
                r'session-ctl\s+spawn\s+(?:--[a-zA-Z0-9_-]+(?:\s+[^\s"\'\-]+)?\s+)*(?:["\']([^"\']+)["\']|(\S+))',
                cmd_source,
            )
            if m_spawn_cmd:
                extracted_title = (m_spawn_cmd.group(1) or m_spawn_cmd.group(2) or "").strip()
            elif ":" in title and "spawn" in title:
                extracted_title = title.split(":", 1)[1].strip()

        if is_send:
            m_send_cmd = re.search(
                r'session-ctl\s+send\s+(?:--[a-zA-Z0-9_-]+(?:\s+[^\s"\'\-]+)?\s+)*(?:["\']([^"\']+)["\']|(\S+))',
                cmd_source,
            )
            if m_send_cmd:
                tgt = (m_send_cmd.group(1) or m_send_cmd.group(2) or "").strip()
                if tgt.startswith("room-"):
                    if not extracted_room_id:
                        extracted_room_id = tgt
                elif not extracted_title:
                    extracted_title = tgt
            elif ":" in title and "send" in title:
                tgt = title.split(":", 1)[1].strip()
                if tgt.startswith("room-"):
                    if not extracted_room_id:
                        extracted_room_id = tgt
                elif not extracted_title:
                    extracted_title = tgt

        if not extracted_room_id and extracted_title and re.match(r'^\(\d+줄\)', extracted_title):
            continue

        # 유효하지 않은 roomId(예: 오케스트레이터 ID) 정제
        if extracted_room_id and not _is_valid_room_id(extracted_room_id):
            extracted_room_id = ""

        # 도구 실행 자체(spawn/send 명령) 결과에서 에러 검사
        res_output = f"{output}\n{next_result_text}".strip()
        cmd_failed = False
        cmd_err_msg = ""
        if s.get("status") == "error":
            cmd_failed = True
            cmd_err_msg = str(s.get("error") or s.get("detail") or "도구 실행 실패")[:100]
        elif res_output:
            if re.search(r"Command exited with code\s+[1-9]", res_output):
                cmd_failed = True
                m_err = re.search(r"Command output:\s*(.*)", res_output, re.DOTALL)
                cmd_err_msg = (m_err.group(1).strip() if m_err else "명령어 비정상 종료")[:100]
            elif any(err_word in res_output for err_word in ("⚠️ 세션 목록 조회 실패", "⚠️ 작업 원장 조회 실패", "API 연결 실패", "세션 생성 실패", "세션 지시 실패")):
                cmd_failed = True
                cmd_err_msg = res_output.split("\n", 1)[0][:100]

        candidates.append({
            "room_id": extracted_room_id,
            "title": extracted_title,
            "failed": cmd_failed,
            "err_msg": cmd_err_msg,
            "step_status": s.get("status") or "done",
            "step_index": idx,
        })

    if not candidates:
        return []

    # 2. DB 조회로 누락된 room_id / title 보강
    if db is not None:
        try:
            rooms = db.get_rooms(limit=100, include_hidden=True) if hasattr(db, "get_rooms") else db.list_rooms(include_hidden=True)
            for cand in candidates:
                # room_id가 없고 title이 있을 때 DB에서 정확 일치 매칭
                if not cand["room_id"] and cand["title"]:
                    clean_cand_title = cand["title"].strip()
                    cand_norm = _normalize_title(clean_cand_title)
                    for r in rooms:
                        r_id = r.get("id") or ""
                        if not _is_valid_room_id(r_id):
                            continue
                        r_title = (r.get("title") or "").strip()
                        if r_title == clean_cand_title or _normalize_title(r_title) == cand_norm:
                            cand["room_id"] = r_id
                            cand["title"] = r_title
                            break

                # room_id가 있고 title이 없거나 room_id와 같을 때 DB에서 title 보강
                if cand["room_id"] and (not cand["title"] or cand["title"] == cand["room_id"]):
                    for r in rooms:
                        if r.get("id") == cand["room_id"] and r.get("title"):
                            cand["title"] = r["title"]
                            break
        except Exception:
            pass

    # 3. 중복 제거 및 동일 작업 병합 (Deduplication & Merge)
    # 동일한 title이거나 동일한 room_id를 가진 항목들을 하나로 통합한다.
    groups: list[dict[str, Any]] = []

    for cand in candidates:
        r_id = cand["room_id"]
        title_str = cand["title"]
        norm_title = _normalize_title(title_str)

        # 기존 그룹 중 매칭되는 그룹 탐색
        matched_group = None
        for g in groups:
            # 1) room_id가 둘 다 있고 일치할 때
            if r_id and g["room_id"] and r_id == g["room_id"]:
                matched_group = g
                break
            # 2) 정규화된 title이 일치할 때 (재시도나 다른 스텝에서 room_id가 보강된 경우 포함)
            if norm_title and g["norm_title"] and norm_title == g["norm_title"]:
                matched_group = g
                break

        if matched_group is not None:
            # 병합: 더 유효한 room_id가 있으면 업데이트
            if not matched_group["room_id"] and r_id:
                matched_group["room_id"] = r_id
            if not matched_group["title"] and title_str:
                matched_group["title"] = title_str
                matched_group["norm_title"] = norm_title
            matched_group["candidates"].append(cand)
        else:
            groups.append({
                "room_id": r_id,
                "title": title_str,
                "norm_title": norm_title,
                "candidates": [cand],
            })

    # 4. 각 그룹별 상태(status) 및 사유(reason) 판정
    delegated: list[dict[str, Any]] = []

    for g in groups:
        final_rid = g["room_id"]
        final_title = g["title"] or final_rid or "하위 에이전트"

        # 오케스트레이터 ID 또는 자기 자신은 제외
        if _is_orchestrator_room_id(final_rid) or _is_orchestrator_room_id(g["title"]) or _is_orchestrator_room_id(final_title):
            continue
        # 유효한 room_id도 없고 title도 없으면 제외
        if not final_rid and not g["title"]:
            continue

        status = "done"
        reason = "정상 완료"

        # A. 런타임 실행 중 여부 확인
        if exec_mod is not None and final_rid:
            try:
                is_busy_fn = getattr(exec_mod, "is_room_busy", None)
                if callable(is_busy_fn) and is_busy_fn(final_rid):
                    status = "running"
                    reason = "백그라운드 실행 중"
            except Exception:
                pass

        # B. master_tasks 원장에서 상태 확인
        if status != "running" and tasks_mod is not None and final_rid:
            try:
                room_tasks = tasks_mod.list_tasks(include_closed=True, limit=50)
                for t in room_tasks:
                    if t.get("roomId") == final_rid:
                        t_status = t.get("status")
                        if t_status in ("queued", "running", "stalled"):
                            status = "running"
                            reason = t.get("feed") or "실행 중..."
                            break
                        elif t_status in ("failed", "lost"):
                            status = "error"
                            reason = t.get("error") or "작업 실패"
                            break
                        elif t_status == "cancelled":
                            status = "error"
                            reason = t.get("error") or "작업 취소됨"
                            break
                        elif t_status == "done":
                            status = "done"
                            reason = "정상 완료"
                            break
            except Exception:
                pass

        # C. 도구 실행 단계들의 실패 여부 반영 (모든 시도가 실패한 경우에만 error, 하나라도 성공했으면 done/running 유지)
        if status != "running":
            has_success = any(not c["failed"] for c in g["candidates"])
            if not has_success:
                status = "error"
                last_err = next((c["err_msg"] for c in reversed(g["candidates"]) if c["err_msg"]), "명령 실행 오류")
                reason = last_err

        # 안전망: final_rid가 유효한 room- 형식이 아니면 절대 roomId 필드에 문자열을 채우지 않는다!
        clean_final_rid = final_rid if _is_valid_room_id(final_rid) else ""

        delegated.append({
            "roomId": clean_final_rid,
            "title": final_title,
            "status": status,
            "reason": reason,
        })

    return delegated


async def handle_orchestrator_agents_get(request: web.Request) -> web.Response:
    """GET /api/orchestrator/agents — 전체 에이전트(마스터, 세컨브레인, 봇개발, 커스텀 에이전트) 설정 목록 반환."""
    agent_id = (request.query.get("id") or request.query.get("agentId") or "").strip()
    if agent_id:
        cfg = agent_configs.get_agent_config(agent_id)
        if not cfg:
            return web.json_response({"ok": False, "error": f"에이전트를 찾을 수 없습니다: {agent_id}"}, status=404)
        return web.json_response({"ok": True, "agent": cfg})

    agents = agent_configs.list_agent_configs()
    return web.json_response({
        "ok": True,
        "total": len(agents),
        "agents": agents,
        "orchestratorRoomIds": [a["id"] for a in agents],
    })


async def handle_orchestrator_agent_upsert(request: web.Request) -> web.Response:
    """POST /api/orchestrator/agents — 에이전트 설정 저장(기존 에이전트 수정 또는 신규 커스텀 에이전트 생성)."""
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "유효하지 않은 JSON 요청입니다."}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"ok": False, "error": "JSON 객체가 필요합니다."}, status=400)

    action = str(payload.get("action") or "upsert").strip().lower()
    if action == "reset":
        target_id = str(payload.get("id") or payload.get("agentId") or "").strip()
        try:
            saved = agent_configs.reset_builtin_agent_config(target_id)
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
    else:
        try:
            saved = agent_configs.upsert_agent_config(payload)
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    current_chat_db = _get_mod_attr("chat_db", chat_db)
    try:
        ensure_orchestrator_room(
            saved["id"],
            current_chat_db,
            model=saved.get("model"),
            effort=saved.get("effort"),
        )
        current_chat_db.update_room(
            saved["id"],
            title=saved.get("title") or saved.get("name"),
            model=saved.get("model"),
            effort=saved.get("effort"),
            workdir=saved.get("workdir"),
            parent_agent_id=saved.get("parentAgentId"),
            force_title=True,
        )
    except Exception as exc:
        logger.warning("[orchestrator] 에이전트 설정 DB 동기화 경고 (%s): %s", saved["id"], exc)

    from bot.web.execution import broadcast_ws_json
    broadcast_fn = _get_mod_attr("broadcast_ws_json", broadcast_ws_json)
    all_agents = agent_configs.list_agent_configs()
    if broadcast_fn:
        await broadcast_fn({
            "type": "agent_configs_updated",
            "agent": saved,
            "agents": all_agents,
            "orchestratorRoomIds": [a["id"] for a in all_agents],
        })

    return web.json_response({
        "ok": True,
        "agent": saved,
        "agents": all_agents,
        "orchestratorRoomIds": [a["id"] for a in all_agents],
    })


async def handle_orchestrator_agent_delete(request: web.Request) -> web.Response:
    """POST /api/orchestrator/agents/delete (또는 DELETE) — 커스텀 에이전트 삭제."""
    agent_id = request.match_info.get("agent_id", "").strip()
    payload: dict[str, Any] = {}
    try:
        payload = await request.json()
    except Exception:
        pass
    if not agent_id and isinstance(payload, dict):
        agent_id = str(payload.get("id") or payload.get("agentId") or "").strip()
    if not agent_id:
        agent_id = (request.query.get("id") or request.query.get("agentId") or "").strip()
    if not agent_id:
        return web.json_response({"ok": False, "error": "삭제할 에이전트 ID가 필요합니다."}, status=400)

    try:
        deleted = agent_configs.delete_custom_agent_config(agent_id)
    except ValueError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400)
    if not deleted:
        return web.json_response({"ok": False, "error": f"에이전트를 찾을 수 없습니다: {agent_id}"}, status=404)

    current_chat_db = _get_mod_attr("chat_db", chat_db)
    try:
        current_chat_db.delete_room(agent_id, hard_delete=True, allow_custom_agent=True)
    except Exception:
        pass

    from bot.web.execution import broadcast_ws_json, cancel_active_run
    cancel_fn = _get_mod_attr("cancel_active_run", cancel_active_run)
    if cancel_fn:
        cancel_fn(agent_id)
    broadcast_fn = _get_mod_attr("broadcast_ws_json", broadcast_ws_json)
    all_agents = agent_configs.list_agent_configs()
    if broadcast_fn:
        await broadcast_fn({
            "type": "agent_configs_updated",
            "deletedAgentId": agent_id,
            "agents": all_agents,
            "orchestratorRoomIds": [a["id"] for a in all_agents],
        })

    return web.json_response({
        "ok": True,
        "deletedAgentId": agent_id,
        "agents": all_agents,
        "orchestratorRoomIds": [a["id"] for a in all_agents],
    })


async def handle_orchestrator_agent_preview(request: web.Request) -> web.Response:
    """POST/GET /api/orchestrator/agents/preview — 에이전트 지침 및 하위 채팅 전달 프롬프트 조립 미리보기 반환."""
    if getattr(request, "method", "POST") == "POST":
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        agent_id = str(payload.get("id") or payload.get("agentId") or CORE_ORCHESTRATOR_ROOM_ID).strip()
        sample_task = str(payload.get("sampleTask") or payload.get("sample_task") or "").strip()
        if isinstance(payload.get("draft"), dict):
            overrides = payload.get("draft")
        elif isinstance(payload.get("overrides"), dict):
            overrides = payload.get("overrides")
        else:
            overrides = payload
    else:
        agent_id = (request.query.get("id") or request.query.get("agentId") or CORE_ORCHESTRATOR_ROOM_ID).strip()
        sample_task = (request.query.get("sampleTask") or request.query.get("sample_task") or "").strip()
        overrides = None

    preview = agent_configs.build_agent_prompt_preview(
        agent_id,
        sample_task=sample_task,
        overrides=overrides,
    )
    return web.json_response({"ok": True, "preview": preview, **preview})






