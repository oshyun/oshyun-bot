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

# ── web_turn_execution 로 이전된 심볼 재-익스포트(호환 facade) ──
from web_turn_execution import (  # noqa: F401
    execute_chat_turn,
)

# ── web_agent_reporting 로 이전된 심볼 재-익스포트(호환 facade) ──
from web_agent_reporting import (  # noqa: F401
    _recent_reported_rooms,
    mark_room_completion_reported,
    is_room_completion_recently_reported,
    _invoke_on_complete_hook,
    _finalize_orchestrator_completion,
    _room_parent_id,
    _notify_master_agent,
)

# ── web_room_queue 로 이전된 심볼 재-익스포트(호환 facade) ──
from web_room_queue import (  # noqa: F401
    cancel_active_run,
    _is_task_rec_aborted,
    run_room_worker,
    is_room_busy,
    enqueue_room_turn,
)

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
