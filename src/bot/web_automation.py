"""web_automation — 정기 작업(Schedules) 및 토큰 버닝(Token Burn) REST API 핸들러.

모든 비즈니스 로직(자연어 cron 파싱, 유효성 검증, 상태 라벨링, 쿼터 조건 계산, 실행 트리거)을
백엔드에서 처리하고, 프론트엔드는 순수 렌더링만 수행하도록 뷰 모델 JSON을 반환한다.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any

from aiohttp import web

import burn_runner
import burn_todo
import cron as cron_mod
import scheduler
import schedules
from agent_engine import run_agent
from bot_logger import logger, vault_log
from paths import WORKDIR
from web_execution import broadcast_ws_json

_NL_TO_CRON_PROMPT = (
    "다음 사용자의 정기 작업 요청을 5필드 cron 식과 실행 프롬프트, 한 줄 설명으로 변환하라.\n"
    '반드시 JSON 객체 하나만 출력하라(다른 텍스트/코드펜스 금지): '
    '{"cron": "분 시 일 월 요일", "prompt": "에이전트에게 시킬 작업", "description": "한국어 요약"}\n'
    "요청: "
)

_BURN_STATUS_LABELS = {
    "pending": "대기",
    "running": "실행 중",
    "completed": "완료",
    "failed": "실패",
}

_WORKDIR_LABELS = {
    "repo": "코드 워크트리",
    "vault": "Obsidian 볼트",
}


def _extract_json_obj(text: str) -> dict[str, Any] | None:
    """에이전트 응답에서 JSON 객체 하나를 추출한다."""
    cleaned = text.strip()
    m = re.search(r"\{[\s\S]*\}", cleaned)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _format_schedule_view(entry: dict[str, Any]) -> dict[str, Any]:
    """프론트엔드 렌더링용 정기 작업 뷰 모델을 생성한다."""
    enabled = bool(entry.get("enabled", True))
    return {
        "id": entry.get("id", ""),
        "cron": entry.get("cron", ""),
        "prompt": entry.get("prompt", ""),
        "description": entry.get("description") or entry.get("prompt", ""),
        "model": entry.get("model") or "",
        "kind": str(entry.get("kind") or "prompt"),
        "kindLabel": "스케줄",
        "enabled": enabled,
        "statusLabel": "활성" if enabled else "일시중지",
        "created": entry.get("created", ""),
    }


def _format_burn_task_view(task: dict[str, Any]) -> dict[str, Any]:
    """프론트엔드 렌더링용 토큰 버닝 TODO 뷰 모델을 생성한다."""
    status = str(task.get("status") or "pending")
    wt = str(task.get("workdir_type") or "repo")
    return {
        "id": task.get("id", ""),
        "title": task.get("title", ""),
        "prompt": task.get("prompt", ""),
        "category": task.get("category") or "custom",
        "workdirType": wt,
        "workdirLabel": _WORKDIR_LABELS.get(wt, wt),
        "priority": int(task.get("priority", 10)),
        "status": status,
        "statusLabel": _BURN_STATUS_LABELS.get(status, status),
        "repeatable": bool(task.get("repeatable", False)),
        "lastRun": task.get("last_run"),
        "resultSummary": task.get("result_summary"),
    }


async def _broadcast_automation_update(scope: str) -> None:
    """자동화 상태 변경 시 연결된 웹 클라이언트에 즉시 갱신 신호를 보낸다."""
    try:
        await broadcast_ws_json({"type": "automation_updated", "scope": scope})
    except Exception:
        pass


async def _read_json_body(request: web.Request) -> dict[str, Any]:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


async def handle_schedules_list(_request: web.Request) -> web.Response:
    """GET /api/schedules — 등록된 정기 작업 목록을 반환한다."""
    items = [_format_schedule_view(e) for e in schedules.list_all()]
    return web.json_response({"status": "ok", "items": items})


async def handle_schedules_create(request: web.Request) -> web.Response:
    """POST /api/schedules — 자연어 또는 직접 입력으로 정기 작업을 등록한다."""
    body = await _read_json_body(request)
    natural_text = str(body.get("naturalText") or "").strip()
    cron_expr = str(body.get("cron") or "").strip()
    prompt_text = str(body.get("prompt") or "").strip()
    description = str(body.get("description") or "").strip()
    model = str(body.get("model") or "").strip() or None

    if natural_text:
        extra_args = [x for x in (os.getenv("AGENT_EXTRA_ARGS", "")).split() if x]
        res = await run_agent(
            _NL_TO_CRON_PROMPT + natural_text,
            session_id=None,
            workdir=WORKDIR,
            extra_args=extra_args,
            timeout=60,
        )
        parsed = _extract_json_obj(str(res.get("result") or ""))
        if not parsed or not parsed.get("cron") or not parsed.get("prompt"):
            return web.json_response(
                {"status": "error", "error": "자연어 요청에서 실행 주기(cron)와 작업을 추출하지 못했습니다."},
                status=400,
            )
        cron_expr = str(parsed["cron"]).strip()
        prompt_text = str(parsed["prompt"]).strip()
        description = str(parsed.get("description") or natural_text).strip()

    if not cron_expr or not prompt_text:
        return web.json_response(
            {"status": "error", "error": "실행 주기(cron)와 작업 프롬프트를 모두 입력해 주세요."},
            status=400,
        )

    try:
        cron_mod.Cron(cron_expr)
    except ValueError as exc:
        return web.json_response(
            {"status": "error", "error": f"올바르지 않은 cron 형식입니다: {exc}"},
            status=400,
        )

    entry = schedules.add(
        chat_id=0,
        cron=cron_expr,
        prompt=prompt_text,
        description=description or prompt_text[:50],
        model=model,
    )
    await _broadcast_automation_update("schedules")
    return web.json_response({"status": "ok", "item": _format_schedule_view(entry)})


async def handle_schedules_update(request: web.Request) -> web.Response:
    """PATCH /api/schedules/{id} — 정기 작업 활성화/비활성화 토글."""
    sched_id = request.match_info.get("id", "").strip()
    body = await _read_json_body(request)
    if "enabled" not in body:
        return web.json_response(
            {"status": "error", "error": "enabled 필드가 필요합니다."},
            status=400,
        )
    updated = schedules.set_enabled(sched_id, bool(body["enabled"]))
    if not updated:
        return web.json_response(
            {"status": "error", "error": "해당 스케줄을 찾을 수 없습니다."},
            status=404,
        )
    await _broadcast_automation_update("schedules")
    return web.json_response({"status": "ok", "item": _format_schedule_view(updated)})


async def handle_schedules_delete(request: web.Request) -> web.Response:
    """DELETE /api/schedules/{id} — 정기 작업 삭제."""
    sched_id = request.match_info.get("id", "").strip()
    if not schedules.remove(sched_id):
        return web.json_response(
            {"status": "error", "error": "해당 스케줄을 찾을 수 없습니다."},
            status=404,
        )
    await _broadcast_automation_update("schedules")
    return web.json_response({"status": "ok"})


async def handle_schedules_run(request: web.Request) -> web.Response:
    """POST /api/schedules/{id}/run — 특정 정기 작업을 즉시 1회 실행한다."""
    sched_id = request.match_info.get("id", "").strip()
    target = next((e for e in schedules.list_all() if e.get("id") == sched_id), None)
    if not target:
        return web.json_response(
            {"status": "error", "error": "해당 스케줄을 찾을 수 없습니다."},
            status=404,
        )

    from web_server import _notify_web_chat, _trigger_schedule_in_master

    extra_args = [x for x in (os.getenv("AGENT_EXTRA_ARGS", "")).split() if x]
    timeout = int(os.getenv("AGENT_TIMEOUT", "0"))
    asyncio.create_task(
        scheduler._fire(
            None,
            target,
            workdir=WORKDIR,
            extra_args=extra_args,
            timeout=timeout,
            send_to_chat=_notify_web_chat,
            vault_log=vault_log,
            trigger_master=_trigger_schedule_in_master,
            trigger_source="manual",
        )
    )
    return web.json_response({"status": "ok", "message": "마스터 에이전트(__core__)에 스케줄 실행을 트리거했습니다."})


async def handle_burn_status(_request: web.Request) -> web.Response:
    """GET /api/burn/status — 주간 쿼터 조건 및 토큰 버닝 TODO 목록을 반환한다."""
    usage_data, _ = await burn_runner.get_agy_usage_data()
    eligible, reason, meta = burn_runner.evaluate_burn_condition(usage_data, force=False)
    tasks = burn_todo.list_all()
    is_running = any(t.get("status") == "running" for t in tasks)
    has_pending = any(
        t.get("status") == "pending" or (t.get("repeatable") and t.get("status") == "completed")
        for t in tasks
    )

    hours_left = meta.get("hours_left")
    hours_label = f"{hours_left:.1f}시간 남음" if isinstance(hours_left, (int, float)) else "-"
    weekly_pct = meta.get("weekly_pct")

    return web.json_response({
        "status": "ok",
        "eligible": eligible,
        "reason": reason,
        "groupName": meta.get("group_name") or "-",
        "weeklyPct": weekly_pct if isinstance(weekly_pct, int) else None,
        "hoursLeftLabel": hours_label,
        "isRunning": is_running,
        "canRun": (not is_running) and has_pending,
        "items": [_format_burn_task_view(t) for t in tasks],
    })


async def handle_burn_todo_create(request: web.Request) -> web.Response:
    """POST /api/burn/todo — 새 토큰 버닝 개선 과제를 추가한다."""
    body = await _read_json_body(request)
    title = str(body.get("title") or "").strip()
    prompt = str(body.get("prompt") or "").strip()
    workdir_type = str(body.get("workdirType") or "repo").strip()
    if workdir_type not in ("repo", "vault"):
        workdir_type = "repo"
    try:
        priority = int(body.get("priority", 10))
    except (TypeError, ValueError):
        priority = 10

    if not title or not prompt:
        return web.json_response(
            {"status": "error", "error": "과제 제목과 실행 프롬프트를 모두 입력해 주세요."},
            status=400,
        )

    entry = burn_todo.add(
        title,
        prompt,
        workdir_type=workdir_type,
        priority=priority,
        repeatable=bool(body.get("repeatable", False)),
    )
    await _broadcast_automation_update("burn")
    return web.json_response({"status": "ok", "item": _format_burn_task_view(entry)})


async def handle_burn_todo_delete(request: web.Request) -> web.Response:
    """DELETE /api/burn/todo/{id} — 토큰 버닝 과제를 삭제한다."""
    task_id = request.match_info.get("id", "").strip()
    if not burn_todo.remove(task_id):
        return web.json_response(
            {"status": "error", "error": "해당 과제를 찾을 수 없습니다."},
            status=404,
        )
    await _broadcast_automation_update("burn")
    return web.json_response({"status": "ok"})


async def handle_burn_run(request: web.Request) -> web.Response:
    """POST /api/burn/run — 토큰 버닝 개선 작업을 수동 즉시 실행한다."""
    tasks = burn_todo.list_all()
    if any(t.get("status") == "running" for t in tasks):
        return web.json_response(
            {"status": "error", "error": "이미 실행 중인 토큰 버닝 작업이 있습니다."},
            status=409,
        )

    body = await _read_json_body(request)
    task_id = str(body.get("taskId") or "").strip() or None

    from web_server import _notify_web_chat

    async def _run_and_notify() -> None:
        try:
            await burn_runner.run_auto_burn_pipeline(
                bot=None,
                chat_id=0,
                send_to_chat=_notify_web_chat,
                force=True,
                task_id=task_id,
                vault_log=vault_log,
            )
        finally:
            await _broadcast_automation_update("burn")

    asyncio.create_task(_run_and_notify())
    return web.json_response({"status": "ok", "message": "토큰 버닝 실행을 시작했습니다."})
