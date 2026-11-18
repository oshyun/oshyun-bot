"""web_server — Flutter Web 및 실시간 대화/음성, 세컨드브레인 볼트 파일 관리 API 서버.

기능별 서브모듈로 분해되어 있으며, 하위 호환성을 위해 모든 엔드포인트와 심볼을 파사드로 재수출(Re-export)한다:
- web_auth: 비밀 토큰 게이트웨이, 세션 쿠키 서명 및 무차별 대입 방어
- web_speech: 한국어 신경망 음성 합성(edge-tts) 및 TTS 텍스트 정제
- web_vault: Obsidian 볼트 파일 탐색/읽기/쓰기/생성 REST API
- web_rooms: 대화방 인덱스(SQLite SSOT), 히스토리, 세션 관리, 첨부파일 업로드
- web_execution: 비동기 워커 큐(FIFO), 백그라운드 재부착 및 턴 실행기
- web_orchestrator: 총괄 에이전트(Core Orchestrator) 및 마스터 슈퍼바이저 세션 제어
- web_ws: WebSocket 실시간 이벤트 디스패처
"""

from __future__ import annotations

import asyncio
import fcntl
import gzip
import os
import re
import ssl
import subprocess
import sys
import time
from typing import Any

# macOS 환경에서 aiohttp FileResponse sendfile 커널 버그(헤더 누락 및 HTTP/0.9 전송 현상) 방지
os.environ.setdefault("AIOHTTP_NOSENDFILE", "1")

from aiohttp import WSMsgType, web, web_fileresponse
web_fileresponse.NOSENDFILE = True

import burn_runner
import chat_db
import scheduler
import schedules
from bot_logger import logger, setup_web_logger, vault_log
from master_supervisor import (
    CORE_ROOM_ID,
    MASTER_PATROL_CHANNEL,
    MasterSupervisor,
)
from paths import (
    ATTACHED_FILES_DIR,
    CHATS_DIR,
    CHAT_ROOMS_FILE,
    CODEDIR,
    DEFAULT_SSL_CERT_FILE,
    DEFAULT_SSL_KEY_FILE,
    JOBS_DIR,
    LOGS_DIR,
    MASTER_STATE_FILE,
    ROOT_DIR,
    SESSION_LOG_FILE,
    STATE_DIR,
    WEB_JOBS_FILE,
    WORKDIR,
)

CHAT_ROOMS_BACKUP_FILE = os.path.join(STATE_DIR, "chat_rooms_backup.json")

# ── agent_engine 심볼 Re-export (테스트 mock 호환용) ──────────────────────
import agent_engine
from agent_engine import (
    AGY_DEFAULT_MODEL,
    ENGINE_LABELS,
    ENGINES,
    check_and_reset_if_idle,
    clear_session,
    delete_session_files,
    detached_pid_alive,
    fmt_cost,
    get_coding_mode,
    get_effort,
    get_engine,
    get_model,
    get_model_candidates,
    get_model_labels,
    get_session,
    get_supported_efforts,
    get_cached_agy_usage,
    refresh_agy_quota_cache,
    is_quota_exhausted_error,
    is_session_stale_error,
    kill_job,
    set_coding_mode,
    set_effort,
    set_engine,
    set_model,
    set_session,
    spawn_agent,
    update_last_activity,
    watch_agent,
)

# ── 서브모듈 심볼 Re-export (100% 하위 호환성 보장) ───────────────────────
import web_auth
from web_auth import (
    AUTH_COOKIE_NAME,
    AUTH_DELAY_SECONDS,
    AUTH_LOCKOUT_DURATION_SECONDS,
    AUTH_LOCKOUT_WINDOW_SECONDS,
    AUTH_MAX_FAILURES,
    AUTH_SESSION_SECRET,
    AUTH_SESSION_TTL_SECONDS,
    WEB_AUTH_REQUIRED,
    WEB_TOKEN,
    _create_session_cookie_value,
    _failed_auth_attempts,
    _get_client_ip,
    _is_authenticated,
    _is_ip_locked,
    _record_failed_attempt,
    _redirect_response,
    _reset_failed_attempts,
    _verify_session_cookie,
    _verify_token,
    auth_middleware,
    cors_middleware,
    handle_login_page,
    handle_login_submit,
    handle_logout,
    render_login_page,
)

import web_speech
from web_speech import (
    AVAILABLE_TTS_VOICES,
    DEFAULT_TTS_MODE,
    DEFAULT_TTS_VOICE,
    VALID_TTS_MODES,
    VALID_TTS_VOICE_IDS,
    VOICE_MODE_PROMPT_SUFFIX,
    _current_tts_mode,
    _current_tts_voice,
    clean_text_for_speech,
    deduplicate_speech_text,
    handle_tts,
    synthesize_edge_tts_mp3,
)

import web_vault
from web_vault import (
    ALLOWED_NOTE_EXTENSIONS,
    EXCLUDED_DIR_NAMES,
    _build_directory_tree,
    _resolve_vault_absolute_path,
    handle_vault_create_file,
    handle_vault_get_file,
    handle_vault_recent,
    handle_vault_save_file,
    handle_vault_tree,
)

import web_execution
from web_execution import (
    _active_agent_pids,
    _active_watch_tasks,
    _connected_websockets,
    _format_tool_step_info,
    _last_chat_requests,
    _load_web_jobs,
    _master_supervisor,
    _master_supervisor_task,
    _recover_single_web_job,
    _recover_web_jobs,
    _remove_web_job,
    _room_live_feeds,
    _room_live_snapshots,
    _room_queues,
    _room_session_ids,
    _room_workers,
    _save_web_job,
    _serialize_live_run_entry,
    _user_cancelled_rooms,
    _watch_and_finish_web_job,
    broadcast_ws_json,
    build_active_runs_payload,
    cancel_active_run,
    enqueue_room_turn,
    execute_chat_turn,
    extract_stream_events_from_line,
    get_live_run_for_room,
    run_room_worker,
)

import web_automation
from web_automation import (
    handle_burn_run,
    handle_burn_status,
    handle_burn_todo_create,
    handle_burn_todo_delete,
    handle_schedules_create,
    handle_schedules_delete,
    handle_schedules_list,
    handle_schedules_run,
    handle_schedules_update,
)
import web_rooms
from web_rooms import (
    CORE_ORCHESTRATOR_ROOM_ID,
    CORE_ORCHESTRATOR_TITLE,
    HIDDEN_ROOMS_FILE,
    _append_room_message,
    _get_room_chat_file,
    _load_chat_rooms_backup,
    _load_chat_rooms_index,
    _load_hidden_rooms,
    _load_room_messages,
    _normalize_room_ts,
    _save_chat_rooms_backup,
    _save_chat_rooms_index,
    _save_hidden_room,
    _save_hidden_rooms,
    _update_room_metadata,
    handle_api_delete_session,
    handle_api_rename_room,
    handle_api_room_settings,
    handle_api_upload,
    handle_chat_history,
    handle_chat_rooms,
    handle_chat_rooms_backup,
    handle_chat_rooms_backup_save,
    handle_get_attachment,
    handle_archive_chat_room,
)

import web_orchestrator
from web_orchestrator import (
    _WebServerSupervisorHost,
    _find_similar_orchestrator_room,
    _format_updated_at,
    _inspect_agy_brain,
    _is_room_currently_running,
    _resolve_orchestrator_target_room,
    ensure_core_orchestrator_room,
    handle_orchestrator_archive_session,
    handle_orchestrator_dispatch,
    handle_orchestrator_graph,
    handle_orchestrator_memory_get,
    handle_orchestrator_memory_post,
    handle_orchestrator_skill_post,
    handle_orchestrator_notify,
    handle_orchestrator_patrol_trigger,
    handle_orchestrator_read,
    handle_orchestrator_reset,
    handle_orchestrator_sessions,
    handle_orchestrator_summaries,
    handle_orchestrator_summary_update,
    handle_orchestrator_task_update,
    handle_orchestrator_tasks,
    handle_task_events_callback,
    reset_core_orchestrator_state,
)

import web_ws
from web_ws import handle_websocket

# ── 웹 서버 기본 설정 ─────────────────────────────────────────────────────
WEB_ENABLED = os.getenv("WEB_ENABLED", os.getenv("VOICE_WEB_ENABLED", "1")).strip() != "0"
WEB_HOST = os.getenv("WEB_HOST", os.getenv("VOICE_WEB_HOST", "127.0.0.1")).strip() or "127.0.0.1"
WEB_PORT = int(os.getenv("WEB_PORT", os.getenv("VOICE_WEB_PORT", "8765")).strip() or "8765")
INTERNAL_STATIC_DIR = os.path.join(
    ROOT_DIR, "src", "bot", "static"
)
DEFAULT_WEB_BUILD_DIR = INTERNAL_STATIC_DIR
WEB_STATIC_DIR = (
    os.getenv("WEB_STATIC_DIR", os.getenv("VOICE_WEB_STATIC_DIR", "")).strip() or DEFAULT_WEB_BUILD_DIR
)


def build_status_payload() -> dict:
    """현재 에이전트·모델·세션·TTS 음성 목록 상태를 담은 단일 페이로드를 생성한다."""
    current_engine = get_engine()
    current_model = get_model()
    is_coding = get_coding_mode()
    session_namespace = "coding_" if is_coding else ""
    return {
        "type": "status",
        "engine": current_engine,
        "engineLabel": ENGINE_LABELS.get(current_engine, current_engine),
        "model": current_model,
        "effort": get_effort(),
        "codingMode": is_coding,
        "sessionId": get_session(ns=session_namespace),
        "workdir": (CODEDIR or WORKDIR) if is_coding else WORKDIR,
        "vaultDir": WORKDIR,
        "codeDir": CODEDIR,
        "availableEngines": [
            {"id": engine_id, "label": ENGINE_LABELS.get(engine_id, engine_id)}
            for engine_id in ENGINES
        ],
        "availableModels": [
            {
                "id": model_id,
                "label": model_label,
                "supportedEfforts": list(get_supported_efforts(current_engine, model_id)),
            }
            for model_id, model_label in get_model_labels(current_engine).items()
        ],
        "supportedEfforts": list(get_supported_efforts(current_engine, current_model)),
        "ttsVoices": list(AVAILABLE_TTS_VOICES),
        "defaultTtsVoice": DEFAULT_TTS_VOICE,
        "ttsVoice": web_speech._current_tts_voice,
        "ttsMode": web_speech._current_tts_mode,
        "supportedTtsModes": [
            {"id": "off", "label": "사용 안 함 (기본값 · 음소거)"},
            {"id": "voice_only", "label": "음성 질문 시에만 낭독"},
            {"id": "always", "label": "항상 낭독 (텍스트 및 음성 모두)"},
        ],
        "activeRuns": build_active_runs_payload(),
        "usage": get_cached_agy_usage(allow_refresh=True),
    }


def get_mac_address_for_ip(ip_address: str) -> str:
    """주어진 IP에 대한 MAC 주소를 조회한다 (로컬호스트, LAN ARP 테이블, 또는 외부망 식별)."""
    if not ip_address or ip_address in ("127.0.0.1", "::1", "localhost"):
        try:
            output = subprocess.check_output(
                ["ifconfig", "en0"], stderr=subprocess.DEVNULL, text=True
            )
            match = re.search(r"ether\s+([0-9a-fA-F:]{11,17})", output)
            if match:
                parts = [p.zfill(2) for p in match.group(1).lower().split(":")]
                return ":".join(parts)
        except Exception:
            pass
        return "로컬 호스트"

    # 로컬 네트워크 기기인 경우 ARP 테이블 조회
    try:
        output = subprocess.check_output(
            ["arp", "-n", ip_address], stderr=subprocess.DEVNULL, text=True
        )
        match = re.search(r"\s+at\s+([0-9a-fA-F:]{11,17})\s+", output)
        if match:
            mac_str = match.group(1).lower()
            if "(incomplete)" not in mac_str:
                parts = [p.zfill(2) for p in mac_str.split(":")]
                return ":".join(parts)
    except Exception:
        pass

    if ip_address.startswith(("192.168.", "10.", "172.16.", "172.17.", "172.18.", "172.19.", "172.2", "172.3")):
        return "조회 불가 (LAN ARP 미등록)"

    return "외부망 접속 (WAN 라우팅에 의해 은닉)"


async def handle_status(request: web.Request) -> web.Response:
    """GET /api/status — 현재 봇 설정, 세션 상태 및 클라이언트 접속 IP/MAC 조회."""
    payload = build_status_payload()
    forwarded = request.headers.get("X-Forwarded-For", "").strip()
    real_ip = request.headers.get("X-Real-IP", "").strip()
    if forwarded:
        client_ip = forwarded.split(",")[0].strip()
    elif real_ip:
        client_ip = real_ip
    else:
        client_ip = request.remote or "알 수 없음"

    client_mac = get_mac_address_for_ip(client_ip)

    if client_ip in ("192.168.0.1", "192.168.1.1", "10.0.0.1"):
        ip_type = "nat_loopback"
        ip_desc = "iptime 공유기 게이트웨이 (내부 Wi-Fi에서 DDNS 접속 시 헤어핀 NAT SNAT 적용)"
        mac_label = f"{client_mac} (iptime 공유기)" if ":" in client_mac else client_mac
    elif client_ip in ("127.0.0.1", "::1", "localhost"):
        ip_type = "localhost"
        ip_desc = "로컬호스트 직접 접속 (서버 본체)"
        mac_label = f"{client_mac} (서버 본체)" if ":" in client_mac else client_mac
    elif client_ip.startswith(("192.168.", "10.", "172.16.", "172.17.", "172.18.", "172.19.", "172.2", "172.3")):
        ip_type = "lan"
        ip_desc = "내부 로컬 Wi-Fi / 유선 LAN 단말 직접 접속"
        mac_label = client_mac
    else:
        ip_type = "wan"
        ip_desc = "외부 공인망 / 모바일 LTE·5G 접속"
        mac_label = client_mac

    payload["clientIp"] = client_ip
    payload["clientMac"] = mac_label
    payload["ipType"] = ip_type
    payload["ipDescription"] = ip_desc
    return web.json_response(payload)


async def handle_usage(request: web.Request) -> web.Response:
    """GET /api/usage — 현재 AGY 쿼터 잔여량(5시간/주간) 요약 및 캐시 상태 반환."""
    refresh_param = request.query.get("refresh", "").lower() in ("1", "true", "yes")
    if refresh_param:
        await refresh_agy_quota_cache()
    usage = get_cached_agy_usage(allow_refresh=True)
    return web.json_response({
        "status": "ok",
        "usage": usage,
    })


async def handle_settings(request: web.Request) -> web.Response:
    """POST /api/settings — 엔진·모델·Effort·코딩모드 변경 및 세션 초기화."""
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "유효하지 않은 JSON 요청입니다."}, status=400)

    requested_engine = payload.get("engine")
    engine_changed = False
    if isinstance(requested_engine, str) and requested_engine in ENGINES:
        if requested_engine != get_engine():
            set_engine(requested_engine)
            engine_changed = True

    requested_model = payload.get("model")
    if isinstance(requested_model, str) and requested_model.strip():
        clean_model = requested_model.strip()
        curr_eng = get_engine()
        candidates = get_model_candidates(curr_eng)
        if not engine_changed or clean_model in candidates or any(clean_model.startswith(c) for c in candidates):
            set_model(clean_model)

    if "effort" in payload:
        requested_effort = payload.get("effort")
        set_effort(requested_effort if requested_effort else None)

    if "codingMode" in payload and isinstance(payload.get("codingMode"), bool):
        requested_coding = payload["codingMode"]
        if requested_coding and not CODEDIR:
            return web.json_response(
                {"error": "AGENT_CODEDIR가 설정되지 않아 코딩 모드를 켤 수 없습니다."},
                status=400,
            )
        set_coding_mode(requested_coding)

    if payload.get("resetSession"):
        session_namespace = "coding_" if get_coding_mode() else ""
        clear_session(ns=session_namespace)
        vault_log("🆕 세션 리셋", f"[웹/음성앱] {session_namespace or 'vault'} 세션 수동 초기화")

    requested_tts_voice = payload.get("ttsVoice")
    if isinstance(requested_tts_voice, str) and requested_tts_voice in VALID_TTS_VOICE_IDS:
        web_speech._current_tts_voice = requested_tts_voice

    requested_tts_mode = payload.get("ttsMode")
    if isinstance(requested_tts_mode, str) and requested_tts_mode in VALID_TTS_MODES:
        web_speech._current_tts_mode = requested_tts_mode

    resp_payload = build_status_payload()
    resp_payload["ok"] = True
    resp_payload["status"] = "ok"
    return web.json_response(resp_payload)


async def handle_fallback_dashboard(request: web.Request) -> web.Response:
    """Flutter Web 빌드가 아직 준비되지 않은 환경에서 기본 상태 및 안내를 서빙한다."""
    status_data = build_status_payload()
    return web.Response(
        text=f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <link rel="icon" type="image/svg+xml" href="/favicon.svg">
  <link rel="alternate icon" href="/favicon.ico">
  <title>oshyun-bot Dashboard</title>
  <style>
    body {{
      background: #0B0F19; color: #F8FAFC;
      font-family: -apple-system, BlinkMacSystemFont, sans-serif;
      padding: 24px; margin: 0;
    }}
    .card {{
      max-width: 600px; margin: 40px auto; background: #1E293B;
      padding: 24px; border-radius: 16px; border: 1px solid #334155;
    }}
    h1 {{ font-size: 20px; color: #38BDF8; margin-top: 0; }}
    .badge {{
      display: inline-block; background: #10B981; color: white;
      font-size: 11px; font-weight: bold; padding: 4px 10px; border-radius: 20px;
    }}
    .stat-row {{ margin: 12px 0; font-size: 14px; display: flex; justify-content: space-between; }}
    .btn {{
      display: inline-block; background: #EF4444; color: white; text-decoration: none;
      padding: 10px 16px; border-radius: 8px; font-size: 13px; font-weight: 600; border: none; cursor: pointer;
    }}
  </style>
</head>
<body>
  <div class="card">
    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px;">
      <h1>oshyun-bot Hub</h1>
      <span class="badge">보안 세션 활성</span>
    </div>
    <p style="color: #94A3B8; font-size: 13px;">외부 보안 게이트웨이 인증을 통과하였습니다. 시스템이 정상 가동 중입니다.</p>
    <hr style="border: 0; border-top: 1px solid #334155; margin: 16px 0;">
    <div class="stat-row"><span style="color:#94A3B8;">기본 엔진</span><span><b>{status_data.get('engineLabel')}</b> ({status_data.get('model')})</span></div>
    <div class="stat-row"><span style="color:#94A3B8;">볼트 경로</span><span><code>{status_data.get('workdir')}</code></span></div>
    <div class="stat-row"><span style="color:#94A3B8;">WebSocket 주소</span><span><code>/ws</code></span></div>
    <div class="stat-row"><span style="color:#94A3B8;">볼트 탐색 API</span><span><code>/api/vault/tree</code></span></div>
    <hr style="border: 0; border-top: 1px solid #334155; margin: 16px 0;">
    <form method="POST" action="/logout">
      <button type="submit" class="btn">세션 로그아웃</button>
    </form>
  </div>
</body>
</html>""",
        content_type="text/html",
    )


_scheduler_task: asyncio.Task | None = None
_burn_recover_task: asyncio.Task | None = None


async def _trigger_schedule_in_master(schedule_entry: dict, master_prompt: str) -> None:
    """정기 작업 시점 도래/수동 실행 시 마스터 에이전트(__core__) 턴을 명시적으로 트리거한다."""
    if not master_prompt:
        return
    chat_db.get_or_create_room(
        CORE_ROOM_ID,
        session_id=CORE_ROOM_ID,
        title="총괄 에이전트 (Core)",
        channel="orchestrator",
    )
    core_sid = _room_session_ids.get(CORE_ROOM_ID) or get_session(ns="core_")
    await enqueue_room_turn(
        room_id=CORE_ROOM_ID,
        user_text=master_prompt,
        is_voice_mode=False,
        client_session_id=core_sid,
        is_new_session=False,
        channel=scheduler.SCHEDULE_TRIGGER_CHANNEL,
    )
    sid = schedule_entry.get("id") or ""
    desc = schedule_entry.get("description") or sid or "정기 작업"
    try:
        await broadcast_ws_json({
            "type": "scheduler_notice",
            "roomId": CORE_ROOM_ID,
            "scheduleId": sid,
            "text": f"🔔 스케줄 [{desc}] → 마스터(__core__) 트리거",
        })
    except Exception:
        pass


async def _notify_web_chat(_bot: Any, _chat_id: Any, text: str) -> None:
    """정기 작업 및 토큰 버닝 결과를 웹 통합 DB(마스터 방)와 WebSocket으로 전달한다."""
    if not text:
        return
    try:
        chat_db.get_or_create_room(
            CORE_ROOM_ID,
            session_id=CORE_ROOM_ID,
            title="총괄 에이전트 (Core)",
            channel="orchestrator",
        )
        chat_db.add_message(
            room_id=CORE_ROOM_ID,
            role="assistant",
            text=text,
            channel="scheduler",
            session_id=CORE_ROOM_ID,
        )
    except Exception as exc:
        logger.warning("[scheduler] 웹 DB 기록 실패: %s", exc)
    try:
        await broadcast_ws_json({"type": "scheduler_notice", "roomId": CORE_ROOM_ID, "text": text})
    except Exception:
        pass


def create_web_app(static_dir: str | None = None) -> web.Application:
    """WebSocket·REST API 및 세컨드브레인 볼트 관리, 보안 게이트웨이 인증, 선택적 Flutter Web 정적 파일 서빙을 포함한 aiohttp 앱을 생성한다."""
    try:
        chat_db.init_db()
    except Exception as exc:
        logger.warning("[chat_db] init_db 실패: %s", exc)

    for worker in list(_room_workers.values()):
        if not worker.done():
            worker.cancel()
    _connected_websockets.clear()
    _active_watch_tasks.clear()
    _active_agent_pids.clear()
    _last_chat_requests.clear()
    _room_queues.clear()
    _room_workers.clear()
    _room_session_ids.clear()
    _room_live_feeds.clear()
    _room_live_snapshots.clear()

    global _master_supervisor
    _master_supervisor = MasterSupervisor(
        _WebServerSupervisorHost(),
        tick_seconds=int(os.getenv("MASTER_TICK_SECONDS", "5")),
        interval_seconds=int(os.getenv("MASTER_PATROL_INTERVAL", "0")),
        stall_seconds=int(os.getenv("MASTER_STALL_SECONDS", "900")),
    )
    web_execution._master_supervisor = _master_supervisor

    web_app = web.Application(middlewares=[cors_middleware, auth_middleware])

    async def _on_web_startup(_app: web.Application) -> None:
        global _master_supervisor_task, _scheduler_task, _burn_recover_task
        web_execution.set_shutting_down(False)
        try:
            await _recover_web_jobs()
        except Exception as exc:
            logger.error("[web_jobs] 시작 시 작업 복구 실패: %s", exc)

        if _master_supervisor is not None:
            _master_supervisor_task = asyncio.create_task(_master_supervisor.run_forever())
            web_execution._master_supervisor_task = _master_supervisor_task

        if os.getenv("WEB_SCHEDULER_ENABLED", "1") != "0":
            try:
                schedules.ensure_default_burn_schedule()
            except Exception as exc:
                logger.warning("[schedules] 기본 토큰 버닝 스케줄 시드 실패: %s", exc)
            extra_args = [x for x in (os.getenv("AGENT_EXTRA_ARGS", "")).split() if x]
            timeout = int(os.getenv("AGENT_TIMEOUT", "0"))
            _scheduler_task = asyncio.create_task(
                scheduler.run_scheduler(
                    None,
                    workdir=WORKDIR,
                    extra_args=extra_args,
                    timeout=timeout,
                    send_to_chat=_notify_web_chat,
                    vault_log=vault_log,
                    trigger_master=_trigger_schedule_in_master,
                )
            )
            _burn_recover_task = asyncio.create_task(
                burn_runner.recover_stuck_burn_tasks(
                    bot=None,
                    chat_id=0,
                    send_to_chat=_notify_web_chat,
                    vault_log=vault_log,
                )
            )

    async def _on_web_shutdown(_app: web.Application) -> None:
        web_execution.set_shutting_down(True)
        try:
            web_execution.persist_pending_room_turns()
        except Exception as exc:
            logger.warning("[web_jobs] 종료 시 대기열 저장 경고: %s", exc)
        try:
            detached = await web_execution.detach_active_tasks()
            if detached:
                logger.info("[web_jobs] 종료 시 활성 태스크 %d건 안전 분리(detach)", detached)
        except Exception as exc:
            logger.warning("[web_jobs] 종료 시 태스크 분리 경고: %s", exc)

    async def _on_web_cleanup(_app: web.Application) -> None:
        global _master_supervisor_task, _scheduler_task, _burn_recover_task
        web_execution.set_shutting_down(True)
        for task in (_master_supervisor_task, _scheduler_task, _burn_recover_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        _master_supervisor_task = None
        _scheduler_task = None
        _burn_recover_task = None
        web_execution._master_supervisor_task = None

    web_app.on_startup.append(_on_web_startup)
    web_app.on_shutdown.append(_on_web_shutdown)
    web_app.on_cleanup.append(_on_web_cleanup)

    # 보안 게이트웨이 인증 엔드포인트
    web_app.router.add_get("/login", handle_login_page)
    web_app.router.add_post("/login", handle_login_submit)
    web_app.router.add_post("/logout", handle_logout)

    # API 엔드포인트
    web_app.router.add_get("/api/status", handle_status)
    web_app.router.add_get("/api/usage", handle_usage)
    web_app.router.add_post("/api/settings", handle_settings)
    web_app.router.add_post("/api/session/delete", handle_api_delete_session)
    web_app.router.add_post("/api/session/batch-delete", handle_api_delete_session)
    web_app.router.add_post("/api/chat/room/rename", handle_api_rename_room)
    web_app.router.add_post("/api/chat/room/settings", handle_api_room_settings)
    web_app.router.add_get("/api/tts", handle_tts)
    web_app.router.add_post("/api/tts", handle_tts)
    web_app.router.add_get("/ws", handle_websocket)

    # 사진/파일 업로드 및 첨부파일 서빙 엔드포인트
    web_app.router.add_get("/api/attachments/{filename}", handle_get_attachment)
    web_app.router.add_post("/api/upload", handle_api_upload)
    web_app.router.add_post("/api/attachments/upload", handle_api_upload)

    # 총괄 에이전트 (Core Orchestrator) 모니터 REST API
    web_app.router.add_get("/api/orchestrator/sessions", handle_orchestrator_sessions)
    web_app.router.add_get("/api/orchestrator/summaries", handle_orchestrator_summaries)
    web_app.router.add_get("/api/orchestrator/session/read", handle_orchestrator_read)
    web_app.router.add_post("/api/orchestrator/session/summary", handle_orchestrator_summary_update)
    web_app.router.add_post("/api/orchestrator/dispatch", handle_orchestrator_dispatch)
    web_app.router.add_post("/api/orchestrator/notify", handle_orchestrator_notify)
    web_app.router.add_get("/api/orchestrator/graph", handle_orchestrator_graph)
    web_app.router.add_get("/api/orchestrator/memory", handle_orchestrator_memory_get)
    web_app.router.add_post("/api/orchestrator/memory", handle_orchestrator_memory_post)
    web_app.router.add_post("/api/orchestrator/skill", handle_orchestrator_skill_post)
    web_app.router.add_post("/api/orchestrator/reset", handle_orchestrator_reset)
    web_app.router.add_get("/api/orchestrator/tasks", handle_orchestrator_tasks)
    web_app.router.add_post("/api/orchestrator/task/update", handle_orchestrator_task_update)
    web_app.router.add_post("/api/orchestrator/patrol/trigger", handle_orchestrator_patrol_trigger)
    web_app.router.add_post("/api/orchestrator/session/archive", handle_orchestrator_archive_session)
    web_app.router.add_post("/v1/callbacks/task-events", handle_task_events_callback)
    web_app.router.add_post("/api/orchestrator/task-events", handle_task_events_callback)

    # 대화방 및 메시지 히스토리 / 백업
    web_app.router.add_get("/api/chat/rooms", handle_chat_rooms)
    web_app.router.add_post("/api/chat/rooms/archive", handle_archive_chat_room)
    web_app.router.add_post("/api/chat/rooms/{room_id}/archive", handle_archive_chat_room)
    web_app.router.add_get("/api/chat/rooms/backup", handle_chat_rooms_backup)
    web_app.router.add_post("/api/chat/rooms/backup", handle_chat_rooms_backup_save)
    web_app.router.add_get("/api/chat/history", handle_chat_history)

    # 정기 작업(Schedules) 및 토큰 버닝(Token Burn) 자동화 API
    web_app.router.add_get("/api/schedules", handle_schedules_list)
    web_app.router.add_post("/api/schedules", handle_schedules_create)
    web_app.router.add_patch("/api/schedules/{id}", handle_schedules_update)
    web_app.router.add_delete("/api/schedules/{id}", handle_schedules_delete)
    web_app.router.add_post("/api/schedules/{id}/run", handle_schedules_run)
    web_app.router.add_get("/api/burn/status", handle_burn_status)
    web_app.router.add_post("/api/burn/todo", handle_burn_todo_create)
    web_app.router.add_delete("/api/burn/todo/{id}", handle_burn_todo_delete)
    web_app.router.add_post("/api/burn/run", handle_burn_run)

    # 세컨드브레인 볼트 파일 관리 엔드포인트
    web_app.router.add_get("/api/vault/tree", handle_vault_tree)
    web_app.router.add_get("/api/vault/recent", handle_vault_recent)
    web_app.router.add_get("/api/vault/file", handle_vault_get_file)
    web_app.router.add_post("/api/vault/file", handle_vault_save_file)
    web_app.router.add_post("/api/vault/create", handle_vault_create_file)

    resolved_static_dir = static_dir if static_dir is not None else WEB_STATIC_DIR
    index_html_path = os.path.join(resolved_static_dir, "index.html")
    sw_js_path = os.path.join(resolved_static_dir, "sw.js")
    if os.path.isdir(resolved_static_dir) and os.path.isfile(index_html_path):

        _index_gz_cache: dict[str, Any] = {"mtime": None, "data": b""}

        async def serve_index(request: web.Request) -> web.StreamResponse:
            no_cache_headers = {
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
                "Vary": "Accept-Encoding",
            }
            accept_enc = request.headers.get("Accept-Encoding", "")
            if "gzip" in accept_enc.lower():
                try:
                    mtime = os.path.getmtime(index_html_path)
                    if _index_gz_cache["mtime"] != mtime:
                        with open(index_html_path, "rb") as f:
                            raw_bytes = f.read()
                        _index_gz_cache["data"] = gzip.compress(raw_bytes, compresslevel=6)
                        _index_gz_cache["mtime"] = mtime
                    return web.Response(
                        body=_index_gz_cache["data"],
                        headers={
                            **no_cache_headers,
                            "Content-Type": "text/html; charset=utf-8",
                            "Content-Encoding": "gzip",
                        },
                    )
                except OSError:
                    pass

            resp = web.FileResponse(index_html_path)
            for k, v in no_cache_headers.items():
                resp.headers[k] = v
            return resp

        if os.path.isfile(sw_js_path):
            async def serve_sw(_request: web.Request) -> web.FileResponse:
                resp = web.FileResponse(sw_js_path)
                resp.headers["Content-Type"] = "application/javascript"
                resp.headers["Service-Worker-Allowed"] = "/"
                resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
                return resp

            web_app.router.add_get("/sw.js", serve_sw)

        web_app.router.add_get("/", serve_index)
        web_app.router.add_get("/voice", serve_index)
        web_app.router.add_get("/voice/", serve_index)
        web_app.router.add_static("/", resolved_static_dir, show_index=False)
    else:
        web_app.router.add_get("/", handle_fallback_dashboard)
        web_app.router.add_get("/voice", handle_fallback_dashboard)
        web_app.router.add_get("/voice/", handle_fallback_dashboard)

    return web_app


create_voice_web_app = create_web_app


def get_ssl_context() -> ssl.SSLContext | None:
    """반환된 SSL 컨텍스트 또는 SSL 미사용 시 None.
    
    WEB_SSL_ENABLED=0 이면 명시적으로 비활성화한다.
    기본적으로 DEFAULT_SSL_CERT_FILE / DEFAULT_SSL_KEY_FILE 이 존재하면
    자동으로 HTTPS 컨텍스트를 로드한다.
    """
    enabled_val = os.getenv("WEB_SSL_ENABLED", "").strip().lower()
    if enabled_val in ("0", "false", "no"):
        return None

    cert_path = os.getenv("WEB_SSL_CERT", "").strip() or DEFAULT_SSL_CERT_FILE
    key_path = os.getenv("WEB_SSL_KEY", "").strip() or DEFAULT_SSL_KEY_FILE

    if not os.path.exists(cert_path) or not os.path.exists(key_path):
        if enabled_val in ("1", "true", "yes"):
            logger.warning("WEB_SSL_ENABLED=1 설정되었으나 인증서/키 파일이 없습니다: cert=%s, key=%s", cert_path, key_path)
        return None

    try:
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
        logger.info("SSL/TLS 인증서 로드 성공 (HTTPS 활성화): cert=%s", cert_path)
        return ctx
    except Exception as exc:
        logger.error("SSL 컨텍스트 초기화 실패: %s", exc)
        return None


async def start_web_server(
    host: str = WEB_HOST,
    port: int = WEB_PORT,
    ssl_context: ssl.SSLContext | None = None,
) -> web.AppRunner | None:
    """telegram_bot.py 이벤트 루프 안에서 비차단으로 WebSocket/HTTP 서버를 기동한다."""
    if not WEB_ENABLED:
        logger.info("WEB_ENABLED=0 — 웹 API 서버 기동을 건너뜁니다.")
        return None

    if ssl_context is None:
        ssl_context = get_ssl_context()

    web_app = create_web_app()
    runner = web.AppRunner(web_app, access_log=None)
    await runner.setup()
    try:
        site = web.TCPSite(runner, host=host, port=port, ssl_context=ssl_context)
        await site.start()
        proto = "https" if ssl_context else "http"
        ws_proto = "wss" if ssl_context else "ws"
        logger.info("웹 API 서버 기동 완료: %s://%s:%d (WS: %s://%s:%d/ws)", proto, host, port, ws_proto, host, port)
        return runner
    except OSError as exc:
        logger.warning("웹 API 서버 포트(%s:%d) 바인딩 실패(무시): %s", host, port, exc)
        await runner.cleanup()
        return None


start_voice_web_server = start_web_server

_instance_lock_file = None


def _acquire_instance_lock() -> bool:
    """오직 하나의 web_server.py 프로세스만 실행되도록 OS 레벨 배타적 파일 락을 획득한다."""
    global _instance_lock_file
    lock_path = os.path.join(LOGS_DIR, "web.lock")
    try:
        os.makedirs(LOGS_DIR, exist_ok=True)
        _instance_lock_file = open(lock_path, "w")
        fcntl.flock(_instance_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _instance_lock_file.write(f"{os.getpid()}\n")
        _instance_lock_file.flush()
        return True
    except (BlockingIOError, OSError):
        return False


def main() -> None:
    """단독 프로세스로 웹 API 서버를 실행한다."""
    if not WEB_ENABLED:
        logger.info("WEB_ENABLED=0 — 웹 API 서버 기동을 건너뜁니다.")
        sys.exit(0)

    if not _acquire_instance_lock():
        logger.error("이미 다른 web_server 인스턴스가 실행 중입니다. 중복 실행을 방지하기 위해 종료합니다.")
        sys.exit(1)

    web_log = setup_web_logger()
    ssl_ctx = get_ssl_context()
    proto = "https" if ssl_ctx else "http"
    web_log.info("웹 API 서버 시작 (독립 프로세스 모드): %s://%s:%d", proto, WEB_HOST, WEB_PORT)
    web_app = create_web_app()
    web.run_app(web_app, host=WEB_HOST, port=WEB_PORT, ssl_context=ssl_ctx, access_log=None, shutdown_timeout=2.0)


if __name__ == "__main__":
    main()
