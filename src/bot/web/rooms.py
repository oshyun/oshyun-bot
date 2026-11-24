"""web_rooms — 대화방 목록(SSOT), 대화 히스토리, 세션 삭제, 방 이름/설정 변경 및 첨부파일 업로드 REST API 모듈.

순환 import 방향: rooms → runtime_state(agent_reporting 을 import 하지 않음).
agent_reporting → rooms 는 지연 import 로만 허용(방향 단일화).
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
from typing import Any
import uuid

from aiohttp import web

import bot.core.agent_configs as agent_configs
import bot.core.chat_db as chat_db
from bot.engine.agent_engine import clear_session, delete_session_files, kill_job
from bot.core.bot_logger import logger, vault_log
from bot.engine.engine_settings import get_coding_mode, get_effort, get_model
import bot.core.paths as paths
from bot.core.paths import ATTACHED_FILES_DIR, CHATS_DIR, STATE_DIR, child_default_workdir
from bot.web.runtime_state import _get_mod_attr, get_master_supervisor

HIDDEN_ROOMS_FILE = os.path.join(STATE_DIR, "hidden_chat_rooms.json")
CORE_ORCHESTRATOR_ROOM_ID = "__core__"
CORE_ORCHESTRATOR_TITLE = "마스터 에이전트"
EXPERT_GENERAL_ROOM_ID = "__expert_general__"
EXPERT_GENERAL_TITLE = "일반 대화"
EXPERT_VAULT_ROOM_ID = "__expert_vault__"
EXPERT_VAULT_TITLE = "세컨브레인 에이전트"
EXPERT_CODING_ROOM_ID = "__expert_coding__"
EXPERT_CODING_TITLE = "봇개발 에이전트"
ORCHESTRATOR_ROOM_IDS = (
    CORE_ORCHESTRATOR_ROOM_ID,
    EXPERT_GENERAL_ROOM_ID,
    EXPERT_VAULT_ROOM_ID,
    EXPERT_CODING_ROOM_ID,
)


def _all_orchestrator_room_ids() -> tuple[str, ...]:
    try:
        return agent_configs.get_orchestrator_room_ids()
    except Exception:
        return ORCHESTRATOR_ROOM_IDS


def _is_orchestrator_room_id(room_id: str | None) -> bool:
    if not room_id:
        return False
    clean = str(room_id).strip()
    return clean in ORCHESTRATOR_ROOM_IDS or clean in _all_orchestrator_room_ids()



def _get_hidden_file() -> str:
    return _get_mod_attr("HIDDEN_ROOMS_FILE", HIDDEN_ROOMS_FILE)


def _load_hidden_rooms() -> set[str]:
    """사용자가 삭제(숨김) 처리한 대화방/세션 ID 집합을 반환한다 (마스터 및 전문가 ID는 자동 자정 정화)."""
    hidden_file = _get_hidden_file()
    hidden: set[str] = set()
    if os.path.isfile(hidden_file):
        try:
            with open(hidden_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    hidden = set(str(x) for x in data if x)
        except Exception:
            pass

    # 마스터 및 전문가 에이전트(ORCHESTRATOR_ROOM_IDS)는 영구 숨김 불가 — 자정(Self-healing) 처리
    all_orch_ids = _all_orchestrator_room_ids()
    polluted = any(orch_id in hidden for orch_id in all_orch_ids)
    if polluted:
        for orch_id in all_orch_ids:
            hidden.discard(orch_id)
        try:
            tmp_path = f"{hidden_file}.tmp.{os.getpid()}"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(sorted(list(hidden)), f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, hidden_file)
        except Exception:
            pass
    return hidden


def _save_hidden_rooms(room_ids: list[str] | set[str]) -> None:
    """하나 이상의 대화방/세션 ID를 숨김 목록에 원자적으로 추가 저장한다 (ORCHESTRATOR_ROOM_IDS는 원천 차단)."""
    all_orch_ids = set(_all_orchestrator_room_ids())
    cleaned = [str(r).strip() for r in room_ids if r and str(r).strip() and str(r).strip() not in all_orch_ids]
    if not cleaned:
        return
    hidden = _load_hidden_rooms()
    hidden.update(cleaned)
    for orch_id in all_orch_ids:
        hidden.discard(orch_id)
    hidden_file = _get_hidden_file()
    os.makedirs(os.path.dirname(hidden_file), exist_ok=True)
    tmp_path = f"{hidden_file}.tmp.{os.getpid()}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(sorted(list(hidden)), f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, hidden_file)
    except Exception as exc:
        logger.warning("숨김 대화방 저장 실패 (%s): %s", cleaned, exc)
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _save_hidden_room(room_id: str) -> None:
    """대화방/세션 ID를 숨김 목록에 원자적으로 추가 저장한다."""
    if not room_id:
        return
    _save_hidden_rooms([room_id])


async def handle_chat_rooms_backup(request: web.Request) -> web.Response:
    """GET /api/chat/rooms/backup — SQLite SSOT 대화방 목록 반환."""
    rooms = _load_chat_rooms_index()
    return web.json_response({"ok": True, "rooms": rooms})


async def handle_chat_rooms_backup_save(request: web.Request) -> web.Response:
    """POST /api/chat/rooms/backup — 클라이언트 캐시 역동기화 차단 (SQLite 단일 SSOT 보장)."""
    try:
        payload = await request.json()
        rooms = payload.get("rooms") if isinstance(payload, dict) else []
        count = len(rooms) if isinstance(rooms, list) else 0
        return web.json_response({"ok": True, "count": count, "ssot": "chat_db"})
    except Exception:
        return web.json_response({"ok": False, "error": "Invalid payload"}, status=400)


def _get_room_chat_file(room_id: str) -> str:
    """레거시 호환용 경로 헬퍼."""
    safe_id = re.sub(r"[^a-zA-Z0-9_\-]", "", str(room_id).strip()) or "default"
    return os.path.join(CHATS_DIR, f"{safe_id}.jsonl")


def _append_room_message(room_id: str, msg_record: dict) -> int | None:
    """대화방 전용 SQLite DB(chat_db)에 메시지 레코드를 추가하고 생성된 메시지 ID를 반환한다 (단일 SSOT)."""
    if not room_id:
        return None
    clean_rid = str(room_id).strip()
    sid = str(msg_record.get("sessionId") or "").strip() or None
    current_chat_db = _get_mod_attr("chat_db", chat_db)
    current_load_hidden = _get_mod_attr("_load_hidden_rooms", _load_hidden_rooms)

    if not _is_orchestrator_room_id(clean_rid):
        hidden = current_load_hidden()
        if clean_rid in hidden or (sid and sid in hidden):
            return None
    try:
        role = msg_record.get("role") or msg_record.get("type") or "user"
        text = msg_record.get("text") or ""
        channel = msg_record.get("channel") or "web"
        steps = msg_record.get("steps")
        attachments = msg_record.get("attachments") or msg_record.get("attachedFiles")
        model = msg_record.get("model")
        effort = msg_record.get("effort")
        cost = msg_record.get("cost")
        msg_id = current_chat_db.add_message(
            room_id=clean_rid,
            role=role,
            text=text,
            channel=channel,
            steps=steps,
            attachments=attachments,
            model=model,
            effort=effort,
            cost=cost,
            session_id=sid,
            created_at=msg_record.get("createdAt"),
        )
        return msg_id if isinstance(msg_id, int) and msg_id > 0 else None
    except Exception as exc:
        logger.warning("[chat_db] SQLite 메시지 저장 실패 (%s): %s", clean_rid, exc)
        return None


def _load_room_messages(room_id: str) -> list[dict]:
    """특정 대화방의 메시지 히스토리를 오직 SQLite DB(chat_db)에서만 반환한다 (단일 SSOT)."""
    if not room_id:
        return []
    clean_rid = str(room_id).strip()
    current_chat_db = _get_mod_attr("chat_db", chat_db)
    current_load_hidden = _get_mod_attr("_load_hidden_rooms", _load_hidden_rooms)

    if not _is_orchestrator_room_id(clean_rid) and clean_rid in current_load_hidden():
        return []
    try:
        msgs = current_chat_db.get_messages(clean_rid)
        if _is_orchestrator_room_id(clean_rid) and msgs:
            try:
                import bot.web.orchestrator as web_orchestrator
                for m in msgs:
                    if m.get("role") == "assistant" or m.get("sender") == "assistant":
                        chips = web_orchestrator.extract_delegated_subagents(
                            m.get("steps"),
                            m.get("text") or "",
                            chat_db_module=current_chat_db,
                        )
                        if chips:
                            m["delegatedRooms"] = chips
            except Exception as orch_err:
                logger.debug("[web_rooms] 위임 하위 에이전트 추출 오류: %s", orch_err)
        return msgs
    except Exception as exc:
        logger.warning("[chat_db] SQLite 메시지 로드 실패 (%s): %s", clean_rid, exc)
        return []


def _normalize_room_ts(ts: Any) -> float:
    """타임스탬프(초/밀리초/문자열)를 초 단위 float로 정규화한다."""
    if ts is None:
        return 0.0
    try:
        val = float(ts)
        if val > 1e11:  # 밀리초 타임스탬프(> 1000억) -> 초 변환
            val = val / 1000.0
        return val
    except (ValueError, TypeError):
        return 0.0


def _load_chat_rooms_index() -> list[dict]:
    """서버에 저장된 대화방 목록을 최근 사용순으로 정렬하여 반환한다 (단일 진실 원천: 오직 SQLite chat.db 만 참조)."""
    current_chat_db = _get_mod_attr("chat_db", chat_db)
    current_load_hidden = _get_mod_attr("_load_hidden_rooms", _load_hidden_rooms)
    hidden = current_load_hidden()
    raw_rooms: list[dict] = []

    try:
        db_rooms = current_chat_db.get_rooms(limit=100, include_hidden=False)
        raw_rooms.extend(db_rooms)
    except Exception as exc:
        logger.warning("[chat_db] SQLite 대화방 목록 로드 실패: %s", exc)

    # 정규화 및 단일 방 병합 (Disjoint-Set Union 기반 강력한 중복 제거 및 클러스터링)
    parsed_records: list[tuple[dict, str, str | None]] = []
    parent: dict[str, str] = {}

    def find(k: str) -> str:
        if parent.setdefault(k, k) != k:
            parent[k] = find(parent[k])
        return parent[k]

    def union(k1: str, k2: str) -> None:
        r1, r2 = find(k1), find(k2)
        if r1 != r2:
            parent[r1] = r2

    for r in raw_rooms:
        if not isinstance(r, dict):
            continue
        rid = str(r.get("id") or r.get("roomId") or "").strip()
        sid = str(r.get("sessionId") or "").strip() or None

        if (rid and rid in hidden) or (sid and sid in hidden):
            continue
        if not rid and not sid:
            continue

        primary_k = rid or sid
        if not primary_k:
            continue
        find(primary_k)
        if rid and sid:
            union(rid, sid)
        for orch_id in _all_orchestrator_room_ids():
            if rid == orch_id or sid == orch_id:
                union(primary_k, orch_id)

        parsed_records.append((r, rid, sid))

    # 루트(컴포넌트)별 레코드 수집
    groups: dict[str, list[tuple[dict, str, str | None]]] = {}
    for r, rid, sid in parsed_records:
        k = rid or sid
        if not k:
            continue
        root = find(k)
        groups.setdefault(root, []).append((r, rid, sid))

    all_orch_ids_set = set(_all_orchestrator_room_ids())
    merged_rooms_list: list[dict] = []
    for root, recs in groups.items():
        group_rids = {r[1] for r in recs if r[1]}
        group_sids = {r[2] for r in recs if r[2]}

        orch_match = None
        for orch_id in _all_orchestrator_room_ids():
            if orch_id in group_rids or orch_id in group_sids:
                orch_match = orch_id
                break

        if orch_match:
            canonical_id = orch_match
        else:
            room_prefix_ids = [k for k in group_rids if k.startswith("room-")]
            if room_prefix_ids:
                canonical_id = sorted(room_prefix_ids)[0]
            elif group_rids:
                canonical_id = next(iter(group_rids))
            elif group_sids:
                canonical_id = next(iter(group_sids))
            else:
                canonical_id = root

        valid_sids = [s for s in group_sids if s and s != canonical_id and not s.startswith("room-")]
        if valid_sids:
            canonical_sid = valid_sids[0]
        else:
            non_room_sids = [s for s in group_sids if s and not s.startswith("room-")]
            if non_room_sids:
                canonical_sid = non_room_sids[0]
            elif canonical_id not in all_orch_ids_set and not canonical_id.startswith("room-"):
                canonical_sid = canonical_id
            else:
                canonical_sid = None

        merged: dict = {
            "id": canonical_id,
            "roomId": canonical_id,
            "sessionId": canonical_sid,
            "title": "",
            "preview": "",
            "messageCount": 0,
            "messages": [],
            "updatedAt": 0.0,
            "isNewSession": not bool(canonical_sid),
            "isArchived": False,
            "isOrchestratorChild": False,
            "parentAgentId": None,
            "lastChannel": "",
            "model": None,
            "effort": None,
            "workdir": None,
        }

        agent_cfg = agent_configs.get_agent_config(canonical_id) if canonical_id in all_orch_ids_set else None
        if canonical_id == CORE_ORCHESTRATOR_ROOM_ID:
            merged["title"] = (agent_cfg.get("title") if agent_cfg else None) or CORE_ORCHESTRATOR_TITLE
        elif canonical_id == EXPERT_GENERAL_ROOM_ID:
            merged["title"] = (agent_cfg.get("title") if agent_cfg else None) or EXPERT_GENERAL_TITLE
        elif canonical_id == EXPERT_VAULT_ROOM_ID:
            merged["title"] = (agent_cfg.get("title") if agent_cfg else None) or EXPERT_VAULT_TITLE
        elif canonical_id == EXPERT_CODING_ROOM_ID:
            merged["title"] = (agent_cfg.get("title") if agent_cfg else None) or EXPERT_CODING_TITLE
        elif agent_cfg:
            merged["title"] = agent_cfg.get("title") or agent_cfg.get("name") or canonical_id

        for r, _, _ in recs:
            if r.get("isArchived"):
                merged["isArchived"] = True
            if r.get("parentAgentId") and not merged.get("parentAgentId"):
                merged["parentAgentId"] = str(r["parentAgentId"]).strip()
            if r.get("isOrchestratorChild") or r.get("lastChannel") == "orchestrator":
                merged["isOrchestratorChild"] = True
            if r.get("lastChannel") and not merged.get("lastChannel"):
                merged["lastChannel"] = str(r["lastChannel"])
            if r.get("model") and not merged.get("model"):
                merged["model"] = r["model"]
            if r.get("effort") and not merged.get("effort"):
                merged["effort"] = r["effort"]
            if r.get("workdir") and not merged.get("workdir"):
                merged["workdir"] = str(r["workdir"]).strip()
            rtitle = str(r.get("title") or "").strip()
            if rtitle and rtitle not in ("새 대화", "대화방"):
                if not merged["title"] or merged["title"] in ("새 대화", "대화방") or canonical_id in all_orch_ids_set:
                    if canonical_id not in all_orch_ids_set:
                        merged["title"] = rtitle

            rpreview = str(r.get("preview") or "").strip()
            if len(rpreview) > len(merged["preview"]):
                merged["preview"] = rpreview

            rmsgs = r.get("messages")
            if isinstance(rmsgs, list) and len(rmsgs) > len(merged["messages"]):
                merged["messages"] = rmsgs

            mc = r.get("messageCount")
            if isinstance(mc, int) and mc > merged["messageCount"]:
                merged["messageCount"] = mc

            ru = _normalize_room_ts(r.get("updatedAt") or r.get("createdAt"))
            if ru > merged["updatedAt"]:
                merged["updatedAt"] = ru

        if not merged["title"]:
            if canonical_id == CORE_ORCHESTRATOR_ROOM_ID:
                merged["title"] = CORE_ORCHESTRATOR_TITLE
            elif canonical_id == EXPERT_GENERAL_ROOM_ID:
                merged["title"] = EXPERT_GENERAL_TITLE
            elif canonical_id == EXPERT_VAULT_ROOM_ID:
                merged["title"] = EXPERT_VAULT_TITLE
            elif canonical_id == EXPERT_CODING_ROOM_ID:
                merged["title"] = EXPERT_CODING_TITLE
            elif agent_cfg:
                merged["title"] = agent_cfg.get("title") or agent_cfg.get("name") or canonical_id
            else:
                merged["title"] = "새 대화"

        if not merged.get("workdir"):
            if agent_cfg and agent_cfg.get("workdir"):
                merged["workdir"] = str(agent_cfg["workdir"]).strip()
            elif canonical_id == EXPERT_CODING_ROOM_ID:
                merged["workdir"] = paths.CODEDIR or paths.WORKDIR
            elif canonical_id in (CORE_ORCHESTRATOR_ROOM_ID, EXPERT_GENERAL_ROOM_ID, EXPERT_VAULT_ROOM_ID):
                merged["workdir"] = paths.WORKDIR
            elif child_default_workdir(merged.get("parentAgentId")):
                merged["workdir"] = child_default_workdir(merged.get("parentAgentId"))
            else:
                is_coding = get_coding_mode()
                merged["workdir"] = (paths.CODEDIR or paths.WORKDIR) if is_coding else paths.WORKDIR

        if merged.get("isOrchestratorChild") and not merged.get("parentAgentId"):
            merged["parentAgentId"] = CORE_ORCHESTRATOR_ROOM_ID

        # 에이전트/부모 계층 기반 기본 모델 및 Effort 계산
        parent_aid = merged.get("parentAgentId")
        parent_cfg = agent_configs.get_agent_config(parent_aid) if parent_aid else None
        if canonical_id in all_orch_ids_set and agent_cfg:
            merged["defaultModel"] = agent_cfg.get("model") or get_model()
            merged["defaultEffort"] = agent_cfg.get("effort") or get_effort()
        elif parent_cfg:
            merged["defaultModel"] = parent_cfg.get("childModel") or parent_cfg.get("model") or get_model()
            merged["defaultEffort"] = parent_cfg.get("childEffort") or parent_cfg.get("effort") or get_effort()
        else:
            merged["defaultModel"] = get_model()
            merged["defaultEffort"] = get_effort()

        if merged["messages"] and len(merged["messages"]) > merged["messageCount"]:
            merged["messageCount"] = len(merged["messages"])

        merged_rooms_list.append(merged)

    # 실행 중인 활성 키 집합 조회
    active_keys: set[str] = set()
    mod = sys.modules.get("bot.web.server")
    if mod:
        for attr in ("_active_watch_tasks", "_active_agent_pids", "_room_queues", "_room_live_snapshots"):
            d = getattr(mod, attr, None)
            if isinstance(d, dict):
                active_keys.update(d.keys())

    valid_rooms = [
        r for r in merged_rooms_list
        if (
            r.get("id") in all_orch_ids_set
            or r.get("messageCount", 0) > 0
            or (r.get("messages") and len(r["messages"]) > 0)
            or (r.get("title") and r.get("title") not in ("새 대화", "대화방", ""))
            or str(r.get("id") or "") in active_keys
            or str(r.get("sessionId") or "") in active_keys
        )
    ]
    valid_rooms.sort(
        key=lambda x: _normalize_room_ts(x.get("updatedAt") or x.get("createdAt")),
        reverse=True,
    )
    return valid_rooms


def _save_chat_rooms_index(rooms: list[dict]) -> None:
    """레거시 파일 인덱스 저장 비활성화 (SQLite chat.db 단일 SSOT 원칙)."""
    return


def _update_room_metadata(
    room_id: str,
    title: str | None = None,
    preview: str | None = None,
    delta_msgs: int = 1,
    session_id: str | None = None,
) -> None:
    """대화방 메타데이터를 오직 SQLite DB(chat_db)에만 갱신한다 (단일 SSOT)."""
    if not room_id:
        return
    clean_rid = str(room_id).strip()
    clean_sid = str(session_id).strip() if session_id else None
    current_chat_db = _get_mod_attr("chat_db", chat_db)
    current_load_hidden = _get_mod_attr("_load_hidden_rooms", _load_hidden_rooms)

    is_orch = _is_orchestrator_room_id(clean_rid)
    if not is_orch:
        hidden = current_load_hidden()
        if clean_rid in hidden or (clean_sid and clean_sid in hidden):
            return
    if clean_rid == CORE_ORCHESTRATOR_ROOM_ID:
        title = CORE_ORCHESTRATOR_TITLE
    elif clean_rid == EXPERT_GENERAL_ROOM_ID:
        title = EXPERT_GENERAL_TITLE
    elif clean_rid == EXPERT_VAULT_ROOM_ID:
        title = EXPERT_VAULT_TITLE
    elif clean_rid == EXPERT_CODING_ROOM_ID:
        title = EXPERT_CODING_TITLE
    elif is_orch:
        cfg = agent_configs.get_agent_config(clean_rid)
        if cfg:
            title = cfg.get("title") or cfg.get("name") or title
    try:
        current_chat_db.get_or_create_room(clean_rid, session_id=clean_sid, title=title, channel="web")
        current_chat_db.update_room(
            room_id=clean_rid,
            title=title,
            preview=preview,
            session_id=clean_sid,
            channel="web",
            force_title=is_orch,
        )
    except Exception as exc:
        logger.warning("[chat_db] SQLite 방 메타데이터 갱신 실패: %s", exc)


async def handle_api_delete_session(request: web.Request) -> web.Response:
    """POST /api/session/delete (및 /api/session/batch-delete) — 대화방 삭제 후 SQLite 영구 삭제 및 브로드캐스트."""
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    session_id = str(payload.get("sessionId") or request.query.get("sessionId") or request.query.get("session_id") or "").strip() or None
    room_id = str(payload.get("roomId") or request.query.get("roomId") or request.query.get("room_id") or "").strip() or None

    raw_room_ids = payload.get("roomIds")
    raw_session_ids = payload.get("sessionIds")
    raw_items = payload.get("items") or payload.get("rooms")

    target_room_ids: list[str] = []
    target_session_ids: list[str] = []

    if room_id:
        target_room_ids.append(room_id)
    if session_id:
        target_session_ids.append(session_id)

    if isinstance(raw_room_ids, list):
        for rid in raw_room_ids:
            clean_rid = str(rid or "").strip()
            if clean_rid and clean_rid not in target_room_ids:
                target_room_ids.append(clean_rid)

    if isinstance(raw_session_ids, list):
        for sid in raw_session_ids:
            clean_sid = str(sid or "").strip()
            if clean_sid and clean_sid not in target_session_ids:
                target_session_ids.append(clean_sid)

    if isinstance(raw_items, list):
        for item in raw_items:
            if isinstance(item, dict):
                irid = str(item.get("roomId") or item.get("id") or "").strip()
                isid = str(item.get("sessionId") or "").strip()
                if irid and irid not in target_room_ids:
                    target_room_ids.append(irid)
                if isid and isid not in target_session_ids:
                    target_session_ids.append(isid)
            elif isinstance(item, str) and item.strip():
                clean_item = item.strip()
                if clean_item not in target_room_ids:
                    target_room_ids.append(clean_item)

    current_chat_db = _get_mod_attr("chat_db", chat_db)
    for key in list(dict.fromkeys(target_room_ids + target_session_ids)):
        try:
            row = current_chat_db.get_room(key)
            if row:
                db_rid = str(row.get("id") or "").strip()
                db_sid = str(row.get("sessionId") or "").strip()
                if db_rid and db_rid not in target_room_ids:
                    target_room_ids.append(db_rid)
                if db_sid and db_sid not in target_session_ids:
                    target_session_ids.append(db_sid)
        except Exception:
            pass

    all_target_keys = list(dict.fromkeys(target_room_ids + target_session_ids))
    target_set = set(all_target_keys)

    # 0. 진행 중인 에이전트 실행 및 백그라운드 web_jobs 즉시 중단·제거
    cancel_run_fn = _get_mod_attr("cancel_active_run", None)
    load_jobs_fn = _get_mod_attr("_load_web_jobs", None)
    remove_job_fn = _get_mod_attr("_remove_web_job", None)

    for k in all_target_keys:
        if cancel_run_fn:
            cancel_run_fn(k)
        mod = sys.modules.get("bot.web.server")
        if mod and hasattr(mod, "_room_session_ids"):
            mod._room_session_ids.pop(k, None)

    if load_jobs_fn and remove_job_fn:
        jobs_data = load_jobs_fn()
        for jid, jrec in list(jobs_data.items()):
            if not isinstance(jrec, dict):
                continue
            j_rid = str(jrec.get("room_id") or "").strip()
            j_sid = str(jrec.get("session_id") or "").strip()
            if (j_rid and j_rid in target_set) or (j_sid and j_sid in target_set):
                j_pid = jrec.get("pid")
                if j_pid:
                    kill_job_fn = _get_mod_attr("kill_job", kill_job)
                    kill_job_fn(j_pid)
                remove_job_fn(jid)

    # 1. 숨김 목록에 선제 등록 (마스터 및 전문가 에이전트는 영구 숨김 제외)
    hideable_keys = [k for k in all_target_keys if k not in ORCHESTRATOR_ROOM_IDS]
    if hideable_keys:
        _save_hidden_rooms(hideable_keys)

    mod = sys.modules.get("bot.web.server")
    if CORE_ORCHESTRATOR_ROOM_ID in all_target_keys:
        clear_session(ns="core_")
        if mod and hasattr(mod, "_room_session_ids"):
            mod._room_session_ids.pop(CORE_ORCHESTRATOR_ROOM_ID, None)
    if EXPERT_VAULT_ROOM_ID in all_target_keys:
        clear_session(ns="vault_")
        if mod and hasattr(mod, "_room_session_ids"):
            mod._room_session_ids.pop(EXPERT_VAULT_ROOM_ID, None)
    if EXPERT_CODING_ROOM_ID in all_target_keys:
        clear_session(ns="coding_")
        if mod and hasattr(mod, "_room_session_ids"):
            mod._room_session_ids.pop(EXPERT_CODING_ROOM_ID, None)

    # 2. SQLite DB 영구 삭제 처리 (마스터 및 전문가 에이전트는 DB 삭제 제외)
    deletable_keys = [k for k in all_target_keys if k not in ORCHESTRATOR_ROOM_IDS]
    if deletable_keys:
        try:
            if len(deletable_keys) == 1:
                current_chat_db.delete_room(deletable_keys[0], hard_delete=True)
            else:
                current_chat_db.delete_rooms_bulk(deletable_keys, hard_delete=True)
        except Exception as exc:
            logger.warning("[chat_db] 대화방 삭제 실패: %s", exc)

        # 2-1. 마스터 위임 작업 원장에서도 삭제된 대화방 작업 자동 종결
        try:
            import bot.master.master_tasks as master_tasks
            master_tasks.handle_rooms_deleted(all_target_keys)
        except Exception:
            pass

        # 2-2. 마스터 감독 루프에 변경 사항 즉시 반영
        try:
            supervisor = get_master_supervisor()
            if supervisor:
                supervisor.poke()
        except Exception:
            pass

    # 3. 레거시 chats/{room_id}.jsonl 파일이 남아있다면 제거
    for rid in all_target_keys:
        chat_file = _get_room_chat_file(rid)
        if os.path.isfile(chat_file):
            try:
                os.remove(chat_file)
            except OSError:
                pass

    if len(target_session_ids) > 1:
        del_results = [delete_session_files(sid) for sid in target_session_ids if sid]
        del_result = {"deleted": True, "count": len(del_results), "details": del_results}
    elif session_id:
        del_result = delete_session_files(session_id)
    elif target_session_ids:
        del_result = delete_session_files(target_session_ids[0])
    else:
        del_result = {"deleted": True}

    deleted_count = max(len(target_room_ids), len(target_session_ids), len(all_target_keys))
    vault_log(
        "🗑️ 대화방 삭제",
        f"[REST API] count={deleted_count}, roomIds={target_room_ids}, sessionIds={target_session_ids} (대화방 및 메시지 영구 삭제 완료)",
    )

    primary_rid = room_id or (target_room_ids[0] if target_room_ids else None)
    primary_sid = session_id or (target_session_ids[0] if target_session_ids else None)

    broadcast_fn = _get_mod_attr("broadcast_ws_json", None)
    if broadcast_fn:
        await broadcast_fn({
            "type": "session_deleted",
            "roomId": primary_rid,
            "sessionId": primary_sid,
            "roomIds": target_room_ids,
            "sessionIds": target_session_ids,
            "ok": True,
            "result": del_result,
        })
        build_status_fn = _get_mod_attr("build_status_payload", None)
        if build_status_fn:
            await broadcast_fn(build_status_fn())

    return web.json_response({
        "ok": True,
        "roomId": primary_rid,
        "sessionId": primary_sid,
        "roomIds": target_room_ids,
        "sessionIds": target_session_ids,
        "deletedCount": deleted_count,
        "hidden": True,
        "result": del_result,
    })


async def handle_api_rename_room(request: web.Request) -> web.Response:
    """POST /api/chat/room/rename — 특정 대화방의 제목을 변경하고 SQLite SSOT에 영속 저장 후 브로드캐스트한다."""
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "유효하지 않은 JSON 요청입니다."}, status=400)

    room_id = str(payload.get("roomId") or payload.get("id") or "").strip()
    session_id = str(payload.get("sessionId") or "").strip() or None
    new_title = str(payload.get("title") or "").strip()

    target_key = room_id or session_id
    if not target_key or not new_title:
        return web.json_response({"ok": False, "error": "roomId 및 title 이 필요합니다."}, status=400)

    new_title = new_title[:80]
    current_chat_db = _get_mod_attr("chat_db", chat_db)

    try:
        current_chat_db.update_room(room_id=target_key, title=new_title, session_id=session_id, force_title=True)
        if session_id and session_id != target_key:
            current_chat_db.update_room(room_id=session_id, title=new_title, session_id=session_id, force_title=True)
    except Exception as exc:
        logger.warning("[chat_db] 대화방 이름 변경 실패 (%s): %s", target_key, exc)

    broadcast_fn = _get_mod_attr("broadcast_ws_json", None)
    if broadcast_fn:
        await broadcast_fn({
            "type": "room_renamed",
            "roomId": room_id or session_id,
            "sessionId": session_id,
            "title": new_title,
        })

    return web.json_response({
        "ok": True,
        "roomId": room_id or session_id,
        "sessionId": session_id,
        "title": new_title,
    })


async def handle_api_room_settings(request: web.Request) -> web.Response:
    """POST /api/chat/room/settings — 특정 대화방의 model/effort 설정을 변경하고 SQLite SSOT에 저장 후 브로드캐스트한다."""
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "유효하지 않은 JSON 요청입니다."}, status=400)

    room_id = str(payload.get("roomId") or payload.get("id") or "").strip()
    session_id = str(payload.get("sessionId") or "").strip() or None
    target_key = room_id or session_id
    if not target_key:
        return web.json_response({"ok": False, "error": "roomId 또는 sessionId 가 필요합니다."}, status=400)

    model = payload.get("model")
    clean_model = str(model).strip() if (isinstance(model, str) and model.strip()) else None

    effort = payload.get("effort")
    clean_effort = str(effort).strip() if (isinstance(effort, str) and effort.strip()) else None

    workdir = payload.get("workdir")
    clean_workdir = str(workdir).strip() if (isinstance(workdir, str) and workdir.strip()) else None
    current_chat_db = _get_mod_attr("chat_db", chat_db)

    update_kwargs: dict[str, Any] = {"model": clean_model, "effort": clean_effort}
    if "workdir" in payload:
        update_kwargs["workdir"] = clean_workdir

    try:
        current_chat_db.update_room(room_id=target_key, session_id=session_id, **update_kwargs)
        if session_id and session_id != target_key:
            current_chat_db.update_room(room_id=session_id, session_id=session_id, **update_kwargs)
    except Exception as exc:
        logger.warning("[chat_db] 대화방 설정 변경 실패 (%s): %s", target_key, exc)
        return web.json_response({"ok": False, "error": str(exc)}, status=500)

    updated_room = current_chat_db.get_room(target_key)
    parent_aid = (updated_room.get("parent_agent_id") or updated_room.get("parentAgentId")) if updated_room else None
    parent_cfg = agent_configs.get_agent_config(parent_aid) if parent_aid else None
    orch_cfg = agent_configs.get_agent_config(target_key) if target_key in agent_configs.get_orchestrator_room_ids() else None
    if orch_cfg:
        def_m = orch_cfg.get("model") or get_model()
        def_e = orch_cfg.get("effort") or get_effort()
    elif parent_cfg:
        def_m = parent_cfg.get("childModel") or parent_cfg.get("model") or get_model()
        def_e = parent_cfg.get("childEffort") or parent_cfg.get("effort") or get_effort()
    else:
        def_m = get_model()
        def_e = get_effort()

    broadcast_fn = _get_mod_attr("broadcast_ws_json", None)
    if broadcast_fn:
        broadcast_data: dict[str, Any] = {
            "type": "room_settings_updated",
            "roomId": target_key,
            "sessionId": session_id,
            "model": clean_model,
            "effort": clean_effort,
            "defaultModel": def_m,
            "defaultEffort": def_e,
            "room": updated_room,
        }
        if "workdir" in payload:
            broadcast_data["workdir"] = clean_workdir
        await broadcast_fn(broadcast_data)

    resp_data: dict[str, Any] = {
        "ok": True,
        "roomId": target_key,
        "sessionId": session_id,
        "model": clean_model,
        "effort": clean_effort,
        "defaultModel": def_m,
        "defaultEffort": def_e,
        "room": updated_room,
    }
    if "workdir" in payload:
        resp_data["workdir"] = clean_workdir
    return web.json_response(resp_data)


async def handle_get_attachment(request: web.Request) -> web.Response:
    """GET /api/attachments/{filename} — 업로드된 첨부파일(사진/이미지 등)을 서빙한다."""
    filename = request.match_info.get("filename", "").strip()
    if not filename:
        return web.Response(status=400, text="파일명이 필요합니다.")
    safe_name = os.path.basename(filename)
    if safe_name != filename or ".." in filename:
        return web.Response(status=400, text="잘못된 파일 경로입니다.")
    file_path = os.path.join(ATTACHED_FILES_DIR, safe_name)
    if not os.path.isfile(file_path):
        return web.Response(status=404, text="파일을 찾을 수 없습니다.")
    return web.FileResponse(file_path)


async def handle_api_upload(request: web.Request) -> web.Response:
    """POST /api/upload (및 /api/attachments/upload) — 사진/파일을 업로드하여 data/attached_files 에 저장한다."""
    os.makedirs(ATTACHED_FILES_DIR, exist_ok=True)
    uploaded_files: list[dict[str, Any]] = []

    content_type = request.content_type or ""
    if "multipart" in content_type:
        reader = await request.multipart()
        while True:
            part = await reader.next()
            if part is None:
                break
            if part.filename:
                raw_filename = os.path.basename(part.filename.strip())
                ext = os.path.splitext(raw_filename)[1].lower() or ".png"
                unique_hex = uuid.uuid4().hex[:12]
                safe_name = f"{int(time.time())}_{unique_hex}{ext}"
                file_path = os.path.join(ATTACHED_FILES_DIR, safe_name)
                size = 0
                with open(file_path, "wb") as f:
                    while True:
                        chunk = await part.read_chunk(65536)
                        if not chunk:
                            break
                        size += len(chunk)
                        f.write(chunk)
                uploaded_files.append({
                    "name": raw_filename,
                    "savedName": safe_name,
                    "path": file_path,
                    "url": f"/api/attachments/{safe_name}",
                    "size": size,
                    "type": part.headers.get("Content-Type", ""),
                })
    elif "json" in content_type:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        raw_files = payload.get("files") if isinstance(payload, dict) else None
        if isinstance(raw_files, list):
            for item in raw_files:
                if not isinstance(item, dict):
                    continue
                raw_name = os.path.basename(str(item.get("name") or "image.png").strip())
                b64_data = str(item.get("data") or "").strip()
                if not b64_data:
                    continue
                if "," in b64_data:
                    b64_data = b64_data.split(",", 1)[1]
                try:
                    file_bytes = base64.b64decode(b64_data)
                except Exception:
                    continue
                ext = os.path.splitext(raw_name)[1].lower() or ".png"
                unique_hex = uuid.uuid4().hex[:12]
                safe_name = f"{int(time.time())}_{unique_hex}{ext}"
                file_path = os.path.join(ATTACHED_FILES_DIR, safe_name)
                with open(file_path, "wb") as f:
                    f.write(file_bytes)
                uploaded_files.append({
                    "name": raw_name,
                    "savedName": safe_name,
                    "path": file_path,
                    "url": f"/api/attachments/{safe_name}",
                    "size": len(file_bytes),
                    "type": item.get("type", "image/png"),
                })
    else:
        return web.json_response(
            {"ok": False, "error": "지원되지 않는 요청 형식입니다. (multipart/form-data 또는 JSON 권장)"},
            status=400,
        )

    if not uploaded_files:
        return web.json_response({"ok": False, "error": "업로드된 파일이 없습니다."}, status=400)

    vault_log(
        "📎 파일 업로드",
        f"[사진/파일 업로드 완료] {len(uploaded_files)}개 파일 ({', '.join(f['name'] for f in uploaded_files)})",
    )
    return web.json_response({"ok": True, "files": uploaded_files})


async def handle_chat_rooms(_request: web.Request) -> web.Response:
    """GET /api/chat/rooms — 우리 앱 전용 대화방 목록 반환 (초고속 단일 진실 원천 SSOT)."""
    current_load_index = _get_mod_attr("_load_chat_rooms_index", _load_chat_rooms_index)
    rooms = current_load_index()
    from bot.web.runtime_state import build_active_runs_payload
    build_payload_fn = _get_mod_attr("build_active_runs_payload", build_active_runs_payload)
    return web.json_response({
        "ok": True,
        "rooms": rooms,
        "activeRuns": build_payload_fn(),
    })


async def handle_search_chat_rooms(request: web.Request) -> web.Response:
    """GET /api/chat/rooms/search?q=... — 대화방 제목·메시지 본문 전체 검색."""
    query = (request.query.get("q") or "").strip()
    if not query:
        return web.json_response({"ok": True, "rooms": [], "query": ""})
    try:
        limit = min(max(int(request.query.get("limit", "100")), 1), 200)
    except ValueError:
        limit = 100
    current_chat_db = _get_mod_attr("chat_db", chat_db)
    rooms = current_chat_db.search_rooms(query, limit=limit)
    return web.json_response({
        "ok": True,
        "rooms": rooms,
        "query": query,
    })


async def handle_chat_history(request: web.Request) -> web.Response:
    """GET /api/chat/history?roomId=...&sessionId=... — 특정 대화방의 대화 내역 전체를 반환한다."""
    room_id = request.query.get("roomId", "").strip() or request.query.get("sessionId", "").strip()
    if not room_id:
        return web.json_response({"ok": False, "error": "roomId 또는 sessionId 파라미터가 필요합니다."}, status=400)

    current_load_hidden = _get_mod_attr("_load_hidden_rooms", _load_hidden_rooms)
    hidden = current_load_hidden()
    if room_id in hidden and room_id not in ORCHESTRATOR_ROOM_IDS:
        return web.json_response({"ok": True, "roomId": room_id, "messages": [], "liveRun": None})

    current_load_msgs = _get_mod_attr("_load_room_messages", _load_room_messages)
    messages = current_load_msgs(room_id)
    resolved_room_id = room_id
    if not messages:
        current_load_index = _get_mod_attr("_load_chat_rooms_index", _load_chat_rooms_index)
        rooms = current_load_index()
        matched = next((r for r in rooms if r.get("sessionId") == room_id or r.get("id") == room_id), None)
        if matched:
            target_id = matched.get("id")
            if target_id and target_id != room_id:
                resolved_room_id = target_id
                messages = current_load_msgs(target_id)
            if not messages and matched.get("messages"):
                messages = matched.get("messages")

    from bot.web.runtime_state import get_live_run_for_room
    get_live_run_fn = _get_mod_attr("get_live_run_for_room", get_live_run_for_room)
    live_run = get_live_run_fn(resolved_room_id) or get_live_run_fn(room_id)
    if messages:
        active_msg_ids: set[Any] = set()
        if live_run and live_run.get("asstMsgId"):
            active_msg_ids.add(live_run["asstMsgId"])
        from bot.web.job_recovery import _load_web_jobs
        load_jobs_fn = _get_mod_attr("_load_web_jobs", _load_web_jobs)
        for job_rec in load_jobs_fn().values():
            if isinstance(job_rec, dict) and job_rec.get("msg_id"):
                active_msg_ids.add(job_rec["msg_id"])
        if active_msg_ids:
            messages = [m for m in messages if m.get("id") not in active_msg_ids]
        if live_run and live_run.get("isRunning") and messages:
            if messages[-1].get("role") in ("assistant", "model") and not str(messages[-1].get("text") or "").strip():
                messages = messages[:-1]

    current_chat_db = _get_mod_attr("chat_db", chat_db)
    room_rec = current_chat_db.get_room(resolved_room_id) or current_chat_db.get_room(room_id)
    resolved_workdir = (room_rec.get("workdir") if room_rec and room_rec.get("workdir") else None)
    if not resolved_workdir:
        if resolved_room_id == EXPERT_CODING_ROOM_ID or room_id == EXPERT_CODING_ROOM_ID:
            resolved_workdir = paths.CODEDIR or paths.WORKDIR
        elif (
            resolved_room_id in (CORE_ORCHESTRATOR_ROOM_ID, EXPERT_VAULT_ROOM_ID)
            or room_id in (CORE_ORCHESTRATOR_ROOM_ID, EXPERT_VAULT_ROOM_ID)
        ):
            resolved_workdir = paths.WORKDIR
        elif child_default_workdir(room_rec.get("parent_agent_id") if room_rec else None):
            resolved_workdir = child_default_workdir(room_rec.get("parent_agent_id"))
        else:
            is_coding = get_coding_mode()
            resolved_workdir = (paths.CODEDIR or paths.WORKDIR) if is_coding else paths.WORKDIR

    return web.json_response({
        "ok": True,
        "roomId": resolved_room_id,
        "workdir": resolved_workdir,
        "messages": messages,
        "liveRun": live_run,
        "model": (room_rec.get("model") if room_rec else None),
        "effort": (room_rec.get("effort") if room_rec else None),
    })


async def handle_archive_chat_room(request: web.Request) -> web.Response:
    """POST /api/chat/rooms/{room_id}/archive — 대화방 아카이브(보관) 또는 복원."""
    room_id = request.match_info.get("room_id", "").strip()
    payload: dict = {}
    try:
        payload = await request.json()
    except Exception:
        pass
    if not room_id:
        room_id = str(payload.get("roomId") or "").strip()
    if not room_id:
        return web.json_response({"ok": False, "error": "roomId 가 필요합니다."}, status=400)

    if room_id in ORCHESTRATOR_ROOM_IDS or (room_id.startswith("__custom_") and room_id.endswith("__")):
        return web.json_response({"ok": False, "error": "마스터 및 전문가 에이전트 방은 아카이브할 수 없습니다."}, status=400)

    current_chat_db = _get_mod_attr("chat_db", chat_db)
    if "isArchived" in payload:
        is_archived = bool(payload["isArchived"])
    elif "archive" in payload:
        is_archived = bool(payload["archive"])
    else:
        existing = current_chat_db.get_room(room_id)
        is_archived = not bool(existing.get("isArchived")) if existing else True

    ok = current_chat_db.archive_room(room_id, is_archived=is_archived)
    if not ok:
        return web.json_response({"ok": False, "error": "방을 찾을 수 없거나 아카이브 갱신 실패(보호 대상 방 등)"}, status=404)

    # WebSocket 브로드캐스트
    broadcast_fn = _get_mod_attr("broadcast_ws_json", None)
    if broadcast_fn:
        await broadcast_fn({
            "type": "room_archived",
            "roomId": room_id,
            "isArchived": is_archived,
        })

    supervisor = get_master_supervisor()
    if supervisor is not None:
        supervisor.poke()

    return web.json_response({"ok": True, "roomId": room_id, "isArchived": is_archived})


async def handle_batch_archive_chat_rooms(request: web.Request) -> web.Response:
    """POST /api/chat/rooms/batch-archive (및 /api/session/batch-archive) — 대화방 일괄 아카이브(보관) 또는 복원."""
    payload: dict = {}
    try:
        payload = await request.json()
    except Exception:
        pass
    if not isinstance(payload, dict):
        payload = {}

    raw_targets = payload.get("targets") or payload.get("roomIds") or payload.get("sessionIds") or payload.get("rooms") or []
    target_ids: list[str] = []
    if isinstance(raw_targets, list):
        for t in raw_targets:
            if isinstance(t, dict):
                tid = str(t.get("roomId") or t.get("id") or t.get("sessionId") or "").strip()
                if tid and tid not in target_ids:
                    target_ids.append(tid)
            elif isinstance(t, str) and t.strip():
                clean_t = t.strip()
                if clean_t not in target_ids:
                    target_ids.append(clean_t)
    single_target = str(payload.get("roomId") or payload.get("sessionId") or "").strip()
    if single_target and single_target not in target_ids:
        target_ids.append(single_target)

    if not target_ids:
        return web.json_response({"ok": False, "error": "보관 대상(targets 또는 roomIds)이 필요합니다."}, status=400)

    if "isArchived" in payload:
        is_archived = bool(payload["isArchived"])
    elif "archive" in payload:
        is_archived = bool(payload["archive"])
    else:
        is_archived = True

    current_chat_db = _get_mod_attr("chat_db", chat_db)
    archived_count = current_chat_db.archive_rooms_bulk(target_ids, is_archived=is_archived)

    # WebSocket 브로드캐스트
    broadcast_fn = _get_mod_attr("broadcast_ws_json", None)
    if broadcast_fn:
        for tid in target_ids:
            try:
                await broadcast_fn({
                    "type": "room_archived",
                    "roomId": tid,
                    "isArchived": is_archived,
                })
            except Exception:
                pass

    supervisor = get_master_supervisor()
    if supervisor is not None:
        supervisor.poke()

    return web.json_response({
        "ok": True,
        "archivedCount": archived_count,
        "isArchived": is_archived,
        "targets": target_ids,
    })

