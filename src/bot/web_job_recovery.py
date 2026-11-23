"""web_job_recovery — 웹 job 영속화·재시작 복구·좀비 세션 회수 모듈.

web_jobs.json 의 원자적 load/save/remove 와 대기 턴 영속화/복원
(persist_pending_room_turns, _restore_pending_room_turns), 재시작 복구
(_recover_single_web_job, _recover_web_jobs, _recover_startup_placeholders),
좀비 세션 능동 회수(reap_zombie_sessions, run_zombie_reaper_loop)를 담당한다.
영속화와 복구가 같은 파일 포맷/레코드를 다루므로 한 모듈로 묶는다.
web_runtime_state 의 mutable 전역을 참조만 하고 절대 재바인딩하지 않는다.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import chat_db
from agent_engine import (
    detached_pid_alive,
    get_effort,
    get_engine,
    get_model,
    kill_job,
    watch_agent,
)
from bot_logger import logger
from json_store import read_json, write_json_atomic
from master_supervisor import MASTER_PATROL_CHANNEL
import master_tasks
from paths import JOBS_DIR
from stream_parser import (
    _EMPTY_OUTPUT_TEXT,
    _NO_RESULT_TEXT,
    extract_agy_transcript_final_response,
)
from web_jobs import (
    extract_steps_from_jsonl as _extract_steps_from_jsonl,
    load_web_jobs as _load_web_jobs_from_file,
    remove_job_files as _remove_job_files,
    remove_web_job as _remove_web_job_from_file,
    save_web_job as _save_web_job_to_file,
    sweep_orphan_job_files as _sweep_orphan_job_files,
)

from web_agent_reporting import _notify_master_agent
from web_room_queue import enqueue_room_turn
from web_stream_watch import _watch_and_finish_web_job, extract_stream_events_from_line
from web_runtime_state import (
    ORCHESTRATOR_ROOM_IDS,
    WEB_JOBS_FILE,
    _active_agent_pids,
    _active_watch_tasks,
    _get_mod_attr,
    _room_live_feeds,
    _room_live_snapshots,
    _room_queues,
    _room_session_ids,
    _room_workers,
    _sync_global_session_if_needed,
    _user_cancelled_rooms,
    broadcast_ws_json,
    build_active_runs_payload,
    get_master_supervisor,
    is_shutting_down,
)



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

def _is_task_rec_aborted(rec: Any) -> bool:
    """작업 레코드 중단 판별은 web_room_queue 로 위임한다(순환 import 회피 지연 임포트)."""
    from web_room_queue import _is_task_rec_aborted as _fn

    return _fn(rec)


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
        if get_master_supervisor():
            get_master_supervisor().poke()
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

    if get_master_supervisor():
        get_master_supervisor().poke()

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

        if get_master_supervisor():
            get_master_supervisor().poke()

        reaped_rooms.append(room_id)

    return reaped_rooms

async def run_zombie_reaper_loop(
    interval: float = 60.0,
    max_inactive_seconds: float = 900.0,
    max_elapsed_seconds: float = 1800.0,
) -> None:
    """주기적으로 고아 세션을 감지하여 회수하는 백그라운드 루프."""
    logger.info("[zombie_reaper] 고아 세션 능동 회수 루프 가동 (주기: %.0f초, 무응답 한계: %.0f초, 최대 실행: %.0f초)", interval, max_inactive_seconds, max_elapsed_seconds)
    while not is_shutting_down():
        try:
            await asyncio.sleep(interval)
            if is_shutting_down():
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
