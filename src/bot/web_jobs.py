"""web_jobs — 웹 비서 실행 작업(web_jobs.json) 스레드 안전 영속화 및 고아 파일 정리."""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from json_store import read_json, write_json_atomic

_WEB_JOBS_LOCK = threading.Lock()


def load_web_jobs(jobs_file: str) -> dict[str, Any]:
    """진행 중인 웹 비서 job 레코드를 읽어온다."""
    with _WEB_JOBS_LOCK:
        return read_json(jobs_file, {}, expect_type=dict)


def save_web_job(jobs_file: str, job_id: str, rec: dict[str, Any]) -> None:
    """웹 비서 job 레코드를 디스크에 스레드 안전·원자적으로 저장한다."""
    if not job_id:
        return
    with _WEB_JOBS_LOCK:
        data = read_json(jobs_file, {}, expect_type=dict)
        data[str(job_id)] = rec
        write_json_atomic(jobs_file, data)


def remove_web_job(jobs_file: str, job_id: str) -> None:
    """완료되거나 취소된 웹 비서 job 레코드를 디스크에서 스레드 안전하게 제거한다."""
    if not job_id:
        return
    with _WEB_JOBS_LOCK:
        data = read_json(jobs_file, {}, expect_type=dict)
        if str(job_id) in data:
            del data[str(job_id)]
            write_json_atomic(jobs_file, data)


def remove_job_files(*paths: str | None) -> None:
    """작업 출력/에러 임시 파일을 안전하게 삭제한다."""
    for path in paths:
        if not path:
            continue
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


def sweep_orphan_job_files(
    jobs_dir: str,
    active_ids: set[str],
    *,
    max_age_seconds: int = 3600,
) -> int:
    """JOBS_DIR 내 활성 레코드에 없는 오래된 고아 출력 파일(.jsonl/.err)을 정리한다."""
    try:
        names = os.listdir(jobs_dir)
    except OSError:
        return 0

    now = time.time()
    removed = 0
    for name in names:
        if not (name.endswith(".jsonl") or name.endswith(".err")):
            continue
        stem = name.rsplit(".", 1)[0]
        if stem in active_ids:
            continue
        full_path = os.path.join(jobs_dir, name)
        try:
            if max_age_seconds > 0 and (now - os.path.getmtime(full_path)) < max_age_seconds:
                continue
            os.remove(full_path)
            removed += 1
        except OSError:
            pass
    return removed


def extract_steps_from_jsonl(
    out_file: str,
    extract_fn: Any,
) -> tuple[list[dict[str, Any]], int]:
    """out_file에서 실행된 도구 단계(steps)와 현재 바이트 오프셋을 추출한다."""
    steps: list[dict[str, Any]] = []
    initial_offset = 0
    if not out_file or not os.path.exists(out_file):
        return steps, initial_offset
    try:
        with open(out_file, "rb") as f:
            raw_data = f.read()
            initial_offset = len(raw_data)
            for raw_line in raw_data.splitlines():
                line_str = raw_line.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue
                for ev in extract_fn(line_str):
                    if ev.get("kind") == "tool_call":
                        steps.append(ev)
                    elif ev.get("kind") == "tool_result" and steps:
                        last_step = steps[-1]
                        last_step["status"] = "done"
                        res_detail = ev.get("detail") or ""
                        if res_detail:
                            last_step["detail"] = (
                                (last_step.get("detail") or "") + f"\n결과: {res_detail[:300]}"
                            ).strip()
    except OSError:
        pass
    return steps, initial_offset


def compute_remaining_timeout(started_at: float, base_timeout: int) -> int:
    """재부착 작업의 남은 타임아웃(초)을 계산한다. base_timeout <= 0이면 0(무제한)."""
    if not base_timeout or base_timeout <= 0:
        return 0
    elapsed = max(0, int(time.time() - started_at))
    return max(1, base_timeout - elapsed)
