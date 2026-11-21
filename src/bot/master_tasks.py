"""master_tasks — 마스터 에이전트 작업 원장(Task Ledger) 및 하위 채팅 요약본 영속화.

마스터 에이전트가 하위 채팅(AGY 세션)에 위임한 작업과 채팅방별 요약본(`room_summaries`)을
`data/state/master_tasks.json` 에 기록한다.
감독 루프(master_supervisor)는 이 원장을 근거로 하위 작업의 진행·정체·완료를 추적하고,
오케스트레이터(web_orchestrator)는 `room_summaries` 를 통해 유사 채팅방을 매칭·재사용한다.

파일 구조:
    {
      "tasks":          {"<task_id>": {...작업 레코드...}},
      "patrol":         {"enabled": bool, "lastRunAt": float, "status": str, ...},
      "room_summaries": {"<room_id>": {...채팅방 요약 레코드 (summary <= 500자)...}}
    }

작업 상태(status):
    queued    — 하위 채팅 대기열에 적재됨
    running   — 하위 채팅에서 실행 중
    stalled   — 실행 중이지만 일정 시간 이상 진행 변화가 없음 (검토 필요)
    done      — 하위 채팅이 답변을 완료함 (검토 전이면 검토 필요)
    failed    — 실행 오류/중단 (검토 필요)
    lost      — 재시작 등으로 추적이 끊기고 결과도 찾지 못함 (검토 필요)
    cancelled — 사용자가 취소함
    closed    — 마스터(또는 사용자)가 검토를 마치고 종결함
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from typing import Any

from json_store import read_json, write_json_atomic
from paths import MASTER_TASKS_FILE

ACTIVE_STATUSES = ("queued", "running", "stalled")
ATTENTION_STATUSES = ("done", "failed", "stalled", "lost")
TERMINAL_STATUSES = ("done", "failed", "lost", "cancelled", "closed")
ALL_STATUSES = ACTIVE_STATUSES + ("done", "failed", "lost", "cancelled", "closed")


def is_task_aborted(task: dict | None) -> bool:
    """사용자가 명시적으로 중단(cancelled)했거나 중단 확인(user_confirmed/user_cancelled)된 작업인지 판별한다."""
    if not isinstance(task, dict):
        return False
    return bool(
        task.get("user_cancelled")
        or task.get("user_confirmed")
        or task.get("status") == "cancelled"
    )

# 종결 작업 및 채팅 요약은 최신 N건만 보존한다(원장 비대화 방지). 진행 중 작업은 항상 보존.
MAX_TERMINAL_KEEP = 200
MAX_NOTES = 10
MAX_ROOM_SUMMARIES_KEEP = 300

_CORE_ROOM_ID = "__core__"
_MAX_ROOM_SUMMARY_CHARS = 500
_MAX_ROOM_QUERIES = 6
_MAX_QUERY_DIGEST_CHARS = 140
_MAX_RESULT_DIGEST_CHARS = 260
_MAX_NOTE_DIGEST_CHARS = 200

_lock = threading.Lock()


def _now() -> float:
    return time.time()


def _clip_line(text: Any, limit: int) -> str:
    """공백·개행을 한 줄로 정규화하고 지정 길이 이내로 자른다."""
    s = " ".join(str(text or "").split())
    if limit <= 0:
        return ""
    return s if len(s) <= limit else s[: max(1, limit - 1)] + "…"


def _extract_query_digest(instruction: str, title: str = "") -> str:
    """위임 지시문에서 보일러플레이트 헤더를 제거하고 핵심 질문/지시 요약을 추출한다."""
    raw_lines = [ln.strip() for ln in str(instruction or "").splitlines() if ln.strip()]
    filtered = [ln for ln in raw_lines if not re.match(r"^\[마스터[^\]]*\]$", ln)]
    chosen_lines = filtered if filtered else raw_lines
    digest = _clip_line(" ".join(chosen_lines), _MAX_QUERY_DIGEST_CHARS)
    if not digest and title:
        digest = _clip_line(title, _MAX_QUERY_DIGEST_CHARS)
    return digest


def _compose_room_summary_text(entry: dict[str, Any]) -> str:
    """채팅방 요약 레코드를 500자 하드 캡(Token Firewall) 내의 단일 요약 문자열로 합성한다."""
    custom = _clip_line(entry.get("customSummary"), _MAX_ROOM_SUMMARY_CHARS)
    if custom:
        return custom

    title = _clip_line(entry.get("title"), 60)
    queries = [str(q).strip() for q in (entry.get("queries") or []) if str(q).strip()]
    last_res = _clip_line(entry.get("lastResult"), 200)
    last_note = _clip_line(entry.get("lastNote"), 140)

    parts: list[str] = []
    if title:
        parts.append(f"[주제] {title}")

    tail_part = ""
    if last_res:
        tail_part = f"[결과] {last_res}"
    elif last_note:
        tail_part = f"[메모] {last_note}"

    if queries:
        used_chars = sum(len(p) for p in parts) + len(tail_part) + 12
        q_budget = max(60, _MAX_ROOM_SUMMARY_CHARS - used_chars - 5)
        recent_qs = _clip_line("; ".join(queries[-3:]), q_budget)
        if recent_qs:
            parts.append(f"[질문] {recent_qs}")

    if tail_part:
        parts.append(tail_part)

    return _clip_line(" | ".join(parts), _MAX_ROOM_SUMMARY_CHARS)


def _sync_room_summary_unlocked(
    data: dict[str, Any],
    *,
    room_id: str,
    session_id: str | None = None,
    title: str | None = None,
    query: str | None = None,
    result_summary: str | None = None,
    note: str | None = None,
    task_id: str | None = None,
    status: str | None = None,
    increment_task_count: bool = False,
    custom_summary: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """잠금 내부에서 `data['room_summaries'][room_id]` 를 생성 또는 누적 갱신한다."""
    rid = str(room_id or "").strip()
    ts = float(now if now is not None else _now())
    summaries: dict[str, Any] = data.setdefault("room_summaries", {})
    existing = summaries.get(rid)
    if isinstance(existing, dict):
        entry = dict(existing)
        entry["queries"] = list(existing.get("queries") or [])
    else:
        entry = {
            "roomId": rid,
            "sessionId": "",
            "title": str(title or "").strip() or rid,
            "summary": "",
            "customSummary": "",
            "queries": [],
            "lastResult": "",
            "lastNote": "",
            "taskCount": 0,
            "lastTaskId": "",
            "lastStatus": "",
            "createdAt": ts,
            "updatedAt": ts,
        }

    clean_sid = str(session_id or "").strip()
    if clean_sid:
        entry["sessionId"] = clean_sid

    clean_title = str(title or "").strip()
    if clean_title and (clean_title != rid or not entry.get("title") or entry.get("title") == rid):
        entry["title"] = clean_title

    if query:
        q_digest = _extract_query_digest(query)
        if q_digest:
            qs = [str(q) for q in entry.get("queries", []) if str(q).strip() and str(q) != q_digest]
            qs.append(q_digest)
            entry["queries"] = qs[-_MAX_ROOM_QUERIES:]

    if result_summary is not None:
        r_digest = _clip_line(result_summary, _MAX_RESULT_DIGEST_CHARS)
        if r_digest:
            entry["lastResult"] = r_digest

    if note is not None:
        n_digest = _clip_line(note, _MAX_NOTE_DIGEST_CHARS)
        if n_digest:
            entry["lastNote"] = n_digest

    if task_id:
        entry["lastTaskId"] = str(task_id).strip()
    if status:
        entry["lastStatus"] = str(status).strip()
    if increment_task_count:
        entry["taskCount"] = int(entry.get("taskCount") or 0) + 1

    if custom_summary is not None:
        entry["customSummary"] = _clip_line(custom_summary, _MAX_ROOM_SUMMARY_CHARS)

    entry["updatedAt"] = ts
    entry["summary"] = _compose_room_summary_text(entry)
    summaries[rid] = entry
    deleted_rooms = data.get("deleted_rooms")
    if isinstance(deleted_rooms, list) and rid in deleted_rooms:
        data["deleted_rooms"] = [x for x in deleted_rooms if x != rid]
    return dict(entry)


def _is_deleted_task(task: dict[str, Any]) -> bool:
    if task.get("status") != "closed":
        return False
    for n in task.get("notes") or []:
        if isinstance(n, dict) and "대화방 삭제" in str(n.get("text") or ""):
            return True
    return False


def _backfill_room_summaries_unlocked(data: dict[str, Any]) -> None:
    """기존 `tasks` 원장에는 있으나 `room_summaries` 가 비어 있는 채팅방을 자동 역산·보충한다."""
    tasks = data.get("tasks") or {}
    summaries = data.setdefault("room_summaries", {})
    deleted_set = {str(x).strip() for x in (data.get("deleted_rooms") or []) if str(x).strip()}
    deleted_set.update(
        str(t.get("roomId") or "").strip()
        for t in tasks.values()
        if isinstance(t, dict) and _is_deleted_task(t)
    )
    missing_tasks = [
        t for t in tasks.values()
        if isinstance(t, dict)
        and str(t.get("roomId") or "").strip() not in ("", _CORE_ROOM_ID)
        and str(t.get("roomId") or "").strip() not in summaries
        and str(t.get("roomId") or "").strip() not in deleted_set
    ]
    if not missing_tasks:
        return
    missing_tasks.sort(key=lambda t: float(t.get("createdAt") or 0))
    for t in missing_tasks:
        rid = str(t.get("roomId") or "").strip()
        notes = t.get("notes") or []
        last_note = notes[-1].get("text") if notes and isinstance(notes[-1], dict) else None
        res_preview = t.get("resultPreview") or (f"오류: {t['error']}" if t.get("error") else None)
        _sync_room_summary_unlocked(
            data,
            room_id=rid,
            session_id=t.get("sessionId"),
            title=t.get("title"),
            query=t.get("instruction"),
            result_summary=res_preview,
            note=last_note,
            task_id=t.get("id"),
            status=t.get("status"),
            increment_task_count=True,
            now=float(t.get("updatedAt") or t.get("createdAt") or _now()),
        )


def _load() -> dict[str, Any]:
    data = read_json(MASTER_TASKS_FILE, {}, expect_type=dict)
    if not isinstance(data.get("tasks"), dict):
        data["tasks"] = {}
    if not isinstance(data.get("patrol"), dict):
        data["patrol"] = {}
    if not isinstance(data.get("room_summaries"), dict):
        data["room_summaries"] = {}
    if not isinstance(data.get("deleted_rooms"), list):
        data["deleted_rooms"] = []
    _backfill_room_summaries_unlocked(data)
    return data


def _save(data: dict[str, Any]) -> None:
    _prune(data)
    write_json_atomic(MASTER_TASKS_FILE, data)


def _prune(data: dict[str, Any]) -> None:
    tasks: dict = data["tasks"]
    terminal = sorted(
        (t for t in tasks.values() if t.get("status") in TERMINAL_STATUSES),
        key=lambda t: float(t.get("updatedAt") or 0),
        reverse=True,
    )
    for stale in terminal[MAX_TERMINAL_KEEP:]:
        tasks.pop(stale.get("id"), None)

    summaries: dict = data.get("room_summaries") or {}
    if len(summaries) > MAX_ROOM_SUMMARIES_KEEP:
        ordered = sorted(
            summaries.items(),
            key=lambda kv: float((kv[1] or {}).get("updatedAt") or 0),
            reverse=True,
        )
        data["room_summaries"] = dict(ordered[:MAX_ROOM_SUMMARIES_KEEP])

    deleted_rooms = data.get("deleted_rooms")
    if isinstance(deleted_rooms, list) and len(deleted_rooms) > MAX_ROOM_SUMMARIES_KEEP:
        data["deleted_rooms"] = deleted_rooms[-MAX_ROOM_SUMMARIES_KEEP:]


def needs_attention(task: dict) -> bool:
    """마스터 LLM 개입/복구가 필요한 작업인지 판별한다.

    - reviewed=True 인 작업은 제외한다.
    - 사용자가 명시적으로 중단(cancelled/user_cancelled/user_confirmed)한 작업은 어떤 경우에도 제외한다.
    - failed / stalled / lost 상태는 비정상·미응답이므로 항상 검토가 필요하다.
    - done 상태는 비동기 위임인데 아직 마스터에게 보고(reportedAt)되지 않은 경우에만 검토가 필요하다
      (이미 보고 완료되었거나 동기 호출 wait=True 인 정상 완료 작업은 제외).
    """
    if task.get("reviewed"):
        return False
    if is_task_aborted(task):
        return False
    status = task.get("status")
    if status in ("failed", "stalled", "lost"):
        return True
    if status == "done":
        return not bool(task.get("reportedAt") or task.get("wait"))
    return False


def create_task(
    *,
    room_id: str,
    instruction: str,
    session_id: str | None = None,
    title: str = "",
    origin: str = "dispatch",
    wait: bool = False,
    task_id: str | None = None,
) -> dict:
    """새 위임 작업을 원장에 등록하고, 하위 채팅 요약본(`room_summaries`)을 함께 갱신한다."""
    now = _now()
    resolved_id = str(task_id or "").strip() or f"mt-{uuid.uuid4().hex[:8]}"
    task = {
        "id": resolved_id,
        "roomId": room_id,
        "sessionId": session_id,
        "title": title or room_id,
        "instruction": instruction,
        "origin": origin,
        "wait": bool(wait),
        "status": "queued",
        "createdAt": now,
        "updatedAt": now,
        "startedAt": None,
        "finishedAt": None,
        "lastActivityAt": now,
        "feed": "",
        "stepCount": 0,
        "resultPreview": "",
        "error": "",
        "reviewed": False,
        "reviewedAt": None,
        "user_cancelled": False,
        "user_confirmed": False,
        "notes": [],
    }
    with _lock:
        data = _load()
        data["tasks"][task["id"]] = task
        if str(room_id or "").strip() not in ("", _CORE_ROOM_ID):
            _sync_room_summary_unlocked(
                data,
                room_id=room_id,
                session_id=session_id,
                title=title or room_id,
                query=instruction,
                task_id=task["id"],
                status="queued",
                increment_task_count=True,
                now=now,
            )
        _save(data)
    return dict(task)


def get_task(task_id: str) -> dict | None:
    task = _load()["tasks"].get(str(task_id or "").strip())
    return dict(task) if task else None


def update_task(task_id: str, *, note: str | None = None, **fields: Any) -> dict | None:
    """작업 레코드 필드를 갱신하고, 완료·오류·메모 변경 시 채팅방 요약본(`room_summaries`)도 동기화한다."""
    tid = str(task_id or "").strip()
    now = _now()
    with _lock:
        data = _load()
        task = data["tasks"].get(tid)
        if task is None:
            return None
        new_status = fields.get("status")
        if new_status and new_status not in ALL_STATUSES:
            raise ValueError(f"알 수 없는 작업 상태: {new_status}")

        # 사용자가 이미 명시적으로 중단한 작업은 force=True 가 아니면
        # active(queued/running/stalled) 상태로 부활시키거나 덮어쓰지 못하도록 엄격히 차단한다.
        if is_task_aborted(task) and not fields.get("force", False):
            if new_status and new_status in ACTIVE_STATUSES:
                fields.pop("status", None)
                new_status = task.get("status")

        # 작업이 cancelled 로 변경되거나 명시적 취소 플래그가 들어온 경우:
        # user_cancelled, user_confirmed, reviewed 를 True 로 강제 확정하여
        # 상위 에이전트 감독/재시도 루프에서 영구 배제한다.
        if (
            new_status == "cancelled"
            or fields.get("user_cancelled")
            or fields.get("user_confirmed")
        ):
            fields["status"] = "cancelled"
            fields["user_cancelled"] = True
            fields["user_confirmed"] = True
            fields["reviewed"] = True
            if "reviewedAt" not in fields:
                fields["reviewedAt"] = now

        if new_status and new_status != task.get("status") and "reviewed" not in fields:
            # 새 결과(완료/실패/정체)가 나오면 이전 검토 완료 표시는 무효다.
            # 단, 이미 보고(reportedAt)되었거나 동기 대기(wait=True)로 결과를 전달한 done 작업은 자동 검토 완료.
            already_delivered = new_status == "done" and bool(
                fields.get("reportedAt") or task.get("reportedAt") or fields.get("wait") or task.get("wait")
            )
            fields["reviewed"] = already_delivered
            fields["reviewedAt"] = _now() if already_delivered else None
        task.update(fields)
        if note:
            notes = list(task.get("notes") or [])
            notes.append({"at": now, "text": str(note)[:500]})
            task["notes"] = notes[-MAX_NOTES:]
        task["updatedAt"] = now

        rid = str(task.get("roomId") or "").strip()
        if rid and rid != _CORE_ROOM_ID and (
            note or any(k in fields for k in ("resultPreview", "error", "status", "sessionId", "title"))
        ):
            res_summary: str | None = None
            if fields.get("resultPreview"):
                res_summary = str(fields["resultPreview"])
            elif fields.get("error"):
                res_summary = f"오류: {fields['error']}"
            _sync_room_summary_unlocked(
                data,
                room_id=rid,
                session_id=fields.get("sessionId") or task.get("sessionId"),
                title=fields.get("title") or task.get("title"),
                result_summary=res_summary,
                note=note,
                task_id=tid,
                status=new_status or task.get("status"),
                now=now,
            )

        _save(data)
        return dict(task)


def claim_report(task_id: str) -> bool:
    """작업 결과를 마스터에게 보고할 권리를 원자적으로 선점한다(중복 보고 방지).

    처음 호출한 쪽만 True. 서버 완료 훅 · 감독 루프 안전망 · 클라이언트 감시자가 경쟁해도 한 번만 보고된다.
    정상 완료(`done`)된 작업이 보고를 선점하면 자동 검토 완료(`reviewed=True`)도 함께 기록한다.
    """
    tid = str(task_id or "").strip()
    now = _now()
    with _lock:
        data = _load()
        task = data["tasks"].get(tid)
        if task is None or task.get("reportedAt"):
            return False
        task["reportedAt"] = now
        if task.get("status") == "done":
            task["reviewed"] = True
            task["reviewedAt"] = now
        _save(data)
        return True


def release_report(task_id: str) -> None:
    """보고 적재에 실패했을 때 선점을 되돌려 다음 기회에 재시도하게 한다."""
    tid = str(task_id or "").strip()
    with _lock:
        data = _load()
        task = data["tasks"].get(tid)
        if task is not None and task.get("reportedAt"):
            task["reportedAt"] = None
            if task.get("status") == "done":
                task["reviewed"] = False
                task["reviewedAt"] = None
            _save(data)


def unreported_async_tasks(*, now: float, max_age: float, min_age: float = 20.0) -> list[dict]:
    """종결됐지만 마스터에게 아직 보고되지 않은 비동기 위임 작업(안전망 대상)."""
    out = []
    for t in _load()["tasks"].values():
        if is_task_aborted(t) or t.get("status") == "cancelled" or t.get("reviewed"):
            continue
        if t.get("status") not in ("done", "failed", "lost") or t.get("wait") or t.get("reportedAt"):
            continue
        if t.get("origin") != "dispatch":
            continue
        fin = float(t.get("finishedAt") or t.get("updatedAt") or 0)
        if min_age <= now - fin <= max_age:
            out.append(dict(t))
    out.sort(key=lambda t: float(t.get("finishedAt") or 0))
    return out


def mark_reviewed(task_ids: list[str], *, expected_status: dict[str, str] | None = None) -> int:
    """점검이 끝난 작업을 검토 완료로 표시한다.

    expected_status 를 주면 점검 시작 시점과 상태가 같은 작업만 표시한다
    (점검 도중 상태가 바뀐 작업은 다음 점검에서 다시 보도록 남긴다).
    """
    marked = 0
    now = _now()
    with _lock:
        data = _load()
        for tid in task_ids:
            task = data["tasks"].get(tid)
            if task is None:
                continue
            if expected_status and task.get("status") != expected_status.get(tid):
                continue
            task["reviewed"] = True
            task["reviewedAt"] = now
            task["updatedAt"] = now
            marked += 1
        if marked:
            _save(data)
    return marked


def mark_room_reviewed(room_id: str) -> int:
    """특정 대화방(room_id)에 속한 모든 위임 작업을 검토 완료(reviewed=True) 처리한다."""
    if not room_id:
        return 0
    rid = str(room_id).strip()
    marked = 0
    now = _now()
    with _lock:
        data = _load()
        for task in data["tasks"].values():
            if str(task.get("roomId") or "").strip() == rid:
                if not task.get("reviewed"):
                    task["reviewed"] = True
                    task["reviewedAt"] = now
                    task["updatedAt"] = now
                    marked += 1
        if marked:
            _save(data)
    return marked


def handle_rooms_deleted(room_ids: list[str]) -> int:
    """대화방이 삭제되었을 때 해당 방의 작업을 종결(closed)하고 요약본(`room_summaries`)도 정리한다."""
    if not room_ids:
        return 0
    room_set = set(str(rid).strip() for rid in room_ids if str(rid).strip())
    updated = 0
    removed_summaries = False
    now = _now()
    with _lock:
        data = _load()
        for task in data["tasks"].values():
            if str(task.get("roomId") or "").strip() in room_set:
                if task.get("status") not in ("closed", "cancelled"):
                    task["status"] = "closed"
                    task["reviewed"] = True
                    task["notes"] = (task.get("notes") or [])[-MAX_NOTES + 1:] + [{
                        "at": now,
                        "text": "대화방 삭제로 인한 작업 자동 종결",
                    }]
                    task["updatedAt"] = now
                    updated += 1
        summaries = data.get("room_summaries") or {}
        deleted_rooms = list(data.get("deleted_rooms") or [])
        deleted_changed = False
        for rid in room_set:
            if rid in summaries:
                summaries.pop(rid, None)
                removed_summaries = True
            if rid not in deleted_rooms:
                deleted_rooms.append(rid)
                deleted_changed = True
        if deleted_changed:
            data["deleted_rooms"] = deleted_rooms
        if updated or removed_summaries or deleted_changed:
            _save(data)
    return updated


def _clone_room_summary(raw: dict[str, Any]) -> dict[str, Any]:
    out = dict(raw)
    out["queries"] = list(raw.get("queries") or [])
    return out


def get_room_summary(room_id: str) -> dict | None:
    """특정 하위 채팅방의 관리 중인 요약본 레코드를 반환한다."""
    rid = str(room_id or "").strip()
    if not rid:
        return None
    raw = _load()["room_summaries"].get(rid)
    return _clone_room_summary(raw) if isinstance(raw, dict) else None


def list_room_summaries(*, limit: int = 100) -> list[dict]:
    """모든 하위 채팅 요약본을 최근 갱신순으로 반환한다."""
    items = [
        _clone_room_summary(v)
        for v in _load()["room_summaries"].values()
        if isinstance(v, dict)
    ]
    items.sort(key=lambda item: float(item.get("updatedAt") or 0), reverse=True)
    return items[: max(1, int(limit))]


def list_room_summaries_map(*, limit: int = 300) -> dict[str, dict]:
    """채팅방 ID → 요약본 레코드 딕셔너리 맵을 반환한다."""
    return {
        str(item.get("roomId") or "").strip(): item
        for item in list_room_summaries(limit=limit)
        if str(item.get("roomId") or "").strip()
    }


def upsert_room_summary(
    room_id: str,
    *,
    title: str | None = None,
    session_id: str | None = None,
    summary: str | None = None,
    query: str | None = None,
    result_summary: str | None = None,
    note: str | None = None,
    status: str | None = None,
) -> dict:
    """특정 채팅방의 요약본(`room_summaries`)을 명시적으로 생성 또는 갱신한다."""
    rid = str(room_id or "").strip()
    if not rid:
        raise ValueError("room_id 가 비어 있습니다.")
    with _lock:
        data = _load()
        updated = _sync_room_summary_unlocked(
            data,
            room_id=rid,
            session_id=session_id,
            title=title,
            query=query,
            result_summary=result_summary,
            note=note,
            status=status,
            custom_summary=summary,
        )
        _save(data)
        return _clone_room_summary(updated)


def list_tasks(*, include_closed: bool = True, limit: int = 100) -> list[dict]:
    """작업 목록을 최신 생성순으로 반환한다."""
    tasks = [
        dict(t) for t in _load()["tasks"].values()
        if include_closed or t.get("status") not in ("closed", "cancelled")
    ]
    tasks.sort(key=lambda t: float(t.get("createdAt") or 0), reverse=True)
    return tasks[: max(1, int(limit))]


def get_patrol_state() -> dict:
    return dict(_load()["patrol"])


def update_patrol_state(**fields: Any) -> dict:
    with _lock:
        data = _load()
        data["patrol"].update(fields)
        _save(data)
        return dict(data["patrol"])


def clear_terminal_tasks() -> int:
    """종결/완료/실패/취소 상태의 위임 작업 기록을 원장에서 정리한다(진행 중 작업은 유지)."""
    removed = 0
    with _lock:
        data = _load()
        tasks = data["tasks"]
        for tid in list(tasks.keys()):
            if tasks[tid].get("status") in TERMINAL_STATUSES:
                tasks.pop(tid, None)
                removed += 1
        if removed:
            _save(data)
    return removed
