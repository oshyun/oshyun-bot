"""master_supervisor — 마스터 에이전트 감독 루프 & 마스터-슬레이브 관계도.

마스터 에이전트의 핵심 역할은 평범한 대화보다 **하위 채팅(AGY 세션)을 유지보수**하는 것이다.
이 모듈은 그 구조를 세 층으로 나눈다.

1. 작업 원장(`master_tasks`) — 마스터가 위임한 작업의 영속 기록(재시작에도 유지).
2. 감독 루프(`MasterSupervisor.run_forever`) — LLM 없이 몇 초마다 돈다.
   - 원장과 실시간 실행 상태를 대조(reconcile)해 진행/정체/유실을 판정한다.
   - 관계도(graph)가 바뀌면 WebSocket(`master_graph`)으로 즉시 브로드캐스트한다.
   - 검토가 필요한 작업이 있고 주기가 됐고 **마스터가 한가할 때만** 정기 점검(patrol) 턴을
     마스터 대화방(`__core__`) 대기열에 넣는다. → 이때만 LLM 비용이 든다.
3. 사용자 우선 — 사용자 요청이 마스터에 들어오면 진행 중인 점검 턴을 즉시 양보(preempt)시키고
   사용자 요청을 먼저 처리한다(web_server.enqueue_room_turn 쪽 훅). 양보된 점검은 잠시 후 재시도한다.

웹 서버 내부 상태(대기열·실행 스냅샷·DB)는 `host` 객체를 통해서만 접근한다(순환 import 방지·테스트 용이).
host 가 제공해야 하는 메서드:
    snapshot() -> {"rooms": [...], "liveRuns": [...]}     # 대화방 인덱스, 실행/대기 중 스냅샷
    core_busy() -> bool                                     # 마스터 대화방이 실행 중이거나 대기열이 있음
    async enqueue_patrol(prompt) -> asyncio.Future          # 마스터 대화방에 점검 턴 적재
    async broadcast(message: dict) -> None                  # 모든 WS 클라이언트에 이벤트 전송
    latest_reply(room_id, since_ts) -> str | None           # 해당 방의 since 이후 마지막 어시스턴트 답변
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from typing import Any

import master_memory
import master_tasks
from bot_logger import logger

CORE_ROOM_ID = "__core__"
MASTER_PATROL_CHANNEL = "master_patrol"


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)).strip() or default)
    except ValueError:
        return default


# 정기 점검 주기(초). 0 이면 자동 점검을 끈다(수동 '하위 에이전트 점검'은 가능).
PATROL_INTERVAL_SECONDS = _env_int("MASTER_PATROL_INTERVAL", 0)
# 실행 중 작업이 이 시간(초) 이상 진행 변화가 없으면 '정체(stalled)'로 판정.
STALL_SECONDS = _env_int("MASTER_STALL_SECONDS", 900)
# 감독 루프 틱(초). LLM 을 부르지 않는 가벼운 대조·브로드캐스트 주기.
TICK_SECONDS = max(1, _env_int("MASTER_TICK_SECONDS", 5))
# 실시간 실행 흔적이 사라진 뒤 결과 조회(유실 판정)까지 기다리는 유예(초).
LOST_GRACE_SECONDS = _env_int("MASTER_LOST_GRACE_SECONDS", 90)
# 서버 완료 훅이 놓친(재시작·예외 등) 비동기 작업을 마스터에게 뒤늦게라도 보고하는 안전망 범위(초).
REPORT_MAX_AGE_SECONDS = 6 * 3600
# 사용자 요청에 양보한 점검을 다시 시도하기까지의 대기(초).
PREEMPT_RETRY_SECONDS = 60
# 관계도에 '최근 대화'로 함께 보여줄 범위.
RECENT_ROOM_SECONDS = 3600
RECENT_ROOM_LIMIT = 8
# 종결된 위임 작업을 관계도에 남겨두는 시간.
TASK_GRAPH_RETENTION_SECONDS = 24 * 3600

STATUS_LABELS = {
    "queued": "대기",
    "running": "실행 중",
    "stalled": "정체",
    "done": "완료",
    "failed": "실패",
    "lost": "유실",
    "cancelled": "취소",
    "closed": "종결",
}


def _norm_ts(val: Any) -> float:
    """초/밀리초/ISO 문자열 혼재 타임스탬프를 초 단위 float 로 정규화한다."""
    if val is None or val == "":
        return 0.0
    if isinstance(val, (int, float)):
        f = float(val)
        return f / 1000.0 if f > 1e12 else f
    try:
        f = float(str(val))
        return f / 1000.0 if f > 1e12 else f
    except ValueError:
        pass
    try:
        from datetime import datetime, timezone

        s = str(val).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


def _clip(text: Any, n: int) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


# ── 1. 원장 ↔ 실시간 상태 대조 ─────────────────────────────────────────────

def _live_index(live_runs: list[dict]) -> dict[str, dict]:
    idx: dict[str, dict] = {}
    for run in live_runs:
        for key in (run.get("roomId"), run.get("sessionId")):
            if key:
                idx.setdefault(str(key), run)
    return idx


def reconcile_tasks(
    tasks: list[dict],
    live_runs: list[dict],
    *,
    now: float,
    stall_seconds: int = STALL_SECONDS,
    latest_reply=None,
) -> list[tuple[str, dict]]:
    """진행 중 작업을 실시간 상태와 대조해 (task_id, 변경필드) 목록을 돌려준다(부작용 없음).

    - 실행 흔적이 있으면 피드/스텝 수 변화로 마지막 활동 시각을 갱신하고, 오래 변화가 없으면 정체.
    - 실행 흔적이 없으면(재시작·취소 등으로 워커 훅이 못 남긴 경우) 유예 후 DB 답변으로 완료/유실 판정.
    """
    live = _live_index(live_runs)
    changes: list[tuple[str, dict]] = []
    for task in tasks:
        status = task.get("status")
        if status not in master_tasks.ACTIVE_STATUSES:
            continue
        tid = task["id"]
        run = live.get(str(task.get("roomId") or "")) or (
            live.get(str(task.get("sessionId"))) if task.get("sessionId") else None
        )
        if run is not None:
            upd: dict[str, Any] = {}
            feed = str(run.get("feed") or "")
            step_count = len(run.get("steps") or [])
            progressed = feed != (task.get("feed") or "") or step_count != int(task.get("stepCount") or 0)
            if progressed:
                upd.update(feed=feed, stepCount=step_count, lastActivityAt=now)
            last_act = now if progressed else float(task.get("lastActivityAt") or task.get("updatedAt") or now)
            if status == "stalled" and progressed:
                upd["status"] = "running"
            elif status == "running" and stall_seconds > 0 and now - last_act >= stall_seconds:
                upd["status"] = "stalled"
            if upd:
                changes.append((tid, upd))
            continue

        # 실행 흔적 없음 — 워커 완료 훅이 곧 기록할 수 있으므로 유예를 둔다.
        if now - float(task.get("updatedAt") or now) < LOST_GRACE_SECONDS:
            continue
        reply = latest_reply(task.get("roomId"), float(task.get("createdAt") or 0)) if latest_reply else None
        if reply:
            if "⚠️ 서버 재시작으로 답변이 중단되었습니다." in reply or "⚠️ 작업이 중단되었습니다." in reply:
                changes.append((tid, {
                    "status": "failed",
                    "finishedAt": now,
                    "error": reply[:300],
                }))
            else:
                changes.append((tid, {
                    "status": "done",
                    "finishedAt": now,
                    "resultPreview": _clip(reply, 400),
                }))
        else:
            changes.append((tid, {
                "status": "lost",
                "finishedAt": now,
                "error": "실행 추적이 끊겼고 결과 답변을 찾지 못했습니다(서버 재시작/취소 추정).",
            }))
    return changes


# ── 2. 정기 점검 프롬프트 ─────────────────────────────────────────────────

def build_patrol_prompt(
    targets: list[dict],
    live_runs: list[dict] | None = None,
    *,
    now: float,
    reason: str,
) -> str:
    """미응답·비정상 위임 작업이 감지됐을 때만 마스터 에이전트에게 보낼 압축 복구 지시문을 만든다."""
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(now))
    lines = [
        f"[정기 점검 · 자동] {reason} ({stamp})",
        f"■ 검토가 필요한 위임 작업 {len(targets)}건 (미보고·정체·실패·유실)",
    ]
    for i, t in enumerate(targets, start=1):
        label = STATUS_LABELS.get(t.get("status"), t.get("status"))
        detail = t.get("error") or t.get("resultPreview") or t.get("feed") or ""
        lines.append(
            f"{i}. [{t['id']}] {label} · \"{_clip(t.get('title'), 30)}\" (roomId: {t.get('roomId')})"
        )
        lines.append(f"   지시: {_clip(t.get('instruction'), 120)}")
        if detail:
            lines.append(f"   상태/사유: {_clip(detail, 160)}")

    lines += [
        "",
        "■ 조치 지침",
        "1. 원인을 확인할 건만 `session-ctl read <roomId> --limit 4` 로 대화 결과를 확인하라.",
        "2. 재시도가 필요하면 원래 지시 범위 내에서 `session-ctl send <roomId> \"...\" --async` 로 재지시하고, 종결할 건은 `session-ctl task <taskId> --note \"<판정>\" --close` 로 닫아라.",
        "3. 사용자에게 보여줄 조치 요약을 2~4줄로 작성하라.",
    ]
    return "\n".join(lines)


# ── 3. 마스터-슬레이브 관계도 ─────────────────────────────────────────────

_CHILD_STATUS_RANK = {
    "running": 0, "queued": 1, "stalled": 2, "failed": 3, "lost": 4,
    "attention": 5, "done": 6, "idle": 7,
}


def build_graph(
    *,
    rooms: list[dict],
    live_runs: list[dict],
    tasks: list[dict],
    patrol: dict,
    now: float,
) -> dict:
    """마스터(__core__)를 중심으로 하위 채팅 노드와 연결선(위임/관찰)을 구성한다.

    포함 기준: 지금 실행·대기 중인 채팅 + 원장에 활성/검토대기/최근 작업이 있는 채팅 +
    최근 1시간 내 갱신된 채팅(recentOnly=True, UI 에서 숨김 가능).
    """
    room_meta = {str(r.get("id") or r.get("roomId") or ""): r for r in rooms}
    live_by_room = {str(r.get("roomId") or ""): r for r in live_runs if r.get("roomId")}
    core_run = live_by_room.get(CORE_ROOM_ID)

    tasks_by_room: dict[str, list[dict]] = {}
    for t in tasks:
        rid = str(t.get("roomId") or "")
        if not rid:
            continue
        # 이미 삭제된 대화방(room_meta 에 없음)은 실행 중이 아닌 한 관계도에서 제외
        if rid not in room_meta and not (live_by_room.get(rid) and live_by_room[rid].get("isRunning")):
            continue
        keep = (
            t.get("status") in master_tasks.ACTIVE_STATUSES
            or master_tasks.needs_attention(t)
            or now - float(t.get("updatedAt") or 0) <= TASK_GRAPH_RETENTION_SECONDS
        )
        if keep and t.get("status") != "closed" or (keep and now - float(t.get("updatedAt") or 0) <= 600):
            tasks_by_room.setdefault(rid, []).append(t)

    child_ids: list[str] = []
    for rid in list(live_by_room.keys()) + list(tasks_by_room.keys()):
        if rid and rid != CORE_ROOM_ID and rid not in child_ids:
            if rid not in room_meta and not (live_by_room.get(rid) and live_by_room[rid].get("isRunning")):
                continue
            child_ids.append(rid)
    recent_ids: list[str] = []
    for r in sorted(rooms, key=lambda x: _norm_ts(x.get("updatedAt")), reverse=True):
        rid = str(r.get("id") or r.get("roomId") or "")
        if not rid or rid == CORE_ROOM_ID or rid in child_ids:
            continue
        if now - _norm_ts(r.get("updatedAt")) > RECENT_ROOM_SECONDS:
            break
        recent_ids.append(rid)
        if len(recent_ids) >= RECENT_ROOM_LIMIT:
            break

    try:
        room_summaries_map = master_tasks.list_room_summaries_map(limit=200)
    except Exception:
        room_summaries_map = {}

    nodes: list[dict] = []
    edges: list[dict] = []
    for rid in child_ids + recent_ids:
        meta = room_meta.get(rid, {})
        run = live_by_room.get(rid)
        rsum = room_summaries_map.get(rid) or {}
        room_tasks = sorted(tasks_by_room.get(rid, []), key=lambda t: float(t.get("createdAt") or 0), reverse=True)
        attention = any(master_tasks.needs_attention(t) for t in room_tasks)
        active_task = next((t for t in room_tasks if t.get("status") in master_tasks.ACTIVE_STATUSES), None)

        if run is not None and run.get("isRunning") and run.get("feed") != "대기열 처리 중…" or (run and run.get("steps")):
            status = "running"
        elif run is not None:
            status = "queued"
        elif active_task is not None:
            status = active_task["status"]
        elif any(t.get("status") in ("failed", "lost") and not t.get("reviewed") for t in room_tasks):
            status = next(t["status"] for t in room_tasks if t.get("status") in ("failed", "lost") and not t.get("reviewed"))
        elif attention:
            status = "attention"
        else:
            status = "idle"
        if active_task is not None and active_task.get("status") == "stalled":
            status = "stalled"

        dispatched = bool(room_tasks) or (run is not None and run.get("channel") == "orchestrator")
        node = {
            "id": rid,
            "kind": "chat",
            "title": meta.get("title") or (room_tasks[0].get("title") if room_tasks else "") or "대화",
            "sessionId": meta.get("sessionId") or (run or {}).get("sessionId") or (
                room_tasks[0].get("sessionId") if room_tasks else None
            ),
            "status": status,
            "statusLabel": "검토 필요" if status == "attention" else STATUS_LABELS.get(status, "대기 없음" if status == "idle" else status),
            "feed": (run or {}).get("feed") or (active_task or {}).get("feed") or "",
            "startedAt": (run or {}).get("startedAt"),
            "queueCount": int((run or {}).get("queueCount") or 0),
            "channel": (run or {}).get("channel") or "",
            "userText": _clip((run or {}).get("userText"), 120),
            "preview": _clip(meta.get("preview"), 120),
            "summary": rsum.get("summary") or _clip(meta.get("preview"), 160),
            "queries": list(rsum.get("queries") or []),
            "updatedAt": _norm_ts(meta.get("updatedAt")),
            "attention": attention or status in ("stalled", "failed", "lost"),
            "recentOnly": rid in recent_ids,
            "archived": bool(meta.get("isArchived")),
            "tasks": [
                {
                    "id": t["id"],
                    "status": t.get("status"),
                    "statusLabel": STATUS_LABELS.get(t.get("status"), t.get("status")),
                    "instruction": _clip(t.get("instruction"), 140),
                    "resultPreview": _clip(t.get("resultPreview") or t.get("error"), 160),
                    "reviewed": bool(t.get("reviewed")),
                    "createdAt": t.get("createdAt"),
                    "origin": t.get("origin"),
                    "lastNote": ((t.get("notes") or [{}])[-1] or {}).get("text", ""),
                }
                for t in room_tasks[:5]
            ],
        }
        nodes.append(node)
        edges.append({
            "from": CORE_ROOM_ID,
            "to": rid,
            "type": "dispatch" if dispatched else "observe",
            "active": status in ("running", "queued"),
            "attention": node["attention"],
        })

    nodes.sort(key=lambda n: (n["recentOnly"], _CHILD_STATUS_RANK.get(n["status"], 9), -float(n["updatedAt"] or 0)))

    core_channel = (core_run or {}).get("channel") or ""
    core_status = "idle"
    if core_run is not None:
        core_status = "patrolling" if core_channel == MASTER_PATROL_CHANNEL else "running"
    interval = int(patrol.get("intervalSeconds") or PATROL_INTERVAL_SECONDS)
    last_run = float(patrol.get("lastRunAt") or 0)
    master = {
        "id": CORE_ROOM_ID,
        "kind": "master",
        "title": "마스터 에이전트",
        "status": core_status,
        "feed": (core_run or {}).get("feed") or "",
        "startedAt": (core_run or {}).get("startedAt"),
        "queueCount": int((core_run or {}).get("queueCount") or 0),
        "channel": core_channel,
    }
    active_count = sum(1 for t in tasks if t.get("status") in master_tasks.ACTIVE_STATUSES)
    attention_count = sum(1 for t in tasks if master_tasks.needs_attention(t))
    try:
        memory_snapshot = master_memory.get_memory_snapshot(limit_per_tier=25)
    except Exception:
        memory_snapshot = {}
    return {
        "master": master,
        "nodes": nodes,
        "edges": edges,
        "patrol": {
            "enabled": bool(patrol.get("enabled", PATROL_INTERVAL_SECONDS > 0)),
            "intervalSeconds": interval,
            "status": patrol.get("status") or "idle",
            "lastRunAt": last_run or None,
            "nextRunAt": (last_run + interval) if last_run else None,
            "lastReason": patrol.get("lastReason") or "",
            "lastResultPreview": patrol.get("lastResultPreview") or "",
            "runs": int(patrol.get("runs") or 0),
            "preempted": int(patrol.get("preempted") or 0),
        },
        "summary": {
            "children": sum(1 for n in nodes if not n["recentOnly"] and not n.get("archived")),
            "running": sum(1 for n in nodes if n["status"] in ("running", "queued")),
            "attention": attention_count,
            "activeTasks": active_count,
            "archived": sum(1 for n in nodes if n.get("archived")),
        },
        "memory": memory_snapshot,
    }


def graph_signature(graph: dict) -> str:
    """관계도 변경 감지용 해시(경과 시간처럼 매초 바뀌는 값은 포함하지 않는다)."""
    raw = json.dumps(graph, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


# ── 4. 감독 루프 ─────────────────────────────────────────────────────────

class MasterSupervisor:
    """주기적 하위 작업 감시 + 관계도 브로드캐스트 + 정기 점검 턴 스케줄링."""

    def __init__(
        self,
        host: Any,
        *,
        tick_seconds: int = TICK_SECONDS,
        interval_seconds: int = PATROL_INTERVAL_SECONDS,
        stall_seconds: int = STALL_SECONDS,
    ) -> None:
        self.host = host
        self.tick_seconds = tick_seconds
        self.interval_seconds = interval_seconds
        self.stall_seconds = stall_seconds
        self._wake = asyncio.Event()
        self._patrol_task: asyncio.Task | None = None
        self._last_signature = ""
        self._last_graph: dict | None = None

    # 외부(웹 서버)에서 실행 상태가 바뀔 때 호출 — 다음 틱을 기다리지 않고 즉시 반영.
    def poke(self) -> None:
        self._wake.set()

    @property
    def patrol_running(self) -> bool:
        return self._patrol_task is not None and not self._patrol_task.done()

    def _patrol_enabled(self, patrol: dict) -> bool:
        return bool(patrol.get("enabled", self.interval_seconds > 0)) and self.interval_seconds > 0

    def current_graph(self) -> dict:
        snap = self.host.snapshot()
        patrol = master_tasks.get_patrol_state()
        patrol.setdefault("intervalSeconds", self.interval_seconds)
        patrol["enabled"] = self._patrol_enabled(patrol)
        return build_graph(
            rooms=snap.get("rooms") or [],
            live_runs=snap.get("liveRuns") or [],
            tasks=master_tasks.list_tasks(limit=500),
            patrol=patrol,
            now=time.time(),
        )

    def _reconcile_now(self, now: float, live_runs: list[dict]) -> int:
        """LLM 호출 없이 원장과 실시간 실행·DB 상태를 대조해 최신화하고, 이미 보고된 done 작업을 자동 검토 처리한다."""
        reconciled = 0
        latest_reply_fn = getattr(self.host, "latest_reply", None)
        for tid, upd in reconcile_tasks(
            master_tasks.list_tasks(include_closed=False, limit=500),
            live_runs,
            now=now,
            stall_seconds=self.stall_seconds,
            latest_reply=latest_reply_fn,
        ):
            master_tasks.update_task(tid, **upd)
            reconciled += 1
            if upd.get("status"):
                logger.info("[master] 작업 %s 상태 → %s", tid, upd["status"])

        auto_review_ids = [
            t["id"]
            for t in master_tasks.list_tasks(include_closed=False, limit=500)
            if t.get("status") == "done"
            and not t.get("reviewed")
            and (t.get("reportedAt") or t.get("wait"))
        ]
        if auto_review_ids:
            reconciled += master_tasks.mark_reviewed(
                auto_review_ids,
                expected_status={tid: "done" for tid in auto_review_ids},
            )
        return reconciled

    async def tick(self) -> None:
        now = time.time()
        snap = self.host.snapshot()
        live_runs = snap.get("liveRuns") or []

        self._reconcile_now(now, live_runs)

        # 안전망: 끝났는데 마스터에게 보고되지 않은 비동기 작업을 틱당 1건씩 보고(마스터 대기열 폭주 방지).
        notify_task = getattr(self.host, "notify_task", None)
        if notify_task is not None:
            pending = master_tasks.unreported_async_tasks(now=now, max_age=REPORT_MAX_AGE_SECONDS)
            if pending:
                logger.info("[master] 미보고 작업 %s 안전망 보고", pending[0]["id"])
                await notify_task(pending[0])

        patrol = master_tasks.get_patrol_state()
        if self._patrol_enabled(patrol) and not self.patrol_running:
            due = now - float(patrol.get("lastRunAt") or 0) >= self.interval_seconds
            targets = [t for t in master_tasks.list_tasks(include_closed=False, limit=500) if master_tasks.needs_attention(t)]
            if due and targets and not self.host.core_busy():
                await self.start_patrol(reason="정기 점검", targets=targets)

        await self.broadcast_graph_if_changed()

    async def broadcast_graph_if_changed(self, *, force: bool = False) -> dict:
        graph = self.current_graph()
        sig = graph_signature(graph)
        if force or sig != self._last_signature:
            self._last_signature = sig
            self._last_graph = graph
            await self.host.broadcast({"type": "master_graph", "graph": graph})
        return graph

    async def start_patrol(self, *, reason: str, targets: list[dict] | None = None, force: bool = False) -> dict:
        """백엔드에서 원장·실행 상태를 먼저 자동 대조하고, 미응답·비정상 작업이 있을 때만 마스터 LLM 점검 턴을 적재한다."""
        if self.patrol_running:
            return {"ok": False, "status": "already_running", "error": "이미 정기 점검이 진행 중입니다."}

        now = time.time()
        live_runs = self.host.snapshot().get("liveRuns") or []
        reconciled = 0
        if targets is None:
            reconciled = self._reconcile_now(now, live_runs)
            all_open = master_tasks.list_tasks(include_closed=False, limit=500)
            targets = [t for t in all_open if master_tasks.needs_attention(t)]
        else:
            all_open = master_tasks.list_tasks(include_closed=False, limit=500)

        active_count = sum(1 for t in all_open if t.get("status") in master_tasks.ACTIVE_STATUSES)

        if not targets:
            runs = int(master_tasks.get_patrol_state().get("runs") or 0) + 1
            summary_msg = f"자동 점검 완료: 이상 없음 (진행 중 {active_count}건, 미응답·오류 0건)"
            master_tasks.update_patrol_state(
                lastRunAt=now,
                lastFinishedAt=now,
                lastReason=reason,
                status="idle",
                runs=runs,
                targetIds=[],
                lastResultPreview=summary_msg,
            )
            await self.broadcast_graph_if_changed(force=True)
            logger.info("[master] %s (%s)", summary_msg, reason)
            return {
                "ok": True,
                "status": "clean",
                "llmTriggered": False,
                "targets": 0,
                "activeCount": active_count,
                "reconciledCount": reconciled,
                "message": summary_msg,
            }

        if self.host.core_busy() and not force:
            return {"ok": False, "status": "busy", "error": "마스터 에이전트가 사용자 요청을 처리 중입니다. 끝나면 점검합니다."}

        prompt = build_patrol_prompt(targets, live_runs, now=now, reason=reason)
        expected = {t["id"]: t.get("status") for t in targets}

        fut = await self.host.enqueue_patrol(prompt)
        runs = int(master_tasks.get_patrol_state().get("runs") or 0) + 1
        master_tasks.update_patrol_state(
            lastRunAt=now,
            lastReason=reason,
            status="running",
            runs=runs,
            targetIds=list(expected),
        )
        logger.info("[master] 복구 점검 시작 (%s, 이상 대상 %d건)", reason, len(targets))
        self._patrol_task = asyncio.create_task(self._await_patrol(fut, expected))
        self.poke()
        return {
            "ok": True,
            "status": "started",
            "llmTriggered": True,
            "targets": len(targets),
            "reconciledCount": reconciled,
            "message": f"미응답·비정상 작업 {len(targets)}건 감지 — 마스터 복구 점검을 시작합니다.",
        }

    async def _await_patrol(self, fut: asyncio.Future, expected: dict[str, str]) -> None:
        try:
            result = await fut
            reply = str((result or {}).get("reply") or "")
            ok = bool((result or {}).get("ok")) and not reply.startswith("⚠️")
            if ok:
                master_tasks.mark_reviewed(list(expected), expected_status=expected)
            master_tasks.update_patrol_state(
                status="idle" if ok else "error",
                lastFinishedAt=time.time(),
                lastResultPreview=_clip(reply, 300),
            )
        except asyncio.CancelledError:
            # 사용자 요청에 양보(preempt)했거나 사용자가 취소함 → 잠시 후 재시도되도록 lastRunAt 을 당긴다.
            st = master_tasks.get_patrol_state()
            master_tasks.update_patrol_state(
                status="preempted",
                preempted=int(st.get("preempted") or 0) + 1,
                lastRunAt=time.time() - self.interval_seconds + PREEMPT_RETRY_SECONDS,
            )
            logger.info("[master] 정기 점검이 사용자 요청에 양보되어 중단됨 — %d초 후 재시도", PREEMPT_RETRY_SECONDS)
        except Exception as exc:
            master_tasks.update_patrol_state(status="error", lastResultPreview=_clip(f"점검 오류: {exc}", 300))
            logger.warning("[master] 정기 점검 오류: %s", exc)
        finally:
            self.poke()

    async def run_forever(self) -> None:
        logger.info(
            "[master] 감독 루프 시작 (틱 %ss, 점검 주기 %ss, 정체 판정 %ss)",
            self.tick_seconds, self.interval_seconds, self.stall_seconds,
        )
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[master] 감독 루프 틱 오류: %s", exc)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.tick_seconds)
                # 연속 이벤트를 한 번에 반영하도록 짧게 디바운스
                await asyncio.sleep(0.3)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
