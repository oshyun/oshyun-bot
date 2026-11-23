"""burn_runner — 주간 잔여 쿼터 토큰 버닝 개선 실행기.

주간 리셋 6시간 미만 & 잔여 쿼터 30% 이상일 때, 또는 사용자의 수동 트리거(/burn run) 시
burn_todo 에 등록된 개선 작업을 격리된 Git 워크트리에서 실행하고 결과를 텔레그램으로 보고한다.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
import time
from typing import Any

import bot.core.burn_todo as burn_todo
from bot.engine.agent_engine import detached_pid_alive, format_token_usage, get_agy_usage_data, get_engine, get_model, get_supported_efforts, spawn_agent, watch_agent
from bot.core.json_store import read_json, write_json_atomic
from bot.core.paths import BURN_STATE_FILE, JOBS_DIR, ROOT_DIR, WORKDIR

logger = logging.getLogger("oshyun-bot.burn_runner")

# 토큰 버닝 기본 제한시간: 1시간 (3600초)
DEFAULT_BURN_TIMEOUT = int(os.getenv("BURN_TIMEOUT", "3600"))


def _get_burn_state() -> dict[str, Any]:
    return read_json(
        BURN_STATE_FILE,
        {"last_burned_cycle": None, "last_run_at": None, "history": []},
        expect_type=dict,
    )


def _save_burn_state(state: dict[str, Any]) -> None:
    write_json_atomic(BURN_STATE_FILE, state, indent=2)


def evaluate_burn_condition(
    usage_data: dict[str, Any] | None, *, force: bool = False
) -> tuple[bool, str, dict[str, Any]]:
    """AGY 쿼터 상태를 분석하여 토큰 버닝 실행 조건 충족 여부를 판정한다.

    반환값: (should_burn, reason, meta)
    """
    if force:
        return True, "사용자 수동 강제 실행", {}

    if not usage_data:
        return False, "AGY 사용량 데이터가 없습니다.", {}

    data = usage_data.get("command", {}).get("data", {})
    groups = data.get("groups", [])
    if not groups:
        return False, "쿼터 그룹 정보를 찾을 수 없습니다.", {}

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    state = _get_burn_state()
    last_burned_cycle = state.get("last_burned_cycle")

    # 대상 그룹 탐색 (Gemini Models 우선, 없으면 첫 번째 활성 그룹)
    target_group = None
    for g in groups:
        if "gemini" in g.get("name", "").lower():
            target_group = g
            break
    if not target_group and groups:
        target_group = groups[0]

    buckets = target_group.get("buckets", [])
    weekly_bucket = None
    five_hour_bucket = None
    for b in buckets:
        w = b.get("window", "").lower()
        b_name = b.get("name", "").lower()
        if "weekly" in w or "weekly" in b_name:
            weekly_bucket = b
        elif "5h" in w or "five hour" in b_name:
            five_hour_bucket = b

    if not weekly_bucket:
        return False, "주간 한도 버킷을 찾을 수 없습니다.", {}

    reset_raw = weekly_bucket.get("reset_time")
    weekly_frac = weekly_bucket.get("remaining_fraction", 0.0)
    weekly_pct = int(round(weekly_frac * 100))

    if not reset_raw:
        return False, "리셋 시간 정보가 없습니다.", {}

    try:
        reset_dt = datetime.datetime.fromisoformat(reset_raw.replace("Z", "+00:00"))
    except Exception as exc:
        return False, f"리셋 시간 파싱 실패: {exc}", {}

    diff_sec = (reset_dt - now_utc).total_seconds()
    hours_left = diff_sec / 3600.0

    meta = {
        "group_name": target_group.get("name"),
        "weekly_pct": weekly_pct,
        "hours_left": hours_left,
        "reset_time": reset_raw,
    }

    # 1. 리셋 시간 검사: 0 < 남은시간 <= 6시간
    if diff_sec <= 0:
        return False, "이미 리셋 시각이 지났습니다.", meta
    if hours_left > 6.0:
        return (
            False,
            f"리셋까지 {hours_left:.1f}시간 남아있습니다 (6시간 이하일 때 발동).",
            meta,
        )

    # 2. 주간 잔여 쿼터 검사: >= 30%
    if weekly_pct < 30:
        return (
            False,
            f"주간 잔여 쿼터가 {weekly_pct}% 로 부족합니다 (최소 30% 필요).",
            meta,
        )

    # 3. 5시간 한도 버퍼 검사 (있을 경우 최소 15% 이상 확보)
    if five_hour_bucket and not five_hour_bucket.get("disabled"):
        five_h_frac = five_hour_bucket.get("remaining_fraction", 1.0)
        five_h_pct = int(round(five_h_frac * 100))
        meta["5h_pct"] = five_h_pct
        if five_h_pct < 15:
            return (
                False,
                f"5시간 한도 잔여율이 {five_h_pct}% 로 부족하여 봇 기본 동작을 위해 보류합니다.",
                meta,
            )

    # 4. 동일 리셋 주기 중복 실행 방지
    if last_burned_cycle == reset_raw:
        return False, f"이번 리셋 주기({reset_raw})에는 이미 버닝을 완료했습니다.", meta

    return (
        True,
        f"리셋까지 {hours_left:.1f}시간 남음, 주간 잔여 쿼터 {weekly_pct}% 로 버닝 조건 충족!",
        meta,
    )


def _extract_weekly_pct(usage_data: dict[str, Any] | None) -> int | None:
    """AGY 쿼터 데이터에서 주간 한도 잔여율(%)을 추출한다."""
    if not usage_data:
        return None
    data = usage_data.get("command", {}).get("data", {})
    groups = data.get("groups", [])
    for g in groups:
        for b in g.get("buckets", []):
            w = b.get("window", "").lower()
            b_name = b.get("name", "").lower()
            if "weekly" in w or "weekly" in b_name:
                frac = b.get("remaining_fraction")
                if frac is not None:
                    return int(round(frac * 100))
    return None


async def _finish_burn_task(
    task: dict[str, Any],
    *,
    bot,
    chat_id: int,
    send_to_chat,
    agent_output: str,
    workdir_type: str,
    branch_name: str | None,
    wt_path: str | None,
    token_usage: dict[str, Any] | None = None,
    cost: float | None = None,
    initial_weekly_pct: int | None = None,
    vault_log=None,
) -> dict[str, Any]:
    """버닝 작업 완료 후 Git 커밋/diff 검증, 토큰 사용량 집계 및 텔레그램 리포트를 발송한다."""
    task_id = task["id"]
    title = task.get("title", task_id)
    session_id = task.get("session_id", "")
    diff_summary = ""
    commit_hash = ""

    # 완료 시점 쿼터 잔여율 조회
    final_usage_data, _ = await get_agy_usage_data()
    final_weekly_pct = _extract_weekly_pct(final_usage_data)

    # 1. repo 작업인 경우 커밋 및 diff 확인
    if workdir_type == "repo" and wt_path and os.path.exists(wt_path):
        # 혹시 에이전트가 커밋을 빼먹었을 경우 자동 커밋
        status_proc = await asyncio.create_subprocess_exec(
            "git", "-C", wt_path, "status", "--porcelain",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        s_out, _ = await status_proc.communicate()
        if s_out.strip():
            await asyncio.create_subprocess_exec("git", "-C", wt_path, "add", "-A")
            await asyncio.create_subprocess_exec(
                "git", "-C", wt_path, "commit", "-m", f"feat(auto-burn): {title}"
            )

        # diff --stat 확인
        diff_proc = await asyncio.create_subprocess_exec(
            "git", "-C", wt_path, "diff", "--stat", "origin/main",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        d_out, _ = await diff_proc.communicate()
        diff_summary = d_out.decode("utf-8", "replace").strip()

        # 최근 커밋 해시
        rev_proc = await asyncio.create_subprocess_exec(
            "git", "-C", wt_path, "rev-parse", "--short", "HEAD",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        r_out, _ = await rev_proc.communicate()
        commit_hash = r_out.decode("utf-8", "replace").strip()

    # 2. 토큰 사용량 포맷팅
    token_str = format_token_usage(token_usage)

    # 3. 상태 갱신
    summary_text = (
        f"브랜치 {branch_name} ({commit_hash}) - {token_str}"
    )
    burn_todo.update_task(
        task_id,
        status="completed",
        pid=None,
        token_usage=token_usage,
        cost=cost,
        result_summary=summary_text,
    )

    # 4. 결과 리포트 전송
    report_lines = [
        f"✅ **[토큰 버닝 완료] {title}**\n",
        f"• **세션 ID**: `{session_id}`",
        f"• 🪙 **사용한 토큰량**: **{token_str}**",
    ]
    if cost:
        report_lines.append(f"• 💰 **추정 비용**: ${cost:.4f}")
    if initial_weekly_pct is not None and final_weekly_pct is not None:
        delta = initial_weekly_pct - final_weekly_pct
        delta_str = f"(-{delta}%p 소진)" if delta > 0 else "(소진량 미미)"
        report_lines.append(f"• 📊 **주간 쿼터 변화**: {initial_weekly_pct}% → **{final_weekly_pct}%** {delta_str}")
    elif final_weekly_pct is not None:
        report_lines.append(f"• 📊 **주간 쿼터 잔여**: **{final_weekly_pct}%**")

    if workdir_type == "repo":
        report_lines.append(f"• **생성 브랜치**: `{branch_name}` (`{commit_hash}`)")
        report_lines.append(f"• **워크트리 경로**: `{wt_path}`")
        if diff_summary:
            report_lines.append(f"• **변경 통계**:\n```\n{diff_summary}\n```")
        report_lines.append(
            "\n💡 **적용 방법**: 내용 검토 후 마음에 들면 `main`에 merge 하고 워크트리를 제거하세요:\n"
            f"`git merge {branch_name} && git worktree remove {wt_path}`\n"
        )
    else:
        report_lines.append("• **볼트 작업 완료**\n")

    report_lines.append(f"**[에이전트 요약 보고]**\n{agent_output[:1500]}")
    final_report = "\n".join(report_lines)

    await send_to_chat(bot, chat_id, final_report)
    if vault_log:
        vault_log("✅ 토큰버닝 완료", f"{title} ({token_str}, 커밋: {commit_hash or 'done'})")

    return {
        "status": "completed",
        "task_id": task_id,
        "session_id": session_id,
        "branch_name": branch_name,
        "wt_path": wt_path,
        "token_usage": token_usage,
        "token_summary": token_str,
        "summary": summary_text,
    }


async def execute_burn_task(
    task: dict[str, Any],
    *,
    bot,
    chat_id: int,
    send_to_chat,
    model: str | None = None,
    vault_log=None,
    timeout: int = DEFAULT_BURN_TIMEOUT,
) -> dict[str, Any]:
    """선정된 개선 작업을 분리 세션(spawn_agent)으로 실행하고 세션/PID 기록 후 결과를 보고한다."""
    task_id = task["id"]
    title = task.get("title", task_id)
    workdir_type = task.get("workdir_type", "repo")
    prompt = task.get("prompt", "").strip()

    effective_model = model or task.get("model") or get_model()
    timestamp_slug = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    session_id = f"burn-{task_id}-{timestamp_slug}"

    # 버닝에서는 모델/엔진이 지원하는 가장 높은 effort(max 또는 high)를 적용하여 심층 사고 및 토큰 소비 극대화
    cur_engine = get_engine()
    supported_efforts = get_supported_efforts(cur_engine, effective_model)
    effective_effort = supported_efforts[-1] if supported_efforts else "max"

    # 시작 전 주간 쿼터 잔여율 측정
    init_usage_data, _ = await get_agy_usage_data()
    initial_weekly_pct = _extract_weekly_pct(init_usage_data)

    wt_path = None
    branch_name = None
    target_workdir = WORKDIR

    if workdir_type == "repo":
        branch_name = f"auto-burn-{task_id}-{timestamp_slug}"
        wt_path = os.path.abspath(os.path.join(ROOT_DIR, "..", f"oshyun-bot-wt-{branch_name}"))
        target_workdir = wt_path

        # 1. 격리 워크트리 생성
        logger.info("토큰 버닝 워크트리 생성: %s (branch: %s)", wt_path, branch_name)
        add_proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            ROOT_DIR,
            "worktree",
            "add",
            "-b",
            branch_name,
            wt_path,
            "HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, add_err = await add_proc.communicate()
        if add_proc.returncode != 0:
            err_msg = add_err.decode("utf-8", "replace").strip()
            burn_todo.update_task(task_id, status="failed", result_summary=f"워크트리 생성 실패: {err_msg}")
            await send_to_chat(
                bot,
                chat_id,
                f"⚠️ **[토큰 버닝 실패]** 워크트리 생성 중 오류가 발생했습니다:\n`{err_msg}`",
            )
            return {"status": "failed", "error": err_msg}

    # 2. 에이전트 프롬프트 구성
    wrapped_prompt = (
        f"[토큰 버닝 자동 개선 지침]\n"
        f"당신은 프로젝트를 심층적으로 점검하고 개선하는 시니어 엔지니어링 에이전트입니다.\n"
        f"현재 작업 디렉토리: {target_workdir}\n"
    )
    if workdir_type == "repo":
        wrapped_prompt += (
            f"이 디렉토리는 안전하게 분리된 Git 워크트리(브랜치: {branch_name})입니다.\n"
            f"작업을 완료한 후 코드/테스트가 정상 동작하는지 직접 검증하고, 변경사항을 커밋해주세요.\n"
            f"절대 main 브랜치나 워크트리 외부를 직접 수정하지 마세요.\n\n"
        )
    wrapped_prompt += f"[개선 목표]\n{prompt}\n\n[결과 보고]\n작업한 상세 내역과 변경된 파일 목록을 정리해 주세요."

    # 3. 분리 세션(spawn_agent)으로 프로세스 분리 실행 및 영속화
    out_file = os.path.join(JOBS_DIR, f"{session_id}.jsonl")
    err_file = os.path.join(JOBS_DIR, f"{session_id}.err")

    proc = spawn_agent(
        wrapped_prompt,
        None,  # 새 독립 세션
        workdir=target_workdir,
        out_file=out_file,
        err_file=err_file,
        model=effective_model,
        effort=effective_effort,
        coding=(workdir_type == "repo"),
    )

    burn_todo.update_task(
        task_id,
        status="running",
        session_id=session_id,
        pid=proc.pid,
        out_file=out_file,
        err_file=err_file,
        branch_name=branch_name,
        wt_path=wt_path,
        workdir_type=workdir_type,
        started_at=datetime.datetime.now().astimezone().isoformat(),
        last_run=timestamp_slug,
    )

    # 4. 사용자 알림: 버닝 작업 시작 (세션 ID 명시)
    start_msg = (
        f"🔥 **[토큰 버닝] 프로젝트 개선 작업 시작**\n\n"
        f"• **과제**: {title}\n"
        f"• **세션 ID**: `{session_id}` (PID: `{proc.pid}`)\n"
        f"• **작업공간**: `{'Git 워크트리 (' + (branch_name or '') + ')' if workdir_type == 'repo' else 'Obsidian 볼트'}`\n"
        f"• **적용 모델**: `{effective_model}` (에포트: **{effective_effort.upper()}** - 최대 사고 모드)\n"
        f"• **제한 시간**: {timeout // 60}분 ({timeout}초)\n"
        f"• 백그라운드에서 심층 개선을 수행합니다..."
    )
    await send_to_chat(bot, chat_id, start_msg)
    if vault_log:
        vault_log("🔥 토큰버닝 시작", f"{title} (세션: {session_id}, PID: {proc.pid}) [{effective_effort}]")

    # 5. watch_agent 로 출력 파일 tail 및 결과 도출
    run_res = None
    try:
        run_res = await watch_agent(
            out_file,
            pid=proc.pid,
            start_ts=time.time(),
            timeout=timeout,
            err_file=err_file,
            proc=proc,
        )
        agent_output = run_res.get("result", "")
    except Exception as exc:
        logger.exception("토큰 버닝 에이전트 실행 실패: %s", exc)
        agent_output = f"실행 중 예외 발생: {exc}"

    return await _finish_burn_task(
        task,
        bot=bot,
        chat_id=chat_id,
        send_to_chat=send_to_chat,
        agent_output=agent_output,
        workdir_type=workdir_type,
        branch_name=branch_name,
        wt_path=wt_path,
        token_usage=run_res.get("usage") if isinstance(run_res, dict) else None,
        cost=run_res.get("cost") if isinstance(run_res, dict) else None,
        initial_weekly_pct=initial_weekly_pct,
        vault_log=vault_log,
    )


async def run_auto_burn_pipeline(
    *,
    bot,
    chat_id: int,
    send_to_chat,
    force: bool = False,
    task_id: str | None = None,
    vault_log=None,
) -> bool:
    """토큰 버닝 파이프라인 전체를 검사하고 조건 충족 시 작업을 실행한다."""
    usage_data, raw_str = await get_agy_usage_data()
    should_burn, reason, meta = evaluate_burn_condition(usage_data, force=force)

    if not should_burn:
        logger.info("토큰 버닝 미발동: %s", reason)
        if force:
            await send_to_chat(bot, chat_id, f"ℹ️ 토큰 버닝을 시작할 수 없습니다: {reason}")
        return False

    # 작업 선택
    task = None
    if task_id:
        task = burn_todo.get_task(task_id)
    if not task:
        task = burn_todo.get_next_pending()

    if not task:
        logger.info("실행할 대기 중인 토큰 버닝 작업이 없습니다.")
        if force:
            await send_to_chat(
                bot,
                chat_id,
                "ℹ️ 대기 중인(pending) 버닝 작업이 없습니다. `/burn todo` 로 작업을 확인하거나 추가하세요.",
            )
        return False

    # 리셋 주기 락 저장 (force 가 아닐 때만 주기 락 갱신)
    if not force and meta.get("reset_time"):
        state = _get_burn_state()
        state["last_burned_cycle"] = meta["reset_time"]
        state["last_run_at"] = datetime.datetime.now().astimezone().isoformat()
        _save_burn_state(state)

    # 실행
    asyncio.create_task(
        execute_burn_task(
            task,
            bot=bot,
            chat_id=chat_id,
            send_to_chat=send_to_chat,
            vault_log=vault_log,
        )
    )
    return True


async def recover_stuck_burn_tasks(
    *,
    bot,
    chat_id: int,
    send_to_chat,
    vault_log=None,
) -> int:
    """봇 재시작 시 running 상태인 작업을 감지하여,
    1) 기존 세션 프로세스가 살아있으면 watch_agent 로 모니터링을 재개하고,
    2) 프로세스가 실행되고 있지 않으면 작업을 자동으로 즉시 다시 트리거한다.
    """
    running_tasks = [t for t in burn_todo.list_all() if t.get("status") == "running"]
    if not running_tasks:
        return 0

    recovered_count = 0

    for t in running_tasks:
        tid = t["id"]
        title = t.get("title", tid)
        session_id = t.get("session_id", "미부여")
        pid = t.get("pid")
        out_file = t.get("out_file")
        err_file = t.get("err_file")
        wt_path = t.get("wt_path")
        branch_name = t.get("branch_name")
        workdir_type = t.get("workdir_type", "repo")

        logger.warning(
            "중단/미완료 토큰 버닝 작업 감지: [%s] %s (세션: %s, PID: %s)",
            tid, title, session_id, pid,
        )

        is_alive = detached_pid_alive(pid) if pid else False

        # 경우 1: 기존 세션으로 여전히 실행 중인 프로세스가 있는 경우 -> 모니터링 재개
        if is_alive and out_file and os.path.exists(out_file):
            logger.info("기존 세션 프로세스 생존 확인 (PID: %s) — 모니터링 재개", pid)
            msg = (
                f"🏃 **[토큰 버닝 모니터링 재개]** 재시작 전 실행 중이던 작업이 백그라운드에서 계속 실행 중입니다.\n"
                f"• **과제**: {title}\n"
                f"• **세션 ID**: `{session_id}` (PID: `{pid}`)\n"
                f"• 완료 시 결과를 이어서 보고합니다."
            )
            await send_to_chat(bot, chat_id, msg)

            async def _resume_watcher(
                curr_task=t,
                curr_out_file=out_file,
                curr_pid=pid,
                curr_err_file=err_file,
                curr_workdir_type=workdir_type,
                curr_branch_name=branch_name,
                curr_wt_path=wt_path,
            ):
                run_res = None
                try:
                    run_res = await watch_agent(
                        curr_out_file,
                        pid=curr_pid,
                        start_ts=time.time(),
                        timeout=DEFAULT_BURN_TIMEOUT,
                        err_file=curr_err_file,
                    )
                    agent_output = run_res.get("result", "")
                except Exception as exc:
                    agent_output = f"재개 감시 중 오류 발생: {exc}"

                await _finish_burn_task(
                    curr_task,
                    bot=bot,
                    chat_id=chat_id,
                    send_to_chat=send_to_chat,
                    agent_output=agent_output,
                    workdir_type=curr_workdir_type,
                    branch_name=curr_branch_name,
                    wt_path=curr_wt_path,
                    token_usage=run_res.get("usage") if isinstance(run_res, dict) else None,
                    cost=run_res.get("cost") if isinstance(run_res, dict) else None,
                    vault_log=vault_log,
                )

            asyncio.create_task(_resume_watcher())
            recovered_count += 1
            continue

        # 경우 2: 기존 세션으로 실행되고 있는 게 없는 경우 -> 커밋 확인 또는 자동 재트리거
        has_commit = False
        commit_hash = ""
        if wt_path and os.path.exists(wt_path):
            c_proc = await asyncio.create_subprocess_exec(
                "git", "-C", wt_path, "log", "origin/main..HEAD", "--oneline",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            c_out, _ = await c_proc.communicate()
            commits = c_out.decode("utf-8", "replace").strip()
            if commits:
                has_commit = True
                commit_hash = commits.splitlines()[0]

        if has_commit and branch_name:
            # 이미 커밋이 완료된 상태에서 재시작된 경우: completed 처리
            summary = f"브랜치 {branch_name} ({commit_hash}) - 재시작 전 커밋 보존됨"
            burn_todo.update_task(tid, status="completed", pid=None, result_summary=summary)
            msg = (
                f"ℹ️ **[토큰 버닝 복구]** 재시작 전 커밋 완료된 작업이 발견되어 완료 처리되었습니다.\n"
                f"• **과제**: {title}\n"
                f"• **세션 ID**: `{session_id}`\n"
                f"• **보존 브랜치**: `{branch_name}` (`{commit_hash}`)"
            )
            await send_to_chat(bot, chat_id, msg)
        else:
            # 커밋 없이 프로세스가 죽은 경우: 자동 재트리거!
            # 기존 워크트리 정리
            if wt_path and os.path.exists(wt_path):
                await asyncio.create_subprocess_exec("git", "-C", ROOT_DIR, "worktree", "remove", "--force", wt_path)
            if branch_name:
                await asyncio.create_subprocess_exec("git", "-C", ROOT_DIR, "branch", "-D", branch_name)

            burn_todo.update_task(
                tid,
                status="pending",
                pid=None,
                result_summary=f"세션 {session_id} 비정상 종료로 자동 재트리거됨",
            )

            msg = (
                f"🔄 **[토큰 버닝 자동 재트리거]** 기존 세션(`{session_id}`, PID: `{pid or 'N/A'}`)으로 실행 중인 프로세스가 없어, **작업을 자동으로 다시 시작(트리거)합니다.**\n"
                f"• **과제**: {title}"
            )
            await send_to_chat(bot, chat_id, msg)

            # 즉시 자동 재트리거!
            asyncio.create_task(
                run_auto_burn_pipeline(
                    bot=bot,
                    chat_id=chat_id,
                    send_to_chat=send_to_chat,
                    force=True,
                    task_id=tid,
                    vault_log=vault_log,
                )
            )

        recovered_count += 1
        if vault_log:
            vault_log("🔄 버닝 복구/재트리거", f"{title} (세션: {session_id})")

    return recovered_count
