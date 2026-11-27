"""web_room_queue — 대화방 FIFO 큐·워커 및 즉시 중단(취소) 처리 모듈.

enqueue_room_turn 이 룸 큐에 턴을 적재하고 run_room_worker 가 순차 소비하며,
cancel_active_run 이 진행 중 에이전트 프로세스(kill_job/killpg)와 대기열·스냅샷을
즉시 정리한다. 큐 적재와 소비·중단이 같은 런타임 상태를 다루므로 한 모듈로 묶는다.
web_runtime_state 의 mutable 전역을 참조만 하고 절대 재바인딩하지 않는다.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any
from collections.abc import Callable

from bot.engine.agent_engine import kill_job
from bot.core.bot_logger import logger
from bot.master.master_supervisor import MASTER_PATROL_CHANNEL
import bot.master.master_tasks as master_tasks



from bot.web.runtime_state import CORE_ORCHESTRATOR_ROOM_ID, _active_agent_pids, _active_watch_tasks, _get_mod_attr, _room_live_feeds, _room_live_snapshots, _room_queues, _room_session_ids, _room_workers, _user_cancelled_rooms, broadcast_ws_json, poke_master_supervisor



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
                        try:
                            from bot.web.agent_reporting import cancel_slot_message_for_task
                            cancel_slot_message_for_task(queued_tid, r_key, reason="대기열에서 취소됨")
                        except Exception:
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
            try:
                from bot.web.agent_reporting import cancel_slot_message_for_task
                cancel_slot_message_for_task(snap_tid, r_key, reason="사용자에 의해 작업이 취소되었습니다.")
            except Exception:
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
    if was_running:
        poke_master_supervisor()
    return was_running

async def run_room_worker(r_key: str) -> None:
    from bot.web.turn_execution import execute_chat_turn  # 순환 import 회피 지연 임포트
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

    return {"already_running": is_already_running, "queue_size": q.qsize()}
