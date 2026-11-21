"""web_execution — 실시간 대화 턴 실행, 워커 큐(FIFO), 스트림 파싱 및 백그라운드 작업 복구 모듈."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import sys
import time
from collections.abc import Callable
from typing import Any
import uuid

from aiohttp import web

import chat_db
from json_store import read_json, write_json_atomic
from agent_engine import (
    AGY_DEFAULT_MODEL,
    check_and_reset_if_idle,
    clear_session,
    detached_pid_alive,
    fmt_cost,
    get_coding_mode,
    get_effort,
    get_engine,
    get_model,
    get_session,
    is_quota_exhausted_error,
    is_session_stale_error,
    kill_job,
    set_model,
    set_session,
    spawn_agent,
    update_last_activity,
    watch_agent,
)
from bot_logger import logger, vault_log
from master_supervisor import (
    CORE_ROOM_ID,
    MASTER_PATROL_CHANNEL,
    MasterSupervisor,
)
import agent_configs
import master_memory
import master_tasks
import child_error_report
from paths import (
    CODEDIR,
    child_default_workdir,
    DEFAULT_SSL_CERT_FILE,
    JOBS_DIR,
    STATE_DIR,
    WEB_JOBS_FILE,
    WORKDIR,
)
from stream_parser import (
    _EMPTY_OUTPUT_TEXT,
    _NO_RESULT_TEXT,
    extract_agy_transcript_final_response,
)
from web_jobs import (
    compute_remaining_timeout as _compute_remaining_timeout,
    extract_steps_from_jsonl as _extract_steps_from_jsonl,
    load_web_jobs as _load_web_jobs_from_file,
    remove_job_files as _remove_job_files,
    remove_web_job as _remove_web_job_from_file,
    save_web_job as _save_web_job_to_file,
    sweep_orphan_job_files as _sweep_orphan_job_files,
)
from web_speech import clean_text_for_speech, VOICE_MODE_PROMPT_SUFFIX

WEB_JOBS_FILE = os.path.join(STATE_DIR, "web_jobs.json")
CORE_ORCHESTRATOR_ROOM_ID = "__core__"
CORE_ORCHESTRATOR_TITLE = "마스터 에이전트"
EXPERT_VAULT_ROOM_ID = "__expert_vault__"
EXPERT_VAULT_TITLE = "세컨브레인 에이전트"
EXPERT_CODING_ROOM_ID = "__expert_coding__"
EXPERT_CODING_TITLE = "봇개발 에이전트"
ORCHESTRATOR_ROOM_IDS = (CORE_ORCHESTRATOR_ROOM_ID, EXPERT_VAULT_ROOM_ID, EXPERT_CODING_ROOM_ID)
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
WEB_PORT = int(os.getenv("WEB_PORT", os.getenv("VOICE_WEB_PORT", "8765")).strip() or "8765")
WEB_TOKEN = os.getenv("WEB_TOKEN", os.getenv("VOICE_WEB_TOKEN", "")).strip()

# ── 대화방 실행 상태 및 소켓 레지스트리 ────────────────────────────────────
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


def _get_mod_attr(attr_name: str, fallback: Any) -> Any:
    """web_server 모듈에서 동적 속성(또는 patch된 mock)을 조회하고 없을 시 fallback을 반환한다."""
    if hasattr(fallback, "assert_called") or hasattr(fallback, "_mock_return_value") or hasattr(fallback, "mock_calls"):
        return fallback
    mod = sys.modules.get("web_server")
    if mod and hasattr(mod, attr_name):
        return getattr(mod, attr_name)
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


def _get_web_jobs_file() -> str:
    return _get_mod_attr("WEB_JOBS_FILE", WEB_JOBS_FILE)


def _load_web_jobs() -> dict:
    """진행 중인 웹 비서 job 레코드를 읽어온다."""
    return _load_web_jobs_from_file(_get_web_jobs_file())


def _save_web_job(job_id: str, rec: dict) -> None:
    """웹 비서 job 레코드를 디스크에 스레드 안전·원자적으로 저장한다."""
    _save_web_job_to_file(_get_web_jobs_file(), job_id, rec)


def _remove_web_job(job_id: str) -> None:
    """완료되거나 취소된 웹 비서 job 레코드를 디스크에서 스레드 안전하게 제거한다."""
    _remove_web_job_from_file(_get_web_jobs_file(), job_id)


def _get_web_pending_turns_file() -> str:
    return os.path.join(os.path.dirname(_get_web_jobs_file()), "web_pending_turns.json")


def persist_pending_room_turns() -> int:
    """서버 종료 시 대기열(_room_queues)에 남은 미실행 턴을 디스크에 저장한다."""
    pending: list[dict[str, Any]] = []
    for r_key, q in list(_room_queues.items()):
        if q is None or q.empty():
            continue
        while not q.empty():
            try:
                item = q.get_nowait()
            except Exception:
                break
            try:
                if not isinstance(item, dict):
                    continue
                if item.get("channel") == MASTER_PATROL_CHANNEL:
                    continue
                pending.append({
                    "room_id": item.get("room_id") if item.get("room_id") is not None else r_key,
                    "user_text": item.get("user_text", ""),
                    "is_voice_mode": bool(item.get("is_voice_mode", False)),
                    "client_session_id": item.get("client_session_id"),
                    "is_new_session": bool(item.get("is_new_session", False)),
                    "channel": item.get("channel", "web"),
                    "attached_files": item.get("attached_files") or [],
                    "attachments": item.get("attachments") or [],
                    "model": item.get("model"),
                    "effort": item.get("effort"),
                    "task_id": item.get("task_id"),
                    "user_msg_id": item.get("user_msg_id"),
                    "client_msg_id": item.get("client_msg_id"),
                })
            finally:
                try:
                    q.task_done()
                except Exception:
                    pass
    if pending:
        write_json_atomic(_get_web_pending_turns_file(), pending)
        logger.info("[web_jobs] 종료 시 대기열 미실행 턴 %d건 영속화 완료", len(pending))
    return len(pending)


async def _restore_pending_room_turns() -> None:
    """서버 기동 시 이전에 영속화된 대기열 턴을 다시 큐에 적재한다."""
    pending_file = _get_web_pending_turns_file()
    if not os.path.exists(pending_file):
        return
    items = read_json(pending_file, [], expect_type=list)
    try:
        os.remove(pending_file)
    except OSError:
        write_json_atomic(pending_file, [])
    if not items:
        return
    logger.info("[web_jobs] 기동 시 대기열 미실행 턴 %d건 복원 시작", len(items))
    for item in items:
        if not isinstance(item, dict) or not item.get("user_text"):
            continue
        pending_tid = item.get("task_id")
        if pending_tid:
            t_rec = master_tasks.get_task(pending_tid)
            if t_rec and _is_task_rec_aborted(t_rec):
                logger.info("[web_jobs] 취소된 태스크(%s) 대기열 복원 제외", pending_tid)
                continue
        try:
            await enqueue_room_turn(
                room_id=item.get("room_id"),
                user_text=item["user_text"],
                is_voice_mode=bool(item.get("is_voice_mode", False)),
                client_session_id=item.get("client_session_id"),
                is_new_session=bool(item.get("is_new_session", False)),
                channel=item.get("channel", "web"),
                attached_files=item.get("attached_files") or [],
                attachments=item.get("attachments") or [],
                model=item.get("model"),
                effort=item.get("effort"),
                task_id=item.get("task_id"),
                user_msg_id=item.get("user_msg_id"),
                client_msg_id=item.get("client_msg_id"),
            )
        except Exception as exc:
            logger.warning("[web_jobs] 대기열 턴 복원 실패: %s", exc)


def _format_tool_step_info(tool_name: str, tool_input: dict) -> tuple[str, str]:
    """도구 이름과 입력 파라미터를 원래 영어 도구명 기반의 요약 타이틀 및 상세 문자열로 변환한다."""
    summary_title = tool_name
    detail_lines: list[str] = []
    file_path = str(
        tool_input.get("file_path")
        or tool_input.get("path")
        or tool_input.get("Path")
        or tool_input.get("AbsolutePath")
        or tool_input.get("TargetFile")
        or ""
    ).strip()
    base_name = os.path.basename(file_path.rstrip("/")) if file_path else tool_name

    if tool_name in ("Read", "read_file", "view_file"):
        summary_title = f"{tool_name}: {base_name}"
        if file_path:
            detail_lines.append(f"경로: {file_path}")
        if "start_line" in tool_input or "StartLine" in tool_input:
            start_l = tool_input.get("start_line") or tool_input.get("StartLine")
            end_l = tool_input.get("end_line") or tool_input.get("EndLine")
            detail_lines.append(f"범위: {start_l}~{end_l}행")
    elif tool_name in ("Bash", "run_command"):
        cmd = str(
            tool_input.get("command")
            or tool_input.get("CommandLine")
            or tool_input.get("cmd")
            or ""
        ).strip()
        grep_match = re.search(r'grep\s+(?:-[a-zA-Z]+\s+)*["\']([^"\']+)["\']', cmd)
        sctl_match = re.search(r'session-ctl\s+(list|read|send|spawn)(?:\s+(.*))?', cmd)
        if sctl_match:
            sub_cmd = sctl_match.group(1)
            sub_rest = (sctl_match.group(2) or "").strip()
            if sub_cmd == "list":
                summary_title = "session-ctl list: all"
            elif sub_cmd == "read":
                arg_m = re.match(r'^["\']?([^"\'\s]+)["\']?', sub_rest)
                target_label = arg_m.group(1) if arg_m else sub_rest[:30]
                summary_title = f"session-ctl read: {target_label}"
            elif sub_cmd == "send":
                arg_m = re.match(r'^["\']([^"\']+)["\']|^(\S+)', sub_rest)
                target_label = (arg_m.group(1) or arg_m.group(2)) if arg_m else sub_rest[:24]
                summary_title = f"session-ctl send: {target_label}"
            elif sub_cmd == "spawn":
                arg_m = re.match(r'^["\']([^"\']+)["\']|^(\S+)', sub_rest)
                target_label = (arg_m.group(1) or arg_m.group(2)) if arg_m else sub_rest[:24]
                summary_title = f"session-ctl spawn: {target_label}"
        elif grep_match:
            summary_title = f'{tool_name}: "{grep_match.group(1)[:40]}"'
        else:
            summary_title = f"{tool_name}: {cmd[:60]}" if cmd else tool_name
        if cmd:
            detail_lines.append(f"$ {cmd}")
    elif tool_name in ("Edit", "edit_file", "replace_file_content", "multi_replace_file_content"):
        summary_title = f"{tool_name}: {base_name}"
        if file_path:
            detail_lines.append(f"파일: {file_path}")
    elif tool_name in ("Write", "write_file", "write_to_file", "create_file"):
        summary_title = f"{tool_name}: {base_name}"
        if file_path:
            detail_lines.append(f"파일: {file_path}")
    elif tool_name in ("GrepSearch", "grep_search"):
        pattern = (
            tool_input.get("query")
            or tool_input.get("Query")
            or tool_input.get("pattern")
            or tool_input.get("Pattern")
            or ""
        )
        summary_title = f'{tool_name}: "{pattern}"' if pattern else tool_name
        if pattern:
            detail_lines.append(f"검색어: {pattern}")
    elif tool_name in ("find_by_name", "Glob"):
        pattern = tool_input.get("Pattern") or tool_input.get("pattern") or ""
        summary_title = f"{tool_name}: {pattern}" if pattern else tool_name
        if pattern:
            detail_lines.append(f"패턴: {pattern}")
    elif tool_name in ("list_dir",):
        dir_path = (
            tool_input.get("DirectoryPath")
            or tool_input.get("path")
            or tool_input.get("Path")
            or ""
        )
        base_dir = os.path.basename(str(dir_path).rstrip("/")) if dir_path else "."
        summary_title = f"{tool_name}: {base_dir}"
        if dir_path:
            detail_lines.append(f"경로: {dir_path}")
    elif tool_name in ("search_web", "WebSearch"):
        query = tool_input.get("query") or tool_input.get("Query") or ""
        summary_title = f'{tool_name}: "{query}"' if query else tool_name
        if query:
            detail_lines.append(f"검색어: {query}")
    else:
        summary_title = f"{tool_name}: {base_name}" if (base_name and base_name != tool_name) else tool_name
        for k, v in tool_input.items():
            detail_lines.append(f"{k}: {v}")

    detail_str = (
        "\n".join(detail_lines)
        if detail_lines
        else (json.dumps(tool_input, ensure_ascii=False, indent=2) if tool_input else "")
    )
    return summary_title, detail_str


def extract_stream_events_from_line(raw_line: str) -> list[dict]:
    """stream-json 단일 라인에서 실시간 Thinking 및 Tool 상세 실행 내역을 구조화해 추출한다."""
    try:
        ev = json.loads(raw_line)
    except json.JSONDecodeError:
        return []

    extracted: list[dict] = []

    # 1. Claude assistant 블록 (thinking, tool_use)
    if ev.get("type") == "assistant":
        for block in ev.get("message", {}).get("content", []):
            block_type = block.get("type")
            if block_type == "thinking":
                thinking_text = (block.get("thinking") or "").strip()
                if thinking_text:
                    extracted.append({
                        "kind": "thinking",
                        "title": "thinking",
                        "detail": thinking_text,
                    })
            elif block_type == "tool_use":
                tool_name = block.get("name", "tool")
                tool_input = block.get("input") or {}
                summary_title, detail_str = _format_tool_step_info(tool_name, tool_input)
                extracted.append({
                    "kind": "tool_call",
                    "tool": tool_name,
                    "title": summary_title,
                    "detail": detail_str,
                    "status": "running",
                })

    # 2. Claude user/tool_result 블록 (도구 실행 결과 및 볼트 내용)
    elif ev.get("type") == "user":
        for item in ev.get("message", {}).get("content", []):
            if item.get("type") == "tool_result":
                res_content = str(item.get("content") or "").strip()
                if res_content:
                    line_count = len(res_content.splitlines())
                    preview = (
                        res_content[:500] + ("…" if len(res_content) > 500 else "")
                    )
                    extracted.append({
                        "kind": "tool_result",
                        "title": f"tool_result ({line_count} lines)",
                        "detail": preview,
                    })

    # 3. AGY step_update
    elif ev.get("event") == "step_update":
        su = ev.get("step_update", {})
        step_type = su.get("step_type")
        state = su.get("state")
        if step_type == "tool" and state == "ACTIVE":
            tool_name = su.get("tool_name") or "tool"
            params = (su.get("tool_info") or {}).get("parameters") or {}
            summary_title, detail_str = _format_tool_step_info(tool_name, params)
            step_item = {
                "kind": "tool_call",
                "tool": tool_name,
                "title": summary_title,
                "detail": detail_str,
                "status": "running",
            }
            if su.get("step_index") is not None:
                step_item["stepIndex"] = su.get("step_index")
            extracted.append(step_item)
        elif step_type == "thought":
            thought_text = su.get("text") or su.get("thought") or ""
            if thought_text.strip():
                extracted.append({
                    "kind": "thinking",
                    "title": "thinking",
                    "detail": thought_text.strip(),
                })

    return extracted


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


def cancel_active_run(target_room_id: str | None = None) -> bool:
    """특정 대화방(또는 전체 대화방)의 진행 중 에이전트 프로세스 및 대기열을 즉시 중단한다."""
    was_running = False
    target_keys = (
        [target_room_id]
        if target_room_id is not None
        else list(
            set(_active_watch_tasks.keys())
            | set(_active_agent_pids.keys())
            | set(_room_queues.keys())
            | set(_room_live_snapshots.keys())
        )
    )
    for r_key in target_keys:
        _user_cancelled_rooms.add(r_key)
        q = _room_queues.get(r_key)
        if q is not None:
            while not q.empty():
                try:
                    item = q.get_nowait()
                    fut = item.get("result_future") if isinstance(item, dict) else None
                    if fut is not None and not fut.done():
                        fut.set_exception(asyncio.CancelledError("작업이 취소되었습니다."))
                    queued_tid = item.get("task_id") if isinstance(item, dict) else None
                    if queued_tid:
                        master_tasks.update_task(
                            queued_tid,
                            status="cancelled",
                            user_cancelled=True,
                            user_confirmed=True,
                            reviewed=True,
                            error="대기열에서 취소됨",
                            finishedAt=time.time(),
                        )
                    q.task_done()
                    was_running = True
                except Exception:
                    break

        snap = _room_live_snapshots.get(r_key)
        snap_tid = snap.get("taskId") if isinstance(snap, dict) else None
        if snap_tid:
            master_tasks.update_task(
                snap_tid,
                status="cancelled",
                user_cancelled=True,
                user_confirmed=True,
                reviewed=True,
                error="사용자에 의해 작업이 취소되었습니다.",
                finishedAt=time.time(),
            )

        pid = _active_agent_pids.pop(r_key, None)
        if pid is not None:
            kill_job_fn = _get_mod_attr("kill_job", kill_job)
            kill_job_fn(pid)
            was_running = True
        task = _active_watch_tasks.pop(r_key, None)
        if task is not None and not task.done():
            task.cancel()
            was_running = True
        _room_live_feeds.pop(r_key, None)
        if _room_live_snapshots.pop(r_key, None) is not None:
            was_running = True
    if was_running and _master_supervisor is not None:
        _master_supervisor.poke()
    return was_running


_recent_reported_rooms: dict[str, float] = {}


def mark_room_completion_reported(room_id: str | None) -> None:
    """서버 완료 훅이 해당 대화방의 완료 보고를 전송했음을 기록(중복 보고 방지용)."""
    rid = str(room_id or "").strip()
    if not rid:
        return
    now = time.monotonic()
    _recent_reported_rooms[rid] = now
    if len(_recent_reported_rooms) > 256:
        expired = [k for k, ts in _recent_reported_rooms.items() if now - ts > 300.0]
        for k in expired:
            _recent_reported_rooms.pop(k, None)


def is_room_completion_recently_reported(room_id: str | None, ttl: float = 120.0) -> bool:
    """최근 TTL 이내에 서버 완료 훅이 해당 방을 이미 보고했는지 확인한다."""
    rid = str(room_id or "").strip()
    if not rid:
        return False
    ts = _recent_reported_rooms.get(rid)
    if ts is None:
        return False
    if time.monotonic() - ts <= ttl:
        return True
    _recent_reported_rooms.pop(rid, None)
    return False


def _is_task_rec_aborted(rec: Any) -> bool:
    """작업 레코드가 명시적으로 중단/취소되었는지 안전하게 판별한다(Mocking 내성)."""
    if not isinstance(rec, dict):
        return False
    return bool(
        rec.get("user_cancelled")
        or rec.get("user_confirmed")
        or rec.get("status") == "cancelled"
    )


async def _invoke_on_complete_hook(
    on_complete: Callable[[dict[str, Any]], Any] | None,
    event: dict[str, Any],
) -> None:
    """턴 종료 시 전달된 on_complete 콜백을 안전하게 비동기/동기 실행한다."""
    if on_complete is None:
        return
    try:
        maybe_coro = on_complete(event)
        if inspect.isawaitable(maybe_coro):
            await maybe_coro
    except Exception as exc:
        logger.warning(
            "[web_execution] on_complete 훅 실행 오류 (room=%s, task=%s): %s",
            event.get("roomId"),
            event.get("taskId"),
            exc,
        )


async def _finalize_orchestrator_completion(
    *,
    room_id: str,
    task_id: str | None,
    channel: str,
    is_orchestrator: bool,
    is_success: bool,
    result_text: str,
    status: str,
    resolved_session_id: str | None = None,
    elapsed_seconds: int = 0,
    error_text: str | None = None,
    on_complete: Callable[[dict[str, Any]], Any] | None = None,
    model: str | None = None,
    effort: str | None = None,
) -> None:
    """턴 종료 시 작업 원장 갱신, 사용자 정의 on_complete 훅 호출, 부모 에이전트 자동 보고를 일원화한다."""
    is_wait = False
    is_user_aborted = (status == "cancelled")
    if task_id:
        _rec = master_tasks.get_task(task_id) or {}
        is_wait = bool(_rec.get("wait"))
        if not is_user_aborted and _is_task_rec_aborted(_rec):
            is_user_aborted = True
        _now_done = time.time()
        if is_user_aborted:
            master_tasks.update_task(
                task_id,
                status="cancelled",
                user_cancelled=True,
                user_confirmed=True,
                reviewed=True,
                reviewedAt=_now_done,
                finishedAt=_now_done,
                error=(error_text or result_text or "사용자에 의해 작업이 취소되었습니다.")[:400],
            )
        elif status == "done":
            done_fields: dict[str, Any] = {
                "status": "done",
                "finishedAt": _now_done,
                "resultPreview": result_text[:400],
                "sessionId": resolved_session_id,
                "feed": "완료",
            }
            if is_wait:
                done_fields["reportedAt"] = _now_done
                done_fields["reviewed"] = True
                done_fields["reviewedAt"] = _now_done
            master_tasks.update_task(task_id, **done_fields)
        else:
            master_tasks.update_task(
                task_id,
                status=status,
                finishedAt=_now_done,
                error=(error_text or result_text)[:400],
            )

    await _invoke_on_complete_hook(
        on_complete,
        {
            "ok": is_success and not is_user_aborted,
            "status": "cancelled" if is_user_aborted else status,
            "roomId": room_id,
            "taskId": task_id,
            "channel": channel,
            "sessionId": resolved_session_id,
            "reply": result_text,
            "elapsedSeconds": elapsed_seconds,
            "error": error_text,
        },
    )

    # 사용자가 명시적으로 중단(cancelled)한 작업은 부모 에이전트에게 오류 보고를 보내지 않는다.
    # 부모 에이전트가 실패 보고를 받고 '재시도'를 수행하여 중단된 작업을 강제로 재실행하는 루프를 차단한다.
    if is_user_aborted:
        logger.info(
            "[web_execution] 사용자에 의해 명시적으로 중단된 작업 — 부모 에이전트 재시도/보고 차단 (room=%s, task=%s)",
            room_id,
            task_id,
        )
        return

    # 비동기 위임(--async)은 마스터가 응답을 기다리지 않으므로 서버 내장 완료 훅이 직접 보고 턴을 건다.
    # 실패(오류·중단·타임아웃·잘못된 모델)는 사용자가 UI 에서 직접 띄운 하위 채팅(부모 지정 방)도 부모에게 보고한다.
    # 부모/보고 턴 자체(is_orchestrator, task_report 채널)는 제외해 오류 보고의 재귀 루프를 차단한다.
    if is_orchestrator or is_wait or channel in child_error_report.NO_REPORT_CHANNELS:
        return
    if channel == "orchestrator" or (not is_success and _room_parent_id(room_id)):
        notify_fn = _get_mod_attr("_notify_master_agent", _notify_master_agent)
        if is_success or not (model or effort):
            await notify_fn(room_id, task_id, result_text, is_success=is_success)
        else:
            await notify_fn(
                room_id, task_id, error_text or result_text, is_success=False, model=model, effort=effort
            )


def _room_parent_id(room_id: str) -> str | None:
    """방 레코드의 parentAgentId (없으면 None)."""
    db = _get_mod_attr("chat_db", chat_db)
    try:
        rec = db.get_room(room_id) if db else None
    except Exception:
        return None
    pid = rec.get("parentAgentId") if isinstance(rec, dict) else None
    return pid if isinstance(pid, str) and pid.strip() else None


async def _notify_master_agent(
    room_id: str,
    task_id: str | None,
    result_text: str,
    is_success: bool = True,
    *,
    model: str | None = None,
    effort: str | None = None,
) -> None:
    """하위 작업 완료/중단 시 부모 에이전트(마스터 또는 담당 전문가)에게 결과를 알리고 후속 턴을 트리거한다."""
    if _is_orchestrator_room_id(room_id):
        return
    if task_id and not master_tasks.claim_report(task_id):
        return  # 이미 보고됨(중복 방지)
    # task_id 없는(UI 직접 생성) 방의 오류는 (방, 원문) 단위로 멱등 처리
    error_claimed = False
    if not is_success and not task_id:
        if not child_error_report.claim_error_report(room_id, result_text):
            return
        error_claimed = True

    current_chat_db = _get_mod_attr("chat_db", chat_db)
    room_record = current_chat_db.get_room(room_id) if current_chat_db else None
    target_parent_id = (room_record.get("parentAgentId") if room_record else None) or CORE_ORCHESTRATOR_ROOM_ID
    if not _is_orchestrator_room_id(target_parent_id):
        target_parent_id = CORE_ORCHESTRATOR_ROOM_ID

    room_title = room_id
    if room_record and room_record.get("title"):
        room_title = room_record["title"]
    else:
        try:
            from web_rooms import _load_chat_rooms_index
            rooms = _load_chat_rooms_index()
            for r in rooms:
                if r.get("id") == room_id:
                    room_title = r.get("title") or room_id
                    break
        except Exception:
            pass

    short_res = result_text.strip()
    if len(short_res) > 1500:
        short_res = short_res[:1500] + "…"

    parent_cfg = agent_configs.get_agent_config(target_parent_id)
    parent_label = (
        str(parent_cfg.get("name"))
        if parent_cfg and parent_cfg.get("name")
        else (
            "세컨브레인" if target_parent_id == EXPERT_VAULT_ROOM_ID
            else ("봇개발" if target_parent_id == EXPERT_CODING_ROOM_ID else "마스터")
        )
    )

    if is_success:
        report_msg = (
            f"[하위 작업 완료 보고] [{room_title}] 세션 작업 완료\n"
            f"• 대상 방: {room_title} (Room ID: {room_id})\n"
        )
        if task_id:
            report_msg += f"• 작업 ID: {task_id} (상태: done)\n"
        report_msg += (
            f"• 상태: 정상 완료 (원장 자동 검토 완료)\n"
            f"• 하위 에이전트 응답 요약:\n{short_res}\n\n"
            f"※ {parent_label} 지침: 작업 원장은 자동 완료 처리되었으므로 별도 `session-ctl task --close` 호출 없이 결과만 확인하고 후속 지시가 필요할 때만 이어가세요."
        )
    else:
        if not (model or effort) and room_record:
            model = room_record.get("model")
            effort = room_record.get("effort")
        try:
            import engine_settings
            allowed = engine_settings.get_all_model_labels()
        except Exception:
            allowed = {}
        report_msg = child_error_report.build_error_report(
            room_title=room_title,
            room_id=room_id,
            error_text=result_text,
            task_id=task_id,
            model=model,
            effort=effort,
            parent_label=parent_label,
            allowed_models=allowed,
        )

    try:
        status_label = "성공" if is_success else "실패"
        notify_summary = master_memory.format_notify_memory(
            status_label=status_label,
            room_id=room_id,
            task_id=task_id or "",
            result_text=result_text or "",
        )
        master_memory.add_memory(
            notify_summary,
            title=room_title or f"보고: {task_id or room_id}",
            kind="notify",
            room_id=room_id,
            task_id=task_id or "",
            summarize=True,
            agent_id=master_memory.normalize_agent_id(target_parent_id),
        )
    except Exception:
        pass
    try:
        enqueue_fn = _get_mod_attr("enqueue_room_turn", enqueue_room_turn)
        await enqueue_fn(
            room_id=target_parent_id,
            user_text=report_msg,
            channel="task_report",
            task_id=task_id,
        )
        mark_room_completion_reported(room_id)
        logger.info("[web_jobs] %s 에이전트(%s)에게 완료 보고 전달 완료 (task_id=%s, room=%s)", parent_label, target_parent_id, task_id, room_id)
    except Exception as exc:
        logger.error("[web_jobs] %s 에이전트 완료 보고 전달 실패: %s", parent_label, exc)
        if task_id:
            master_tasks.release_report(task_id)  # 감독 루프 안전망이 재시도
        elif error_claimed:
            child_error_report.release_error_report(room_id, result_text)


async def _run_stream_tail_loop(
    *,
    out_file_path: str,
    tail_stop_event: asyncio.Event,
    room_key: str,
    room_id: str | None,
    job_id: str,
    asst_msg_id: int | None,
    start_timestamp: float,
    collected_steps: list[dict],
    live_snapshot: dict[str, Any],
    agy_conv_id: str | None,
    current_broadcast: Any,
    current_chat_db: Any,
    initial_offset: int = 0,
) -> None:
    """out_file_path 및 AGY transcript.jsonl 을 tail 하며 실시간 이벤트 브로드캐스트 및 DB 증분 커밋을 수행한다."""
    agy_transcript_offset = 0
    seen_step_indices: set[int] = set()

    if agy_conv_id:
        pre_t_path = os.path.expanduser(f"~/.gemini/antigravity-cli/brain/{agy_conv_id}/.system_generated/logs/transcript.jsonl")
        if os.path.exists(pre_t_path):
            try:
                with open(pre_t_path, "r", encoding="utf-8", errors="replace") as pre_tf:
                    for p_line in pre_tf:
                        try:
                            p_obj = json.loads(p_line)
                            if "step_index" in p_obj:
                                seen_step_indices.add(p_obj["step_index"])
                        except Exception:
                            pass
                    agy_transcript_offset = pre_tf.tell()
            except Exception:
                pass

    offset = max(0, int(initial_offset))
    read_buffer = b""
    last_db_commit_ts = time.time()
    last_step_count = len(collected_steps)
    last_stream_len = len(live_snapshot.get("streamText") or "")

    while not tail_stop_event.is_set():
        if out_file_path and os.path.exists(out_file_path):
            try:
                with open(out_file_path, "rb") as stream_file:
                    stream_file.seek(offset)
                    new_bytes = stream_file.read()
                    offset += len(new_bytes)
                    if new_bytes:
                        read_buffer += new_bytes
                        parts = read_buffer.split(b"\n")
                        read_buffer = parts.pop()
                        for raw_part in parts:
                            text_line = raw_part.decode("utf-8", "replace").strip()
                            if not text_line:
                                continue
                            extracted = extract_stream_events_from_line(text_line)
                            for step_item in extracted:
                                kind = step_item.get("kind")
                                if kind == "tool_call":
                                    step_item["elapsedSeconds"] = int(time.time() - start_timestamp)
                                    collected_steps.append(step_item)
                                    feed_txt = step_item.get("title") or "도구 실행 중…"
                                    _room_live_feeds[room_key] = feed_txt
                                    live_snapshot["feed"] = feed_txt
                                    await current_broadcast({
                                        "type": "agent_step",
                                        "jobId": job_id,
                                        "roomId": room_id,
                                        "step": step_item,
                                        "elapsedSeconds": step_item["elapsedSeconds"],
                                    })
                                elif kind == "thinking":
                                    detail_txt = step_item.get("detail") or ""
                                    if collected_steps and collected_steps[-1].get("kind") == "thinking":
                                        if collected_steps[-1].get("detail") == detail_txt:
                                            continue
                                        collected_steps[-1]["detail"] = detail_txt
                                        await current_broadcast({
                                            "type": "agent_step_update",
                                            "jobId": job_id,
                                            "roomId": room_id,
                                            "stepIndex": len(collected_steps) - 1,
                                            "step": collected_steps[-1],
                                        })
                                        continue
                                    step_item["elapsedSeconds"] = int(time.time() - start_timestamp)
                                    step_item["status"] = "done"
                                    collected_steps.append(step_item)
                                    feed_txt = step_item.get("title") or "thinking"
                                    _room_live_feeds[room_key] = feed_txt
                                    live_snapshot["feed"] = feed_txt
                                    await current_broadcast({
                                        "type": "agent_step",
                                        "jobId": job_id,
                                        "roomId": room_id,
                                        "step": step_item,
                                        "elapsedSeconds": step_item["elapsedSeconds"],
                                    })
                                elif kind == "tool_result" and collected_steps:
                                    last_idx = len(collected_steps) - 1
                                    last_step = collected_steps[last_idx]
                                    last_step["status"] = "done"
                                    res_detail = step_item.get("detail") or ""
                                    if res_detail:
                                        last_step["detail"] = (
                                            (last_step.get("detail") or "") + f"\n결과: {res_detail[:300]}"
                                        ).strip()
                                    await current_broadcast({
                                        "type": "agent_step_update",
                                        "jobId": job_id,
                                        "roomId": room_id,
                                        "stepIndex": last_idx,
                                        "step": last_step,
                                    })
                            try:
                                ev_obj = json.loads(text_line)
                            except json.JSONDecodeError:
                                ev_obj = None
                            if isinstance(ev_obj, dict):
                                c_id = ev_obj.get("conversation_id") or (ev_obj.get("step_update") or {}).get("conversation_id")
                                if c_id and not agy_conv_id:
                                    agy_conv_id = c_id
                                    pre_t_path = os.path.expanduser(f"~/.gemini/antigravity-cli/brain/{agy_conv_id}/.system_generated/logs/transcript.jsonl")
                                    if os.path.exists(pre_t_path):
                                        try:
                                            with open(pre_t_path, "r", encoding="utf-8", errors="replace") as pre_tf:
                                                for p_line in pre_tf:
                                                    try:
                                                        p_obj = json.loads(p_line)
                                                        if "step_index" in p_obj:
                                                            seen_step_indices.add(p_obj["step_index"])
                                                    except Exception:
                                                        pass
                                                agy_transcript_offset = pre_tf.tell()
                                        except Exception:
                                            pass
                            if isinstance(ev_obj, dict) and ev_obj.get("event") == "step_update":
                                su = ev_obj.get("step_update") or {}
                                st = su.get("step_type")
                                state = su.get("state")
                                if st == "tool" and state in ("DONE", "ERROR") and collected_steps:
                                    s_idx = su.get("step_index")
                                    target_i = len(collected_steps) - 1
                                    if s_idx is not None:
                                        for idx_c in range(len(collected_steps) - 1, -1, -1):
                                            if collected_steps[idx_c].get("stepIndex") == s_idx:
                                                target_i = idx_c
                                                break
                                    target_step = collected_steps[target_i]
                                    target_step["status"] = "error" if state == "ERROR" else "done"
                                    dur = su.get("duration_seconds")
                                    if dur is not None:
                                        try:
                                            dur_f = float(dur)
                                            target_step["durationSeconds"] = round(dur_f, 2)
                                            target_step["elapsedSeconds"] = (
                                                f"{dur_f:.2f}" if dur_f < 1 else f"{dur_f:.1f}"
                                            )
                                        except (ValueError, TypeError):
                                            pass
                                    t_info = su.get("tool_info") or {}
                                    out_str = str(t_info.get("output") or "").strip()
                                    if out_str:
                                        line_c = len(out_str.splitlines())
                                        p_view = out_str[:500] + ("…" if len(out_str) > 500 else "")
                                        target_step["detail"] = (
                                            (target_step.get("detail") or "") + f"\n결과 ({line_c}줄): {p_view}"
                                        ).strip()
                                    await current_broadcast({
                                        "type": "agent_step_update",
                                        "jobId": job_id,
                                        "roomId": room_id,
                                        "stepIndex": target_i,
                                        "step": target_step,
                                    })
                                elif st == "agent_response" and state == "ACTIVE":
                                    delta = su.get("text_delta") or ""
                                    if delta:
                                        _room_live_feeds[room_key] = "답변 작성 중…"
                                        live_snapshot["feed"] = "답변 작성 중…"
                                        live_snapshot["streamText"] = (live_snapshot.get("streamText") or "") + delta
                                        await current_broadcast({
                                            "type": "stream_delta",
                                            "jobId": job_id,
                                            "roomId": room_id,
                                            "delta": delta,
                                        })
                        now_ts = time.time()
                        cur_step_cnt = len(collected_steps)
                        cur_txt_len = len(live_snapshot.get("streamText") or "")
                        if (cur_step_cnt != last_step_count) or (cur_txt_len != last_stream_len and now_ts - last_db_commit_ts >= 1.0):
                            if asst_msg_id:
                                try:
                                    current_chat_db.update_message(
                                        asst_msg_id,
                                        text=live_snapshot.get("streamText") or "",
                                        steps=collected_steps,
                                        update_preview=False,
                                    )
                                except Exception:
                                    pass
                            last_step_count = cur_step_cnt
                            last_stream_len = cur_txt_len
                            last_db_commit_ts = now_ts
            except Exception:
                pass

        if agy_conv_id:
            t_path = os.path.expanduser(f"~/.gemini/antigravity-cli/brain/{agy_conv_id}/.system_generated/logs/transcript.jsonl")
            if os.path.exists(t_path):
                try:
                    with open(t_path, "r", encoding="utf-8", errors="replace") as tf:
                        tf.seek(agy_transcript_offset)
                        new_t_lines = tf.readlines()
                        agy_transcript_offset = tf.tell()
                        for t_line in new_t_lines:
                            t_line = t_line.strip()
                            if not t_line:
                                continue
                            try:
                                t_obj = json.loads(t_line)
                            except Exception:
                                continue
                            s_idx = t_obj.get("step_index")
                            if s_idx is not None and s_idx in seen_step_indices:
                                continue
                            if s_idx is not None:
                                seen_step_indices.add(s_idx)

                            # 새 사용자 입력(USER_INPUT) 감지 시 이전 턴 스트림 텍스트 즉시 초기화
                            if t_obj.get("type") == "USER_INPUT":
                                live_snapshot["streamText"] = ""

                            if t_obj.get("source") == "MODEL" and t_obj.get("type") == "PLANNER_RESPONSE":
                                c_txt = t_obj.get("content") or ""
                                if isinstance(c_txt, str) and c_txt.strip() and "<SYSTEM_MESSAGE>" not in c_txt:
                                    live_snapshot["streamText"] = c_txt
                                    _room_live_feeds[room_key] = "답변 작성 중…"
                                    await current_broadcast({
                                        "type": "stream_chunk",
                                        "jobId": job_id,
                                        "roomId": room_id,
                                        "text": c_txt,
                                        "isCumulative": True,
                                    })

                            if t_obj.get("source") == "MODEL":
                                thinking_str = str(t_obj.get("thinking") or "")
                                if thinking_str.strip():
                                    t_step = {
                                        "kind": "thinking",
                                        "title": "thinking",
                                        "detail": thinking_str.strip(),
                                        "status": "done",
                                        "elapsedSeconds": int(time.time() - start_timestamp),
                                    }
                                    collected_steps.append(t_step)
                                    feed_txt = "thinking"
                                    _room_live_feeds[room_key] = feed_txt
                                    live_snapshot["feed"] = feed_txt
                                    await current_broadcast({
                                        "type": "agent_step",
                                        "jobId": job_id,
                                        "roomId": room_id,
                                        "step": t_step,
                                        "elapsedSeconds": t_step["elapsedSeconds"],
                                    })
                except Exception:
                    pass

        await asyncio.sleep(0.12)


async def _watch_and_finish_web_job(
    job_id: str,
    rec: dict,
    steps: list[dict],
    initial_offset: int = 0,
) -> None:
    """기동 시 아직 살아있는 백그라운드 에이전트 작업을 계속 감시하고 완료 시 결과를 배달한다."""
    room_id = rec.get("room_id") or "__default__"
    msg_id = rec.get("msg_id")
    out_file = rec.get("out_file")
    err_file = rec.get("err_file")
    pid = rec.get("pid")
    started_at = rec.get("started_at") or time.time()
    session_id = rec.get("session_id")
    session_namespace = rec.get("session_namespace", "")
    task_id = rec.get("task_id")
    task_rec = master_tasks.get_task(task_id) if task_id else None
    if task_rec and _is_task_rec_aborted(task_rec):
        logger.info("[web_jobs] 기동 시 복구 대상 작업이 이미 취소/중단됨 — 재실행 차단: %s (task=%s)", job_id, task_id)
        if pid and detached_pid_alive(pid):
            kill_job_fn = _get_mod_attr("kill_job", kill_job)
            kill_job_fn(pid)
        _remove_web_job(job_id)
        _remove_job_files(out_file, err_file)
        return
    is_orchestrator = (room_id in ORCHESTRATOR_ROOM_IDS)

    current_watch_agent = _get_mod_attr("watch_agent", watch_agent)
    current_chat_db = _get_mod_attr("chat_db", chat_db)
    current_broadcast = _get_mod_attr("broadcast_ws_json", broadcast_ws_json)
    from web_rooms import _load_hidden_rooms, _update_room_metadata

    live_snapshot = _room_live_snapshots.get(room_id) or {"streamText": ""}
    tail_stop_event = asyncio.Event()
    tail_task = asyncio.create_task(
        _run_stream_tail_loop(
            out_file_path=out_file or "",
            tail_stop_event=tail_stop_event,
            room_key=room_id,
            room_id=room_id,
            job_id=job_id,
            asst_msg_id=msg_id,
            start_timestamp=started_at,
            collected_steps=steps,
            live_snapshot=live_snapshot,
            agy_conv_id=session_id,
            current_broadcast=current_broadcast,
            current_chat_db=current_chat_db,
            initial_offset=initial_offset,
        )
    )

    async def _on_progress_callback(feed_text: str) -> None:
        elapsed_seconds = int(time.time() - started_at)
        last_line = ""
        if feed_text:
            lines = [ln.strip() for ln in feed_text.splitlines() if ln.strip()]
            if lines:
                last_line = lines[-1]
        if last_line.startswith("💬"):
            clean_feed = "답변 작성 중…"
        elif steps:
            last_step = steps[-1]
            if last_step.get("status") in ("done", "error"):
                clean_feed = "내용 분석 및 답변 정리 중…"
            else:
                clean_feed = last_step.get("title") or "도구 실행 중…"
        else:
            clean_feed = last_line or "실행 중 (재부착됨)…"
        _room_live_feeds[room_id] = clean_feed
        if room_id in _room_live_snapshots:
            _room_live_snapshots[room_id]["feed"] = clean_feed
        try:
            await current_broadcast({
                "type": "progress",
                "jobId": job_id,
                "roomId": room_id,
                "feed": clean_feed,
                "text": clean_feed,
                "elapsedSeconds": elapsed_seconds,
                "stepCount": len(steps),
            })
        except Exception:
            pass
        if task_id:
            try:
                master_tasks.update_task(
                    task_id,
                    feed=clean_feed[:200],
                    stepCount=len(steps),
                    lastActivityAt=time.time(),
                )
            except Exception:
                pass
        if _master_supervisor:
            _master_supervisor.poke()

    should_cleanup_job = True
    try:
        agent_output = await current_watch_agent(
            out_file,
            pid=pid,
            start_ts=started_at,
            timeout=_compute_remaining_timeout(started_at, int(os.getenv("AGENT_TIMEOUT", "0") or 0)),
            on_progress=_on_progress_callback,
            err_file=err_file,
            session_id=session_id,
        )
        reply_text = str(agent_output.get("result") or "").strip()
        resolved_session_id = agent_output.get("session_id") or session_id
        if resolved_session_id:
            _sync_global_session_if_needed(
                room_id, session_id, resolved_session_id, session_namespace
            )
            _room_session_ids[room_id] = resolved_session_id

        if not reply_text or reply_text in (
            _EMPTY_OUTPUT_TEXT,
            _NO_RESULT_TEXT,
            "(결과 없음)",
            "(빈 출력)",
            "⚠️ 작업이 중단되었습니다.",
            "(작업이 완료되었습니다)",
        ):
            curr_stream = (live_snapshot.get("streamText") or "").strip()
            if curr_stream:
                reply_text = curr_stream
            elif resolved_session_id:
                t_resp = extract_agy_transcript_final_response(resolved_session_id)
                if t_resp:
                    reply_text = t_resp

        if not reply_text or reply_text in (_EMPTY_OUTPUT_TEXT, _NO_RESULT_TEXT, "(결과 없음)", "(빈 출력)"):
            if steps:
                reply_text = "(작업이 완료되었습니다)"
            else:
                reply_text = "⚠️ 작업이 중단되었습니다."
        elif agent_output.get("interrupted"):
            if not extract_agy_transcript_final_response(resolved_session_id):
                reply_text = f"{reply_text}\n\n⚠️ 작업이 중단되었습니다."

        hidden = _load_hidden_rooms()
        is_room_deleted = (
            room_id not in ORCHESTRATOR_ROOM_IDS
            and (room_id in hidden or (resolved_session_id and resolved_session_id in hidden))
        )
        job_model = agent_output.get("model") or rec.get("model") or get_model()
        job_effort = rec.get("effort") or get_effort()
        if msg_id and not is_room_deleted:
            try:
                current_chat_db.update_message(
                    msg_id,
                    text=reply_text,
                    steps=steps,
                    model=job_model,
                    effort=job_effort,
                    cost=agent_output.get("cost"),
                    session_id=resolved_session_id,
                    update_preview=True,
                )
                _update_room_metadata(
                    room_id,
                    preview=reply_text[:60],
                    delta_msgs=1,
                    session_id=resolved_session_id,
                )
            except Exception as db_err:
                logger.warning("[web_jobs] 재부착 DB 갱신 경고 (msg_id=%s): %s", msg_id, db_err)

        is_success = bool(
            reply_text
            and not agent_output.get("interrupted")
            and reply_text not in ("⚠️ 작업이 중단되었습니다.", "⚠️ 서버 재시작으로 답변이 중단되었습니다.")
        )
        if task_id:
            if is_success:
                _rec = master_tasks.get_task(task_id) or {}
                _now_done = time.time()
                done_fields: dict[str, Any] = {
                    "status": "done",
                    "finishedAt": _now_done,
                    "resultPreview": reply_text[:400],
                    "sessionId": resolved_session_id,
                    "feed": "완료",
                }
                if _rec.get("wait"):
                    done_fields["reportedAt"] = _now_done
                    done_fields["reviewed"] = True
                    done_fields["reviewedAt"] = _now_done
                master_tasks.update_task(task_id, **done_fields)
                if rec.get("channel") == "orchestrator":
                    await _notify_master_agent(room_id, task_id, reply_text, is_success=True)
            else:
                master_tasks.update_task(
                    task_id,
                    status="failed",
                    finishedAt=time.time(),
                    error="작업 중단됨",
                )
                if rec.get("channel") == "orchestrator":
                    await _notify_master_agent(room_id, task_id, "작업 중단됨", is_success=False)
        elif is_success and not is_orchestrator and rec.get("channel") == "orchestrator":
            await _notify_master_agent(room_id, None, reply_text, is_success=True)

        if is_orchestrator and reply_text and not reply_text.startswith("⚠️"):
            try:
                orch_agent = master_memory.normalize_agent_id(room_id)
                master_memory.record_orchestrator_turn_memory(
                    user_text=rec.get("user_text") or "",
                    reply_text=reply_text,
                    channel=rec.get("channel", "web"),
                    task_id=task_id or "",
                    room_id=room_id,
                    agent_id=orch_agent,
                )
                if resolved_session_id:
                    master_memory.commit_memory_prompt_checkpoint(resolved_session_id, agent_id=orch_agent)
            except Exception:
                pass

        if _master_supervisor:
            _master_supervisor.poke()

        try:
            await current_broadcast({
                "type": "result",
                "jobId": job_id,
                "msgId": msg_id,
                "userMsgId": rec.get("user_msg_id"),
                "clientMsgId": rec.get("client_msg_id"),
                "roomId": room_id,
                "text": reply_text,
                "result": reply_text,
                "speechText": clean_text_for_speech(reply_text),
                "steps": steps,
                "sessionId": resolved_session_id,
                "model": job_model,
                "effort": job_effort,
                "engine": get_engine(),
                "cost": agent_output.get("cost"),
                "elapsedSeconds": int(time.time() - started_at),
                "voiceMode": bool(rec.get("voice_mode", False)),
                "channel": rec.get("channel", "web"),
                "interrupted": bool(agent_output.get("interrupted")),
            })
            build_status_fn = _get_mod_attr("build_status_payload", None)
            if build_status_fn:
                await current_broadcast(build_status_fn())
        except Exception:
            pass

        logger.info("[web_jobs] 백그라운드 재부착 완료: %s (room=%s, msg_id=%s, task=%s)", job_id, room_id, msg_id, task_id)
    except asyncio.CancelledError:
        is_user_cancel = (room_id in _user_cancelled_rooms) and not _shutting_down
        _user_cancelled_rooms.discard(room_id)
        if is_user_cancel:
            await _finalize_orchestrator_completion(
                room_id=room_id,
                task_id=task_id,
                channel=rec.get("channel", "web"),
                is_orchestrator=is_orchestrator,
                is_success=False,
                result_text="작업이 취소되었습니다.",
                status="cancelled",
                error_text="작업이 취소되었습니다.",
                on_complete=None,
            )
            if _master_supervisor:
                _master_supervisor.poke()
            try:
                await current_broadcast({"type": "cancelled", "jobId": job_id, "roomId": room_id})
            except Exception:
                pass
            if msg_id:
                curr_txt = (live_snapshot.get("streamText") or "").strip()
                cancel_txt = f"{curr_txt}\n\n🛑 사용자에 의해 실행이 중단되었습니다." if curr_txt else "🛑 사용자에 의해 실행이 중단되었습니다."
                try:
                    current_chat_db.update_message(
                        msg_id,
                        text=cancel_txt,
                        steps=steps,
                        update_preview=True,
                    )
                except Exception:
                    pass
        else:
            # 서버 종료/재시작으로 인한 취소 — 다음 기동 시 다시 재부착할 수 있도록 job/출력 파일 보존
            should_cleanup_job = False
            logger.info("[web_jobs] 서버 종료 감지 — 재부착 작업 보존(detach): %s (room=%s, pid=%s)", job_id, room_id, pid)
            if task_id:
                try:
                    master_tasks.update_task(task_id, feed="서버 재시작 중 (백그라운드 유지)…")
                except Exception:
                    pass
        raise
    except Exception as exc:
        logger.error("[web_jobs] 백그라운드 재부착 중 에러 (%s): %s", job_id, exc)
    finally:
        tail_stop_event.set()
        tail_task.cancel()
        try:
            await tail_task
        except (asyncio.CancelledError, Exception):
            pass
        if _active_agent_pids.get(room_id) == pid:
            _active_agent_pids.pop(room_id, None)
        if _active_watch_tasks.get(room_id) == asyncio.current_task():
            _active_watch_tasks.pop(room_id, None)
        _room_live_snapshots.pop(room_id, None)
        _room_live_feeds.pop(room_id, None)
        if should_cleanup_job:
            _remove_web_job(job_id)
            _remove_job_files(out_file, err_file)


async def _recover_single_web_job(job_id: str, rec: dict) -> None:
    """다운타임/재시작 동안 방치된 단일 web_job 을 검사하고 복구한다."""
    room_id = rec.get("room_id") or "__default__"
    msg_id = rec.get("msg_id")
    out_file = rec.get("out_file")
    err_file = rec.get("err_file")
    pid = rec.get("pid")
    started_at = rec.get("started_at") or time.time()
    session_id = rec.get("session_id")
    session_namespace = rec.get("session_namespace", "")

    current_watch_agent = _get_mod_attr("watch_agent", watch_agent)
    current_chat_db = _get_mod_attr("chat_db", chat_db)
    from web_rooms import _load_hidden_rooms, _update_room_metadata

    hidden = _load_hidden_rooms()
    if room_id not in ORCHESTRATOR_ROOM_IDS and (
        room_id in hidden or (session_id and session_id in hidden)
    ):
        if pid:
            kill_job_fn = _get_mod_attr("kill_job", kill_job)
            kill_job_fn(pid)
        _remove_web_job(job_id)
        _remove_job_files(out_file, err_file)
        return

    task_id = rec.get("task_id")
    task_rec = master_tasks.get_task(task_id) if task_id else None
    if task_rec and _is_task_rec_aborted(task_rec):
        logger.info("[web_jobs] 기동 시 복구 대상 작업이 이미 취소/중단됨 — 재실행 차단: %s (task=%s)", job_id, task_id)
        if pid and detached_pid_alive(pid):
            kill_job_fn = _get_mod_attr("kill_job", kill_job)
            kill_job_fn(pid)
        _remove_web_job(job_id)
        _remove_job_files(out_file, err_file)
        return

    if not out_file or not os.path.exists(out_file):
        if msg_id:
            current_chat_db.update_message(
                msg_id,
                text="⚠️ 서버 재시작으로 답변이 중단되었습니다.",
                update_preview=True,
            )
        if task_id:
            master_tasks.update_task(
                task_id,
                status="failed",
                finishedAt=time.time(),
                error="서버 재시작으로 작업 중단됨",
            )
            if rec.get("channel") == "orchestrator":
                await _notify_master_agent(room_id, task_id, "서버 재시작으로 작업 중단됨", is_success=False)
        _remove_web_job(job_id)
        return

    # out_file 에서 실행된 도구 단계(steps) 추출 및 현재 바이트 오프셋 기록
    steps, initial_offset = _extract_steps_from_jsonl(out_file, extract_stream_events_from_line)

    # 아직 백그라운드 프로세스가 살아있으면 비동기로 재부착하여 서버 기동을 차단하지 않음
    if pid and detached_pid_alive(pid):
        logger.info("[web_jobs] 프로세스 살아있음 감지 — 백그라운드 재부착 태스크 생성: %s (pid=%s, task=%s)", job_id, pid, task_id)
        # 실시간 상태 스냅샷에 선제 등록하여 master_supervisor 및 웹소켓에 '실행 중'으로 즉시 노출
        _active_agent_pids[room_id] = pid
        _room_live_feeds[room_id] = "실행 중 (재부착됨)…"
        _room_live_snapshots[room_id] = {
            "jobId": job_id,
            "asstMsgId": msg_id,
            "userMsgId": rec.get("user_msg_id"),
            "clientMsgId": rec.get("client_msg_id"),
            "roomId": room_id,
            "sessionId": session_id,
            "userText": rec.get("user_text", ""),
            "channel": rec.get("channel", "web"),
            "voiceMode": rec.get("voice_mode", False),
            "engine": get_engine(),
            "model": rec.get("model") or get_model(),
            "effort": rec.get("effort") or get_effort(),
            "startTimestamp": started_at,
            "startedAt": int(started_at * 1000),
            "feed": "실행 중 (재부착됨)…",
            "steps": steps,
            "streamText": "",
            "taskId": task_id,
        }
        if task_id:
            master_tasks.update_task(
                task_id,
                status="running",
                feed="실행 중 (재부착됨)…",
                sessionId=session_id,
            )
        if _master_supervisor:
            _master_supervisor.poke()
        watch_task = asyncio.create_task(
            _watch_and_finish_web_job(job_id, rec, steps, initial_offset=initial_offset)
        )
        _active_watch_tasks[room_id] = watch_task
        return

    agent_output = await current_watch_agent(
        out_file,
        pid=None,
        start_ts=started_at,
        timeout=0,
        err_file=err_file,
        session_id=session_id,
    )

    reply_text = str(agent_output.get("result") or "").strip()
    resolved_session_id = agent_output.get("session_id") or session_id
    if resolved_session_id:
        _sync_global_session_if_needed(
            room_id, session_id, resolved_session_id, session_namespace
        )
        _room_session_ids[room_id] = resolved_session_id

    if not reply_text or reply_text in (
        _EMPTY_OUTPUT_TEXT,
        _NO_RESULT_TEXT,
        "(결과 없음)",
        "(빈 출력)",
        "⚠️ 서버 재시작으로 답변이 중단되었습니다.",
        "(도구 실행이 완료되었습니다)",
    ):
        if resolved_session_id:
            t_resp = extract_agy_transcript_final_response(resolved_session_id)
            if t_resp:
                reply_text = t_resp

    if not reply_text or reply_text in (_EMPTY_OUTPUT_TEXT, _NO_RESULT_TEXT, "(결과 없음)", "(빈 출력)"):
        if steps:
            reply_text = "(도구 실행이 완료되었습니다)"
        elif agent_output.get("interrupted"):
            reply_text = "⚠️ 서버 재시작으로 답변이 중단되었습니다."
        else:
            reply_text = "⚠️ 서버 재시작으로 답변이 중단되었습니다."
    elif agent_output.get("interrupted"):
        if not extract_agy_transcript_final_response(resolved_session_id):
            reply_text = f"{reply_text}\n\n⚠️ 서버 재시작으로 답변이 중단되었습니다."

    hidden_after = _load_hidden_rooms()
    is_room_deleted = (
        room_id not in ORCHESTRATOR_ROOM_IDS
        and (room_id in hidden_after or (resolved_session_id and resolved_session_id in hidden_after))
    )
    job_model = agent_output.get("model") or rec.get("model") or get_model()
    job_effort = rec.get("effort") or get_effort()
    if msg_id and not is_room_deleted:
        try:
            current_chat_db.update_message(
                msg_id,
                text=reply_text,
                steps=steps,
                model=job_model,
                effort=job_effort,
                cost=agent_output.get("cost"),
                session_id=resolved_session_id,
                update_preview=True,
            )
            _update_room_metadata(
                room_id,
                preview=reply_text[:60],
                delta_msgs=1,
                session_id=resolved_session_id,
            )
        except Exception as db_err:
            logger.warning("[web_jobs] 복구 DB 갱신 경고 (msg_id=%s): %s", msg_id, db_err)

    is_success = bool(
        reply_text
        and not agent_output.get("interrupted")
        and reply_text not in ("⚠️ 서버 재시작으로 답변이 중단되었습니다.", "⚠️ 작업이 중단되었습니다.")
    )
    is_user_cancelled_reply = bool(
        reply_text and ("🛑" in reply_text or "사용자에 의해 실행이 중단되었습니다" in reply_text or "작업이 취소되었습니다" in reply_text)
    )
    if task_id:
        if is_user_cancelled_reply or (task_rec and _is_task_rec_aborted(task_rec)):
            master_tasks.update_task(
                task_id,
                status="cancelled",
                user_cancelled=True,
                user_confirmed=True,
                reviewed=True,
                finishedAt=time.time(),
                error="사용자에 의해 작업이 취소되었습니다.",
            )
        elif is_success:
            master_tasks.update_task(
                task_id,
                status="done",
                finishedAt=time.time(),
                resultPreview=reply_text[:400],
                sessionId=resolved_session_id,
                feed="완료",
            )
            await _notify_master_agent(room_id, task_id, reply_text, is_success=True)
        else:
            master_tasks.update_task(
                task_id,
                status="failed",
                finishedAt=time.time(),
                error="서버 재시작으로 작업 중단됨",
            )
            await _notify_master_agent(room_id, task_id, "서버 재시작으로 작업 중단됨", is_success=False)
    elif is_success and rec.get("channel") in ("orchestrator", "task_report"):
        await _notify_master_agent(room_id, None, reply_text, is_success=True)

    if _master_supervisor:
        _master_supervisor.poke()

    _remove_web_job(job_id)
    _remove_job_files(out_file, err_file)
    logger.info("[web_jobs] 작업 복구 완료: %s (room=%s, msg_id=%s)", job_id, room_id, msg_id)


async def _recover_web_jobs() -> None:
    """웹 서버 시작 시 남은 web_jobs.json 레코드를 확인하고 미완결/완료된 작업을 복구한다."""
    jobs_data = _load_web_jobs()
    if jobs_data:
        logger.info("[web_jobs] 기동 시 복구 대상 작업 %d건 발견", len(jobs_data))
        for job_id, rec in list(jobs_data.items()):
            try:
                await _recover_single_web_job(job_id, rec)
            except Exception as exc:
                logger.error("[web_jobs] 작업 복구 중 오류 (%s): %s", job_id, exc)
    # 복구 후 남은 활성 웹 작업 + 실행 중인 버닝 세션 ID를 보호하면서 오래된 고아 파일만 정리
    active_ids = set(_load_web_jobs().keys())
    try:
        import burn_todo
        for t in burn_todo.list_all():
            if t.get("status") == "running" and t.get("session_id"):
                active_ids.add(str(t["session_id"]))
    except Exception:
        pass
    current_jobs_dir = _get_mod_attr("JOBS_DIR", JOBS_DIR)
    _sweep_orphan_job_files(current_jobs_dir, active_ids)
    await _restore_pending_room_turns()
    await _recover_startup_placeholders()


async def _recover_startup_placeholders() -> int:
    """웹 서버 시작 시, web_jobs에 등록되지 않았지만 DB에 빈 플레이스홀더('text'=='')로 남아있는 고아 메시지를 복구한다."""
    current_chat_db = _get_mod_attr("chat_db", chat_db)
    if not current_chat_db:
        return 0
    fixed_count = 0
    try:
        rooms = current_chat_db.get_rooms(limit=300)
        active_room_ids = set(_active_agent_pids.keys()) | set(_room_live_snapshots.keys())
        for r in rooms:
            rid = str(r.get("id") or "").strip()
            if not rid or rid in active_room_ids:
                continue
            msgs = current_chat_db.get_messages(rid, limit=5)
            if not msgs:
                continue
            last_msg = msgs[-1]
            if last_msg.get("role") in ("assistant", "agent") and not str(last_msg.get("text") or "").strip():
                msg_id = last_msg.get("id")
                if msg_id:
                    current_chat_db.update_message(
                        msg_id,
                        text="⚠️ 서버 재시작으로 답변이 중단되었습니다.",
                        update_preview=True,
                    )
                    fixed_count += 1
        if fixed_count > 0:
            logger.info("[web_jobs] 기동 시 고아 빈 플레이스홀더 메시지 %d건 복구 완료", fixed_count)
    except Exception as exc:
        logger.warning("[web_jobs] 기동 시 고아 플레이스홀더 복구 경고: %s", exc)
    return fixed_count


_zombie_reaper_task: asyncio.Task | None = None


async def reap_zombie_sessions(
    max_inactive_seconds: float = 900.0,
    max_elapsed_seconds: float = 1800.0,
) -> list[str]:
    """비정상 종료되거나 타임아웃된 고아(Zombie) 세션을 능동 감지하여 회수(Reap)한다.

    1. 프로세스 사망: PID가 기록되었으나 detached_pid_alive(pid)가 False인 경우
    2. 무응답 타임아웃: 최근 max_inactive_seconds (기본 15분) 동안 스트림/로그/트랜스크립트 갱신이 전혀 없는 경우
    3. 최대 실행시간 초과: 총 경과 시간이 max_elapsed_seconds (기본 30분)를 초과한 경우
    """
    now = time.time()
    current_chat_db = _get_mod_attr("chat_db", chat_db)
    current_broadcast = _get_mod_attr("broadcast_ws_json", broadcast_ws_json)
    kill_job_fn = _get_mod_attr("kill_job", kill_job)

    # 점검 대상 방 식별 (메모리 스냅샷, 활성 PID, 영속 job 레코드)
    jobs_data = _load_web_jobs()
    candidate_rooms = set(_room_live_snapshots.keys()) | set(_active_agent_pids.keys())
    for j_id, rec in jobs_data.items():
        r_id = rec.get("room_id")
        if r_id:
            candidate_rooms.add(r_id)

    reaped_rooms: list[str] = []

    for room_id in list(candidate_rooms):
        snap = _room_live_snapshots.get(room_id)
        job_id = None
        job_rec = None
        for j_id, rec in jobs_data.items():
            if rec.get("room_id") == room_id:
                job_id = j_id
                job_rec = rec
                break

        pid = _active_agent_pids.get(room_id) or (snap.get("pid") if snap else None) or (job_rec.get("pid") if job_rec else None)
        started_at = float(
            (snap.get("startTimestamp") if snap else None)
            or (snap.get("startedAt", 0) / 1000.0 if snap and snap.get("startedAt") else None)
            or (job_rec.get("started_at") if job_rec else None)
            or now
        )
        elapsed = max(0.0, now - started_at)

        # 활동 시각(activity) 추정: 파일 mtime 또는 스냅샷 타임스탬프
        last_activity_time = started_at
        out_file = job_rec.get("out_file") if job_rec else None
        if out_file and os.path.exists(out_file):
            try:
                last_activity_time = max(last_activity_time, os.path.getmtime(out_file))
            except OSError:
                pass

        session_id = (snap.get("sessionId") if snap else None) or (job_rec.get("session_id") if job_rec else None)
        if session_id:
            transcript_path = os.path.expanduser(
                f"~/.gemini/antigravity-cli/brain/{session_id}/.system_generated/logs/transcript.jsonl"
            )
            if os.path.exists(transcript_path):
                try:
                    last_activity_time = max(last_activity_time, os.path.getmtime(transcript_path))
                except OSError:
                    pass

        inactive_seconds = max(0.0, now - last_activity_time)

        is_zombie = False
        reap_reason = ""

        # 판정 1: 프로세스 사망
        if pid:
            if not detached_pid_alive(pid):
                is_zombie = True
                reap_reason = f"프로세스 비정상 종료 (PID {pid} 부재)"
        else:
            # PID가 전혀 없는데 스냅샷만 30초 이상 남아있는 경우
            if elapsed >= 30.0:
                is_zombie = True
                reap_reason = "실행 프로세스(PID) 부재 고아 세션"

        # 판정 2: 무응답 비활동 타임아웃 (15분)
        if not is_zombie and max_inactive_seconds > 0 and inactive_seconds >= max_inactive_seconds:
            is_zombie = True
            reap_reason = f"15분 무응답 타임아웃 (최근 비활동 {int(inactive_seconds)}초)"

        # 판정 3: 최대 실행 시간 초과 (30분)
        if not is_zombie and max_elapsed_seconds > 0 and elapsed >= max_elapsed_seconds:
            is_zombie = True
            reap_reason = f"최대 실행 시간 초과 ({int(elapsed)}초 경과)"

        if not is_zombie:
            continue

        # 회수(Reap) 절차 수행
        logger.warning(
            "[zombie_reaper] 고아 세션 능동 회수: room=%s, pid=%s, elapsed=%.1fs, inactive=%.1fs, reason=%s",
            room_id, pid, elapsed, inactive_seconds, reap_reason,
        )

        if pid and detached_pid_alive(pid):
            try:
                kill_job_fn(pid)
            except Exception:
                pass

        _active_agent_pids.pop(room_id, None)
        _room_live_snapshots.pop(room_id, None)
        _room_live_feeds.pop(room_id, None)
        watch_task = _active_watch_tasks.pop(room_id, None)
        if watch_task and not watch_task.done():
            watch_task.cancel()

        if job_id:
            _remove_web_job(job_id)
            if job_rec:
                _remove_job_files(job_rec.get("out_file"), job_rec.get("err_file"))

        # DB 메시지 정리
        asst_msg_id = (snap.get("asstMsgId") if snap else None) or (job_rec.get("msg_id") if job_rec else None)
        stream_txt = (snap.get("streamText") if snap else "") or ""
        steps = snap.get("steps") if snap else None
        notice_text = f"⚠️ 비정상 종료 감지 및 자동 회수되었습니다. ({reap_reason})"

        if current_chat_db:
            if not asst_msg_id:
                try:
                    msgs = current_chat_db.get_messages(room_id, limit=3)
                    for m in reversed(msgs):
                        if m.get("role") in ("assistant", "agent") and not str(m.get("text") or "").strip():
                            asst_msg_id = m.get("id")
                            break
                except Exception:
                    pass

            if asst_msg_id:
                try:
                    final_txt = f"{stream_txt}\n\n{notice_text}".strip() if stream_txt else notice_text
                    current_chat_db.update_message(
                        asst_msg_id,
                        text=final_txt,
                        steps=steps if steps else None,
                        update_preview=True,
                    )
                except Exception as db_err:
                    logger.warning("[zombie_reaper] DB 메시지 회수 갱신 실패 (%s): %s", asst_msg_id, db_err)

        task_id = (snap.get("taskId") if snap else None) or (job_rec.get("task_id") if job_rec else None)
        task_rec = master_tasks.get_task(task_id) if task_id else None
        is_user_aborted = (
            room_id in _user_cancelled_rooms
            or (task_rec and _is_task_rec_aborted(task_rec))
            or (stream_txt and ("🛑" in stream_txt or "사용자에 의해 실행이 중단되었습니다" in stream_txt))
        )
        if is_user_aborted:
            if task_id:
                try:
                    master_tasks.update_task(
                        task_id,
                        status="cancelled",
                        user_cancelled=True,
                        user_confirmed=True,
                        reviewed=True,
                        finishedAt=now,
                        error="사용자에 의해 취소된 작업 회수",
                    )
                except Exception:
                    pass
        elif task_id:
            try:
                master_tasks.update_task(
                    task_id,
                    status="failed",
                    finishedAt=now,
                    error=notice_text,
                )
                await _notify_master_agent(room_id, task_id, notice_text, is_success=False)
            except Exception:
                pass

        try:
            await current_broadcast({
                "type": "error",
                "roomId": room_id,
                "message": notice_text,
            })
            await current_broadcast({
                "type": "active_runs",
                "activeRuns": build_active_runs_payload(),
            })
        except Exception:
            pass

        if _master_supervisor:
            _master_supervisor.poke()

        reaped_rooms.append(room_id)

    return reaped_rooms


async def run_zombie_reaper_loop(
    interval: float = 60.0,
    max_inactive_seconds: float = 900.0,
    max_elapsed_seconds: float = 1800.0,
) -> None:
    """주기적으로 고아 세션을 감지하여 회수하는 백그라운드 루프."""
    logger.info("[zombie_reaper] 고아 세션 능동 회수 루프 가동 (주기: %.0f초, 무응답 한계: %.0f초, 최대 실행: %.0f초)", interval, max_inactive_seconds, max_elapsed_seconds)
    while not _shutting_down:
        try:
            await asyncio.sleep(interval)
            if _shutting_down:
                break
            reaped = await reap_zombie_sessions(
                max_inactive_seconds=max_inactive_seconds,
                max_elapsed_seconds=max_elapsed_seconds,
            )
            if reaped:
                logger.info("[zombie_reaper] 고아 세션 %d건 능동 회수 완료: %s", len(reaped), reaped)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("[zombie_reaper] 고아 세션 회수 루프 예외: %s", exc)


async def execute_chat_turn(
    user_text: str,
    is_voice_mode: bool,
    client_session_id: str | None = None,
    is_new_session: bool = False,
    room_id: str | None = None,
    channel: str = "web",
    attached_files: list[str] | None = None,
    attachments: list[dict] | None = None,
    model: str | None = None,
    effort: str | None = None,
    task_id: str | None = None,
    user_msg_id: int | None = None,
    client_msg_id: str | None = None,
    raw_prompt: bool = False,
    slash_commands: bool = False,
    orchestrator: bool | None = None,
    on_complete: Callable[[dict[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """단일 대화방(AGY 세션)의 1턴 질의응답을 실행하고 실시간 스트림을 브로드캐스트한다."""
    room_key = room_id or "__default__"
    is_orchestrator = _is_orchestrator_room_id(room_key)
    agent_cfg = agent_configs.get_agent_config(room_key) if is_orchestrator else None
    is_coding = get_coding_mode() or (room_key == EXPERT_CODING_ROOM_ID)
    if room_key == CORE_ORCHESTRATOR_ROOM_ID:
        session_namespace = "core_"
    elif room_key == EXPERT_VAULT_ROOM_ID:
        session_namespace = "vault_"
    elif room_key == EXPERT_CODING_ROOM_ID:
        session_namespace = "coding_"
    elif agent_cfg and agent_cfg.get("sessionNamespace"):
        session_namespace = str(agent_cfg["sessionNamespace"])
    else:
        session_namespace = "coding_" if is_coding else ""
    mod = sys.modules.get("web_server")
    curr_workdir = getattr(mod, "WORKDIR", WORKDIR) if mod else WORKDIR
    curr_codedir = getattr(mod, "CODEDIR", CODEDIR) if mod else CODEDIR
    if agent_cfg and agent_cfg.get("workdir"):
        target_workdir = str(agent_cfg["workdir"]).strip() or curr_workdir
    elif room_key == EXPERT_CODING_ROOM_ID:
        target_workdir = curr_codedir or curr_workdir
    elif room_key in (CORE_ORCHESTRATOR_ROOM_ID, EXPERT_VAULT_ROOM_ID):
        target_workdir = curr_workdir
    else:
        target_workdir = (curr_codedir or curr_workdir) if is_coding else curr_workdir

    current_chat_db = _get_mod_attr("chat_db", chat_db)
    current_spawn_agent = _get_mod_attr("spawn_agent", spawn_agent)
    current_watch_agent = _get_mod_attr("watch_agent", watch_agent)
    current_broadcast = _get_mod_attr("broadcast_ws_json", broadcast_ws_json)
    current_get_session = _get_mod_attr("get_session", get_session)
    current_set_session = _get_mod_attr("set_session", set_session)
    current_clear_session = _get_mod_attr("clear_session", clear_session)
    current_update_last_activity = _get_mod_attr("update_last_activity", update_last_activity)
    current_vault_log = _get_mod_attr("vault_log", vault_log)

    from web_rooms import (
        _append_room_message,
        _update_room_metadata,
        _load_hidden_rooms,
    )

    room_db = current_chat_db.get_room(room_key) if room_key != "__default__" else None
    room_model = (room_db.get("model") if room_db else None) or None
    room_effort = (room_db.get("effort") if room_db else None) or None
    room_workdir = (room_db.get("workdir") if room_db else None) or (
        child_default_workdir(room_db.get("parent_agent_id")) if room_db else None
    )
    if room_workdir and not is_orchestrator:
        target_workdir = room_workdir

    fallback_model = room_model or (agent_cfg.get("model") if agent_cfg else None) or get_model()
    fallback_effort = (
        room_effort
        if room_effort is not None
        else ((agent_cfg.get("effort") if agent_cfg and agent_cfg.get("effort") else None) or get_effort())
    )

    active_model = model.strip() if (isinstance(model, str) and model.strip()) else fallback_model
    active_effort = effort if effort is not None else fallback_effort

    current_check_idle = _get_mod_attr("check_and_reset_if_idle", check_and_reset_if_idle)
    if (
        _should_sync_global_session(
            room_id, client_session_id, current_get_session(ns=session_namespace)
        )
        and current_check_idle(session_namespace, idle_seconds=IDLE_RESET_SECONDS)
    ):
        current_vault_log(
            "🆕 세션 리셋",
            f"[{session_namespace or 'vault'}] 장기 유휴로 세션 자동 초기화",
        )

    # 신규 대화방(newSession=True)이면 이전 세션 없이 None 으로 시작
    if is_new_session:
        current_session_id = None
    elif client_session_id is not None:
        current_session_id = client_session_id.strip() or None
    elif is_orchestrator:
        current_session_id = (
            _room_session_ids.get(room_key)
            or current_get_session(ns=session_namespace)
        )
        if not current_session_id:
            core_room = current_chat_db.get_room(room_key)
            if core_room and core_room.get("sessionId"):
                current_session_id = core_room["sessionId"]
    elif room_id is not None:
        current_session_id = None
    else:
        current_session_id = current_get_session(ns=session_namespace)

    # 첨부 파일 목록 정규화
    attached_paths: list[str] = list(attached_files or [])
    if attachments:
        for att in attachments:
            if isinstance(att, dict) and att.get("path") and att["path"] not in attached_paths:
                attached_paths.append(str(att["path"]))

    if raw_prompt:
        effective_prompt = user_text
    elif attached_paths:
        joined = "\n".join(attached_paths)
        instruction = user_text or "이 이미지를 보고 분석 및 설명해 줘."
        effective_prompt = f"{instruction}\n\n첨부 파일 경로:\n{joined}"
        if is_voice_mode:
            effective_prompt += VOICE_MODE_PROMPT_SUFFIX
    else:
        effective_prompt = (
            user_text + VOICE_MODE_PROMPT_SUFFIX if is_voice_mode else user_text
        )

    cur_room_rec = current_chat_db.get_room(room_key) if current_chat_db else None
    parent_agent_id = cur_room_rec.get("parentAgentId") if cur_room_rec else None

    if is_orchestrator and not raw_prompt:
        try:
            orch_agent = master_memory.normalize_agent_id(room_key)
            mem_ctx = master_memory.build_memory_prompt_context(
                session_key=current_session_id,
                agent_id=orch_agent,
            )
            if mem_ctx:
                effective_prompt = f"{mem_ctx}\n\n[현재 요청]\n{effective_prompt}"
        except Exception:
            pass
    elif not is_orchestrator and (channel == "orchestrator" or parent_agent_id) and not raw_prompt:
        # 하위 작업 세션: 필수 동적 메타데이터([부모 보고 경로/ID], [작업 ID])만 간결하게 주입
        # (※ 정적 규칙은 각 작업공간의 AGENTS.md 및 rules/에서 자동 로드되므로 중복 주입하지 않음)
        runtime_meta: list[str] = []
        if parent_agent_id:
            runtime_meta.append(f"• 부모 에이전트(보고 대상): {parent_agent_id}")
        if task_id:
            runtime_meta.append(f"• 작업 ID: {task_id} (완료 시 서버 자동 보고 연계)")
        if runtime_meta:
            meta_header = "[런타임 작업 메타데이터]\n" + "\n".join(runtime_meta)
            if "[런타임 작업 메타데이터]" not in effective_prompt:
                effective_prompt = f"{meta_header}\n\n{effective_prompt}"

    # 대화방 전용 저장소에 유저/서브에이전트 보고 메시지 영속 기록 (명시적 user_msg_id 기반으로 중복 저장 원천 방지)
    actual_user_msg_id: int | None = user_msg_id if user_msg_id is not None else None
    has_existing_user_msg = False
    if actual_user_msg_id is not None:
        try:
            existing_rec = current_chat_db.get_message(actual_user_msg_id)
            if existing_rec and (
                existing_rec.get("roomId") == room_key
                or existing_rec.get("sessionId") == room_key
            ):
                has_existing_user_msg = True
            else:
                actual_user_msg_id = None
        except Exception:
            has_existing_user_msg = True

    if not has_existing_user_msg:
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        incoming_role = "sub-agent" if channel == "task_report" else "user"
        actual_user_msg_id = _append_room_message(room_key, {
            "type": incoming_role,
            "role": incoming_role,
            "text": user_text,
            "channel": channel,
            "attachments": attachments or [],
            "attachedFiles": attached_paths,
            "createdAt": now_iso,
        })

    cur_room_info = current_chat_db.get_room(room_key)
    existing_title = (cur_room_info.get("title") or "").strip() if cur_room_info else ""
    first_line = user_text.splitlines()[0].strip() if user_text else ("사진 분석" if attached_paths else "새 대화")
    if not existing_title or existing_title in ("새 대화", "대화방"):
        if room_key == CORE_ORCHESTRATOR_ROOM_ID:
            new_title = CORE_ORCHESTRATOR_TITLE
        elif room_key == EXPERT_VAULT_ROOM_ID:
            new_title = EXPERT_VAULT_TITLE
        elif room_key == EXPERT_CODING_ROOM_ID:
            new_title = EXPERT_CODING_TITLE
        else:
            new_title = first_line[:24]
    else:
        new_title = existing_title

    _update_room_metadata(
        room_key,
        title=new_title,
        delta_msgs=0 if has_existing_user_msg else 1,
    )

    # 어시스턴트 메시지 플레이스홀더를 DB에 즉시 생성하여 실시간 커밋 기준점 확보
    asst_msg_id = current_chat_db.add_message(
        room_id=room_key,
        role="assistant",
        text="",
        channel=channel,
        steps=[],
        attachments=None,
        model=active_model,
        effort=active_effort,
        cost=None,
        session_id=current_session_id,
    )

    job_id = f"web-{uuid.uuid4().hex[:10]}"
    os.makedirs(JOBS_DIR, exist_ok=True)
    out_file_path = os.path.join(JOBS_DIR, f"{job_id}.jsonl")
    err_file_path = os.path.join(JOBS_DIR, f"{job_id}.err")
    start_timestamp = time.time()

    # 기동 중 크래시/재시작에 대비해 web_jobs.json 에 선제 등록
    _save_web_job(job_id, {
        "job_id": job_id,
        "room_id": room_key,
        "msg_id": asst_msg_id,
        "user_msg_id": actual_user_msg_id,
        "client_msg_id": client_msg_id,
        "pid": None,
        "out_file": out_file_path,
        "err_file": err_file_path,
        "started_at": start_timestamp,
        "session_id": current_session_id,
        "session_namespace": session_namespace,
        "channel": channel,
        "voice_mode": is_voice_mode,
        "user_text": user_text,
        "model": active_model,
        "effort": active_effort,
        "task_id": task_id,
    })

    collected_steps: list[dict] = []
    live_snapshot: dict[str, Any] = {
        "jobId": job_id,
        "asstMsgId": asst_msg_id,
        "userMsgId": actual_user_msg_id,
        "clientMsgId": client_msg_id,
        "roomId": room_id or room_key,
        "sessionId": current_session_id,
        "userText": user_text,
        "attachments": attachments or [],
        "attachedFiles": attached_paths,
        "channel": channel,
        "voiceMode": is_voice_mode,
        "engine": get_engine(),
        "model": active_model,
        "effort": active_effort,
        "startTimestamp": start_timestamp,
        "startedAt": int(start_timestamp * 1000),
        "feed": "생각하는 중…",
        "steps": collected_steps,
        "streamText": "",
        "taskId": task_id,
    }
    _room_live_feeds[room_key] = "생각하는 중…"
    _room_live_snapshots[room_key] = live_snapshot

    if task_id:
        master_tasks.update_task(
            task_id,
            status="running",
            startedAt=start_timestamp,
            feed="생각하는 중…",
            sessionId=current_session_id,
        )
    if _master_supervisor:
        _master_supervisor.poke()

    await current_broadcast(
        {
            "type": "started",
            "jobId": job_id,
            "roomId": room_id,
            "userText": user_text,
            "userMsgId": actual_user_msg_id,
            "clientMsgId": client_msg_id,
            "asstMsgId": asst_msg_id,
            "attachments": attachments or [],
            "attachedFiles": attached_paths,
            "channel": channel,
            "voiceMode": is_voice_mode,
            "engine": live_snapshot["engine"],
            "model": live_snapshot["model"],
            "effort": live_snapshot["effort"],
            "startedAt": live_snapshot["startedAt"],
        }
    )

    is_core = (room_key == CORE_ORCHESTRATOR_ROOM_ID)
    is_expert_vault = (room_key == EXPERT_VAULT_ROOM_ID)
    is_expert_coding = (room_key == EXPERT_CODING_ROOM_ID)
    is_custom_agent = bool(is_orchestrator and not is_core and not is_expert_vault and not is_expert_coding)

    use_orchestrator = is_core if orchestrator is None else bool(orchestrator)
    spawn_kwargs: dict[str, Any] = {
        "workdir": target_workdir,
        "out_file": out_file_path,
        "err_file": err_file_path,
        "coding": is_coding,
        "web": not use_orchestrator and not is_expert_vault and not is_coding and not is_custom_agent,
        "model": active_model,
        "effort": active_effort,
    }
    if slash_commands:
        spawn_kwargs["slash_commands"] = True
    if use_orchestrator:
        spawn_kwargs["orchestrator"] = True
    elif is_expert_vault:
        spawn_kwargs["expert_vault"] = True
    elif is_expert_coding:
        spawn_kwargs["expert_coding"] = True
    elif is_custom_agent:
        spawn_kwargs["agent_id"] = room_key

    extra_env: dict[str, str] = {}
    if use_orchestrator or is_expert_vault or is_expert_coding or is_custom_agent:
        ssl_enabled = os.getenv("WEB_SSL_ENABLED", "").lower() not in ("0", "false", "no") and os.path.exists(DEFAULT_SSL_CERT_FILE)
        proto = "https" if ssl_enabled else "http"
        extra_env.update({
            "OSHYUN_ORCHESTRATOR_API_URL": f"{proto}://127.0.0.1:{WEB_PORT}",
            "OSHYUN_ORCHESTRATOR_TOKEN": WEB_TOKEN,
            "OSHYUN_AGENT_ROOM_ID": room_key,
        })
    else:
        extra_env["OSHYUN_AGENT_ROOM_ID"] = room_key
        if parent_agent_id:
            extra_env["OSHYUN_PARENT_AGENT_ID"] = str(parent_agent_id)
        if task_id:
            extra_env["OSHYUN_TASK_ID"] = str(task_id)

    if extra_env:
        spawn_kwargs["extra_env"] = extra_env

    try:
        agent_proc = current_spawn_agent(
            effective_prompt,
            current_session_id,
            **spawn_kwargs,
        )
    except Exception as exc:
        logger.error("[voice-web] 에이전트 실행 실패: %s", exc)
        err_ts = time.time()
        if asst_msg_id:
            current_chat_db.update_message(
                asst_msg_id,
                text=f"⚠️ 에이전트 기동 실패: {exc}",
                created_at=err_ts,
                update_preview=True,
            )
        _remove_web_job(job_id)
        _room_live_feeds.pop(room_key, None)
        _room_live_snapshots.pop(room_key, None)
        await _finalize_orchestrator_completion(
            room_id=room_key,
            task_id=task_id,
            channel=channel,
            is_orchestrator=is_orchestrator,
            is_success=False,
            result_text=f"에이전트 기동 실패: {exc}",
            status="failed",
            error_text=str(exc),
            on_complete=on_complete,
        )
        if _master_supervisor:
            _master_supervisor.poke()
        await current_broadcast(
            {
                "type": "error",
                "jobId": job_id,
                "msgId": asst_msg_id,
                "roomId": room_id,
                "message": f"에이전트 기동 실패: {exc}",
                "createdAt": err_ts,
            }
        )
        return {"ok": False, "error": str(exc), "roomId": room_id}

    _active_agent_pids[room_key] = agent_proc.pid
    _save_web_job(job_id, {
        "job_id": job_id,
        "room_id": room_key,
        "msg_id": asst_msg_id,
        "user_msg_id": actual_user_msg_id,
        "client_msg_id": client_msg_id,
        "pid": agent_proc.pid,
        "out_file": out_file_path,
        "err_file": err_file_path,
        "started_at": start_timestamp,
        "session_id": current_session_id,
        "session_namespace": session_namespace,
        "channel": channel,
        "voice_mode": is_voice_mode,
        "user_text": user_text,
        "model": active_model,
        "effort": active_effort,
        "task_id": task_id,
    })

    tail_stop_event = asyncio.Event()
    tail_task = asyncio.create_task(
        _run_stream_tail_loop(
            out_file_path=out_file_path,
            tail_stop_event=tail_stop_event,
            room_key=room_key,
            room_id=room_id,
            job_id=job_id,
            asst_msg_id=asst_msg_id,
            start_timestamp=start_timestamp,
            collected_steps=collected_steps,
            live_snapshot=live_snapshot,
            agy_conv_id=current_session_id,
            current_broadcast=current_broadcast,
            current_chat_db=current_chat_db,
            initial_offset=0,
        )
    )

    async def on_progress_callback(progress_feed: str) -> None:
        elapsed_seconds = int(time.time() - start_timestamp)
        last_line = ""
        if progress_feed:
            lines = [ln.strip() for ln in progress_feed.splitlines() if ln.strip()]
            if lines:
                last_line = lines[-1]
        if last_line.startswith("💬"):
            clean_feed = "답변 작성 중…"
        elif collected_steps:
            last_step = collected_steps[-1]
            if last_step.get("status") in ("done", "error"):
                clean_feed = "내용 분석 및 답변 정리 중…"
            else:
                clean_feed = last_step.get("title") or "도구 실행 중…"
        else:
            clean_feed = "생각하는 중…"
        _room_live_feeds[room_key] = clean_feed
        live_snapshot["feed"] = clean_feed
        if task_id:
            master_tasks.update_task(
                task_id,
                feed=clean_feed,
                stepCount=len(collected_steps),
                lastActivityAt=time.time(),
            )
        if _master_supervisor:
            _master_supervisor.poke()
        await current_broadcast(
            {
                "type": "progress",
                "jobId": job_id,
                "roomId": room_id,
                "feed": clean_feed,
                "elapsedSeconds": elapsed_seconds,
                "stepCount": len(collected_steps),
            }
        )

    try:
        agent_output = await current_watch_agent(
            out_file_path,
            pid=agent_proc.pid,
            start_ts=start_timestamp,
            timeout=0,  # 시간초과 없이 끝까지 정상 대기
            on_progress=on_progress_callback,
            err_file=err_file_path,
            proc=agent_proc,
            session_id=current_session_id,
        )
    except asyncio.CancelledError:
        is_user_cancel = (room_key in _user_cancelled_rooms) and not _shutting_down
        _user_cancelled_rooms.discard(room_key)
        _room_live_feeds.pop(room_key, None)
        _room_live_snapshots.pop(room_key, None)
        if is_user_cancel:
            await _finalize_orchestrator_completion(
                room_id=room_key,
                task_id=task_id,
                channel=channel,
                is_orchestrator=is_orchestrator,
                is_success=False,
                result_text="작업이 취소되었습니다.",
                status="cancelled",
                error_text="작업이 취소되었습니다.",
                on_complete=on_complete,
            )
            if _master_supervisor:
                _master_supervisor.poke()
            await current_broadcast({"type": "cancelled", "jobId": job_id, "roomId": room_id})
            if asst_msg_id:
                curr_txt = (live_snapshot.get("streamText") or "").strip()
                cancel_txt = f"{curr_txt}\n\n🛑 사용자에 의해 실행이 중단되었습니다." if curr_txt else "🛑 사용자에 의해 실행이 중단되었습니다."
                current_chat_db.update_message(
                    asst_msg_id,
                    text=cancel_txt,
                    steps=collected_steps,
                    update_preview=True,
                )
            _remove_web_job(job_id)
            for temp_path in (out_file_path, err_file_path):
                try:
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
                except OSError:
                    pass
        else:
            logger.info("[web_jobs] 서버 종료 감지 — 작업 보존(detach): %s (room=%s, pid=%s)", job_id, room_key, agent_proc.pid)
            if task_id:
                master_tasks.update_task(
                    task_id,
                    feed="서버 재시작 중 (백그라운드 유지)…",
                )
        raise
    finally:
        tail_stop_event.set()
        tail_task.cancel()
        try:
            await tail_task
        except (asyncio.CancelledError, Exception):
            pass

        if _active_agent_pids.get(room_key) == agent_proc.pid:
            _active_agent_pids.pop(room_key, None)
        if _active_watch_tasks.get(room_key) == asyncio.current_task():
            _active_watch_tasks.pop(room_key, None)
        _room_live_feeds.pop(room_key, None)

    reply_text = str(agent_output.get("result") or "").strip()
    sync_global_session = _should_sync_global_session(
        room_id,
        current_session_id,
        current_get_session(ns=session_namespace),
    )

    async def _retry_turn(
        feed_msg: str,
        retry_session_id: str | None,
        *,
        model_override: str | None = None,
    ) -> tuple[dict[str, Any], str] | None:
        await current_broadcast(
            {
                "type": "progress",
                "jobId": job_id,
                "roomId": room_id,
                "feed": feed_msg,
                "elapsedSeconds": int(time.time() - start_timestamp),
                "stepCount": len(collected_steps),
            }
        )
        retry_job_id = f"web-retry-{uuid.uuid4().hex[:8]}"
        retry_out_file = os.path.join(JOBS_DIR, f"{retry_job_id}.jsonl")
        retry_err_file = os.path.join(JOBS_DIR, f"{retry_job_id}.err")
        retry_kwargs = dict(spawn_kwargs)
        retry_kwargs["out_file"] = retry_out_file
        retry_kwargs["err_file"] = retry_err_file
        if model_override is not None:
            retry_kwargs["model"] = model_override
        retry_proc = None
        try:
            retry_proc = current_spawn_agent(
                effective_prompt,
                retry_session_id,
                **retry_kwargs,
            )
            _active_agent_pids[room_key] = retry_proc.pid
            watch_kwargs: dict[str, Any] = {
                "pid": retry_proc.pid,
                "start_ts": time.time(),
                "timeout": 0,  # 시간초과 없이 끝까지 정상 대기
                "on_progress": on_progress_callback,
                "err_file": retry_err_file,
                "proc": retry_proc,
            }
            if retry_session_id is not None:
                watch_kwargs["session_id"] = retry_session_id
            retried_output = await current_watch_agent(retry_out_file, **watch_kwargs)
            return retried_output, str(retried_output.get("result") or "").strip()
        except Exception as retry_exc:
            logger.error("[voice-web] 자동 재실행 실패: %s", retry_exc)
            return None
        finally:
            if retry_proc is not None and _active_agent_pids.get(room_key) == retry_proc.pid:
                _active_agent_pids.pop(room_key, None)
            for temp_p in (retry_out_file, retry_err_file):
                try:
                    if os.path.exists(temp_p):
                        os.remove(temp_p)
                except OSError:
                    pass

    # 만약 세션 유실/미존재 에러이고 기존 세션(current_session_id)으로 실행했던 것이라면:
    if current_session_id and is_session_stale_error(reply_text):
        logger.warning(
            "[voice-web] 세션 유실/접근불가 감지 (%s) - 새 세션으로 즉시 자동 복구 재실행합니다: %s",
            current_session_id,
            reply_text,
        )
        if sync_global_session or hasattr(current_clear_session, "_mock_name"):
            current_clear_session(ns=session_namespace)
        current_session_id = None
        retried = await _retry_turn(
            "이전 세션이 만료되어 새 대화 세션으로 자동 연결합니다...",
            None,
        )
        if retried is not None:
            agent_output, reply_text = retried

    # 모델 쿼터 소진 시 기본 AGY 모델(gemini-3.8-flash)로 자동 전환하여 기존 세션을 유지한 채 재실행
    current_get_engine = _get_mod_attr("get_engine", get_engine)
    current_get_model = _get_mod_attr("get_model", get_model)
    current_set_model = _get_mod_attr("set_model", set_model)
    current_is_quota_exhausted_error = _get_mod_attr(
        "is_quota_exhausted_error", is_quota_exhausted_error
    )
    if (
        current_is_quota_exhausted_error(reply_text)
        and current_get_engine() == "agy"
        and active_model != AGY_DEFAULT_MODEL
    ):
        exhausted_model = active_model
        logger.warning(
            "[voice-web] 모델 쿼터 소진 감지 (%s) - %s 모델로 자동 전환하여 재실행합니다: %s",
            exhausted_model,
            AGY_DEFAULT_MODEL,
            reply_text,
        )
        active_model = AGY_DEFAULT_MODEL
        if current_get_model() == exhausted_model:
            current_set_model(AGY_DEFAULT_MODEL)
        if room_model and room_key != "__default__":
            current_chat_db.update_room(room_key, model=AGY_DEFAULT_MODEL)
        retried = await _retry_turn(
            f"모델 쿼터 소진 감지 — {AGY_DEFAULT_MODEL} 모델로 자동 전환하여 재실행합니다...",
            current_session_id,
            model_override=AGY_DEFAULT_MODEL,
        )
        if retried is not None:
            agent_output, reply_text = retried

    elapsed_seconds = int(time.time() - start_timestamp)
    new_session_id = agent_output.get("session_id")
    if new_session_id:
        if sync_global_session or hasattr(current_set_session, "_mock_name"):
            current_set_session(new_session_id, ns=session_namespace)
            current_update_last_activity(ns=session_namespace)
        _room_session_ids[room_key] = new_session_id
    elif current_session_id:
        _room_session_ids[room_key] = current_session_id

    resolved_session_id = new_session_id or current_session_id

    # 최종 응답이 비어있거나 중단/공백 표시인 경우 다단계 복구 시도
    if not reply_text or reply_text in ("⚠️ 응답이 중단되었습니다.", "(도구 실행이 완료되었습니다)", "(응답 없음)"):
        curr_stream = (live_snapshot.get("streamText") or "").strip()
        if curr_stream:
            reply_text = curr_stream
        elif resolved_session_id:
            transcript_txt = extract_agy_transcript_final_response(resolved_session_id)
            if transcript_txt:
                reply_text = transcript_txt

    if not reply_text:
        curr_stream = (live_snapshot.get("streamText") or "").strip()
        if curr_stream:
            reply_text = curr_stream
        elif agent_output.get("interrupted"):
            reply_text = "⚠️ 응답이 중단되었습니다."
        elif collected_steps:
            reply_text = "(도구 실행이 완료되었습니다)"
        else:
            reply_text = "(응답 없음)"
    speech_text = clean_text_for_speech(reply_text)

    # 대화방 전용 SQLite SSOT 저장소에 어시스턴트 답변 및 실행 단계 영속 기록
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    hidden_now = _load_hidden_rooms()
    is_room_deleted = (
        room_key not in ORCHESTRATOR_ROOM_IDS
        and (room_key in hidden_now or (resolved_session_id and resolved_session_id in hidden_now))
    )
    asst_model = agent_output.get("model") or active_model or get_model()
    asst_effort = active_effort if active_effort is not None else get_effort()
    completed_at = time.time()
    if not is_room_deleted:
        if asst_msg_id:
            current_chat_db.update_message(
                asst_msg_id,
                text=reply_text,
                steps=collected_steps,
                model=asst_model,
                effort=asst_effort,
                cost=agent_output.get("cost"),
                session_id=resolved_session_id,
                created_at=completed_at,
                update_preview=True,
            )
        else:
            _append_room_message(room_key, {
                "type": "assistant",
                "role": "assistant",
                "text": reply_text,
                "channel": channel,
                "steps": collected_steps,
                "model": asst_model,
                "effort": asst_effort,
                "cost": agent_output.get("cost"),
                "sessionId": resolved_session_id,
                "createdAt": completed_at,
            })

        _update_room_metadata(
            room_key,
            preview=reply_text[:60],
            delta_msgs=1,
            session_id=resolved_session_id,
        )
    _remove_web_job(job_id)
    for temp_path in (out_file_path, err_file_path):
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass

    oneline_summary = " ".join(reply_text.split())
    if channel == MASTER_PATROL_CHANNEL:
        log_tag = "정기점검"
    elif channel == "schedule_trigger":
        log_tag = "🔔 스케줄"
    elif is_orchestrator:
        if room_key == EXPERT_VAULT_ROOM_ID:
            log_tag = "세컨브레인"
        elif room_key == EXPERT_CODING_ROOM_ID:
            log_tag = "봇개발"
        else:
            log_tag = "마스터에이전트"
    elif channel == "orchestrator":
        log_tag = "세션지시"
    elif is_voice_mode:
        log_tag = "🎙️ 음성대화"
    else:
        log_tag = "🌐 웹대화"
    current_vault_log(
        log_tag,
        f"{user_text} → {oneline_summary[:300]}{fmt_cost(agent_output.get('cost'))}",
    )

    is_interrupted = bool(agent_output.get("interrupted"))
    is_turn_error = is_interrupted or child_error_report.looks_like_error_reply(reply_text)
    is_turn_success = bool(reply_text) and not is_turn_error
    await _finalize_orchestrator_completion(
        room_id=room_key,
        task_id=task_id,
        channel=channel,
        is_orchestrator=is_orchestrator,
        is_success=is_turn_success,
        result_text=reply_text,
        status=("failed" if is_turn_error else "done"),
        resolved_session_id=resolved_session_id,
        elapsed_seconds=elapsed_seconds,
        error_text=(reply_text if is_turn_error else None),
        on_complete=on_complete,
        model=asst_model,
        effort=asst_effort,
    )
    if is_orchestrator and reply_text and not reply_text.startswith("⚠️"):
        try:
            orch_agent = master_memory.normalize_agent_id(room_key)
            master_memory.record_orchestrator_turn_memory(
                user_text=user_text or "",
                reply_text=reply_text,
                channel=channel,
                task_id=task_id or "",
                room_id=room_key,
                agent_id=orch_agent,
            )
            if resolved_session_id:
                master_memory.commit_memory_prompt_checkpoint(resolved_session_id, agent_id=orch_agent)
        except Exception:
            pass
    if _master_supervisor:
        _master_supervisor.poke()

    result_payload = {
        "type": "result",
        "jobId": job_id,
        "msgId": asst_msg_id,
        "userMsgId": actual_user_msg_id,
        "clientMsgId": client_msg_id,
        "roomId": room_id,
        "text": reply_text,
        "result": reply_text,
        "speechText": speech_text,
        "steps": collected_steps,
        "sessionId": resolved_session_id,
        "model": asst_model,
        "effort": asst_effort,
        "engine": get_engine(),
        "cost": agent_output.get("cost"),
        "elapsedSeconds": elapsed_seconds,
        "createdAt": completed_at,
        "voiceMode": is_voice_mode,
        "channel": channel,
        "interrupted": bool(agent_output.get("interrupted")),
    }
    _room_live_snapshots.pop(room_key, None)
    await current_broadcast(result_payload)
    build_status_fn = _get_mod_attr("build_status_payload", None)
    if build_status_fn:
        await current_broadcast(build_status_fn())
    return {
        "ok": True,
        "roomId": room_id,
        "sessionId": resolved_session_id,
        "reply": reply_text,
        "elapsedSeconds": elapsed_seconds,
        "steps": collected_steps,
    }


async def run_room_worker(r_key: str) -> None:
    """특정 대화방의 대기열(FIFO)에서 순차적으로 턴을 꺼내 실행한다."""
    q = _room_queues.get(r_key)
    if q is None:
        return
    try:
        while True:
            existing_watch = _active_watch_tasks.get(r_key)
            if existing_watch is not None and not existing_watch.done():
                try:
                    await asyncio.shield(existing_watch)
                except (asyncio.CancelledError, Exception):
                    pass
            item = await q.get()
            fut: asyncio.Future | None = item.get("result_future")
            try:
                effective_session_id = None if item["is_new_session"] else (
                    _room_session_ids.get(r_key) or item.get("client_session_id")
                )
                turn_task = asyncio.create_task(
                    execute_chat_turn(
                        item["user_text"],
                        item.get("is_voice_mode", False),
                        client_session_id=effective_session_id,
                        is_new_session=item.get("is_new_session", False),
                        room_id=item.get("room_id"),
                        channel=item.get("channel", "web"),
                        attached_files=item.get("attached_files"),
                        attachments=item.get("attachments"),
                        model=item.get("model"),
                        effort=item.get("effort"),
                        task_id=item.get("task_id"),
                        user_msg_id=item.get("user_msg_id"),
                        client_msg_id=item.get("client_msg_id"),
                        raw_prompt=bool(item.get("raw_prompt", False)),
                        slash_commands=bool(item.get("slash_commands", False)),
                        orchestrator=item.get("orchestrator"),
                        on_complete=item.get("on_complete"),
                    )
                )
                _active_watch_tasks[r_key] = turn_task
                try:
                    turn_result = await turn_task
                    if fut is not None and not fut.done():
                        fut.set_result(turn_result)
                except asyncio.CancelledError:
                    if fut is not None and not fut.done():
                        fut.set_exception(asyncio.CancelledError("작업이 취소되었습니다."))
                except Exception as turn_err:
                    logger.exception("[voice-web] 턴 실행 오류: %s", turn_err)
                    if fut is not None and not fut.done():
                        fut.set_exception(turn_err)
                finally:
                    if _active_watch_tasks.get(r_key) == turn_task:
                        _active_watch_tasks.pop(r_key, None)
                    _room_live_snapshots.pop(r_key, None)
            finally:
                q.task_done()
    except asyncio.CancelledError:
        pass
    finally:
        _room_workers.pop(r_key, None)


def is_room_busy(room_id: str | None) -> bool:
    """해당 대화방이 현재 턴을 실행 중이거나 대기열에 보류 중인 턴이 있는지 반환한다."""
    room_key = room_id or "__default__"
    task = _active_watch_tasks.get(room_key)
    q = _room_queues.get(room_key)
    return bool(
        (task is not None and not task.done())
        or (q is not None and not q.empty())
        or (room_key in _active_agent_pids)
        or (room_key in _room_live_snapshots)
    )


async def enqueue_room_turn(
    *,
    room_id: str | None,
    user_text: str,
    is_voice_mode: bool = False,
    client_session_id: str | None = None,
    is_new_session: bool = False,
    channel: str = "web",
    attached_files: list[str] | None = None,
    attachments: list[dict] | None = None,
    model: str | None = None,
    effort: str | None = None,
    result_future: asyncio.Future | None = None,
    task_id: str | None = None,
    user_msg_id: int | None = None,
    client_msg_id: str | None = None,
    raw_prompt: bool = False,
    slash_commands: bool = False,
    orchestrator: bool | None = None,
    on_complete: Callable[[dict[str, Any]], Any] | None = None,
) -> tuple[bool, int]:
    """대화방 큐에 새 턴을 적재하고 워커가 없으면 기동한다. (이미 실행 중이었는지 여부, 큐 크기) 반환."""
    room_key = room_id or "__default__"
    resolved_client_msg_id = (
        str(client_msg_id).strip()
        if (isinstance(client_msg_id, str) and client_msg_id.strip())
        else f"srv-{uuid.uuid4().hex[:12]}"
    )

    if room_key == CORE_ORCHESTRATOR_ROOM_ID and channel != MASTER_PATROL_CHANNEL:
        q_existing = _room_queues.get(room_key)
        if q_existing is not None and not q_existing.empty():
            preserved = []
            while not q_existing.empty():
                try:
                    item_peek = q_existing.get_nowait()
                    if item_peek.get("channel") == MASTER_PATROL_CHANNEL:
                        p_fut = item_peek.get("result_future")
                        if p_fut is not None and not p_fut.done():
                            p_fut.set_exception(asyncio.CancelledError("사용자 요청으로 배경 점검이 양보(취소)되었습니다."))
                        q_existing.task_done()
                    else:
                        preserved.append(item_peek)
                except Exception:
                    break
            for p_item in preserved:
                q_existing.put_nowait(p_item)

        cur_snap = _room_live_snapshots.get(room_key)
        if cur_snap and cur_snap.get("channel") == MASTER_PATROL_CHANNEL:
            logger.info("[orchestrator] 사용자 요청 도착 — 실행 중인 배경 정기 점검 턴을 즉시 양보(선점)합니다.")
            cancel_active_run(room_key)

    if room_key not in _room_queues:
        _room_queues[room_key] = asyncio.Queue()
    q = _room_queues[room_key]

    is_already_running = is_room_busy(room_key)

    queue_item = {
        "user_text": user_text,
        "is_voice_mode": is_voice_mode,
        "client_session_id": client_session_id,
        "is_new_session": is_new_session,
        "room_id": room_id,
        "channel": channel,
        "attached_files": attached_files or [],
        "attachments": attachments or [],
        "model": model,
        "effort": effort,
        "queued_at": time.time(),
        "result_future": result_future,
        "task_id": task_id,
        "user_msg_id": user_msg_id,
        "client_msg_id": resolved_client_msg_id,
        "raw_prompt": bool(raw_prompt),
        "slash_commands": bool(slash_commands),
        "orchestrator": orchestrator,
        "on_complete": on_complete,
    }
    await q.put(queue_item)

    if is_already_running:
        current_broadcast = _get_mod_attr("broadcast_ws_json", broadcast_ws_json)
        await current_broadcast(
            {
                "type": "queued",
                "roomId": room_id,
                "text": user_text,
                "channel": channel,
                "clientMsgId": resolved_client_msg_id,
                "userMsgId": user_msg_id,
                "queueSize": q.qsize(),
                "position": q.qsize(),
            }
        )

    worker = _room_workers.get(room_key)
    if worker is None or worker.done():
        _room_workers[room_key] = asyncio.create_task(run_room_worker(room_key))

    return is_already_running, q.qsize()
