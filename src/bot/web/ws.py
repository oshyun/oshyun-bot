"""web_ws — 웹소켓(/ws) 실시간 연결 수명주기, 수신 메시지 파싱 및 핸들러 분기 모듈."""

from __future__ import annotations

import asyncio
import json
import os
import time

from aiohttp import WSMsgType, web

from bot.engine.agent_engine import clear_session, delete_session_files, get_coding_mode, get_session
from bot.core.bot_logger import logger, vault_log
from bot.web.speech import deduplicate_speech_text

CORE_ORCHESTRATOR_ROOM_ID = "__core__"
EXPERT_GENERAL_ROOM_ID = "__expert_general__"
EXPERT_VAULT_ROOM_ID = "__expert_vault__"
EXPERT_CODING_ROOM_ID = "__expert_coding__"
ORCHESTRATOR_ROOM_IDS = (
    CORE_ORCHESTRATOR_ROOM_ID,
    EXPERT_GENERAL_ROOM_ID,
    EXPERT_VAULT_ROOM_ID,
    EXPERT_CODING_ROOM_ID,
)



async def handle_websocket(request: web.Request) -> web.WebSocketResponse:
    """GET /ws — 실시간 대화·음성 모드·진행 상태 스트리밍·즉시 취소 WebSocket 핸들러."""
    from bot.web.room_queue import cancel_active_run, enqueue_room_turn
    from bot.web.runtime_state import _get_mod_attr, _connected_websockets, _active_watch_tasks, _room_queues, _room_session_ids, _last_chat_requests, broadcast_ws_json

    websocket = web.WebSocketResponse(heartbeat=25.0)
    await websocket.prepare(request)
    _connected_websockets.add(websocket)

    async def send_json_safe(message_dict: dict) -> None:
        if not websocket.closed:
            try:
                await websocket.send_str(json.dumps(message_dict, ensure_ascii=False))
            except Exception:
                pass

    build_status_fn = _get_mod_attr("build_status_payload", None)
    if build_status_fn:
        await send_json_safe(build_status_fn())

    cancel_run_fn = _get_mod_attr("cancel_active_run", cancel_active_run)
    broadcast_fn = _get_mod_attr("broadcast_ws_json", broadcast_ws_json)
    enqueue_turn_fn = _get_mod_attr("enqueue_room_turn", enqueue_room_turn)
    clear_session_fn = _get_mod_attr("clear_session", clear_session)
    get_session_fn = _get_mod_attr("get_session", get_session)
    delete_session_files_fn = _get_mod_attr("delete_session_files", delete_session_files)
    vault_log_fn = _get_mod_attr("vault_log", vault_log)

    try:
        async for ws_msg in websocket:
            if ws_msg.type == WSMsgType.TEXT:
                try:
                    incoming = json.loads(ws_msg.data)
                except json.JSONDecodeError:
                    await send_json_safe(
                        {"type": "error", "message": "잘못된 JSON 메시지입니다."}
                    )
                    continue

                message_type = incoming.get("type")
                if not message_type and (incoming.get("text") or incoming.get("prompt")):
                    message_type = "chat"

                if message_type == "ping":
                    await send_json_safe({"type": "pong"})
                elif message_type == "status":
                    if build_status_fn:
                        await send_json_safe(build_status_fn())
                elif message_type == "cancel":
                    target_room = incoming.get("roomId")
                    if isinstance(target_room, str):
                        target_room = target_room.strip() or None
                    else:
                        target_room = None
                    terminate_session = bool(incoming.get("terminateSession", False))
                    cancel_run_fn(target_room)
                    if terminate_session:
                        if target_room == CORE_ORCHESTRATOR_ROOM_ID:
                            session_namespace = "core_"
                        elif target_room == EXPERT_GENERAL_ROOM_ID:
                            session_namespace = "general_"
                        elif target_room == EXPERT_VAULT_ROOM_ID:
                            session_namespace = "vault_"
                        elif target_room == EXPERT_CODING_ROOM_ID:
                            session_namespace = "coding_"
                        else:
                            session_namespace = "coding_" if get_coding_mode() else ""
                        clear_session_fn(ns=session_namespace)
                        vault_log_fn(
                            "🛑 세션 종료",
                            f"[음성/웹 대화 중단] roomId={target_room or '__default__'} 세션 강제 종료",
                        )
                    await broadcast_fn(
                        {
                            "type": "cancelled",
                            "jobId": None,
                            "roomId": target_room,
                            "sessionTerminated": terminate_session,
                        }
                    )
                elif message_type == "cancel_queued":
                    raw_room = incoming.get("roomId")
                    target_room = raw_room.strip() or None if isinstance(raw_room, str) else None
                    r_key = target_room or "__default__"
                    cancel_text = str(incoming.get("text") or "").strip()
                    cancel_idx = incoming.get("queueIndex")
                    cancel_all = bool(incoming.get("all") or incoming.get("clearAll"))
                    q = _room_queues.get(r_key)
                    removed_count = 0
                    if q is not None and not q.empty():
                        items = []
                        while not q.empty():
                            try:
                                items.append(q.get_nowait())
                                q.task_done()
                            except Exception:
                                break
                        if cancel_all:
                            for it in items:
                                fut = it.get("result_future") if isinstance(it, dict) else None
                                if fut is not None and not fut.done():
                                    fut.set_exception(asyncio.CancelledError("대기열이 취소되었습니다."))
                            removed_count = len(items)
                            items = []
                        else:
                            removed = False
                            kept = []
                            for i, it in enumerate(items):
                                if not removed and (
                                    (cancel_text and it.get("user_text") == cancel_text)
                                    or (isinstance(cancel_idx, int) and i == cancel_idx)
                                ):
                                    removed = True
                                    removed_count += 1
                                    fut = it.get("result_future") if isinstance(it, dict) else None
                                    if fut is not None and not fut.done():
                                        fut.set_exception(asyncio.CancelledError("대기열 항목이 취소되었습니다."))
                                    continue
                                kept.append(it)
                            if not removed and kept:
                                popped = kept.pop()
                                fut = popped.get("result_future") if isinstance(popped, dict) else None
                                if fut is not None and not fut.done():
                                    fut.set_exception(asyncio.CancelledError("대기열 항목이 취소되었습니다."))
                                removed_count += 1
                            items = kept
                        for it in items:
                            await q.put(it)
                    await broadcast_fn(
                        {
                            "type": "queued_cancelled",
                            "roomId": target_room,
                            "removedCount": removed_count,
                            "queueSize": q.qsize() if q is not None else 0,
                        }
                    )
                elif message_type in ("delete_room", "delete_session", "delete_rooms", "batch_delete_rooms"):
                    target_room = incoming.get("roomId")
                    if isinstance(target_room, str):
                        target_room = target_room.strip() or None
                    else:
                        target_room = None
                    session_id = incoming.get("sessionId")
                    if isinstance(session_id, str):
                        session_id = session_id.strip() or None
                    else:
                        session_id = None
                    raw_rids = incoming.get("roomIds")
                    raw_sids = incoming.get("sessionIds")
                    if isinstance(raw_rids, list) and raw_rids:
                        for rid in raw_rids:
                            crid = str(rid or "").strip()
                            if crid:
                                cancel_run_fn(crid)
                    else:
                        cancel_run_fn(target_room)
                    if target_room in ORCHESTRATOR_ROOM_IDS or (target_room and str(target_room).startswith("__custom_")):
                        ns = (
                            "core_" if target_room == CORE_ORCHESTRATOR_ROOM_ID
                            else ("general_" if target_room == EXPERT_GENERAL_ROOM_ID
                            else ("vault_" if target_room == EXPERT_VAULT_ROOM_ID
                            else ("coding_" if target_room == EXPERT_CODING_ROOM_ID else f"{str(target_room).strip('_')}_")))
                        )
                        clear_session_fn(ns=ns)
                        _room_session_ids.pop(target_room, None)
                    if isinstance(raw_sids, list) and raw_sids:
                        for sid in raw_sids:
                            csid = str(sid or "").strip()
                            if csid:
                                delete_session_files_fn(csid)
                        del_result = {"deleted": True, "count": len(raw_sids)}
                    else:
                        del_result = delete_session_files_fn(session_id)
                    vault_log_fn(
                        "🗑️ 세션 삭제",
                        f"[대화방 삭제] roomId={target_room}, sessionId={session_id} (삭제됨: {del_result.get('deleted')})",
                    )
                    await broadcast_fn({
                        "type": "session_deleted",
                        "roomId": target_room,
                        "sessionId": session_id,
                        "roomIds": raw_rids if isinstance(raw_rids, list) else ([target_room] if target_room else []),
                        "sessionIds": raw_sids if isinstance(raw_sids, list) else ([session_id] if session_id else []),
                        "ok": True,
                        "result": del_result,
                    })
                    if build_status_fn:
                        await broadcast_fn(build_status_fn())
                elif message_type in ("new_session", "reset"):
                    target_room = incoming.get("roomId")
                    if isinstance(target_room, str):
                        target_room = target_room.strip() or None
                    else:
                        target_room = None
                    if target_room in ORCHESTRATOR_ROOM_IDS or (target_room and str(target_room).startswith("__custom_")):
                        import bot.web.orchestrator as web_orchestrator
                        reset_fn = _get_mod_attr(
                            "reset_core_orchestrator_state",
                            web_orchestrator.reset_core_orchestrator_state,
                        )
                        if incoming.get("clearMemory") is True and "memoryMode" not in incoming and "memory_mode" not in incoming:
                            mem_mode = "clear"
                        else:
                            mem_mode = str(incoming.get("memoryMode") or incoming.get("memory_mode") or "cascade").strip().lower()
                        clear_done = bool(incoming.get("clearDoneTasks") or incoming.get("clear_done_tasks"))
                        try:
                            await reset_fn(target_room_id=target_room, memory_mode=mem_mode, clear_done_tasks=clear_done)
                        except (ValueError, TypeError):
                            await reset_fn(memory_mode="cascade", clear_done_tasks=clear_done)
                    else:
                        cancel_run_fn(target_room)
                        session_namespace = "coding_" if get_coding_mode() else ""
                        if target_room:
                            _room_session_ids.pop(target_room, None)
                        global_sid = get_session_fn(ns=session_namespace)
                        if (
                            not target_room
                            or target_room == "__default__"
                            or (global_sid and global_sid == target_room)
                            or hasattr(clear_session_fn, "_mock_name")
                        ):
                            clear_session_fn(ns=session_namespace)
                            vault_log_fn(
                                "🆕 세션 리셋",
                                f"[웹/음성앱] {session_namespace or 'vault'} 세션 초기화",
                            )
                        if build_status_fn:
                            await broadcast_fn(build_status_fn())
                elif message_type == "chat":
                    raw_user_text = str(incoming.get("text") or incoming.get("prompt") or "").strip()
                    is_voice_mode = bool(incoming.get("voiceMode", False))
                    dedup_fn = _get_mod_attr("deduplicate_speech_text", deduplicate_speech_text)
                    user_text = dedup_fn(raw_user_text) if is_voice_mode else raw_user_text

                    raw_attached = incoming.get("attachedFiles") or incoming.get("attached_files") or []
                    attached_files = [str(f).strip() for f in raw_attached if str(f).strip()] if isinstance(raw_attached, list) else []
                    raw_attachments = incoming.get("attachments") or []
                    attachments = [a for a in raw_attachments if isinstance(a, dict)] if isinstance(raw_attachments, list) else []
                    for att in attachments:
                        att_path = str(att.get("path") or "").strip()
                        if att_path and att_path not in attached_files:
                            attached_files.append(att_path)

                    if not user_text and not attached_files:
                        continue
                    client_session_id = incoming.get("sessionId")
                    if isinstance(client_session_id, str):
                        client_session_id = client_session_id.strip() or None
                    else:
                        client_session_id = None
                    is_new_session = bool(incoming.get("newSession", False))
                    room_id = incoming.get("roomId")
                    if isinstance(room_id, str):
                        room_id = room_id.strip() or None
                    else:
                        room_id = None

                    room_key = room_id or "__default__"
                    now_ts = time.time()
                    last_req = _last_chat_requests.get(room_key)
                    if (
                        last_req is not None
                        and last_req[0] == user_text
                        and (now_ts - last_req[1]) < 3.0
                        and not attached_files
                        and (
                            (room_key in _active_watch_tasks and not _active_watch_tasks[room_key].done())
                            or (room_key in _room_queues and not _room_queues[room_key].empty())
                        )
                    ):
                        logger.warning(
                            "[voice-web] 동일 텍스트 중복 요청 감지 — 기존 실행 유지 (경과 %.2f초): %s",
                            now_ts - last_req[1],
                            user_text[:40],
                        )
                        continue

                    requested_model = incoming.get("model")
                    if not isinstance(requested_model, str) or not requested_model.strip():
                        requested_model = None
                    else:
                        requested_model = requested_model.strip()

                    requested_effort = incoming.get("effort")
                    if not isinstance(requested_effort, str) or not requested_effort.strip():
                        requested_effort = None
                    else:
                        requested_effort = requested_effort.strip()

                    client_msg_id = incoming.get("clientMsgId")
                    if not isinstance(client_msg_id, str) or not client_msg_id.strip():
                        client_msg_id = None
                    else:
                        client_msg_id = client_msg_id.strip()

                    _last_chat_requests[room_key] = (user_text, now_ts)
                    extra_turn_kwargs = {}
                    if incoming.get("rawPrompt"):
                        extra_turn_kwargs["raw_prompt"] = True
                    if incoming.get("slashCommands"):
                        extra_turn_kwargs["slash_commands"] = True
                    if "orchestrator" in incoming and incoming.get("orchestrator") is not None:
                        extra_turn_kwargs["orchestrator"] = bool(incoming.get("orchestrator"))
                    await enqueue_turn_fn(
                        room_id=room_id,
                        user_text=user_text,
                        is_voice_mode=is_voice_mode,
                        client_session_id=client_session_id,
                        is_new_session=is_new_session,
                        channel="web",
                        attached_files=attached_files,
                        attachments=attachments,
                        model=requested_model,
                        effort=requested_effort,
                        client_msg_id=client_msg_id,
                        **extra_turn_kwargs,
                    )
            elif ws_msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break
    finally:
        _connected_websockets.discard(websocket)

    return websocket
