"""scheduler — 정기 작업 실행 루프 (봇 프로세스 안의 백그라운드 asyncio 작업).

매분 경계마다 깨어나 schedules.json 의 모든 작업을 cron 매칭하고,
마스터 에이전트(__core__)에 실행 턴을 명시적으로 트리거한다.
마스터 에이전트는 작업 성격에 따라 직접 브리핑하거나 하위 채팅(session-ctl)에
비동기 위임하여 실행·검수·기록 흐름을 일관되게 관리한다.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Awaitable, Callable

import schedules
from agent_engine import fmt_cost, get_agy_usage_report, get_model, run_agent
from cron import Cron

logger = logging.getLogger("oshyun-bot.scheduler")

SCHEDULE_TRIGGER_CHANNEL = "schedule_trigger"


def _is_usage_report_prompt(prompt: str) -> bool:
    low = prompt.lower()
    return prompt in ("/usage", "usage", "/quota", "agy usage") or (
        "agy" in low and ("usage" in low or "사용량" in prompt)
    )


def _is_gmail_schedule(schedule_entry: dict, prompt: str) -> bool:
    """스케줄이 Gmail 메일 수신/동기화 작업인지 판별."""
    if schedule_entry.get("kind") == "gmail":
        return True
    low_prompt = (prompt or "").lower()
    low_desc = str(schedule_entry.get("description") or "").lower()
    for text in (low_prompt, low_desc):
        if ("메일" in text or "gmail" in text) and any(
            k in text for k in ("수신", "동기화", "읽", "가져오", "요약", "정리", "체크")
        ):
            return True
    return False


def build_master_schedule_prompt(
    schedule_entry: dict,
    *,
    trigger_source: str = "cron",
    usage_report: str | None = None,
    mail_report: str | None = None,
) -> str:
    """마스터 에이전트(__core__)에 전달할 정기 스케줄 트리거 프롬프트를 생성한다."""
    sid = str(schedule_entry.get("id") or "").strip() or "-"
    desc = str(schedule_entry.get("description") or "").strip() or "정기 작업"
    cron_expr = str(schedule_entry.get("cron") or "").strip() or "-"
    prompt = str(schedule_entry.get("prompt") or "").strip()
    model = str(schedule_entry.get("model") or "").strip()
    effort = str(schedule_entry.get("effort") or "").strip()
    trigger_label = "수동 즉시 실행" if trigger_source == "manual" else "정기 도래(cron)"

    lines = [
        "[정기 스케줄 실행 요청]",
        f"- 스케줄 ID: {sid}",
        f"- 설명: {desc}",
        f"- 실행 주기(cron): {cron_expr}",
        f"- 트리거 유형: {trigger_label}",
        f"- 지정 모델: {model or '(기본 정책)'}",
    ]
    if effort:
        lines.append(f"- 추론 강도(effort): {effort}")
    lines.extend([
        "",
        "[수행할 작업 지시]",
        prompt or desc,
    ])
    if usage_report is not None:
        lines.extend(["", "[사전 조회된 AGY 사용량 리포트]", usage_report])
    if mail_report is not None:
        lines.extend(["", "[사전 수신된 메일 동기화 현황]", mail_report])

    lines.extend([
        "",
        "[마스터 에이전트 실행 지침]",
        "- 정기 스케줄이 시점에 도래하여 마스터(__core__)에 명시적으로 트리거되었습니다.",
        "- 사전 조회 리포트 브리핑 등 1~2초 내 즉답 가능한 작업은 직접 정리해 보고하고, "
        "볼트 조회·분석·코드·토큰버닝 등 시간이 걸리는 작업은 `session-ctl spawn` 또는 `session-ctl send`로 "
        "하위 채팅에 비동기 위임(--async)한 뒤 완료 보고를 받아 최종 브리핑하세요.",
    ])
    if model:
        lines.append(f"- 지정 모델(`{model}`)이 있으므로 하위 위임 시 해당 모델을 우선 적용하세요.")
    if effort:
        lines.append(f"- 지정 추론 강도(`{effort}`)를 우선 적용하세요.")
    return "\n".join(lines)


async def _fire(
    bot,
    schedule_entry: dict,
    *,
    workdir,
    extra_args,
    timeout,
    send_to_chat,
    vault_log=None,
    trigger_master: Callable[[dict, str], Awaitable[None]] | None = None,
    trigger_source: str = "cron",
) -> None:
    """작업 한 건 실행 → trigger_master가 있으면 마스터(__core__)에 트리거하고, 없으면 직접 실행 fallback."""
    sid = schedule_entry.get("id")
    desc = schedule_entry.get("description") or sid or "정기 작업"
    logger.info("정기 작업 실행 [%s] %s", sid, schedule_entry.get("description"))

    prompt = schedule_entry.get("prompt", "").strip()
    is_usage_report = _is_usage_report_prompt(prompt)
    is_gmail = _is_gmail_schedule(schedule_entry, prompt)

    mail_report: str | None = None
    if is_gmail:
        try:
            from gmail_manager import default_gmail_manager

            sync_res = default_gmail_manager.sync_to_vault()
            mail_report = (
                f"📬 메일 {sync_res['totalCount']}건 수신 완료 "
                f"(미열람 {sync_res['unreadCount']}건). 볼트 노트({sync_res['vaultFile']})에 동기화되었습니다."
            )
        except Exception as exc:
            mail_report = f"⚠️ Gmail 동기화 실패 또는 계정 미연동: {exc}"
            logger.warning("정기 작업 [%s] Gmail 동기화 오류: %s", sid, exc)

    if trigger_master is not None:
        usage_report: str | None = None
        if is_usage_report:
            try:
                usage_report = await get_agy_usage_report()
            except Exception as exc:
                usage_report = f"⚠️ 사용량 리포트 사전 조회 실패: {exc}"
        master_prompt = build_master_schedule_prompt(
            schedule_entry,
            trigger_source=trigger_source,
            usage_report=usage_report,
            mail_report=mail_report,
        )
        try:
            await trigger_master(schedule_entry, master_prompt)
            if vault_log:
                vault_log("🔔 스케줄 트리거", f"{desc} → 마스터(__core__)")
        except Exception as exc:
            logger.warning("정기 작업 [%s] 마스터 트리거 실패: %s", sid, exc)
            if send_to_chat:
                try:
                    await send_to_chat(
                        bot,
                        schedule_entry.get("chat_id", 0),
                        f"⚠️ 정기 작업 [{desc}] 마스터 트리거 중 오류가 발생했습니다: {exc}",
                    )
                except Exception as send_exc:
                    logger.warning("정기 작업 [%s] 오류 알림 전송 실패: %s", sid, send_exc)
        return

    try:
        if is_usage_report:
            result = await get_agy_usage_report()
            cost = 0.0
        elif is_gmail and mail_report and "실패" in mail_report:
            result = mail_report
            cost = 0.0
        else:
            run_result = await run_agent(
                prompt,
                None,  # 새 세션 — 정기 작업은 사용자 대화 맥락을 잇지 않는다.
                workdir=workdir,
                model=schedule_entry.get("model") or get_model(),  # 스케줄에 모델이 지정되었으면 우선 사용, 없으면 발화 시점의 /model 설정.
                extra_args=extra_args,
                timeout=timeout,
            )
            result = run_result["result"]
            cost = run_result["cost"]
    except Exception as exc:  # 예기치 못한 오류로 스케줄러가 죽지 않게 방어.
        logger.warning("정기 작업 [%s] 실행 오류: %s", sid, exc)
        result = f"⚠️ 정기 작업 실행 중 오류가 발생했습니다: {exc}"
        cost = None

    if vault_log:
        vault_log("🔔 스케줄", f"{desc}{fmt_cost(cost)}")
    header = f"🔔 {schedule_entry.get('description') or '정기 작업'}\n\n"
    try:
        await send_to_chat(bot, schedule_entry["chat_id"], header + result)
    except Exception as exc:
        logger.warning("정기 작업 [%s] 결과 전송 실패: %s", sid, exc)


async def run_scheduler(
    bot,
    *,
    workdir,
    extra_args,
    timeout,
    send_to_chat,
    vault_log=None,
    allowed_chat_ids: set[int] | list[int] | None = None,
    trigger_master: Callable[[dict, str], Awaitable[None]] | None = None,
) -> None:
    """매분 cron 을 매칭해 정기 작업을 실행하는 무한 루프."""
    logger.info("스케줄러 시작 — 매분 cron 매칭")
    # id → "YYYY-MM-DD HH:MM": 같은 분에 두 번 실행되는 것을 막는다(경계 부근 재기상 대비).
    fired: dict[str, str] = {}

    while True:
        now = datetime.now().astimezone()
        # 다음 분 경계 직후(+0.5s)까지 대기 — 정확히 분 단위로 깨어 한 번만 매칭한다.
        delay = 60 - (now.second + now.microsecond / 1_000_000)
        try:
            await asyncio.sleep(max(0.0, delay) + 0.5)
        except asyncio.CancelledError:
            logger.info("스케줄러 종료")
            return

        now = datetime.now().astimezone()
        stamp = now.strftime("%Y-%m-%d %H:%M")

        # schedules.json 정기 작업 cron 매칭
        for schedule_entry in schedules.list_all():
            if not schedule_entry.get("enabled", True):
                continue
            sid = schedule_entry.get("id")
            if fired.get(sid) == stamp:
                continue
            try:
                cron = Cron(schedule_entry["cron"])
            except (ValueError, KeyError) as exc:
                logger.warning("작업 [%s] 의 cron 이 올바르지 않습니다(건너뜀): %s", sid, exc)
                continue
            if cron.matches(now):
                fired[sid] = stamp
                asyncio.create_task(
                    _fire(
                        bot,
                        schedule_entry,
                        workdir=workdir,
                        extra_args=extra_args,
                        timeout=timeout,
                        send_to_chat=send_to_chat,
                        vault_log=vault_log,
                        trigger_master=trigger_master,
                        trigger_source="cron",
                    )
                )

        # 오래된 fired 기록 정리 — 현재 분에 발사된 것만 남긴다(메모리 누수 방지).
        fired = {k: v for k, v in fired.items() if v == stamp}
