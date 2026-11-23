"""agent_engine — `claude -p` / `agy` 호출 및 비동기 프로세스 스트리밍 엔진.

텔레그램 및 웹에서 받은 프롬프트를 로컬 CLI로 실행하고 결과 텍스트와
session_id를 돌려준다. 비동기(subprocess)라 이벤트 루프를 막지 않는다.
하위 모듈(engine_sandbox, engine_drivers, engine_settings)을 조합하는 통합 파사드.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import signal
import subprocess
import time
from collections.abc import Callable
from typing import Any

from bot.engine.engine_drivers import AGY_DEFAULT_MODEL, AGY_MODEL_CANDIDATES, AGY_MODELS, CODING_SYSTEM_PROMPT, DEFAULT_ENGINE, DEFAULT_MODEL, EFFORT_LEVELS, ENGINE_LABELS, ENGINE_REGISTRY, ENGINES, MODEL_CANDIDATES, MODEL_LABELS, ORCHESTRATOR_SYSTEM_PROMPT, SYSTEM_PROMPT, AGYEngine, BaseEngine, ClaudeEngine, ProgressCb, _LEGACY_AGY_MODEL_ALIASES, get_engine_instance, get_instruction_dir, get_quota_alert_prompt
from bot.engine.engine_sandbox import SANDBOX_ENABLED, _resolve_agy_model_effort, _resolve_claude_bin, _sandbox_prefix, _seed_claude_home, sandbox_placement_warnings
from bot.engine.engine_settings import _DEFAULTS, _load_settings, _save_settings, _settings, check_and_reset_if_idle, clear_session, delete_session_files, get_coding_mode, get_effort, get_engine, get_last_activity, get_model, get_model_candidates, get_model_labels, get_session, get_supported_efforts, get_verbose, set_coding_mode, set_effort, set_engine, set_model, set_session, set_verbose, update_last_activity
from bot.engine.stream_parser import _EMPTY_OUTPUT_TEXT, _NO_RESULT_TEXT, _append_session_log, _clip, _event_feed_lines, _filter_stderr, _parse_stream, _tool_label
from bot.engine.usage_pricing import fmt_cost, format_token_usage, get_agy_usage_data, get_agy_usage_report, get_cached_agy_usage, refresh_agy_quota_cache

logger = logging.getLogger("oshyun-bot.agent_engine")

_PROGRESS_EMIT_INTERVAL = 1.5
_POLL_INTERVAL = 0.3


def _build_invocation(
    prompt: str,
    session_id: str | None,
    *,
    workdir: str,
    model: str,
    effort: str | None = None,
    extra_args: list[str] | None = None,
    slash_commands: bool = False,
    sandbox: bool | None = None,
    coding: bool = False,
    orchestrator: bool = False,
    expert_vault: bool = False,
    expert_coding: bool = False,
    web: bool = False,
    extra_env: dict[str, str] | None = None,
    agent_id: str | None = None,
) -> dict:
    """엔진(claude / agy) 호출의 명령행·환경변수·정규화된 workdir 를 담은 dict 를 만든다."""
    workdir = os.path.realpath(workdir)
    if not os.path.isdir(workdir):
        from bot.core.paths import ROOT_DIR, WORKDIR
        logger.warning(
            "[agent_engine] 지정된 workdir 경로가 존재하지 않습니다: %s (안전 폴백 적용)",
            workdir,
        )
        fallback = ROOT_DIR if (coding or expert_coding) else WORKDIR
        if not (fallback and os.path.isdir(fallback)):
            fallback = ROOT_DIR if os.path.isdir(ROOT_DIR) else WORKDIR
        if fallback and os.path.isdir(fallback):
            workdir = os.path.realpath(fallback)
    engine_inst = get_engine_instance()
    effective_model = (model or get_model()).strip()
    effective_effort = effort if effort is not None else get_effort()
    return engine_inst.build_cmd(
        prompt,
        session_id,
        workdir=workdir,
        model=effective_model,
        effort=effective_effort,
        extra_args=extra_args,
        slash_commands=slash_commands,
        sandbox=sandbox,
        coding=coding,
        orchestrator=orchestrator,
        expert_vault=expert_vault,
        expert_coding=expert_coding,
        web=web,
        extra_env=extra_env,
        agent_id=agent_id,
    )


def is_session_stale_error(text: str | None) -> bool:
    """세션이 유실·만료되었거나 외부/타 프로세스 세션이어서 접근 불가능한 오류인지 판별."""
    if not text:
        return False
    lower = text.lower()
    patterns = (
        "trajectory not found",
        "failed to ensure trajectory loaded",
        "failed to send message: not_found",
        "not found in any store",
        "no conversation found",
        "conversation not found",
        "session_not_found",
        "not found locally",
    )
    return any(p in lower for p in patterns)


def is_quota_exhausted_error(text: str | None) -> bool:
    """모델 쿼터/사용량 소진 오류인지 판별한다."""
    if not text:
        return False
    lower = text.lower()
    patterns = (
        "individual quota reached",
        "quota reached",
        "quota exceeded",
        "resource_exhausted",
    )
    return any(p in lower for p in patterns)


def detached_pid_alive(pid: int | None) -> bool:
    """분리 실행 프로세스가 살아 있는지 확인 (setsid 라 pid==pgid 검증)."""
    if not pid or int(pid) <= 0:
        return False
    try:
        return os.getpgid(int(pid)) == int(pid)
    except (OSError, TypeError, ValueError):
        return False


def kill_job(pid: int | None) -> None:
    """분리 실행된 프로세스 그룹을 강제 종료한다."""
    if not pid or int(pid) <= 0:
        return
    try:
        os.killpg(os.getpgid(int(pid)), signal.SIGKILL)
    except (OSError, TypeError, ValueError):
        try:
            os.kill(int(pid), signal.SIGKILL)
        except (OSError, TypeError, ValueError):
            pass


def _inject_quota_alert_for_agy(
    prompt: str,
    *,
    slash_commands: bool = False,
    orchestrator: bool = False,
    expert_vault: bool = False,
    expert_coding: bool = False,
    agent_id: str | None = None,
) -> str:
    """AGY 엔진은 --append-system-prompt 가 없으므로 쿼터 부족 시 프롬프트 상단에 [QUOTA ALERT] 를 주입한다."""
    if (
        orchestrator
        or expert_vault
        or expert_coding
        or agent_id
        or slash_commands
        or not prompt
        or prompt.lstrip().startswith(("/", "[QUOTA ALERT]"))
        or get_engine_instance().name != "agy"
    ):
        return prompt
    quota_alert = get_quota_alert_prompt()
    if not quota_alert:
        return prompt
    return f"{quota_alert}\n\n{prompt}"


def spawn_agent(
    prompt: str,
    session_id: str | None,
    *,
    workdir: str,
    out_file: str,
    err_file: str,
    model: str = "",
    effort: str | None = None,
    extra_args: list[str] | None = None,
    slash_commands: bool = False,
    sandbox: bool | None = None,
    coding: bool = False,
    orchestrator: bool = False,
    expert_vault: bool = False,
    expert_coding: bool = False,
    web: bool = False,
    extra_env: dict[str, str] | None = None,
    agent_id: str | None = None,
) -> subprocess.Popen:
    """에이전트를 **분리된 세션(setsid)** 으로 띄우고 stream-json 을 out_file 에 기록한다."""
    effective_prompt = _inject_quota_alert_for_agy(
        prompt,
        slash_commands=slash_commands,
        orchestrator=orchestrator,
        expert_vault=expert_vault,
        expert_coding=expert_coding,
        agent_id=agent_id,
    )
    invocation = _build_invocation(
        effective_prompt,
        session_id,
        workdir=workdir,
        model=model,
        effort=effort,
        extra_args=extra_args,
        slash_commands=slash_commands,
        sandbox=sandbox,
        coding=coding,
        orchestrator=orchestrator,
        expert_vault=expert_vault,
        expert_coding=expert_coding,
        web=web,
        extra_env=extra_env,
        agent_id=agent_id,
    )
    os.makedirs(os.path.dirname(out_file), exist_ok=True)
    out_handle = open(out_file, "wb")
    err_handle = open(err_file, "wb")
    try:
        return subprocess.Popen(
            invocation["cmd"],
            cwd=invocation["workdir"],
            env=invocation["env"],
            stdin=subprocess.DEVNULL,
            stdout=out_handle,
            stderr=err_handle,
            start_new_session=True,
        )
    finally:
        out_handle.close()
        err_handle.close()


async def _reap(proc: subprocess.Popen | None) -> None:
    """종료된 자식을 비차단으로 회수해 좀비를 막는다."""
    if proc is None:
        return
    for _ in range(20):
        if proc.poll() is not None:
            return
        await asyncio.sleep(0.1)


async def watch_agent(
    out_file: str,
    *,
    pid: int,
    start_ts: float,
    timeout: int = 0,
    on_progress: ProgressCb | None = None,
    on_complete: Callable[[dict[str, Any]], Any] | None = None,
    err_file: str | None = None,
    proc: subprocess.Popen | None = None,
    session_id: str | None = None,
) -> dict:
    """분리 실행된 에이전트의 출력 파일(out_file)을 tail 하며 진행 피드를 흘리고 결과를 파싱."""
    async def _emit_complete(result: dict[str, Any]) -> dict[str, Any]:
        if on_complete is not None:
            try:
                maybe_coro = on_complete(result)
                if inspect.isawaitable(maybe_coro):
                    await maybe_coro
            except Exception as exc:
                logger.warning("[agent_engine] on_complete 훅 실행 실패: %s", exc)
        return result
    out_lines: list[str] = []
    feed: list[str] = []
    last_emit = 0.0
    buf = b""
    offset = 0
    done = False
    timed_out = False

    def _read_new() -> bytes:
        nonlocal offset
        try:
            with open(out_file, "rb") as file_handle:
                file_handle.seek(offset)
                chunk = file_handle.read()
        except FileNotFoundError:
            return b""
        offset += len(chunk)
        return chunk

    async def _consume(raw: bytes) -> None:
        nonlocal done, last_emit
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            return
        out_lines.append(line)
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            return
        if ev.get("type") == "result" or ev.get("event") == "result":
            done = True
        if on_progress is not None:
            new = _event_feed_lines(ev)
            if new:
                feed.extend(new)
                now = time.monotonic()
                if now - last_emit >= _PROGRESS_EMIT_INTERVAL:
                    last_emit = now
                    await on_progress("\n".join(feed[-30:]))

    def _alive() -> bool:
        return (proc.poll() is None) if proc is not None else detached_pid_alive(pid)

    dead_confirmed = False
    while not done:
        chunk = _read_new()
        if chunk:
            dead_confirmed = False
            buf += chunk
            parts = buf.split(b"\n")
            buf = parts.pop()
            for raw in parts:
                await _consume(raw)
                if done:
                    break
            continue
        if not _alive():
            if dead_confirmed:
                break
            dead_confirmed = True
            await asyncio.sleep(0.1)
            continue
        if timeout and timeout > 0 and time.time() - start_ts > timeout:
            timed_out = True
            kill_job(pid)
            break
        await asyncio.sleep(_POLL_INTERVAL)

    if not timed_out and buf.strip():
        await _consume(buf)

    await _reap(proc)
    if timed_out:
        return await _emit_complete({
            "result": f"⏱ 시간 초과({timeout}s)로 중단했습니다.",
            "session_id": None,
            "cost": None,
            "model": None,
            "interrupted": False,
        })

    out = "\n".join(out_lines)
    if not done:
        parsed = (
            _parse_stream(
                out, fallback_session_id=session_id, fallback_model=get_model
            )
            if out.strip()
            else None
        )
        if parsed and parsed.get("result") not in (
            None,
            "",
            _EMPTY_OUTPUT_TEXT,
            _NO_RESULT_TEXT,
        ):
            parsed["interrupted"] = True
            return await _emit_complete(parsed)
        err = ""
        if err_file:
            try:
                with open(err_file, encoding="utf-8", errors="replace") as err_handle:
                    err = _filter_stderr(err_handle.read().strip())
            except OSError:
                pass
        msg = f"⚠️ {get_engine()} 오류:\n{err}" if err else "⚠️ 응답이 중단되었습니다."
        return await _emit_complete({
            "result": msg,
            "session_id": (parsed or {}).get("session_id"),
            "cost": None,
            "model": None,
            "interrupted": True,
        })

    parsed = _parse_stream(
        out, fallback_session_id=session_id, fallback_model=get_model
    )
    parsed["interrupted"] = False
    return await _emit_complete(parsed)


async def run_agent(
    prompt: str,
    session_id: str | None,
    *,
    workdir: str,
    model: str = "",
    effort: str | None = None,
    extra_args: list[str] | None = None,
    timeout: int = 0,
    slash_commands: bool = False,
    sandbox: bool | None = None,
    coding: bool = False,
    orchestrator: bool = False,
    expert_vault: bool = False,
    expert_coding: bool = False,
    web: bool = False,
) -> dict:
    """엔진 1회 동기식 실행(PIPE 기반)."""
    effective_prompt = _inject_quota_alert_for_agy(
        prompt,
        slash_commands=slash_commands,
        orchestrator=orchestrator,
        expert_vault=expert_vault,
        expert_coding=expert_coding,
    )
    invocation = _build_invocation(
        effective_prompt,
        session_id,
        workdir=workdir,
        model=model,
        effort=effort,
        extra_args=extra_args,
        slash_commands=slash_commands,
        sandbox=sandbox,
        coding=coding,
        orchestrator=orchestrator,
        expert_vault=expert_vault,
        expert_coding=expert_coding,
        web=web,
    )

    proc = await asyncio.create_subprocess_exec(
        *invocation["cmd"],
        cwd=invocation["workdir"],
        env=invocation["env"],
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=16 * 1024 * 1024,
    )

    out_lines: list[str] = []
    stderr_chunks: list[bytes] = []

    async def _drain_stderr() -> None:
        async for chunk in proc.stderr:
            stderr_chunks.append(chunk)

    async def _read_stdout() -> None:
        async for raw in proc.stdout:
            if not raw.strip():
                continue
            out_lines.append(raw.decode("utf-8", "replace"))

    stderr_task = asyncio.create_task(_drain_stderr())
    try:
        if timeout and timeout > 0:
            await asyncio.wait_for(_read_stdout(), timeout=timeout)
        else:
            await _read_stdout()
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        stderr_task.cancel()
        return {
            "result": f"⏱ 시간 초과({timeout}s)로 중단했습니다.",
            "session_id": session_id,
            "cost": None,
            "model": None,
        }
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        stderr_task.cancel()
        raise
    await proc.wait()
    await stderr_task

    out = "".join(out_lines)
    err = b"".join(stderr_chunks).decode("utf-8", "replace")

    if proc.returncode != 0:
        clean_err = _filter_stderr(err)
        msg = (clean_err or out).strip()
        return {
            "result": f"⚠️ {get_engine()} 실행 오류 (exit {proc.returncode}):\n{msg}",
            "session_id": session_id,
            "cost": None,
            "model": None,
        }

    return _parse_stream(out, fallback_session_id=session_id, fallback_model=get_model)
