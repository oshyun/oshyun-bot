"""web_agent_reporting — 완료 보고·마스터 바이패스 보고·턴 종료 일원화 모듈.

턴 종료 시 작업 원장 갱신·on_complete 훅 실행·부모 에이전트 자동 보고를
일원화한다(_finalize_orchestrator_completion). 하위 작업 결과를 부모 방에
직접 삽입하고 웹소켓으로 즉시 표출하며(_notify_master_agent), 완료 보고
TTL 캐시(_recent_reported_rooms)로 중복 보고를 막는다.
execute_chat_turn 의 종료 경로 계약(정상/실패/취소/종료중 스킵)의 실체가 여기 있다.
web_rooms 의 parentAgentId 조회는 순환 import 회피를 위해 함수 내 지연 import 를 유지한다.
"""

from __future__ import annotations

import inspect
import time
from collections.abc import Callable
from typing import Any

import agent_configs
import chat_db
import child_error_report
from bot_logger import logger
import master_memory
import master_tasks

from web_jobs import _is_task_rec_aborted
from web_runtime_state import (
    CORE_ORCHESTRATOR_ROOM_ID,
    EXPERT_CODING_ROOM_ID,
    EXPERT_GENERAL_ROOM_ID,
    EXPERT_VAULT_ROOM_ID,
    _get_mod_attr,
    _is_orchestrator_room_id,
    broadcast_ws_json,
)



_recent_reported_rooms: dict[str, float] = {}

def mark_room_completion_reported(room_id: str | None) -> None:
    """서버 완료 훅이 해당 대화방의 완료 보고를 전송했음을 기록(중복 보고 방지용)."""
    rid = str(room_id or "").strip()
    if not rid:
        return
    now = time.monotonic()
    _recent_reported_rooms[rid] = now
    if len(_recent_reported_rooms) > 256:
        # 만료 정리 기준을 읽기 TTL(120초, is_room_completion_recently_reported 기본값)과 일치시킨다.
        expired = [k for k, ts in _recent_reported_rooms.items() if now - ts > 120.0]
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
    from web_room_queue import enqueue_room_turn  # 순환 import 회피 지연 임포트
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
