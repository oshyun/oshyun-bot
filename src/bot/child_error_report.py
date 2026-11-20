"""child_error_report.py — 하위 채팅 오류의 부모 에이전트 보고 보조 로직 (순수 함수 + 멱등 가드).

- 오류 응답 판별(`looks_like_error_reply`)
- 모델명 정규화/사전 검증(`normalize_model`)
- 오류 보고 본문 생성(`build_error_report`)
- 동일 오류 중복 보고 방지(`claim_error_report`)
"""

from __future__ import annotations

import hashlib
import re
import time

# 하위 채팅 오류를 부모로 보고할 때 재귀 보고를 막기 위해 제외하는 채널(보고·정기 턴 자체)
NO_REPORT_CHANNELS = frozenset({"task_report", "schedule_trigger", "master_patrol"})

ERROR_HEADER = "[하위 작업 중단/오류 보고] [하위 작업 오류 보고]"

_ERROR_PATTERNS = re.compile(
    r"invalid model selection|model not recognized|⏱\s*시간 초과|에이전트 기동 실패",
    re.IGNORECASE,
)
_MODEL_ERROR = re.compile(r"invalid model selection|model not recognized", re.IGNORECASE)
_EFFORT_SUFFIX = re.compile(r"\s*\(([^()]*)\)\s*$")
_AVAILABLE_MODELS = re.compile(r"Available models:\s*(.+)", re.IGNORECASE | re.DOTALL)


def looks_like_error_reply(text: str | None) -> bool:
    """정상 응답처럼 도착했지만 실제로는 실행 오류인 텍스트인지 판별."""
    s = (text or "").strip()
    return bool(s) and (s.startswith("⚠️") or bool(_ERROR_PATTERNS.search(s[:600])))


def is_model_error(text: str | None) -> bool:
    return bool(_MODEL_ERROR.search(text or ""))


def _compact(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def normalize_model(raw: str, models: dict[str, str]) -> tuple[str | None, str | None]:
    """표시명/별칭 → (실제 모델 키, effort). 해석 불가 시 (None, effort).

    `"Claude Sonnet 5.5 (Medium)"` → (`claude-sonnet-5-5`, `medium`).
    `models` 는 {모델키: 표시명}.
    """
    name = (raw or "").strip()
    effort = None
    m = _EFFORT_SUFFIX.search(name)
    if m:
        effort = m.group(1).strip().lower() or None
        name = name[: m.start()].strip()
    if name in models:
        return name, effort
    q = _compact(name)
    if not q:
        return None, effort
    hits = [
        key for key, label in models.items()
        if q in (_compact(key), _compact(label), _compact(f"claude {label}"))
    ]
    return (hits[0], effort) if len(hits) == 1 else (None, effort)


def format_allowed_models(models: dict[str, str]) -> str:
    return ", ".join(f"{k} ({v})" for k, v in models.items())


def build_error_report(
    *,
    room_title: str,
    room_id: str,
    error_text: str,
    task_id: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    parent_label: str = "마스터",
    allowed_models: dict[str, str] | None = None,
) -> str:
    body = (error_text or "").strip() or "(오류 원문 없음)"
    if len(body) > 1500:
        body = body[:1500] + "…"
    lines = [
        f"{ERROR_HEADER} [{room_title}] 세션 작업 중단",
        f"• 대상 방: {room_title} (Room ID: {room_id})",
    ]
    if task_id:
        lines.append(f"• 작업 ID: {task_id} (상태: failed)")
    lines.append(f"• 사용 모델: {model or '(기본값)'} / effort: {effort or '(기본값)'}")
    lines.append(f"• 오류 원문:\n{body}")
    hint = f"※ {parent_label} 지침: 작업 실패 원인을 확인하고 재시도 여부를 판단하세요."
    if is_model_error(body):
        listed = format_allowed_models(allowed_models) if allowed_models else ""
        if not listed:
            m = _AVAILABLE_MODELS.search(body)
            listed = m.group(1).strip()[:600] if m else ""
        if listed:
            lines.append(f"• 허용 모델 목록: {listed}")
        hint = (
            f"※ {parent_label} 지침: 모델명 오류입니다. 위 허용 모델 키(표시명 아님)로 "
            "`session-ctl send <방ID> ... --model <허용 모델 키>` 재시도하세요."
        )
    lines.append("")
    lines.append(hint)
    return "\n".join(lines)


_reported: dict[str, float] = {}


def claim_error_report(room_id: str, error_text: str, ttl: float = 300.0) -> bool:
    """(방, 오류 원문) 단위로 최초 1회만 True — task_id 가 없는 방의 중복 보고 방지."""
    now = time.monotonic()
    for k in [k for k, ts in _reported.items() if now - ts > ttl]:
        _reported.pop(k, None)
    key = f"{room_id}:{hashlib.sha1((error_text or '').encode('utf-8')).hexdigest()}"
    if key in _reported:
        return False
    _reported[key] = now
    return True


def release_error_report(room_id: str, error_text: str) -> None:
    key = f"{room_id}:{hashlib.sha1((error_text or '').encode('utf-8')).hexdigest()}"
    _reported.pop(key, None)
