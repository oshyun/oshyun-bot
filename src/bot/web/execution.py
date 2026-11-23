"""web_execution — 테스트 mock.patch 전달 전용 임시 facade.

런타임 상태/주입의 실제 정의처는 web_runtime_state 이며, web_server 는 이미
web_runtime_state 속성에 직접 대입하는 방식으로 전환되었다. 이 모듈은 더 이상
런타임 로직을 갖지 않고, 기존 테스트가 `patch.object(web_execution, "X")` /
`patch("bot.web.execution.X")` 로 patch 하는 심볼과 src 내 from-import 호환
심볼을 재-익스포트하는 얇은 facade 만 남는다.

동작: 이 모듈 속성에 setattr 이 발생하면(테스트 mock.patch 등) 값을 아래
상태 소유 모듈들 중 같은 이름을 가진 모든 모듈에 전달한다. 이로써
patch('bot.web.execution.X') 가 새 모듈 내부 호출까지 관통한다.
mutable 전역(dict/set)은 어느 모듈에서도 재바인딩하지 않는다(요소 조작만).

제거 조건: 모든 importer 와 테스트가 새 모듈을 직접 참조·patch 하여
web_execution 재-익스포트를 사용하는 곳이 0건이 되었을 때 삭제한다.
"""

from __future__ import annotations

import sys
import types

# ── 테스트 patch 대상 심볼(관통 patch 를 위해 이 모듈에 존재해야 함) ──
import bot.core.chat_db as chat_db  # noqa: F401
import bot.master.master_memory as master_memory  # noqa: F401
import bot.master.master_tasks as master_tasks  # noqa: F401
from bot.engine.agent_engine import detached_pid_alive, kill_job, set_session, spawn_agent, update_last_activity, watch_agent
from bot.core.bot_logger import vault_log  # noqa: F401
from bot.core.json_store import read_json  # noqa: F401
from bot.core.paths import JOBS_DIR  # noqa: F401
from bot.engine.stream_parser import extract_agy_transcript_final_response  # noqa: F401

# ── web_turn_execution 로 이전된 심볼 재-익스포트(호환 facade) ──
from bot.web.turn_execution import execute_chat_turn

# ── web_agent_reporting 로 이전된 심볼 재-익스포트(호환 facade) ──
from bot.web.agent_reporting import _recent_reported_rooms, mark_room_completion_reported, is_room_completion_recently_reported, _invoke_on_complete_hook, _finalize_orchestrator_completion, _room_parent_id, _notify_master_agent

# ── web_room_queue 로 이전된 심볼 재-익스포트(호환 facade) ──
from bot.web.room_queue import cancel_active_run, run_room_worker, is_room_busy, enqueue_room_turn

# ── web_stream_watch 로 이전된 심볼 재-익스포트(호환 facade) ──
from bot.web.stream_watch import _format_tool_step_info, extract_stream_events_from_line, _watch_and_finish_web_job

# ── web_job_recovery 로 이전된 심볼 재-익스포트(호환 facade) ──
from bot.web.job_recovery import _load_web_jobs, _save_web_job, _remove_web_job, _get_web_pending_turns_file, persist_pending_room_turns, _restore_pending_room_turns, _recover_single_web_job, _recover_web_jobs, _recover_startup_placeholders, reap_zombie_sessions, run_zombie_reaper_loop

# ── web_runtime_state 로 이전된 심볼 재-익스포트(호환 facade) ──
from bot.web.runtime_state import WEB_JOBS_FILE, CORE_ORCHESTRATOR_ROOM_ID, CORE_ORCHESTRATOR_TITLE, EXPERT_GENERAL_ROOM_ID, EXPERT_GENERAL_TITLE, EXPERT_VAULT_ROOM_ID, EXPERT_VAULT_TITLE, EXPERT_CODING_ROOM_ID, EXPERT_CODING_TITLE, ORCHESTRATOR_ROOM_IDS, IDLE_RESET_SECONDS, _is_orchestrator_room_id, _connected_websockets, _active_watch_tasks, _active_agent_pids, _last_chat_requests, _room_queues, _room_workers, _room_session_ids, _room_live_feeds, _room_live_snapshots, _user_cancelled_rooms, set_shutting_down, is_shutting_down, detach_active_tasks, _get_mod_attr, _should_sync_global_session, _sync_global_session_if_needed, _serialize_live_run_entry, get_live_run_for_room, build_active_runs_payload, broadcast_ws_json, get_master_supervisor, poke_master_supervisor


# ── 전달 프록시 facade ──────────────────────────────────────────────────────

_NEW_STATE_MODULES = (
    "bot.web.runtime_state",
    "bot.web.job_recovery",
    "bot.web.stream_watch",
    "bot.web.room_queue",
    "bot.web.agent_reporting",
    "bot.web.turn_execution",
)


class _FacadeModule(types.ModuleType):
    """setattr/getattr 을 상태 소유 모듈(web_runtime_state 등)로 전달하는 모듈 프록시.

    setattr 은 같은 이름을 가진 모든 상태 소유 모듈에 값을 전달한다.
    getattr 은 facade 자체 속성(from-import 재-익스포트 포함)에 없을 때만
    소유 모듈의 현재 값으로 폴백한다 — import 시점 값 사본(stale 스칼라)을
    읽는 대신 항상 최신 값을 돌려준다.
    """

    def __setattr__(self, name: str, value: object) -> None:
        for mod_name in _NEW_STATE_MODULES:
            mod = sys.modules.get(mod_name)
            if mod is not None and name in vars(mod):
                setattr(mod, name, value)
        super().__setattr__(name, value)

    def __getattr__(self, name: str) -> object:
        # __getattr__ 은 일반 조회 실패 시에만 호출되므로 기존 재-익스포트는 그대로 동작한다.
        if name.startswith("__"):
            raise AttributeError(name)
        for mod_name in _NEW_STATE_MODULES:
            mod = sys.modules.get(mod_name)
            if mod is not None and name in vars(mod):
                return vars(mod)[name]
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        )


sys.modules[__name__].__class__ = _FacadeModule
