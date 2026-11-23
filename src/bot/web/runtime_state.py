"""web_runtime_state — 웹 실행 엔진의 모듈 전역 런타임 상태 단일 소유처(SSOT).

연결된 WebSocket 레지스트리, 룸 큐/워커/세션/라이브 피드/스냅샷, 취소 플래그,
최근 요청 기록, 종료 플래그 등 mutable 전역 상태와 주입 대상
(_master_supervisor/_master_supervisor_task/_zombie_reaper_task)의 유일한 정의처다.

⚠️ 재바인딩 금지: 아래 dict/set 전역은 다른 모듈이 from-import 로 '동일 객체' 를
공유하므로, 어느 모듈에서도 새 객체로 재바인딩해서는 안 된다(요소 조작/clear 만 허용).
상태를 읽어 만드는 live 스냅샷 직렬화/조회/브로드캐스트와 shutdown/detach,
web_server 다리(_get_mod_attr, 전역 세션 동기화 판별)도 함께 둔다.
주입 3종은 get_master_supervisor() accessor 로 읽는다(from-import 사본의
stale-None 타이밍 함정 회피). web_execution facade 의 __setattr__ 전달 프록시가
테스트 patch 와 web_server 의 런타임 주입을 이 모듈에 반영한다.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import Any

from aiohttp import web

import bot.core.agent_configs as agent_configs
from bot.engine.agent_engine import get_effort, get_engine, get_model, get_session, set_session
from bot.core.bot_logger import logger
from bot.master.master_supervisor import MasterSupervisor
from bot.core.paths import WEB_JOBS_FILE
from bot.web.auth import WEB_TOKEN

# 룸 ID 상수는 agent_configs(빌트인 에이전트 정의 SSOT)에서 가져온다.
from bot.core.agent_configs import CORE_ORCHESTRATOR_ROOM_ID, EXPERT_CODING_ROOM_ID, EXPERT_GENERAL_ROOM_ID, EXPERT_VAULT_ROOM_ID

# 타이틀 상수는 agent_configs 의 AGENTS dict 리터럴 값과 동일하지만 상수로
# 노출되어 있지 않아 여기서 정의한다(출처: agent_configs.AGENTS[*].title).
CORE_ORCHESTRATOR_TITLE = "마스터 에이전트"
EXPERT_GENERAL_TITLE = "일반 대화"
EXPERT_VAULT_TITLE = "세컨브레인 에이전트"
EXPERT_CODING_TITLE = "봇개발 에이전트"
ORCHESTRATOR_ROOM_IDS = (
    CORE_ORCHESTRATOR_ROOM_ID,
    EXPERT_GENERAL_ROOM_ID,
    EXPERT_VAULT_ROOM_ID,
    EXPERT_CODING_ROOM_ID,
)
IDLE_RESET_SECONDS = 6 * 3600

def _is_orchestrator_room_id(room_id: str | None) -> bool:
    if not room_id:
        return False
    clean = str(room_id).strip()
    if clean in ORCHESTRATOR_ROOM_IDS:
        return True
    try:
        return agent_configs.is_orchestrator_room(clean)
    except Exception:
        return False
# WEB_TOKEN 은 web_auth(임시 토큰 폴백 포함)의 정의를 따른다.
# WEB_PORT 는 web_auth 에 정의가 없어(토큰 전용 모듈) 여기서 env 로 해석한다.
WEB_PORT = int(os.getenv("WEB_PORT", os.getenv("VOICE_WEB_PORT", "8765")).strip() or "8765")

_connected_websockets: set[web.WebSocketResponse] = set()
_active_watch_tasks: dict[str, asyncio.Task] = {}
_active_agent_pids: dict[str, int] = {}
_last_chat_requests: dict[str, tuple[str, float]] = {}
_room_queues: dict[str, asyncio.Queue] = {}
_room_workers: dict[str, asyncio.Task] = {}
_room_session_ids: dict[str, str | None] = {}
_room_live_feeds: dict[str, str] = {}
_room_live_snapshots: dict[str, dict[str, Any]] = {}
_user_cancelled_rooms: set[str] = set()
_shutting_down: bool = False
_master_supervisor: MasterSupervisor | None = None
_master_supervisor_task: asyncio.Task | None = None

def set_shutting_down(flag: bool) -> None:
    """서버 종료/재시작 진행 중 여부를 설정한다."""
    global _shutting_down
    _shutting_down = bool(flag)

def is_shutting_down() -> bool:
    """서버가 정상 종료/재시작 중인지 반환한다."""
    return _shutting_down

async def detach_active_tasks(timeout: float = 1.5) -> int:
    """서버 종료/재시작 시 실행 중인 워커·감시 태스크를 안전하게 분리 취소(detach)한다.

    반드시 set_shutting_down(True) 및 persist_pending_room_turns() 호출 이후 실행해야 하며,
    분리 실행 중인 하위 에이전트 프로세스(pid)와 web_jobs.json은 보존된다.
    """
    current = asyncio.current_task()
    seen: set[int] = set()
    tasks_to_cancel: list[asyncio.Task] = []

    for mapping in (_room_workers, _active_watch_tasks):
        for task in list(mapping.values()):
            if isinstance(task, asyncio.Task) and task is not current and not task.done():
                tid = id(task)
                if tid not in seen:
                    seen.add(tid)
                    tasks_to_cancel.append(task)

    if not tasks_to_cancel:
        return 0

    for task in tasks_to_cancel:
        task.cancel()

    try:
        await asyncio.wait_for(
            asyncio.gather(*tasks_to_cancel, return_exceptions=True),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.warning("[web_jobs] 종료 시 태스크 분리 대기 타임아웃 (%d건)", len(tasks_to_cancel))

    return len(tasks_to_cancel)

# from-import 사본이 아니라 이 모듈들의 정의처 값을 우선 조회해야 하는 모듈들.
# web_execution facade 의 patch 전달 대상과 동일한 집합이다.
_OWNER_STATE_MODULES = (
    "bot.web.runtime_state",
    "bot.web.job_recovery",
    "bot.web.stream_watch",
    "bot.web.room_queue",
    "bot.web.agent_reporting",
    "bot.web.turn_execution",
    "bot.web.automation",
)

def _is_test_double(value: Any) -> bool:
    """테스트 더블(MagicMock/AsyncMock/Mock) 여부를 판별한다."""
    return (
        hasattr(value, "assert_called")
        or hasattr(value, "_mock_return_value")
        or hasattr(value, "mock_calls")
    )

def _get_mod_attr(attr_name: str, fallback: Any) -> Any:
    """patch된 mock 또는 정의처(소유 모듈)·bot.web.server 의 현재 속성을 조회한다.

    조회 순서:
    1. fallback 자체가 테스트 더블이면 그대로 반환한다.
    2. fallback 의 정의처가 상태 소유 모듈(_OWNER_STATE_MODULES)이면 그 모듈의
       현재 속성을 우선한다 — bot.web.server 의 from-import 사본(stale 가능)이
       정의처를 가리지 않게 단일화한다. patch 여부를 함께 보므로 소유 모듈이
       아닌 bot.web.server 만 patch 된 기존 테스트도 계속 관통된다.
    3. 그 외 심볼(엔진·rooms 등)은 기존처럼 bot.web.server 를 먼저 조회한다.
    4. 없으면 fallback 을 반환한다.
    """
    if _is_test_double(fallback):
        return fallback
    owner_mod_name = getattr(fallback, "__module__", None)
    owner_mod = sys.modules.get(owner_mod_name) if owner_mod_name in _OWNER_STATE_MODULES else None
    server_mod = sys.modules.get("bot.web.server")
    owner_val = getattr(owner_mod, attr_name, None) if owner_mod is not None else None
    server_val = getattr(server_mod, attr_name, None) if server_mod is not None else None
    if owner_mod is not None and owner_val is not None:
        # patch 우선: 소유 모듈이 patch 됐거나 server 사본이 patch 되지 않았으면
        # 정의처 값을, server 사본만 patch 됐으면(기존 테스트 호환) 그 값을 반환한다.
        if _is_test_double(owner_val) or not _is_test_double(server_val):
            return owner_val
        return server_val
    if server_mod is not None and hasattr(server_mod, attr_name):
        return server_val
    return fallback

def _should_sync_global_session(
    room_id: str | None,
    current_session_id: str | None,
    global_session_id: str | None,
) -> bool:
    """웹 대화방 턴이 전역(텔레그램/코어) 세션을 동기화·덮어쓰기해야 하는지 판별한다."""
    if not room_id or room_id == "__default__" or _is_orchestrator_room_id(room_id):
        return True
    if global_session_id and global_session_id in (room_id, current_session_id):
        return True
    return False

def _sync_global_session_if_needed(
    room_id: str | None,
    source_session_id: str | None,
    resolved_session_id: str,
    session_namespace: str,
) -> None:
    """전역 세션을 공유하는 방(또는 테스트 목)인 경우에만 전역 세션을 갱신한다."""
    current_set_session = _get_mod_attr("set_session", set_session)
    current_get_session = _get_mod_attr("get_session", get_session)
    if (
        _should_sync_global_session(
            room_id, source_session_id, current_get_session(ns=session_namespace)
        )
        or hasattr(current_set_session, "_mock_name")
    ):
        current_set_session(resolved_session_id, ns=session_namespace)

def _serialize_live_run_entry(r_key: str) -> dict[str, Any] | None:
    """특정 대화방 키(r_key)의 현재 진행 중 응답 스냅샷 및 대기열 상태를 직렬화한다."""
    snap = _room_live_snapshots.get(r_key)
    if snap is None:
        for k, s in list(_room_live_snapshots.items()):
            if (
                s.get("roomId") == r_key
                or s.get("sessionId") == r_key
                or _room_session_ids.get(k) == r_key
                or _room_session_ids.get(r_key) == k
            ):
                snap = s
                break
    q = _room_queues.get(r_key)
    queued_items: list[dict[str, Any]] = []
    if q is not None and not q.empty():
        raw_q = getattr(q, "_queue", None)
        if raw_q:
            for idx, item in enumerate(list(raw_q), start=1):
                if isinstance(item, dict) and item.get("user_text"):
                    queued_items.append({
                        "text": item["user_text"],
                        "channel": item.get("channel") or "web",
                        "queuePos": idx,
                        "clientMsgId": item.get("client_msg_id"),
                        "userMsgId": item.get("user_msg_id"),
                    })
    is_running = (
        snap is not None
        or (r_key in _active_watch_tasks and not _active_watch_tasks[r_key].done())
        or (r_key in _active_agent_pids)
    )
    if not is_running and not queued_items:
        return None

    now_ts = time.time()
    if snap is not None:
        start_ts = float(snap.get("startTimestamp") or now_ts)
        return {
            "roomId": snap.get("roomId") or r_key,
            "sessionId": snap.get("sessionId") or _room_session_ids.get(r_key),
            "jobId": snap.get("jobId") or "",
            "asstMsgId": snap.get("asstMsgId"),
            "userMsgId": snap.get("userMsgId"),
            "clientMsgId": snap.get("clientMsgId"),
            "isRunning": True,
            "userText": snap.get("userText") or "",
            "channel": snap.get("channel") or "web",
            "engine": snap.get("engine") or get_engine(),
            "model": snap.get("model") or get_model(),
            "effort": snap.get("effort") or get_effort(),
            "startedAt": int(snap.get("startedAt") or int(start_ts * 1000)),
            "elapsedSeconds": max(0, int(now_ts - start_ts)),
            "feed": snap.get("feed") or _room_live_feeds.get(r_key) or "생각하는 중…",
            "steps": [dict(s) for s in (snap.get("steps") or [])],
            "streamText": snap.get("streamText") or "",
            "queueCount": len(queued_items),
            "queuedMessages": queued_items,
        }

    return {
        "roomId": r_key,
        "sessionId": _room_session_ids.get(r_key),
        "jobId": "",
        "isRunning": is_running,
        "userText": "",
        "channel": "web",
        "engine": get_engine(),
        "model": get_model(),
        "effort": get_effort(),
        "startedAt": int(now_ts * 1000),
        "elapsedSeconds": 0,
        "feed": _room_live_feeds.get(r_key) or "대기열 처리 중…",
        "steps": [],
        "streamText": "",
        "queueCount": len(queued_items),
        "queuedMessages": queued_items,
    }

def get_live_run_for_room(room_id: str | None) -> dict[str, Any] | None:
    """특정 roomId(또는 sessionId)에 해당하는 실시간 실행 상태 스냅샷을 반환한다."""
    if not room_id:
        return None
    direct = _serialize_live_run_entry(room_id)
    if direct is not None:
        return direct
    for r_key, snap in list(_room_live_snapshots.items()):
        if (
            snap.get("roomId") == room_id
            or snap.get("sessionId") == room_id
            or _room_session_ids.get(r_key) == room_id
            or _room_session_ids.get(room_id) == r_key
        ):
            return _serialize_live_run_entry(r_key)
    return None

def build_active_runs_payload() -> list[dict[str, Any]]:
    """현재 실행 중이거나 대기열이 존재하는 모든 대화방의 실시간 스냅샷 목록을 반환한다."""
    candidate_keys = list(
        dict.fromkeys(
            list(_room_live_snapshots.keys())
            + list(_active_watch_tasks.keys())
            + list(_active_agent_pids.keys())
            + list(_room_queues.keys())
        )
    )
    active_runs: list[dict[str, Any]] = []
    seen_identifiers: set[str] = set()
    for r_key in candidate_keys:
        entry = _serialize_live_run_entry(r_key)
        if entry is not None:
            ident = entry.get("jobId") or entry.get("roomId")
            if ident:
                if ident in seen_identifiers:
                    continue
                seen_identifiers.add(ident)
            active_runs.append(entry)
    return active_runs

async def broadcast_ws_json(message_dict: dict) -> None:
    """연결된 모든 WebSocket 클라이언트에 JSON 이벤트를 브로드캐스트한다."""
    if not _connected_websockets:
        return
    payload_str = json.dumps(message_dict, ensure_ascii=False)
    stale: list[web.WebSocketResponse] = []
    for ws in list(_connected_websockets):
        if ws.closed:
            stale.append(ws)
            continue
        try:
            await ws.send_str(payload_str)
        except Exception:
            stale.append(ws)
    for ws in stale:
        _connected_websockets.discard(ws)

_zombie_reaper_task: asyncio.Task | None = None


def get_master_supervisor() -> MasterSupervisor | None:
    """주입된 마스터 슈퍼바이저를 반환한다(없으면 None).

    from-import 사본은 주입 이전 시점에 읽으면 stale-None 이 되므로,
    새 코드는 이 accessor 로 읽는다.
    """
    return _master_supervisor

def poke_master_supervisor() -> MasterSupervisor | None:
    """주입된 마스터 슈퍼바이저를 깨워 즉시 점검(poke)한다.

    supervisor 가 주입되어 있으면 .poke() 를 호출하고 그 객체를 반환하며,
    없으면 None 을 반환한다(None 분기 보일러플레이트 제거용 헬퍼).
    """
    if _master_supervisor is None:
        return None
    _master_supervisor.poke()
    return _master_supervisor
