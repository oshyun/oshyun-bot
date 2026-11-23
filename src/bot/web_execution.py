"""web_execution — 실시간 대화 턴 실행, 워커 큐(FIFO), 스트림 파싱 및 백그라운드 작업 복구 모듈."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import sys
import time
import types
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

# ── web_stream_watch 로 이전된 심볼 재-익스포트(호환 facade) ──
from web_stream_watch import (  # noqa: F401
    _format_tool_step_info,
    extract_stream_events_from_line,
    _run_stream_tail_loop,
    _watch_and_finish_web_job,
)

# ── web_job_recovery 로 이전된 심볼 재-익스포트(호환 facade) ──
from web_job_recovery import (  # noqa: F401
    _get_web_jobs_file,
    _load_web_jobs,
    _save_web_job,
    _remove_web_job,
    _get_web_pending_turns_file,
    persist_pending_room_turns,
    _restore_pending_room_turns,
    _recover_single_web_job,
    _recover_web_jobs,
    _recover_startup_placeholders,
    reap_zombie_sessions,
    run_zombie_reaper_loop,
)

# ── web_runtime_state 로 이전된 심볼 재-익스포트(호환 facade) ──
from web_runtime_state import (  # noqa: F401
    WEB_JOBS_FILE,
    CORE_ORCHESTRATOR_ROOM_ID,
    CORE_ORCHESTRATOR_TITLE,
    EXPERT_GENERAL_ROOM_ID,
    EXPERT_GENERAL_TITLE,
    EXPERT_VAULT_ROOM_ID,
    EXPERT_VAULT_TITLE,
    EXPERT_CODING_ROOM_ID,
    EXPERT_CODING_TITLE,
    ORCHESTRATOR_ROOM_IDS,
    IDLE_RESET_SECONDS,
    _is_orchestrator_room_id,
    WEB_PORT,
    WEB_TOKEN,
    _connected_websockets,
    _active_watch_tasks,
    _active_agent_pids,
    _last_chat_requests,
    _room_queues,
    _room_workers,
    _room_session_ids,
    _room_live_feeds,
    _room_live_snapshots,
    _user_cancelled_rooms,
    _shutting_down,
    _master_supervisor,
    _master_supervisor_task,
    set_shutting_down,
    is_shutting_down,
    detach_active_tasks,
    _get_mod_attr,
    _should_sync_global_session,
    _sync_global_session_if_needed,
    _serialize_live_run_entry,
    get_live_run_for_room,
    build_active_runs_payload,
    broadcast_ws_json,
    _zombie_reaper_task,
    get_master_supervisor,
)


# ── 대화방 실행 상태 및 소켓 레지스트리 ────────────────────────────────────


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
    if was_running and get_master_supervisor() is not None:
        get_master_supervisor().poke()
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
    """하위 작업 완료/중단 시 부모 에이전트(마스터 또는 담당 전문가) 방에 결과를 직접 바이패스 삽입하고 사용자에게 표출한다."""
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
    if not isinstance(room_record, dict):
        room_record = None
    raw_parent = room_record.get("parentAgentId") if room_record else None
    target_parent_id = (
        raw_parent.strip() if isinstance(raw_parent, str) and raw_parent.strip() else CORE_ORCHESTRATOR_ROOM_ID
    )
    if not _is_orchestrator_room_id(target_parent_id):
        target_parent_id = CORE_ORCHESTRATOR_ROOM_ID

    room_title = room_id
    raw_title = room_record.get("title") if room_record else None
    if isinstance(raw_title, str) and raw_title.strip():
        room_title = raw_title.strip()
    else:
        try:
            from web_rooms import _load_chat_rooms_index
            rooms = _load_chat_rooms_index()
            for r in rooms:
                if isinstance(r, dict) and r.get("id") == room_id:
                    rt = r.get("title")
                    if isinstance(rt, str) and rt.strip():
                        room_title = rt.strip()
                    break
        except Exception:
            pass

    # 하위 에이전트 발화자 레이블 결정
    parent_cfg = agent_configs.get_agent_config(raw_parent) if raw_parent else None
    subagent_name = room_title
    if parent_cfg and parent_cfg.get("name"):
        subagent_name = f"{parent_cfg.get('name')} ({room_title})"

    parent_label = (
        str(parent_cfg.get("name"))
        if parent_cfg and parent_cfg.get("name")
        else (
            "일반 대화" if target_parent_id == EXPERT_GENERAL_ROOM_ID
            else ("세컨브레인" if target_parent_id == EXPERT_VAULT_ROOM_ID
            else ("봇개발" if target_parent_id == EXPERT_CODING_ROOM_ID else "마스터"))
        )
    )

    clean_res = result_text.strip() if result_text else ""
    if is_success:
        msg_text = clean_res if clean_res else "작업이 정상적으로 완료되었습니다."
    else:
        err_body = clean_res if clean_res else "작업이 중단되었습니다."
        msg_text = (
            f"⚠️ **[{room_title}]** 하위 작업이 중단되었거나 오류가 발생했습니다.\n\n"
            f"{err_body}\n\n"
            f"👉 상단 칩을 클릭하여 스레드 패널에서 세부 로그를 확인하거나 추가 지시를 전달할 수 있습니다."
        )

    steps = [{
        "kind": "subagent_bypass",
        "tool": "bypass",
        "title": room_title,
        "roomId": room_id,
        "taskId": task_id,
        "agentName": subagent_name,
        "status": "done" if is_success else "error",
        "reason": "정상 완료" if is_success else (clean_res[:60] if clean_res else "작업 중단"),
        "bypassedFromRoomId": room_id,
    }]

    delegated_rooms = [{
        "roomId": room_id,
        "title": room_title,
        "status": "done" if is_success else "error",
        "reason": "정상 완료" if is_success else "오류",
        "taskId": task_id,
    }]

    # 1. 마스터 메모리에 하위 작업 결과 요약 등록 (다음 질문 시 맥락 인지용)
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

    # 2. 마스터(또는 부모) 방에 어시스턴트 메시지로 직접 삽입 (Bypass)
    msg_id = None
    if current_chat_db:
        try:
            msg_id = current_chat_db.add_message(
                room_id=target_parent_id,
                role="assistant",
                text=msg_text,
                channel="thread_bypass",
                steps=steps,
                model=model,
                effort=effort,
                created_at=time.time(),
            )
        except Exception as exc:
            logger.error("[web_jobs] 부모 방(%s) 바이패스 메시지 저장 실패: %s", target_parent_id, exc)

    # 3. 실시간 웹소켓 브로드캐스트 (사용자 화면에 0초 딜레이 즉시 표출)
    try:
        current_broadcast = _get_mod_attr("broadcast_ws_json", broadcast_ws_json)
        if current_broadcast:
            await current_broadcast({
                "type": "result",
                "roomId": target_parent_id,
                "msgId": msg_id,
                "text": msg_text,
                "steps": steps,
                "agentName": subagent_name,
                "model": model,
                "effort": effort,
                "bypassedFromRoomId": room_id,
                "silentNotification": True,
                "delegatedRooms": delegated_rooms,
                "createdAt": time.time(),
            })
    except Exception as exc:
        logger.error("[web_jobs] 부모 방(%s) 바이패스 웹소켓 브로드캐스트 실패: %s", target_parent_id, exc)

    # 4. 완료 보고 vs 오류 보고 분기:
    # 정상 완료(is_success=True) 시: 마스터가 다시 정리해서 보고하지 않고 사용자에게 100% 바이패스 (2차 턴 차단)
    # 오류 발생(is_success=False) 시: 부모 에이전트(또는 마스터)에게 오류 보고를 전달하여 후속 조치/알림
    if not is_success:
        try:
            if not (model or effort) and room_record:
                raw_m = room_record.get("model")
                raw_e = room_record.get("effort")
                model = raw_m if isinstance(raw_m, str) else None
                effort = raw_e if isinstance(raw_e, str) else None
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
            enqueue_fn = _get_mod_attr("enqueue_room_turn", enqueue_room_turn)
            await enqueue_fn(
                room_id=target_parent_id,
                user_text=report_msg,
                channel="task_report",
                task_id=task_id,
            )
            logger.info("[web_jobs] %s 에이전트(%s)에게 오류 보고 전달 완료 (task_id=%s, room=%s)", parent_label, target_parent_id, task_id, room_id)
        except Exception as exc:
            logger.error("[web_jobs] %s 에이전트 오류 보고 전달 실패: %s", parent_label, exc)
            if task_id:
                master_tasks.release_report(task_id)  # 감독 루프 안전망이 재시도
            elif error_claimed:
                child_error_report.release_error_report(room_id, result_text)

    mark_room_completion_reported(room_id)
    logger.info(
        "[web_jobs] %s 에이전트(%s) 방에 하위 작업(%s) 결과 바이패스 전달 완료 (task_id=%s, msg_id=%s)",
        parent_label,
        target_parent_id,
        room_id,
        task_id,
        msg_id,
    )


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
    elif room_key == EXPERT_GENERAL_ROOM_ID:
        session_namespace = "general_"
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
    elif room_key in (CORE_ORCHESTRATOR_ROOM_ID, EXPERT_GENERAL_ROOM_ID, EXPERT_VAULT_ROOM_ID):
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
        elif room_key == EXPERT_GENERAL_ROOM_ID:
            new_title = EXPERT_GENERAL_TITLE
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
    if get_master_supervisor():
        get_master_supervisor().poke()

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
        if get_master_supervisor():
            get_master_supervisor().poke()
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
        if get_master_supervisor():
            get_master_supervisor().poke()
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
        is_user_cancel = (room_key in _user_cancelled_rooms) and not is_shutting_down()
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
            if get_master_supervisor():
                get_master_supervisor().poke()
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
    _EMPTY_OR_PLACEHOLDER_REPLIES = (
        "",
        _EMPTY_OUTPUT_TEXT,
        _NO_RESULT_TEXT,
        "(결과 없음)",
        "(빈 출력)",
        "⚠️ 응답이 중단되었습니다.",
        "(도구 실행이 완료되었습니다)",
        "(응답 없음)",
    )
    if not reply_text or reply_text in _EMPTY_OR_PLACEHOLDER_REPLIES:
        curr_stream = (live_snapshot.get("streamText") or "").strip()
        if curr_stream:
            reply_text = curr_stream
        elif resolved_session_id:
            transcript_txt = extract_agy_transcript_final_response(resolved_session_id)
            if transcript_txt:
                reply_text = transcript_txt

    if not reply_text or reply_text in (_EMPTY_OUTPUT_TEXT, _NO_RESULT_TEXT, "(결과 없음)", "(빈 출력)"):
        curr_stream = (live_snapshot.get("streamText") or "").strip()
        if curr_stream:
            reply_text = curr_stream
        elif agent_output.get("interrupted"):
            reply_text = "⚠️ 응답이 중단되었습니다."
        elif collected_steps:
            reply_text = "(도구 실행이 완료되었습니다)"
        else:
            reply_text = "(응답 없음)"

    # AGY 세션이 에러 없이 종료되었으나 도구 호출도 텍스트도 전혀 없이 (응답 없음)으로 끝난 경우 1회 자동 재시도
    if (
        reply_text == "(응답 없음)"
        and not collected_steps
        and not agent_output.get("interrupted")
        and room_key not in _user_cancelled_rooms
        and not is_shutting_down()
    ):
        logger.warning(
            "[voice-web] 빈 응답 감지 (room=%s, session=%s) — 1회 자동 재실행합니다.",
            room_key,
            resolved_session_id,
        )
        retried_empty = await _retry_turn(
            "빈 응답이 감지되어 자동으로 다시 요청합니다...",
            resolved_session_id,
        )
        if retried_empty is not None:
            agent_output, retried_text = retried_empty
            retried_sid = agent_output.get("session_id") or resolved_session_id
            if retried_sid:
                resolved_session_id = retried_sid
                _room_session_ids[room_key] = retried_sid
            if not retried_text or retried_text in _EMPTY_OR_PLACEHOLDER_REPLIES:
                curr_stream = (live_snapshot.get("streamText") or "").strip()
                if curr_stream:
                    retried_text = curr_stream
                elif resolved_session_id:
                    transcript_txt = extract_agy_transcript_final_response(resolved_session_id)
                    if transcript_txt:
                        retried_text = transcript_txt
            if retried_text and retried_text not in (_EMPTY_OUTPUT_TEXT, _NO_RESULT_TEXT, "(결과 없음)", "(빈 출력)"):
                reply_text = retried_text
            elif collected_steps:
                reply_text = "(도구 실행이 완료되었습니다)"
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
    if get_master_supervisor():
        get_master_supervisor().poke()

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


# ── 전달 프록시 facade ──────────────────────────────────────────────────────
# 임시 facade — 새 모듈(web_runtime_state 등) 경유 전환 완료 후 축소 예정.
# 제거 조건: 모든 importer 와 테스트가 새 모듈을 직접 참조·patch 하여
# web_execution 재-익스포트를 사용하는 곳이 0건이 되었을 때 축소/제거한다.
# 그 전에는 외부 import(web_server/web_orchestrator/web_ws/web_rooms/web_automation/
# 테스트)가 깨지므로 facade 축소를 금지한다.
#
# 동작: 이 모듈 속성에 setattr 이 발생하면(테스트 mock.patch, web_server 의
# 런타임 주입 등) 값을 아래 모듈들 중 같은 이름을 가진 모든 모듈에 전달한다.
# 이로써 patch('web_execution.X') 가 새 모듈 내부 호출까지 관통한다.
# mutable 전역(dict/set)은 어느 모듈에서도 재바인딩하지 않는다(요소 조작만).

_NEW_STATE_MODULES = (
    "web_runtime_state",
    "web_job_recovery",
    "web_stream_watch",
    "web_room_queue",
    "web_agent_reporting",
    "web_turn_execution",
)


class _FacadeModule(types.ModuleType):
    """setattr 을 상태 소유 모듈(web_runtime_state 등)로 전달하는 모듈 프록시."""

    def __setattr__(self, name: str, value: object) -> None:
        for mod_name in _NEW_STATE_MODULES:
            mod = sys.modules.get(mod_name)
            if mod is not None and name in vars(mod):
                setattr(mod, name, value)
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _FacadeModule
