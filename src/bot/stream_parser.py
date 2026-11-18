"""stream_parser — claude 및 agy의 stream-json 이벤트 파싱 및 피드 변환 헬퍼."""

from __future__ import annotations

import json
import os
import time
from typing import Callable

from paths import SESSION_LOG_FILE as _SESSION_LOG_FILE
from usage_pricing import _calc_cost

# 빈 응답 판정용 표시 문구 — _parse_stream 이 결과 텍스트로 만들고,
# watch_agent 가 '사실상 비었는가' 판정에 재사용한다.
_EMPTY_OUTPUT_TEXT = "(빈 출력)"
_NO_RESULT_TEXT = "(결과 없음)"


def _clip(text: str, limit: int) -> str:
    """텍스트의 공백을 정규화하고 지정 길이를 초과하면 말줄임표(…)를 붙인다."""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tool_label(block: dict) -> str:
    """tool_use 블록을 'Read: `파일경로`' 처럼 사람이 읽을 한 줄로 변환한다."""
    name = block.get("name", "도구")
    tool_input = block.get("input") or {}
    path_hint = (
        tool_input.get("file_path") or tool_input.get("path") or tool_input.get("pattern")
    )
    text_hint = tool_input.get("command") or tool_input.get("query") or tool_input.get("prompt")
    if path_hint:
        return f"{name}: `{_clip(str(path_hint), 200)}`"
    if text_hint:
        return f"{name}: {_clip(str(text_hint), 200)}"
    return name


def _event_feed_lines(ev: dict) -> list[str]:
    """stream-json 이벤트 한 건을 진행 피드에 추가할 표시 줄로 변환한다.

    claude/agy 의 에이전트 루프(생각 → 도구 호출 → 결과 → …)를 turn 단위로 노출해
    사용자가 '지금 무엇을 하는 중인지' 답답하지 않게 보도록 한다.
    """
    # 1. Claude 이벤트 (type="assistant")
    if ev.get("type") == "assistant":
        lines: list[str] = []
        for block in ev.get("message", {}).get("content", []):
            block_type = block.get("type")
            if block_type == "thinking":
                txt = block.get("thinking") or ""
                if txt.strip():
                    lines.append("🤔 " + txt + "\n")
            elif block_type == "text":
                txt = block.get("text") or ""
                if txt.strip():
                    lines.append("💬 " + txt)
            elif block_type == "tool_use":
                lines.append("🔧 " + _tool_label(block))
        return lines

    # 2. AGY 이벤트 (event="step_update")
    if ev.get("event") == "step_update":
        su = ev.get("step_update", {})
        st = su.get("step_type")
        state = su.get("state")
        lines = []
        if st == "tool" and state == "ACTIVE":
            tool_name = su.get("tool_name") or "도구"
            tool_info = su.get("tool_info") or {}
            params = tool_info.get("parameters") or {}
            hint = (
                params.get("CommandLine")
                or params.get("Path")
                or params.get("AbsolutePath")
                or params.get("TargetFile")
                or params.get("SearchPath")
                or params.get("Query")
                or params.get("Pattern")
                or params.get("Url")
                or params.get("DirectoryPath")
                or params.get("file_path")
                or params.get("path")
                or params.get("command")
                or params.get("query")
            )
            if hint:
                lines.append(f"🔧 {tool_name}: `{_clip(str(hint), 200)}`")
            else:
                lines.append(f"🔧 {tool_name}")
        elif st == "agent_response":
            delta = su.get("text_delta") or ""
            if delta.strip():
                lines.append("💬 " + delta)
        return lines

    return []


def _filter_stderr(err: str) -> str:
    """stderr 에서 agy/claude 의 무해한 초기화/경고 로그를 걸러내 실제 오류만 남긴다."""
    if not err:
        return ""
    filtered = []
    for line in err.splitlines():
        # agy glog 초기화 전 무해한 경고/정보 로그 제거
        if "logging before google.Init" in line:
            continue
        if "Failed to resolve model flag" in line:
            continue
        if "Failed to refresh cache in background" in line:
            continue
        filtered.append(line)
    return "\n".join(filtered).strip()


def _append_session_log(out: str) -> None:
    """원본 스트림(JSONL)을 그대로 로그에 추가한다. 최근 100 세션 유지."""
    raw_lines = [line for line in out.splitlines() if line.strip()]
    if not raw_lines:
        return
    try:
        os.makedirs(os.path.dirname(_SESSION_LOG_FILE), exist_ok=True)
        try:
            with open(_SESSION_LOG_FILE, encoding="utf-8") as log_reader:
                existing = log_reader.read()
        except FileNotFoundError:
            existing = ""

        # 세션 구분: 고유 마커로 분리 (git diff '--- a/' 와의 충돌 방지)
        marker = f"=== SESSION_MARKER {time.time()} ===\n"
        if "=== SESSION_MARKER " in existing:
            blocks = existing.split("=== SESSION_MARKER ")
            blocks = [b for b in blocks if b.strip()]
        else:
            # 기존 레거시 구분자 호환
            blocks = existing.split("--- ")
            blocks = [b for b in blocks if b.strip()]

        blocks.append(marker.lstrip("=== SESSION_MARKER ") + "\n".join(raw_lines) + "\n")
        if len(blocks) > 100:
            blocks = blocks[-100:]

        with open(_SESSION_LOG_FILE, "w", encoding="utf-8") as log_writer:
            for block in blocks:
                log_writer.write("=== SESSION_MARKER " + block)
    except Exception:
        pass


def extract_agy_transcript_final_response(conversation_id: str | None) -> str | None:
    """AGY 세션의 brain transcript.jsonl 에서 최종 PLANNER_RESPONSE 의 텍스트 답변을 추출한다."""
    if not conversation_id:
        return None
    transcript_path = os.path.expanduser(
        f"~/.gemini/antigravity-cli/brain/{conversation_id}/.system_generated/logs/transcript.jsonl"
    )
    if not os.path.exists(transcript_path):
        return None

    final_content: str | None = None
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line_str = line.strip()
                if not line_str:
                    continue
                try:
                    obj = json.loads(line_str)
                except Exception:
                    continue
                # MODEL의 PLANNER_RESPONSE 이벤트만 어시스턴트 최종 답변으로 인정 (source가 명시되지 않은 레거시/목 객체 허용)
                if obj.get("type") == "PLANNER_RESPONSE" and obj.get("source") in ("MODEL", None):
                    cnt = obj.get("content")
                    if cnt and isinstance(cnt, str) and cnt.strip():
                        if "<SYSTEM_MESSAGE>" not in cnt:
                            final_content = cnt.strip()
    except Exception:
        pass
    return final_content


def _parse_stream(
    out: str,
    *,
    fallback_session_id: str | None = None,
    fallback_model: str | Callable[[], str] = "",
) -> dict:
    """stream-json 출력을 파싱해 run_agent/watch_agent 반환 dict를 만들고 로그에 기록한다.

    stream-json 이벤트 타입:
      Claude:
        system/init   — session_id, 도구 목록
        assistant     — 응답 텍스트, 실제 model명
        result        — cost, duration, usage 등 최종 요약
      AGY:
        init          — conversation_id, tools, model
        step_update   — agent_response, tool
        result        — conversation_id, status, response, usage
    """
    events: list[dict] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass

    if not events:
        return {
            "result": out.strip() or _EMPTY_OUTPUT_TEXT,
            "session_id": fallback_session_id,
            "cost": None,
            "model": None,
            "usage": {},
        }

    # 이벤트별 값 추출
    result_text: str | None = None
    session_id: str | None = fallback_session_id
    cost: float | None = None
    actual_model: str | None = None
    result_event: dict = {}
    token_usage: dict = {}
    agy_text_deltas: list[str] = []
    agy_turn_has_error: bool = False

    for ev in events:
        # Claude style
        t = ev.get("type")
        if t == "system" and ev.get("subtype") == "init":
            session_id = ev.get("session_id") or session_id
        elif t == "assistant":
            # 텍스트 블록에서 응답 추출
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    result_text = block.get("text", "")
        elif t == "result":
            result_event = ev
            session_id = ev.get("session_id") or session_id
            model_usage = ev.get("modelUsage") or {}
            actual_model = next(iter(model_usage), None)
            mu = model_usage.get(actual_model, {}) if actual_model else {}
            token_usage = ev.get("usage") or mu or {}
            cost = _calc_cost(actual_model, mu) if actual_model else (ev.get("total_cost_usd") or 0.0)
            if result_text is None:
                result_text = ev.get("result")

        # AGY style
        event_name = ev.get("event")
        if event_name == "init":
            session_id = ev.get("conversation_id") or session_id
            init_info = ev.get("init") or {}
            if init_info.get("model"):
                raw_m = str(init_info.get("model"))
                actual_model = raw_m.removesuffix("-high").removesuffix("-medium").removesuffix("-low")
        elif event_name == "step_update":
            su = ev.get("step_update") or {}
            if su.get("conversation_id") and not session_id:
                session_id = su.get("conversation_id")
            st = su.get("step_type")
            if st == "error_message":
                agy_turn_has_error = True
            elif st == "agent_response":
                delta = su.get("text_delta")
                if delta and isinstance(delta, str):
                    agy_text_deltas.append(delta)
                    agy_turn_has_error = False
        elif event_name == "result":
            result_event = ev
            res_obj = ev.get("result")
            if isinstance(res_obj, dict):
                session_id = res_obj.get("conversation_id") or session_id
                resp = res_obj.get("response")
                if resp is not None and str(resp).strip():
                    result_text = str(resp).strip()
                if res_obj.get("status") == "ERROR":
                    # AGY는 기존 세션을 재개할 때 과거 턴에 발생한 error_message가 남아있으면
                    # 현재 턴이 정상 응답을 반환해도 result.status="ERROR" 및 과거 error를 집계한다.
                    has_valid_turn_response = bool(
                        result_text or "".join(agy_text_deltas).strip()
                    )
                    if agy_turn_has_error or not has_valid_turn_response:
                        err = res_obj.get("error")
                        result_text = f"⚠️ {err or result_text or '실행 오류'}"
                usage = res_obj.get("usage") or {}
                token_usage = usage
                if actual_model is None:
                    actual_model = fallback_model() if callable(fallback_model) else fallback_model
                cost = _calc_cost(actual_model, usage)
            elif isinstance(res_obj, str) and res_obj.strip():
                result_text = res_obj.strip()
                session_id = ev.get("session_id") or ev.get("conversation_id") or session_id

    # AGY 스트림에서 최종 result.response 가 비어있거나 누락된 경우, 스트리밍 delta 누적본으로 폴백
    if (not result_text or result_text == _NO_RESULT_TEXT) and agy_text_deltas:
        accumulated = "".join(agy_text_deltas).strip()
        if accumulated:
            result_text = accumulated

    # 스트림이 비정상 종료되었거나 결과 텍스트가 누락된 경우, AGY 세션 transcript.jsonl 로 폴백 복구
    if (not result_text or result_text in (_EMPTY_OUTPUT_TEXT, _NO_RESULT_TEXT)) and session_id:
        transcript_resp = extract_agy_transcript_final_response(session_id)
        if transcript_resp:
            result_text = transcript_resp

    if result_text is None:
        result_text = result_event.get("result") or _NO_RESULT_TEXT
    if result_event.get("is_error"):
        result_text = f"⚠️ {result_text}"

    # 원본 스트림을 그대로 로그에 기록. 세션 구분선 + 원본 JSONL 라인.
    _append_session_log(out)

    return {
        "result": result_text,
        "session_id": session_id,
        "cost": cost,
        "model": actual_model,
        "usage": token_usage,
    }
