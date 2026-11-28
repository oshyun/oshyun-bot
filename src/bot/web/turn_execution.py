"""web_turn_execution — 단일 대화방 1턴 실행 엔진(execute_chat_turn).

질의 프롬프트 구성(첨부/음성/오케스트레이터 메모리/런타임 메타데이터), 세션 해석,
에이전트 spawn/감시(watch), 자동 재실행(세션 유실·쿼터 소진·빈 응답), 결과 영속화와
실시간 브로드캐스트까지 단일 턴의 전 과정을 실행한다.
전역 상태는 web_runtime_state, 종료 보고는 web_agent_reporting 으로 위임하므로
이 모듈 자체 전역 상태는 없다. 함수 본문은 원본에서 무수정 이관했다.
web_rooms 와의 상호 지연 순환을 피하기 위해 함수 내 lazy import 는 그대로 유지한다.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time
import uuid
from collections.abc import Callable
from typing import Any

import bot.core.agent_configs as agent_configs
import bot.core.chat_db as chat_db
import bot.core.child_error_report as child_error_report
from bot.engine.agent_engine import AGY_DEFAULT_MODEL, check_and_reset_if_idle, clear_session, fmt_cost, get_coding_mode, get_effort, get_engine, get_model, get_session, is_quota_exhausted_error, is_session_stale_error, set_model, set_session, spawn_agent, update_last_activity, watch_agent
from bot.core.bot_logger import logger, vault_log
from bot.master.master_supervisor import MASTER_PATROL_CHANNEL
import bot.master.master_memory as master_memory
import bot.master.master_tasks as master_tasks
from bot.core.paths import CODEDIR, DEFAULT_SSL_CERT_FILE, JOBS_DIR, WORKDIR, child_default_workdir
from bot.engine.stream_parser import _EMPTY_OUTPUT_TEXT, _NO_RESULT_TEXT, extract_agy_transcript_final_response
from bot.web.speech import VOICE_MODE_PROMPT_SUFFIX, clean_text_for_speech

from bot.web.agent_reporting import _finalize_orchestrator_completion
from bot.web.runtime_state import CORE_ORCHESTRATOR_ROOM_ID, CORE_ORCHESTRATOR_TITLE, EXPERT_CODING_ROOM_ID, EXPERT_CODING_TITLE, EXPERT_GENERAL_ROOM_ID, EXPERT_GENERAL_TITLE, EXPERT_VAULT_ROOM_ID, EXPERT_VAULT_TITLE, IDLE_RESET_SECONDS, ORCHESTRATOR_ROOM_IDS, WEB_PORT, WEB_TOKEN, _active_agent_pids, _active_watch_tasks, _get_mod_attr, _room_live_feeds, _room_live_snapshots, _is_orchestrator_room_id, _room_session_ids, _should_sync_global_session, _user_cancelled_rooms, broadcast_ws_json, poke_master_supervisor, is_shutting_down
from bot.web.job_recovery import _remove_web_job, _save_web_job
from bot.web.stream_watch import _run_stream_tail_loop


def is_simple_greeting_fast_path(text: str) -> bool:
    """단순 인사·안부·단답형 발화인지 판별하여 불필요한 LLM Thinking(추론) 지연을 방지한다."""
    if not text:
        return False
    cleaned = re.sub(r"[^\w\s가-힣a-zA-Z]", "", text).strip().lower()
    if not cleaned or len(cleaned) > 20:
        return False
    # 작업/명령/질문 키워드가 포함되어 있으면 단순 인사가 아님
    action_keywords = (
        "해줘", "해라", "알려", "어때", "왜", "어떻게", "언제", "누가",
        "코드", "수정", "작업", "파일", "노트", "일정", "캘린더", "검색",
        "분석", "버그", "에러", "오류", "커밋", "푸시", "확인해", "봐줘",
    )
    if any(kw in cleaned for kw in action_keywords):
        return False
    # 단순 인사/안부 패턴 매칭
    greetings = {
        "안녕", "안녕하세요", "안녕하십니까", "안뇽", "반가워", "반갑습니다",
        "하이", "하이요", "헬로", "hi", "hello", "hey",
        "좋은아침", "좋은아침입니다", "굿모닝", "good morning",
        "수고", "수고해", "수고하세요", "고마워", "고맙습니다", "감사", "감사합니다",
        "누구야", "넌누구야", "너누구야", "ㅎㅇ", "ㅂㅇ", "ㅂㅂ",
    }
    cleaned_no_space = cleaned.replace(" ", "")
    return cleaned in greetings or cleaned_no_space in greetings


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
    mod = sys.modules.get("bot.web.server")
    curr_workdir = getattr(mod, "WORKDIR", WORKDIR) if mod else WORKDIR
    curr_codedir = getattr(mod, "CODEDIR", CODEDIR) if mod else CODEDIR
    if agent_cfg and agent_cfg.get("workdir"):
        target_workdir = str(agent_cfg["workdir"]).strip() or curr_workdir
    elif room_key == EXPERT_CODING_ROOM_ID:
        target_workdir = curr_codedir or curr_workdir
    elif room_key == CORE_ORCHESTRATOR_ROOM_ID:
        # 마스터(오케스트레이터)는 볼트를 직접 읽지 않고 하위 에이전트에 위임하므로
        # 거대한 볼트 대신 봇 작업 디렉토리를 워크스페이스로 사용하여 I/O 오버헤드 차단
        target_workdir = curr_codedir or curr_workdir
    elif room_key in (EXPERT_GENERAL_ROOM_ID, EXPERT_VAULT_ROOM_ID):
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

    from bot.web.rooms import _append_room_message, _update_room_metadata, _load_hidden_rooms

    room_db = current_chat_db.get_room(room_key) if room_key != "__default__" else None
    room_model = (room_db.get("model") if room_db else None) or None
    room_effort = (room_db.get("effort") if room_db else None) or None
    parent_agent_id = (room_db.get("parent_agent_id") if room_db else None) or (
        room_db.get("parentAgentId") if room_db else None
    )
    parent_cfg = agent_configs.get_agent_config(parent_agent_id) if parent_agent_id else None

    room_workdir = (room_db.get("workdir") if room_db else None) or (
        child_default_workdir(parent_agent_id) if room_db else None
    )
    if room_workdir and not is_orchestrator:
        target_workdir = room_workdir

    room_add_dirs = (room_db.get("addDirs") if room_db else None)

    if is_orchestrator and agent_cfg:
        agent_def_model = agent_cfg.get("model")
        agent_def_effort = agent_cfg.get("effort")
        agent_def_add_dirs = agent_cfg.get("addDirs")
    elif parent_cfg:
        agent_def_model = parent_cfg.get("childModel") or parent_cfg.get("model")
        agent_def_effort = parent_cfg.get("childEffort") or parent_cfg.get("effort")
        agent_def_add_dirs = parent_cfg.get("addDirs")
    else:
        agent_def_model = None
        agent_def_effort = None
        agent_def_add_dirs = None

    active_add_dirs = room_add_dirs if room_add_dirs is not None else agent_def_add_dirs

    fallback_model = room_model or agent_def_model or get_model()
    fallback_effort = (
        room_effort
        if room_effort is not None
        else (agent_def_effort or get_effort())
    )

    active_model = model.strip() if (isinstance(model, str) and model.strip()) else fallback_model
    active_effort = effort if effort is not None else fallback_effort

    # 단순 인사·단답 발화 시 LLM reasoning effort를 'low'로 자동 완화하여 불필요한 Thinking 지연(10~15초)을 1초대로 단축 (Fast-Path)
    if is_simple_greeting_fast_path(user_text) and not attached_files and not attachments:
        if active_effort in ("medium", "high", "xhigh"):
            logger.info(
                "[turn_execution] 단순 인사 Fast-Path 감지 ('%s') — reasoning effort 완화: '%s' -> 'low'",
                user_text[:20],
                active_effort,
            )
            active_effort = "low"

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

    if current_chat_db and hasattr(current_chat_db, "update_room_status"):
        try:
            current_chat_db.update_room_status(room_key, "running", active_job_id=job_id, run_updated_at=start_timestamp)
        except Exception as st_err:
            logger.debug("[turn_execution] 방(%s) running 상태 DB 갱신 경고: %s", room_key, st_err)

    if task_id:
        master_tasks.update_task(
            task_id,
            status="running",
            startedAt=start_timestamp,
            feed="생각하는 중…",
            sessionId=current_session_id,
        )
    poke_master_supervisor()

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
    if active_add_dirs is not None:
        spawn_kwargs["add_dirs"] = active_add_dirs
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
        poke_master_supervisor()
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
        poke_master_supervisor()
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
            poke_master_supervisor()
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
    ) -> dict[str, Any] | None:
        """재실행 결과를 {"output": 에이전트 출력 dict, "reply_text": 응답 텍스트} 로 반환한다."""
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
            return {
                "output": retried_output,
                "reply_text": str(retried_output.get("result") or "").strip(),
            }
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
            agent_output = retried["output"]
            reply_text = retried["reply_text"]

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
            agent_output = retried["output"]
            reply_text = retried["reply_text"]

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
            agent_output = retried_empty["output"]
            retried_text = retried_empty["reply_text"]
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
    poke_master_supervisor()

    # 턴 알림 레벨 및 유형 결정:
    # 1. 내부 보고/정기 점검 턴 -> internal_report (silent)
    # 2. 상위 에이전트가 하위 작업을 스폰/위임하고 남긴 중간 안내 -> delegation_ack (silent, 알림 억제)
    # 3. 일반 직접 응답 -> direct_answer (alert, 정상 알림)
    turn_type = "direct_answer"
    notify_level = "alert"
    is_silent_notification = False
    delegated_subagents: list[dict] = []

    if channel in ("task_report", "master_patrol", "schedule_trigger"):
        turn_type = "internal_report"
        notify_level = "silent"
        is_silent_notification = True
    else:
        try:
            from bot.web.orchestrator import extract_delegated_subagents
            delegated_subagents = extract_delegated_subagents(
                collected_steps,
                reply_text or "",
                chat_db_module=current_chat_db,
            )
        except Exception as extract_err:
            logger.debug("[web_turn_execution] 위임 하위 에이전트 추출 예외: %s", extract_err)
            delegated_subagents = []

        active_delegations = [
            d for d in delegated_subagents
            if isinstance(d, dict) and d.get("status") in ("running", "queued", "done") and not d.get("failed")
        ]
        if active_delegations:
            turn_type = "delegation_ack"
            notify_level = "silent"
            is_silent_notification = True

    logger.info(
        "[web_turn_execution] 턴 알림 분류: room=%s, turn_type=%s, notify_level=%s (delegated=%d)",
        room_key,
        turn_type,
        notify_level,
        len(delegated_subagents),
    )

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
        "turnType": turn_type,
        "notifyLevel": notify_level,
        "silentNotification": is_silent_notification,
    }
    if delegated_subagents:
        result_payload["delegatedRooms"] = delegated_subagents
    _room_live_snapshots.pop(room_key, None)
    if current_chat_db and hasattr(current_chat_db, "update_room_status"):
        try:
            final_turn_st = "failed" if is_turn_error else "completed"
            current_chat_db.update_room_status(room_key, final_turn_st, active_job_id=None, run_updated_at=completed_at)
        except Exception:
            pass
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