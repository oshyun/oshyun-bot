"""web_stream_watch — stream-json 파싱·실시간 tail·백그라운드 job 감시 모듈.

stream-json 한 줄에서 Thinking/도구 실행 이벤트를 추출하고
(_format_tool_step_info, extract_stream_events_from_line),
출력 파일과 AGY transcript 를 tail 하며 실시간 브로드캐스트·DB 증분 커밋을 수행하고
(_run_stream_tail_loop), 기동 시 살아있는 백그라운드 job 을 재부착 감시해
완료 시 결과를 배달한다(_watch_and_finish_web_job).
파서를 tail 루프가 소비하므로 같은 모듈로 묶는다.
web_runtime_state 의 mutable 전역을 참조만 하고 절대 재바인딩하지 않는다.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
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
import master_memory
import master_tasks
from stream_parser import (
    _EMPTY_OUTPUT_TEXT,
    _NO_RESULT_TEXT,
    extract_agy_transcript_final_response,
)
from web_jobs import (
    compute_remaining_timeout as _compute_remaining_timeout,
    remove_job_files as _remove_job_files,
)
from web_speech import clean_text_for_speech

from web_agent_reporting import _finalize_orchestrator_completion, _notify_master_agent
from web_jobs import _is_task_rec_aborted
from web_runtime_state import (
    ORCHESTRATOR_ROOM_IDS,
    _active_agent_pids,
    _active_watch_tasks,
    _get_mod_attr,
    _room_live_feeds,
    _room_live_snapshots,
    _room_session_ids,
    _sync_global_session_if_needed,
    _user_cancelled_rooms,
    broadcast_ws_json,
    poke_master_supervisor,
    is_shutting_down,
)



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
        poke_master_supervisor()

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

        poke_master_supervisor()

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
        is_user_cancel = (room_id in _user_cancelled_rooms) and not is_shutting_down()
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
            poke_master_supervisor()
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

def _remove_web_job(job_id: str) -> None:
    """web_job 레코드 제거는 영속화 모듈(web_job_recovery)로 위임한다(순환 import 회피 지연 임포트)."""
    from web_job_recovery import _remove_web_job as _remove

    _remove(job_id)


