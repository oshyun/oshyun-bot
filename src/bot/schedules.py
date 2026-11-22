"""schedules — 정기 작업(다이제스트) 영속화 (schedules.json).

각 작업 한 건: ``{id, chat_id, cron, prompt, description, enabled, created}``.
json_store 의 원자적 저장(tmp→replace)을 따른다. 스케줄러가 매분 ``list_all()`` 로
다시 읽으므로 추가/삭제는 재시작 없이 바로 반영된다.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from json_store import read_json, write_json_atomic
from paths import SCHEDULES_FILE


def list_all() -> list[dict]:
    return read_json(SCHEDULES_FILE, [], expect_type=list)


def add(
    chat_id: int,
    cron: str,
    prompt: str,
    description: str,
    model: str | None = None,
    effort: str | None = None,
    *,
    kind: str = "prompt",
) -> dict:
    """새 작업을 추가하고 저장된 항목(생성된 id 포함)을 반환한다."""
    items = list_all()
    entry = {
        "id": uuid.uuid4().hex[:6],
        "chat_id": chat_id,
        "cron": cron,
        "prompt": prompt,
        "description": description,
        "kind": kind if kind in ("prompt", "burn", "gmail") else "prompt",
        "enabled": True,
        "created": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    if model:
        entry["model"] = model
    if effort:
        entry["effort"] = effort
    items.append(entry)
    write_json_atomic(SCHEDULES_FILE, items, indent=2)
    return entry


def remove(sched_id: str) -> bool:
    """id 로 작업을 삭제한다. 삭제했으면 True, 없으면 False."""
    items = list_all()
    kept = [entry for entry in items if entry.get("id") != sched_id]
    if len(kept) == len(items):
        return False
    write_json_atomic(SCHEDULES_FILE, kept, indent=2)
    return True


_UPDATABLE_STR_FIELDS = ("cron", "prompt", "description")
_OPTIONAL_STR_FIELDS = ("model", "effort")


def update_schedule(sched_id: str, **fields) -> dict | None:
    """id 로 작업의 필드(cron, prompt, description, model, effort, enabled)를 수정하고 반환한다."""
    items = list_all()
    for entry in items:
        if entry.get("id") != sched_id:
            continue
        for key in _UPDATABLE_STR_FIELDS:
            if key in fields and fields[key] is not None:
                entry[key] = str(fields[key]).strip()
        for key in _OPTIONAL_STR_FIELDS:
            if key in fields:
                val = str(fields[key] or "").strip()
                if val:
                    entry[key] = val
                else:
                    entry.pop(key, None)
        if "enabled" in fields and fields["enabled"] is not None:
            entry["enabled"] = bool(fields["enabled"])
        entry["updated"] = datetime.now().astimezone().isoformat(timespec="seconds")
        write_json_atomic(SCHEDULES_FILE, items, indent=2)
        return entry
    return None


def set_enabled(sched_id: str, enabled: bool) -> dict | None:
    """id 로 작업의 활성화 상태를 변경하고 수정된 항목을 반환한다."""
    return update_schedule(sched_id, enabled=bool(enabled))


def ensure_default_burn_schedule() -> dict | None:
    """schedules.json 에 토큰 버닝 스케줄이 없으면 매시 정각(0 * * * *) 기본 일반 스케줄을 등록한다."""
    items = list_all()
    if any(item.get("id") == "burn-auto" or item.get("kind") == "burn" for item in items):
        return None
    entry = {
        "id": "burn-auto",
        "chat_id": 0,
        "cron": "0 * * * *",
        "prompt": "토큰 버닝 점검 및 실행",
        "description": "🔥 자동 토큰 버닝 (매시 정각 조건 점검)",
        "enabled": True,
        "created": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    items.append(entry)
    write_json_atomic(SCHEDULES_FILE, items, indent=2)
    return entry


def ensure_default_gmail_schedule(cron: str = "0 8 * * *") -> dict | None:
    """schedules.json 에 Gmail 정기 수신 스케줄이 없으면 기본 스케줄을 등록한다."""
    items = list_all()
    if any(item.get("id") == "gmail-auto" or item.get("kind") == "gmail" for item in items):
        return None
    entry = {
        "id": "gmail-auto",
        "chat_id": 0,
        "cron": cron,
        "prompt": "최근 메일을 수신하고 볼트(_bot/메일.md)를 갱신한 뒤 중요 및 미열람 메일을 간결하게 요약 브리핑해줘.",
        "description": "📧 정기 메일 수신 및 볼트 동기화",
        "kind": "gmail",
        "enabled": True,
        "created": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    items.append(entry)
    write_json_atomic(SCHEDULES_FILE, items, indent=2)
    return entry
