"""agy_wrapper — `agy -p` CLI 기반 Master-Inspector-Sub-Agy 태스크 슈퍼바이저 래퍼.

LLM 자체 Tool Calling 에 의존하지 않고, OS 서브프로세스 종료 시점(Exit Code)과
정규식 XML 태그 파싱만으로 Sub-Agy 실행, Inspector 검증, 보강 재실행,
마스터 보고(HTTP/파일 인박스/tmux)를 제어한다.

Token Firewall (토큰 절약 4대 규칙):
1. Sub-Agy 산출물 파일 오프로딩: 긴 결과물은 `.agy_workspace/{task_id}/output.md` 에 저장 지시.
2. Sub-Agy -> Inspector 입력 절삭: `stdout` 전문은 `full.log` 에 덤프하고 마지막 1,500자(Tail) + `output.md` 만 전달.
3. Inspector -> Master 요약 슬라이싱 & 하드 캡: `<SUMMARY>` 내부만 정규식 추출(실패 시 마지막 300자) 후 최대 500자 하드 캡 적용.
4. Call-by-Reference 포인터 페이로드: 압축 `summary` 와 `pointers` (`artifact_path`, `raw_log_path`) 만 마스터에 전달.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
from typing import Any, Optional
from urllib import error as urllib_error
from urllib import request as urllib_request

WORKSPACE_BASE_DIR = ".agy_workspace"
SUB_STDOUT_TAIL_CHARS = 1500
MAX_SUMMARY_CHARS = 500
FALLBACK_SUMMARY_TAIL_CHARS = 300
SUMMARY_TRUNCATE_SUFFIX = "...(상세 내용은 아티팩트 파일 참조)"

_ANTIGRAVITY_ENV_KEYS = (
    "ANTIGRAVITY_AGENT",
    "ANTIGRAVITY_LS_ADDRESS",
    "ANTIGRAVITY_CSRF_TOKEN",
    "ANTIGRAVITY_PROJECT_ID",
    "ANTIGRAVITY_CONVERSATION_ID",
    "ANTIGRAVITY_TRAJECTORY_ID",
)
_VALID_VERDICTS = ("PASS", "PARTIAL", "FAIL")


@dataclass
class SubAgyRunResult:
    """단일 `agy -p` 서브프로세스 실행 결과."""

    exit_status: str  # "NORMAL" | "TIMEOUT_KILLED" | "ERROR"
    return_code: int
    stdout: str
    stderr: str


@dataclass
class InspectionResult:
    """Inspector Agy의 XML 태그 파싱 및 Token Firewall 절삭 결과."""

    verdict: str  # "PASS" | "PARTIAL" | "FAIL"
    summary_for_master: str
    missing_reason: Optional[str]
    delta_task: Optional[str]


def _sanitize_task_id(task_id: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9._\-]", "_", str(task_id or "").strip())
    return clean.strip("._") or "default_task"


def get_task_pointers(task_id: str, workspace_root: str = WORKSPACE_BASE_DIR) -> dict[str, str]:
    """태스크별 아티팩트(`output.md`)와 원시 로그(`full.log`) 포인터 경로를 반환한다."""
    safe_id = _sanitize_task_id(task_id)
    base = workspace_root.rstrip("/") or WORKSPACE_BASE_DIR
    return {
        "artifact_path": os.path.join(base, safe_id, "output.md"),
        "raw_log_path": os.path.join(base, safe_id, "full.log"),
    }


def _resolve_agy_cmd() -> str:
    """실행 가능한 `agy` CLI 경로를 해석한다 (PATH 우선, 미검출 시 ~/.local/bin/agy)."""
    if shutil.which("agy"):
        return "agy"
    local_bin = os.path.expanduser("~/.local/bin/agy")
    if os.path.exists(local_bin):
        return local_bin
    return "agy"


def _clean_agy_env(env: Optional[dict[str, str]] = None) -> dict[str, str]:
    """부모 Antigravity 세션 변수 충돌을 막기 위해 클린 환경 사본을 만든다."""
    cleaned = dict(os.environ if env is None else env)
    for key in _ANTIGRAVITY_ENV_KEYS:
        cleaned.pop(key, None)
    return cleaned


def _default_callback_url() -> str:
    """로컬 마스터 콜백 엔드포인트 기본 URL을 환경변수·SSL 설정으로부터 추론한다."""
    explicit = os.getenv("OSHYUN_MASTER_CALLBACK_URL", "").strip()
    if explicit:
        return explicit
    port = os.getenv("WEB_PORT", os.getenv("VOICE_WEB_PORT", "8765")).strip() or "8765"
    proto = "http"
    try:
        from paths import DEFAULT_SSL_CERT_FILE

        ssl_enabled = os.getenv("WEB_SSL_ENABLED", "").lower() not in ("0", "false", "no") and os.path.exists(
            DEFAULT_SSL_CERT_FILE
        )
        if ssl_enabled:
            proto = "https"
    except Exception:
        pass
    return f"{proto}://127.0.0.1:{port}/v1/callbacks/task-events"


def prepare_sub_agy_prompt(
    task_id: str,
    task_prompt: str,
    accumulated_context: list[str],
    *,
    workspace_root: str = WORKSPACE_BASE_DIR,
) -> str:
    """Token Firewall 규칙 1(Artifact Offloading)이 주입된 Sub-Agy 프롬프트를 조립한다."""
    pointers = get_task_pointers(task_id, workspace_root=workspace_root)
    artifact_path = pointers["artifact_path"]
    os.makedirs(os.path.dirname(artifact_path), exist_ok=True)

    if accumulated_context:
        prev_summary = "\n---\n".join(accumulated_context)
        body = (
            f"[이전 서브 에이전트가 수행한 중간 결과 요약]\n{prev_summary}\n\n"
            f"[당신이 추가로 완수해야 할 보강 작업]\n{task_prompt}"
        )
    else:
        body = task_prompt

    offload_directive = (
        "\n\n[출력 및 산출물 저장 필수 규칙 (Token Firewall)]\n"
        f"1. 긴 결과물, 코드, 로그 분석 데이터 전문은 절대 표준 출력(stdout)에 직접 출력하지 마십시오.\n"
        f"2. 모든 상세 산출물과 결과 전문은 반드시 로컬 작업 디렉토리의 `{artifact_path}` 파일에 저장하십시오.\n"
        f"3. 표준 출력(stdout)에는 작업 완료 여부, 저장된 파일 경로(`{artifact_path}`), 핵심 결론 3~5줄만 짧게 출력하고 종료하십시오."
    )
    return f"{body}{offload_directive}"


def dump_raw_log_and_build_inspector_input(
    task_id: str,
    sub_stdout: str,
    *,
    workspace_root: str = WORKSPACE_BASE_DIR,
    attempt: int = 1,
) -> tuple[str, str, dict[str, str]]:
    """Token Firewall 규칙 2: `stdout` 전문을 `full.log` 에 덤프하고 Tail 1,500자 + `output.md` 만 반환한다."""
    pointers = get_task_pointers(task_id, workspace_root=workspace_root)
    raw_log_path = pointers["raw_log_path"]
    artifact_path = pointers["artifact_path"]
    os.makedirs(os.path.dirname(raw_log_path), exist_ok=True)

    raw_text = sub_stdout or ""
    if attempt <= 1 or not os.path.exists(raw_log_path):
        with open(raw_log_path, "w", encoding="utf-8") as log_f:
            log_f.write(raw_text)
    else:
        with open(raw_log_path, "a", encoding="utf-8") as log_f:
            log_f.write(f"\n\n=== Attempt #{attempt} ===\n{raw_text}")

    stdout_tail = raw_text[-SUB_STDOUT_TAIL_CHARS:] if raw_text else ""

    artifact_content = ""
    if os.path.isfile(artifact_path):
        with open(artifact_path, "r", encoding="utf-8", errors="replace") as art_f:
            artifact_content = art_f.read().strip()

    return stdout_tail, artifact_content, pointers


def extract_tag(text: str, tag: str, default: str = "") -> str:
    """XML 유사 태그 `<TAG>...</TAG>` 내부 문자열을 정규식으로 추출한다."""
    if not text:
        return default
    pattern = rf"<{re.escape(tag)}>(.*?)</{re.escape(tag)}>"
    match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else default


def cap_summary_text(text: str, max_chars: int = MAX_SUMMARY_CHARS) -> str:
    """요약 문자열에 500자 하드 캡을 적용하고 초과분은 절삭 후 안내 문구를 붙인다."""
    cleaned = (text or "").strip()
    if len(cleaned) > max_chars:
        return cleaned[:max_chars] + SUMMARY_TRUNCATE_SUFFIX
    return cleaned


def extract_and_cap_summary(raw_inspector_stdout: str) -> str:
    """Token Firewall 규칙 3: `<SUMMARY>` 태그 추출 + 외부 텍스트 폐기 + 500자 하드 캡 + 300자 Fallback."""
    raw = raw_inspector_stdout or ""
    extracted = extract_tag(raw, "SUMMARY", "")
    if not extracted:
        extracted = extract_tag(raw, "SUMMARY_FOR_MASTER", "")

    if not extracted:
        # Fallback: 태그 파싱 실패 시에도 전문을 넘기지 않고 맨 마지막 300자만 대체
        stripped = raw.strip()
        if not stripped:
            return "(요약 출력 없음)"
        extracted = stripped[-FALLBACK_SUMMARY_TAIL_CHARS:]

    return cap_summary_text(extracted)


async def run_agy_cli(
    prompt: str,
    timeout_sec: int = 120,
    *,
    extra_args: Optional[list[str]] = None,
    cwd: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
) -> SubAgyRunResult:
    """`agy -p` 명령을 서브프로세스로 실행하고 종료(또는 타임아웃 kill) 시 stdout 을 수거한다."""
    cmd = [_resolve_agy_cmd(), "-p", prompt]
    if extra_args:
        cmd.extend(extra_args)

    exec_kwargs: dict[str, Any] = {
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
        "env": _clean_agy_env(env),
    }
    if cwd:
        exec_kwargs["cwd"] = cwd

    proc = await asyncio.create_subprocess_exec(*cmd, **exec_kwargs)
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_sec
        )
        status = "NORMAL" if proc.returncode == 0 else "ERROR"
        return SubAgyRunResult(
            exit_status=status,
            return_code=proc.returncode or 0,
            stdout=stdout_bytes.decode("utf-8", errors="replace").strip(),
            stderr=stderr_bytes.decode("utf-8", errors="replace").strip(),
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        stdout_bytes, stderr_bytes = await proc.communicate()
        return SubAgyRunResult(
            exit_status="TIMEOUT_KILLED",
            return_code=-9,
            stdout=stdout_bytes.decode("utf-8", errors="replace").strip(),
            stderr=stderr_bytes.decode("utf-8", errors="replace").strip(),
        )


def _build_inspector_prompt(
    *,
    objective: str,
    criteria: str,
    accumulated_context: list[str],
    exit_status: str,
    stdout_tail: str,
    artifact_content: str,
    artifact_path: str,
) -> str:
    prev_text = "\n".join(accumulated_context) if accumulated_context else "없음"
    art_block = artifact_content if artifact_content else "(아직 생성되지 않음)"
    tail_block = stdout_tail if stdout_tail else "(출력 없음)"
    return f"""당신은 서브 에이전트의 작업 결과를 검수하고 마스터 에이전트에게 보고서를 작성하는 Inspector Agy입니다.
아래 [원본 목표 및 완료 기준]과 [서브 에이전트 산출물/로그]를 비교하여 엄격하게 평가하십시오.

[원본 목표]
{objective}

[완료 기준 (Acceptance Criteria)]
{criteria}

[이전 시도 누적 요약]
{prev_text}

[이번 서브 에이전트 실행 상태]
- 종료 상태: {exit_status} (NORMAL 또는 TIMEOUT_KILLED 또는 ERROR)

[서브 에이전트 stdout 마지막 {SUB_STDOUT_TAIL_CHARS}자 (Tail)]
{tail_block}

[산출물 파일 내용 ({artifact_path})]
{art_block}

---
반드시 아래 4개의 XML 태그를 정확히 포함하여 응답을 출력하고 즉시 종료하십시오. 태그 바깥에 다른 인사말이나 추론 과정은 쓰지 마십시오.
만약 완료 기준 중 하나라도 누락되었거나 타임아웃(TIMEOUT_KILLED)으로 잘렸다면 무조건 PARTIAL로 판정해야 합니다.
<SUMMARY> 태그 내부는 마스터에게 전달될 압축 요약문이므로 반드시 500자 이내의 핵심 팩트만 작성하십시오.

<VERDICT>PASS 또는 PARTIAL 또는 FAIL 중 하나만 작성</VERDICT>
<SUMMARY>
현재까지 성공적으로 확인된 작업 결과와 핵심 팩트를 500자 이내로 명확히 요약 작성
</SUMMARY>
<MISSING_REASON>
(PASS일 경우 NONE 작성) PARTIAL이나 FAIL일 경우, 어떤 기준이 왜 미흡했는지 솔직하게 작성
</MISSING_REASON>
<DELTA_TASK_FOR_NEXT_AGENT>
(PASS나 FAIL일 경우 NONE 작성) PARTIAL일 경우, 보강 서브 에이전트가 이미 끝난 작업은 건너뛰고 부족한 부분만 즉시 완수할 수 있도록 구체적인 지시 프롬프트 작성
</DELTA_TASK_FOR_NEXT_AGENT>"""


async def run_inspector_agy(
    objective: str,
    criteria: str,
    accumulated_context: list[str],
    sub_result: SubAgyRunResult,
    *,
    task_id: str = "default",
    workspace_root: str = WORKSPACE_BASE_DIR,
    attempt: int = 1,
    timeout_sec: int = 60,
    repair_on_parse_error: bool = True,
    extra_args: Optional[list[str]] = None,
    cwd: Optional[str] = None,
) -> InspectionResult:
    """중간 에이전트(Inspector)를 `agy -p` 로 실행하여 결과를 검증하고 태그를 파싱한다."""
    stdout_tail, artifact_content, pointers = dump_raw_log_and_build_inspector_input(
        task_id,
        sub_result.stdout,
        workspace_root=workspace_root,
        attempt=attempt,
    )
    inspector_prompt = _build_inspector_prompt(
        objective=objective,
        criteria=criteria,
        accumulated_context=accumulated_context,
        exit_status=sub_result.exit_status,
        stdout_tail=stdout_tail,
        artifact_content=artifact_content,
        artifact_path=pointers["artifact_path"],
    )
    insp_run = await run_agy_cli(
        inspector_prompt, timeout_sec=timeout_sec, extra_args=extra_args, cwd=cwd
    )
    raw = insp_run.stdout

    raw_verdict = extract_tag(raw, "VERDICT", "").upper()
    if raw_verdict not in _VALID_VERDICTS and repair_on_parse_error:
        repair_prompt = (
            "이전 응답이 지정된 XML 태그 포맷을 준수하지 않았습니다.\n"
            "아래 텍스트를 바탕으로 <VERDICT>, <SUMMARY>, <MISSING_REASON>, "
            "<DELTA_TASK_FOR_NEXT_AGENT> 4개의 XML 태그만 정확히 출력하십시오.\n\n"
            f"[원본 응답 발췌]\n{raw[-SUB_STDOUT_TAIL_CHARS:]}"
        )
        repaired_run = await run_agy_cli(
            repair_prompt, timeout_sec=timeout_sec, extra_args=extra_args, cwd=cwd
        )
        if repaired_run.stdout.strip():
            raw = repaired_run.stdout
            raw_verdict = extract_tag(raw, "VERDICT", "").upper()

    verdict = raw_verdict if raw_verdict in _VALID_VERDICTS else "PARTIAL"
    if sub_result.exit_status == "TIMEOUT_KILLED" and verdict == "PASS":
        # 명세서 4절: 타임아웃으로 잘렸다면 무조건 PARTIAL 로 판정
        verdict = "PARTIAL"

    summary = extract_and_cap_summary(raw)
    missing = extract_tag(raw, "MISSING_REASON", "출력 포맷 누락 또는 기준 미달")
    delta = extract_tag(raw, "DELTA_TASK_FOR_NEXT_AGENT", objective)

    if verdict == "PASS":
        missing_out: Optional[str] = None
        delta_out: Optional[str] = None
    else:
        missing_out = None if missing.upper() == "NONE" else missing
        delta_out = None if (verdict == "FAIL" or delta.upper() == "NONE") else delta

    return InspectionResult(
        verdict=verdict,
        summary_for_master=summary,
        missing_reason=missing_out,
        delta_task=delta_out,
    )


def _write_json_file_atomic(dest_path: str, payload: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
    tmp_path = f"{dest_path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, dest_path)


def _write_inbox_report(inbox_dir: str, task_id: str, payload: dict[str, Any]) -> None:
    safe_id = _sanitize_task_id(task_id)
    _write_json_file_atomic(os.path.join(inbox_dir, f"{safe_id}_report.json"), payload)


def report_to_master(
    callback_url: str,
    payload: dict[str, Any],
    *,
    max_retries: int = 3,
    backoff_sec: float = 0.5,
    timeout: int = 10,
    token: Optional[str] = None,
    inbox_dir: Optional[str] = None,
) -> None:
    """LLM 이 아닌 Wrapper(Python 코드)가 직접 마스터에게 보고를 전송한다(HTTP / File Inbox / tmux)."""
    task_id = str(payload.get("task_id") or "task")

    # 방식 C: 파일 인박스 폴링 (inbox_dir 지정 또는 file:// URL)
    if inbox_dir:
        _write_inbox_report(inbox_dir, task_id, payload)
        return
    if callback_url.startswith("file://"):
        file_target = callback_url[len("file://") :]
        if file_target.endswith(".json"):
            _write_json_file_atomic(file_target, payload)
        else:
            _write_inbox_report(file_target, task_id, payload)
        return

    # 방식 B: tmux 세션 주입
    if callback_url.startswith("tmux://"):
        target_session = callback_url[len("tmux://") :].strip()
        summary = str(payload.get("summary") or "")[:200]
        msg = f"[Sub-Agy 보고] task_id={task_id} event={payload.get('event_type')} summary={summary}"
        subprocess.run(["tmux", "send-keys", "-t", target_session, msg, "Enter"], check=True)
        return

    # 방식 A: HTTP 웹훅 콜백 (지수 백오프 재시도)
    auth_token = token or os.getenv("WEB_TOKEN", os.getenv("OSHYUN_ORCHESTRATOR_TOKEN", os.getenv("VOICE_WEB_TOKEN", ""))).strip()
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ssl_ctx = None
    if callback_url.startswith("https://"):
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

    attempts = max(1, int(max_retries))
    last_exc: Exception | None = None
    for idx in range(attempts):
        req = urllib_request.Request(callback_url, data=data, headers=headers, method="POST")
        try:
            kwargs: dict[str, Any] = {"timeout": timeout}
            if ssl_ctx is not None:
                kwargs["context"] = ssl_ctx
            with urllib_request.urlopen(req, **kwargs) as resp:
                resp.read()
            return
        except urllib_error.HTTPError as exc:
            last_exc = exc
            if exc.code < 500 and exc.code != 429:
                raise
        except (urllib_error.URLError, TimeoutError, OSError) as exc:
            last_exc = exc

        if idx < attempts - 1 and backoff_sec > 0:
            time.sleep(backoff_sec * (2**idx))

    if last_exc is not None:
        raise last_exc


def _build_master_payload(
    *,
    task_id: str,
    event_type: str,
    verdict: str,
    attempt: int,
    summary: str,
    missing_reason: Optional[str],
    next_action_taken: str,
    requires_master_intervention: bool,
    pointers: dict[str, str],
) -> dict[str, Any]:
    """Token Firewall 규칙 4(Call-by-Reference 포인터 기반 페이로드)를 생성한다."""
    return {
        "task_id": task_id,
        "event_type": event_type,
        "verdict": verdict,
        "attempt": attempt,
        "summary": summary,
        "missing_reason": missing_reason,
        "next_action_taken": next_action_taken,
        "requires_master_intervention": requires_master_intervention,
        "pointers": pointers,
    }


async def supervise_task(
    task_id: str,
    callback_url: str,
    objective: str,
    criteria: str,
    timeout_sec: int = 120,
    max_retries: int = 2,
    *,
    workspace_root: str = WORKSPACE_BASE_DIR,
    inspector_timeout_sec: int = 60,
    token: Optional[str] = None,
    inbox_dir: Optional[str] = None,
    extra_args: Optional[list[str]] = None,
    cwd: Optional[str] = None,
) -> dict[str, Any]:
    """Sub-Agy 실행 -> Inspector 검수 -> PARTIAL 시 중간 보고 & 재스폰 -> 최종 보고 루프를 제어한다."""
    attempt = 1
    max_attempts = 1 + max(0, int(max_retries))
    accumulated_context: list[str] = []
    current_task_prompt = objective
    resolved_ws = (
        os.path.join(cwd, workspace_root)
        if cwd and not os.path.isabs(workspace_root)
        else workspace_root
    )
    pointers = get_task_pointers(task_id, workspace_root=resolved_ws)

    while attempt <= max_attempts:
        full_sub_prompt = prepare_sub_agy_prompt(
            task_id,
            current_task_prompt,
            accumulated_context,
            workspace_root=resolved_ws,
        )

        sub_result = await run_agy_cli(
            full_sub_prompt, timeout_sec=timeout_sec, extra_args=extra_args, cwd=cwd
        )

        inspection = await run_inspector_agy(
            objective=objective,
            criteria=criteria,
            accumulated_context=accumulated_context,
            sub_result=sub_result,
            task_id=task_id,
            workspace_root=resolved_ws,
            attempt=attempt,
            timeout_sec=inspector_timeout_sec,
            extra_args=extra_args,
            cwd=cwd,
        )

        if inspection.verdict == "PASS":
            payload = _build_master_payload(
                task_id=task_id,
                event_type="COMPLETED",
                verdict="PASS",
                attempt=attempt,
                summary=inspection.summary_for_master,
                missing_reason=None,
                next_action_taken="모든 검증 기준을 통과하여 최종 결과를 보고합니다.",
                requires_master_intervention=False,
                pointers=pointers,
            )
            report_to_master(callback_url, payload, token=token, inbox_dir=inbox_dir)
            return payload

        if inspection.verdict == "PARTIAL":
            accumulated_context.append(inspection.summary_for_master)

            if attempt < max_attempts:
                delta_preview = (inspection.delta_task or objective).strip()[:160]
                payload = _build_master_payload(
                    task_id=task_id,
                    event_type="INTERIM_REPORT_AND_RETRY",
                    verdict="PARTIAL",
                    attempt=attempt,
                    summary=inspection.summary_for_master,
                    missing_reason=inspection.missing_reason,
                    next_action_taken=(
                        f"Attempt #{attempt} 결과가 일부 미흡하여 새로운 보강 에이전트(Attempt #{attempt + 1})를 "
                        f"즉시 스폰하여 추가 작업을 지시했습니다. (추가 지시: {delta_preview})"
                    ),
                    requires_master_intervention=False,
                    pointers=pointers,
                )
                report_to_master(callback_url, payload, token=token, inbox_dir=inbox_dir)
                current_task_prompt = inspection.delta_task or objective
                attempt += 1
                continue

            payload = _build_master_payload(
                task_id=task_id,
                event_type="PARTIAL_SUCCESS",
                verdict="PARTIAL",
                attempt=attempt,
                summary=inspection.summary_for_master,
                missing_reason=inspection.missing_reason,
                next_action_taken="최대 보강 횟수에 도달하여 현재까지의 중간 결과만 최종 보고합니다.",
                requires_master_intervention=True,
                pointers=pointers,
            )
            report_to_master(callback_url, payload, token=token, inbox_dir=inbox_dir)
            return payload

        payload = _build_master_payload(
            task_id=task_id,
            event_type="FAILED",
            verdict="FAIL",
            attempt=attempt,
            summary=inspection.summary_for_master,
            missing_reason=inspection.missing_reason,
            next_action_taken="복구 불가능한 오류로 작업을 중단합니다.",
            requires_master_intervention=True,
            pointers=pointers,
        )
        report_to_master(callback_url, payload, token=token, inbox_dir=inbox_dir)
        return payload

    # 방어적 폴백 (루프 조건상 도달 불가)
    return _build_master_payload(
        task_id=task_id,
        event_type="FAILED",
        verdict="FAIL",
        attempt=attempt,
        summary="(예상치 못한 루프 종료)",
        missing_reason="max_attempts 초과",
        next_action_taken="작업을 중단합니다.",
        requires_master_intervention=True,
        pointers=pointers,
    )


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """CLI 실행 인자를 파싱한다."""
    parser = argparse.ArgumentParser(
        prog="agy_wrapper",
        description="agy -p CLI 기반 Master-Inspector-Sub-Agy 태스크 슈퍼바이저 래퍼",
    )
    parser.add_argument("--task-id", required=True, help="고유 태스크 ID (예: task_001)")
    parser.add_argument(
        "--master-callback",
        default=_default_callback_url(),
        help="마스터 콜백 URL (http(s)://..., file://..., tmux://...)",
    )
    parser.add_argument("--objective", required=True, help="서브 에이전트에게 지시할 원본 목표")
    parser.add_argument("--criteria", required=True, help="Inspector가 검수할 완료 기준")
    parser.add_argument("--timeout", type=int, default=120, help="Sub-Agy 1회 타임아웃(초, 기본 120)")
    parser.add_argument("--inspector-timeout", type=int, default=60, help="Inspector 타임아웃(초, 기본 60)")
    parser.add_argument("--max-retries", type=int, default=2, help="PARTIAL 시 최대 보강 재시도 횟수 (기본 2)")
    parser.add_argument("--workspace-dir", default=WORKSPACE_BASE_DIR, help="아티팩트 오프로딩 루트 디렉토리")
    parser.add_argument("--inbox-dir", default=None, help="방식 C 파일 인박스 디렉토리 (지정 시 HTTP 대신 파일 저장)")
    parser.add_argument("--token", default=None, help="마스터 웹훅 Bearer 인증 토큰 (선택)")
    parser.add_argument("--cwd", default=None, help="Sub-Agy / Inspector 실행 작업 디렉토리 (선택)")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    """CLI 엔트리포인트."""
    args = parse_args(argv)
    result = asyncio.run(
        supervise_task(
            task_id=args.task_id,
            callback_url=args.master_callback,
            objective=args.objective,
            criteria=args.criteria,
            timeout_sec=args.timeout,
            max_retries=args.max_retries,
            workspace_root=args.workspace_dir,
            inspector_timeout_sec=args.inspector_timeout,
            token=args.token,
            inbox_dir=args.inbox_dir,
            cwd=args.cwd,
        )
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result.get("event_type") == "FAILED" else 0


if __name__ == "__main__":
    sys.exit(main())
