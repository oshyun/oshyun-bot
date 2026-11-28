"""통합 SQLite 대화 데이터베이스 모듈 (chat_db.py).

웹/음성 채널(bot.web.server)이 공통으로 바라보는
단일 진실 원천(SSOT) 대화 저장소.
- WAL(Write-Ahead Logging) 모드로 멀티 프로세스 동시 읽기/쓰기 안전성 보장.
- 대화방(rooms) 메타데이터 및 메시지(messages) 내역 영속화.
- 기존 data/chats/*.jsonl 및 chat_rooms.json 레거시 데이터 자동 1회 마이그레이션 지원.
"""

from __future__ import annotations

from datetime import datetime
import json
import logging
import os
import sqlite3
import time
from typing import Any

from bot.core.paths import CHAT_DB_FILE, CHAT_ROOMS_FILE, CHATS_DIR, STATE_DIR

logger = logging.getLogger("oshyun-chat-db")
HIDDEN_ROOMS_FILE = os.path.join(STATE_DIR, "hidden_chat_rooms.json")
CORE_ORCHESTRATOR_ROOM_ID = "__core__"
EXPERT_GENERAL_ROOM_ID = "__expert_general__"
EXPERT_VAULT_ROOM_ID = "__expert_vault__"
EXPERT_CODING_ROOM_ID = "__expert_coding__"
PROTECTED_ROOM_IDS = {CORE_ORCHESTRATOR_ROOM_ID, EXPERT_GENERAL_ROOM_ID, EXPERT_VAULT_ROOM_ID, EXPERT_CODING_ROOM_ID}


def _is_protected_room_id(room_id: str | None) -> bool:
    clean = str(room_id or "").strip()
    if not clean:
        return False
    if clean in PROTECTED_ROOM_IDS:
        return True
    try:
        import bot.core.agent_configs as agent_configs
        return agent_configs.is_orchestrator_room(clean)
    except Exception:
        return False
_SUB_AGENT_REPORT_PREFIXES = (
    "[하위 작업 완료 보고]",
    "[하위 작업 중단/오류 보고]",
    "[System Event] 서브 태스크",
)


def _resolve_message_role(role: str | None, channel: str | None = None, text: str | None = None) -> str:
    """메시지 역할·채널·본문 프리픽스를 바탕으로 하위 에이전트 보고(`sub-agent`) 여부를 판별한다."""
    clean_role = str(role or "user").strip() or "user"
    if clean_role == "sub-agent":
        return "sub-agent"
    if clean_role == "user":
        clean_channel = str(channel or "").strip()
        clean_text = str(text or "").lstrip()
        if clean_channel == "task_report" or any(clean_text.startswith(p) for p in _SUB_AGENT_REPORT_PREFIXES):
            return "sub-agent"
    return clean_role


def _parse_timestamp(val: Any, default: float | None = None) -> float:
    """타임스탬프(float/int 초/밀리초 또는 ISO 8601 문자열)를 안전하게 float 초로 변환한다."""
    if val is None:
        return default if default is not None else time.time()
    if isinstance(val, (int, float)):
        f_val = float(val)
        if f_val > 1e11:  # 밀리초 타임스탬프(> 1000억) -> 초 변환
            f_val = f_val / 1000.0
        return f_val
    if isinstance(val, str):
        val = val.strip()
        if not val:
            return default if default is not None else time.time()
        try:
            f_val = float(val)
            if f_val > 1e11:  # 밀리초 타임스탬프 -> 초 변환
                f_val = f_val / 1000.0
            return f_val
        except ValueError:
            pass
        try:
            dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
            return dt.timestamp()
        except Exception:
            pass
    return default if default is not None else time.time()


def get_connection(db_path: str | None = None) -> sqlite3.Connection:
    """SQLite DB 커넥션을 생성하고 WAL 모드 및 외래키 제약을 활성화한다."""
    path = db_path or CHAT_DB_FILE
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db(db_path: str | None = None, migrate_legacy: bool = True) -> None:
    """테이블 및 인덱스를 생성하고 필요한 경우 레거시 데이터를 마이그레이션한다."""
    with get_connection(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rooms (
                id TEXT PRIMARY KEY,
                session_id TEXT,
                title TEXT NOT NULL,
                preview TEXT DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                last_channel TEXT NOT NULL DEFAULT 'web',
                is_hidden INTEGER DEFAULT 0,
                is_archived INTEGER DEFAULT 0,
                model TEXT DEFAULT NULL,
                effort TEXT DEFAULT NULL,
                workdir TEXT DEFAULT NULL,
                parent_agent_id TEXT DEFAULT NULL,
                add_dirs TEXT DEFAULT NULL
            );
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id TEXT NOT NULL,
                role TEXT NOT NULL,
                channel TEXT NOT NULL DEFAULT 'web',
                text TEXT NOT NULL,
                steps_json TEXT DEFAULT '[]',
                attachments_json TEXT DEFAULT '[]',
                model TEXT,
                effort TEXT,
                cost REAL,
                session_id TEXT,
                created_at REAL NOT NULL,
                FOREIGN KEY(room_id) REFERENCES rooms(id) ON DELETE CASCADE
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rooms_updated ON rooms(updated_at DESC);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rooms_session ON rooms(session_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_room ON messages(room_id, id ASC);")

        # messages 테이블 attachments_json 컬럼 누락 시 하위 호환 추가
        try:
            conn.execute("ALTER TABLE messages ADD COLUMN attachments_json TEXT DEFAULT '[]';")
            conn.commit()
        except sqlite3.OperationalError:
            pass

        # messages 테이블 effort 컬럼 누락 시 하위 호환 추가
        try:
            conn.execute("ALTER TABLE messages ADD COLUMN effort TEXT DEFAULT NULL;")
            conn.commit()
        except sqlite3.OperationalError:
            pass

        # rooms 테이블 model, effort 컬럼 누락 시 하위 호환 추가
        try:
            conn.execute("ALTER TABLE rooms ADD COLUMN model TEXT DEFAULT NULL;")
            conn.commit()
        except sqlite3.OperationalError:
            pass

        try:
            conn.execute("ALTER TABLE rooms ADD COLUMN effort TEXT DEFAULT NULL;")
            conn.commit()
        except sqlite3.OperationalError:
            pass

        # rooms 테이블 is_archived 컬럼 누락 시 하위 호환 추가
        try:
            conn.execute("ALTER TABLE rooms ADD COLUMN is_archived INTEGER DEFAULT 0;")
            conn.commit()
        except sqlite3.OperationalError:
            pass

        # rooms 테이블 workdir 컬럼 누락 시 하위 호환 추가
        try:
            conn.execute("ALTER TABLE rooms ADD COLUMN workdir TEXT DEFAULT NULL;")
            conn.commit()
        except sqlite3.OperationalError:
            pass

        # rooms 테이블 parent_agent_id 컬럼 누락 시 하위 호환 추가
        try:
            conn.execute("ALTER TABLE rooms ADD COLUMN parent_agent_id TEXT DEFAULT NULL;")
            conn.commit()
        except sqlite3.OperationalError:
            pass

        # rooms 테이블 add_dirs 컬럼 누락 시 하위 호환 추가
        try:
            conn.execute("ALTER TABLE rooms ADD COLUMN add_dirs TEXT DEFAULT NULL;")
            conn.commit()
        except sqlite3.OperationalError:
            pass

        conn.execute("CREATE INDEX IF NOT EXISTS idx_rooms_archived ON rooms(is_archived, updated_at DESC);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rooms_parent_agent ON rooms(parent_agent_id);")

        # 밀리초 단위(> 100억)로 저장된 기존 타임스탬프를 초 단위로 자동 정규화 (1회 보정)
        conn.execute("UPDATE rooms SET updated_at = updated_at / 1000.0 WHERE updated_at > 10000000000;")
        conn.execute("UPDATE rooms SET created_at = created_at / 1000.0 WHERE created_at > 10000000000;")
        conn.execute("UPDATE messages SET created_at = created_at / 1000.0 WHERE created_at > 10000000000;")
        # 기존 'user' 역할로 저장된 하위 작업 완료/중단/태스크 보고를 'sub-agent' 로 일괄 보정
        conn.execute("""
            UPDATE messages
            SET role = 'sub-agent'
            WHERE role = 'user'
              AND (
                  channel = 'task_report'
                  OR LTRIM(text) LIKE '[하위 작업 완료 보고]%'
                  OR LTRIM(text) LIKE '[하위 작업 중단/오류 보고]%'
                  OR LTRIM(text) LIKE '[System Event] 서브 태스크%'
              );
        """)
        conn.commit()

    if migrate_legacy:
        migrate_legacy_files_if_needed(db_path)
    cleanup_duplicate_rooms(db_path)


def cleanup_duplicate_rooms(db_path: str | None = None) -> int:
    """동일한 session_id 를 갖는 중복 방들을 대표 방 1개로 통합하고, 메시지 없는 오래된 빈 방을 정리한다."""
    merged_count = 0
    try:
        with get_connection(db_path) as conn:
            cur = conn.cursor()
            # 1. 동일한 session_id 를 가진 방 그룹 조회
            cur.execute("""
                SELECT session_id, COUNT(*) as cnt
                FROM rooms
                WHERE session_id IS NOT NULL AND TRIM(session_id) != ''
                GROUP BY session_id
                HAVING cnt > 1;
            """)
            dups = cur.fetchall()

            for d in dups:
                sid = d["session_id"]
                cur.execute(
                    "SELECT * FROM rooms WHERE session_id = ? ORDER BY (id = '__core__') DESC, (id = session_id) DESC, updated_at DESC;",
                    (sid,),
                )
                matching = cur.fetchall()
                if len(matching) <= 1:
                    continue

                canonical = matching[0]
                canon_id = canonical["id"]

                # 나머지 중복 방들을 canonical_id 로 통합
                for dup in matching[1:]:
                    dup_id = dup["id"]
                    cur.execute("UPDATE messages SET room_id = ? WHERE room_id = ?;", (canon_id, dup_id))
                    if (canonical["title"] in ("새 대화", "대화방") or not canonical["title"]) and dup["title"]:
                        cur.execute("UPDATE rooms SET title = ? WHERE id = ?;", (dup["title"], canon_id))
                    if not canonical["preview"] and dup["preview"]:
                        cur.execute("UPDATE rooms SET preview = ? WHERE id = ?;", (dup["preview"], canon_id))
                    cur.execute("DELETE FROM rooms WHERE id = ?;", (dup_id,))
                    merged_count += 1

            # 2. 백업 반복 동기화로 누적된 중복 메시지 정리 (동일 room_id 내 동일 role·text 중복 제거, steps_json 보유분 우선 보존)
            # 주의: text 가 NULL 이거나 빈 문자열('')인 플레이스홀더/실행 중 메시지는 서로 다른 턴의 독립된 메시지이므로
            # 중복으로 간주하여 삭제하면 절대 안 됨 (TRIM(text) != '' 필수 가드).
            cur.execute("""
                DELETE FROM messages
                WHERE text IS NOT NULL
                  AND TRIM(text) != ''
                  AND id NOT IN (
                    SELECT id FROM (
                        SELECT id,
                               ROW_NUMBER() OVER (
                                   PARTITION BY room_id, role, text
                                   ORDER BY (steps_json IS NOT NULL AND steps_json != '[]') DESC, id ASC
                               ) AS rn
                        FROM messages
                        WHERE text IS NOT NULL AND TRIM(text) != ''
                    )
                    WHERE rn = 1
                );
            """)

            # 3. 메시지가 0개이고 제목이 '새 대화'인 오래된 방 정리
            cur.execute("""
                DELETE FROM rooms
                WHERE is_hidden = 0
                  AND (SELECT COUNT(*) FROM messages m WHERE m.room_id = rooms.id) = 0
                  AND title IN ('새 대화', '대화방', '')
                  AND (strftime('%s', 'now') - created_at) > 1800;
            """)

            # 4. 숨김(삭제) 처리된 방이 DB에 남아있으면 완전 삭제 (운영 DB 한정)
            if db_path is None or db_path == CHAT_DB_FILE:
                if os.path.isfile(HIDDEN_ROOMS_FILE):
                    try:
                        with open(HIDDEN_ROOMS_FILE, "r", encoding="utf-8") as f:
                            hidden_data = json.load(f)
                        if isinstance(hidden_data, list):
                            hidden_ids = [str(x).strip() for x in hidden_data if str(x).strip() and str(x).strip() != "__core__"]
                            if hidden_ids:
                                placeholders = ",".join("?" for _ in hidden_ids)
                                cur.execute(
                                    f"SELECT id FROM rooms WHERE id IN ({placeholders}) OR session_id IN ({placeholders});",
                                    hidden_ids + hidden_ids,
                                )
                                for hrow in cur.fetchall():
                                    hid = hrow["id"]
                                    cur.execute("DELETE FROM messages WHERE room_id = ?;", (hid,))
                                    cur.execute("DELETE FROM rooms WHERE id = ?;", (hid,))
                    except Exception:
                        pass

            conn.commit()
            if merged_count > 0:
                logger.info("SQLite 중복 대화방 정리 완료: %d개 병합", merged_count)
    except Exception as exc:
        logger.warning("대화방 중복 정리 중 오류: %s", exc)
    return merged_count


def migrate_legacy_files_if_needed(db_path: str | None = None) -> None:
    """기존 JSON/JSONL 파일에 저장된 대화 데이터가 있으면 chat.db 로 1회 자동 마이그레이션한다."""
    try:
        with get_connection(db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) AS cnt FROM rooms;")
            row = cur.fetchone()
            if row and row["cnt"] > 0:
                # 이미 데이터가 있으면 마이그레이션 생략
                return

        hidden_rooms: set[str] = set()
        if os.path.isfile(HIDDEN_ROOMS_FILE):
            try:
                with open(HIDDEN_ROOMS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        hidden_rooms = set(str(x) for x in data if x)
            except Exception:
                pass

        legacy_rooms: list[dict] = []
        if os.path.isfile(CHAT_ROOMS_FILE):
            try:
                with open(CHAT_ROOMS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        legacy_rooms = [r for r in data if isinstance(r, dict)]
            except Exception as exc:
                logger.warning("레거시 chat_rooms.json 로드 실패: %s", exc)

        with get_connection(db_path) as conn:
            imported_ids = set()
            for r in legacy_rooms:
                rid = str(r.get("id") or r.get("roomId") or "").strip()
                if not rid:
                    continue
                sid = str(r.get("sessionId") or "").strip() or None
                title = str(r.get("title") or "새 대화")
                preview = str(r.get("preview") or "")
                updated_at = _parse_timestamp(r.get("updatedAt"), default=time.time())
                created_at = _parse_timestamp(r.get("createdAt"), default=updated_at)
                is_hidden = 1 if (rid in hidden_rooms or (sid and sid in hidden_rooms)) else 0
                conn.execute(
                    """
                    INSERT OR REPLACE INTO rooms (id, session_id, title, preview, created_at, updated_at, last_channel, is_hidden)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (rid, sid, title, preview, created_at, updated_at, "web", is_hidden),
                )
                imported_ids.add(rid)

            # chats/*.jsonl 스캔하여 메시지 복원
            if os.path.isdir(CHATS_DIR):
                for fname in os.listdir(CHATS_DIR):
                    if not fname.endswith(".jsonl"):
                        continue
                    rid = fname[:-6]
                    chat_path = os.path.join(CHATS_DIR, fname)
                    if not os.path.isfile(chat_path):
                        continue
                    try:
                        mtime = os.path.getmtime(chat_path)
                    except OSError:
                        mtime = time.time()

                    msgs: list[dict] = []
                    try:
                        with open(chat_path, "r", encoding="utf-8", errors="replace") as f:
                            for line in f:
                                line = line.strip()
                                if not line:
                                    continue
                                try:
                                    msgs.append(json.loads(line))
                                except Exception:
                                    pass
                    except Exception:
                        continue

                    if not msgs:
                        continue

                    first_user = next((m for m in msgs if m.get("type") == "user" or m.get("role") == "user"), None)
                    last_asst = next((m for m in reversed(msgs) if m.get("type") == "assistant" or m.get("role") == "assistant"), None)
                    title = first_user.get("text", "새 대화")[:24] if first_user else "새 대화"
                    preview = last_asst.get("text", "")[:60] if last_asst else ""
                    sid = (last_asst.get("sessionId") if last_asst else None) or (first_user.get("sessionId") if first_user else None)

                    if rid not in imported_ids:
                        is_hidden = 1 if (rid in hidden_rooms or (sid and sid in hidden_rooms)) else 0
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO rooms (id, session_id, title, preview, created_at, updated_at, last_channel, is_hidden)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                            """,
                            (rid, sid, title, preview, mtime, mtime, "web", is_hidden),
                        )
                        imported_ids.add(rid)

                    for m in msgs:
                        role = m.get("role") or m.get("type") or "user"
                        text = m.get("text") or ""
                        steps = m.get("steps") or []
                        steps_json = json.dumps(steps, ensure_ascii=False) if isinstance(steps, list) else "[]"
                        model = m.get("model")
                        cost = float(m["cost"]) if m.get("cost") is not None else None
                        msg_sid = m.get("sessionId") or sid
                        created_at = _parse_timestamp(m.get("createdAt"), default=mtime)
                        conn.execute(
                            """
                            INSERT INTO messages (room_id, role, channel, text, steps_json, model, cost, session_id, created_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                            """,
                            (rid, role, "web", text, steps_json, model, cost, msg_sid, created_at),
                        )

            conn.commit()
            logger.info("레거시 대화 데이터 SQLite 마이그레이션 완료 (방 %d개)", len(imported_ids))
    except Exception as exc:
        logger.warning("레거시 대화 데이터 마이그레이션 중 오류: %s", exc)


def get_or_create_room(
    room_id: str,
    session_id: str | None = None,
    title: str | None = None,
    channel: str = "web",
    model: str | None = None,
    effort: str | None = None,
    workdir: str | None = None,
    parent_agent_id: str | None = None,
    add_dirs: list[str] | str | None = None,
    db_path: str | None = None,
) -> dict:
    """방 ID 또는 세션 ID로 대화방을 조회하고, 없으면 생성하여 반환한다."""
    now = time.time()
    clean_rid = str(room_id).strip()
    clean_sid = str(session_id).strip() if session_id else None
    clean_model = str(model).strip() if model else None
    clean_effort = str(effort).strip() if effort else None
    clean_workdir = str(workdir).strip() if workdir else None
    clean_parent = str(parent_agent_id).strip() if parent_agent_id else None
    clean_add_dirs = None
    if add_dirs is not None:
        if isinstance(add_dirs, (list, tuple)):
            cl = [str(d).strip() for d in add_dirs if str(d).strip()]
            clean_add_dirs = json.dumps(cl, ensure_ascii=False) if cl else None
        elif isinstance(add_dirs, str):
            cl = [d.strip() for d in add_dirs.splitlines() if d.strip() and not d.strip().startswith("#")]
            clean_add_dirs = json.dumps(cl, ensure_ascii=False) if cl else None

    with get_connection(db_path) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM rooms WHERE id = ? OR (session_id IS NOT NULL AND session_id = ?) ORDER BY (id = ?) DESC LIMIT 1;",
            (clean_rid, clean_sid or clean_rid, clean_rid),
        )
        row = cur.fetchone()
        if row:
            updates = []
            params = []
            if clean_sid and row["session_id"] != clean_sid:
                updates.append("session_id = ?")
                params.append(clean_sid)
            if title and title not in ("새 대화", "대화방") and row["title"] in ("새 대화", "대화방", ""):
                updates.append("title = ?")
                params.append(title)
            if clean_model is not None:
                updates.append("model = ?")
                params.append(clean_model)
            if clean_effort is not None:
                updates.append("effort = ?")
                params.append(clean_effort)
            if clean_workdir is not None:
                updates.append("workdir = ?")
                params.append(clean_workdir)
            if clean_parent is not None:
                updates.append("parent_agent_id = ?")
                params.append(clean_parent)
            if clean_add_dirs is not None:
                updates.append("add_dirs = ?")
                params.append(clean_add_dirs)
            if updates:
                params.append(row["id"])
                conn.execute(f"UPDATE rooms SET {', '.join(updates)} WHERE id = ?;", params)
                conn.commit()
            cur.execute("SELECT * FROM rooms WHERE id = ?;", (row["id"],))
            updated_row = cur.fetchone()
            return dict(updated_row) if updated_row else dict(row)

        init_title = title or "새 대화"
        conn.execute(
            """
            INSERT INTO rooms (id, session_id, title, preview, created_at, updated_at, last_channel, is_hidden, is_archived, model, effort, workdir, parent_agent_id, add_dirs)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?, ?);
            """,
            (clean_rid, clean_sid, init_title, "", now, now, channel, clean_model, clean_effort, clean_workdir, clean_parent, clean_add_dirs),
        )
        conn.commit()
        cur.execute("SELECT * FROM rooms WHERE id = ?;", (clean_rid,))
        new_row = cur.fetchone()
        return dict(new_row) if new_row else {
            "id": clean_rid,
            "session_id": clean_sid,
            "title": init_title,
            "preview": "",
            "created_at": now,
            "updated_at": now,
            "last_channel": channel,
            "is_hidden": 0,
            "is_archived": 0,
            "model": clean_model,
            "effort": clean_effort,
            "workdir": clean_workdir,
            "parent_agent_id": clean_parent,
            "add_dirs": clean_add_dirs,
        }


_UNSET = object()


def update_room(
    room_id: str,
    title: str | None = None,
    preview: str | None = None,
    session_id: str | None = None,
    channel: str | None = None,
    model: Any = _UNSET,
    effort: Any = _UNSET,
    workdir: Any = _UNSET,
    parent_agent_id: Any = _UNSET,
    add_dirs: Any = _UNSET,
    is_archived: bool | None = None,
    updated_at: float | None = None,
    force_title: bool = True,
    db_path: str | None = None,
) -> None:
    """방 메타데이터를 갱신한다."""
    if not room_id:
        return
    now = _parse_timestamp(updated_at, default=time.time())
    clean_target = str(room_id).strip()
    clean_sid = str(session_id).strip() if session_id else None

    with get_connection(db_path) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, title, session_id FROM rooms WHERE id = ? OR session_id = ? LIMIT 1;",
            (clean_target, clean_target),
        )
        row = cur.fetchone()
        if not row:
            return

        actual_id = row["id"]
        updates = ["updated_at = ?"]
        params: list[Any] = [now]

        if title:
            existing_title = (row["title"] or "").strip()
            if force_title or existing_title in ("새 대화", "대화방", "") or actual_id == "__core__":
                updates.append("title = ?")
                params.append(title)
        if preview is not None:
            updates.append("preview = ?")
            params.append(preview)
        if clean_sid:
            updates.append("session_id = ?")
            params.append(clean_sid)
        if channel:
            updates.append("last_channel = ?")
            params.append(channel)
        if model is not _UNSET:
            clean_m = str(model).strip() if (model and str(model).strip()) else None
            updates.append("model = ?")
            params.append(clean_m)
        if effort is not _UNSET:
            clean_e = str(effort).strip() if (effort and str(effort).strip()) else None
            updates.append("effort = ?")
            params.append(clean_e)
        if workdir is not _UNSET:
            clean_w = str(workdir).strip() if (workdir and str(workdir).strip()) else None
            updates.append("workdir = ?")
            params.append(clean_w)
        if parent_agent_id is not _UNSET:
            clean_p = str(parent_agent_id).strip() if (parent_agent_id and str(parent_agent_id).strip()) else None
            updates.append("parent_agent_id = ?")
            params.append(clean_p)
        if add_dirs is not _UNSET:
            updates.append("add_dirs = ?")
            if add_dirs is None:
                params.append(None)
            elif isinstance(add_dirs, (list, tuple)):
                clean_list = [str(d).strip() for d in add_dirs if str(d).strip()]
                params.append(json.dumps(clean_list, ensure_ascii=False) if clean_list else None)
            elif isinstance(add_dirs, str):
                parsed = [d.strip() for d in add_dirs.splitlines() if d.strip() and not d.strip().startswith("#")]
                params.append(json.dumps(parsed, ensure_ascii=False) if parsed else None)
            else:
                params.append(None)
        if is_archived is not None:
            updates.append("is_archived = ?")
            params.append(1 if is_archived else 0)

        params.append(actual_id)
        conn.execute(
            f"UPDATE rooms SET {', '.join(updates)} WHERE id = ?;",
            params,
        )

        # 만약 clean_sid와 동일한 id를 가진 별도 중복 방이 존재한다면 actual_id로 즉시 병합
        if clean_sid and clean_sid != actual_id:
            cur.execute(
                "SELECT id FROM rooms WHERE (id = ? OR session_id = ?) AND id != ?;",
                (clean_sid, clean_sid, actual_id),
            )
            for dup in cur.fetchall():
                dup_id = dup["id"]
                conn.execute("UPDATE messages SET room_id = ? WHERE room_id = ?;", (actual_id, dup_id))
                conn.execute("DELETE FROM rooms WHERE id = ?;", (dup_id,))

        conn.commit()


def add_message(
    room_id: str,
    role: str,
    text: str,
    channel: str = "web",
    steps: list | None = None,
    attachments: list | None = None,
    model: str | None = None,
    effort: str | None = None,
    cost: float | None = None,
    session_id: str | None = None,
    created_at: float | None = None,
    db_path: str | None = None,
) -> int:
    """메시지를 추가하고 해당 대화방의 메타데이터(갱신시각, 미리보기, 채널 등)를 자동 갱신한다."""
    clean_rid = str(room_id).strip()
    if not clean_rid:
        return -1
    now = _parse_timestamp(created_at, default=time.time())
    steps_json = json.dumps(steps, ensure_ascii=False) if isinstance(steps, list) else "[]"
    attachments_json = (
        json.dumps(attachments, ensure_ascii=False) if isinstance(attachments, list) else "[]"
    )
    clean_sid = str(session_id).strip() if session_id else None
    resolved_role = _resolve_message_role(role, channel, text)

    # 방이 없으면 자동 생성 또는 기존 매칭 방 조회
    room = get_or_create_room(clean_rid, session_id=clean_sid, channel=channel, db_path=db_path)
    actual_rid = room["id"] if room else clean_rid

    with get_connection(db_path) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO messages (room_id, role, channel, text, steps_json, attachments_json, model, effort, cost, session_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (actual_rid, resolved_role, channel, text, steps_json, attachments_json, model, effort, cost, clean_sid, now),
        )
        msg_id = cur.lastrowid

        # 방 메타데이터 업데이트: assistant 답변이면 (빈 플레이스홀더 제외) preview 갱신, user 첫 발화면 title 갱신
        if resolved_role == "assistant":
            preview = text.strip()[:60] or None
            conn.execute(
                """
                UPDATE rooms
                SET preview = COALESCE(?, preview), updated_at = ?, last_channel = ?, session_id = COALESCE(?, session_id)
                WHERE id = ?;
                """,
                (preview, now, channel, clean_sid, actual_rid),
            )
        else:
            cur.execute("SELECT title FROM rooms WHERE id = ?;", (actual_rid,))
            r = cur.fetchone()
            new_title = None
            if r and (r["title"] in ("새 대화", "대화방") or not r["title"]):
                new_title = text.strip().replace("\n", " ")[:24]

            if new_title:
                conn.execute(
                    """
                    UPDATE rooms
                    SET title = ?, updated_at = ?, last_channel = ?, session_id = COALESCE(?, session_id)
                    WHERE id = ?;
                    """,
                    (new_title, now, channel, clean_sid, actual_rid),
                )
            else:
                conn.execute(
                    """
                    UPDATE rooms
                    SET updated_at = ?, last_channel = ?, session_id = COALESCE(?, session_id)
                    WHERE id = ?;
                    """,
                    (now, channel, clean_sid, actual_rid),
                )

        conn.commit()
        return msg_id or 0


def update_message(
    message_id: int,
    *,
    text: str | None = None,
    steps: list | None = None,
    model: str | None = None,
    effort: str | None = None,
    cost: float | None = None,
    session_id: str | None = None,
    created_at: float | None = None,
    update_preview: bool = True,
    db_path: str | None = None,
) -> bool:
    """진행 중인 메시지 레코드(도구 실행 단계 및 스트리밍 텍스트)를 실시간으로 부분 갱신한다."""
    if not message_id or int(message_id) <= 0:
        return False
    updates = []
    params = []
    if text is not None:
        updates.append("text = ?")
        params.append(text)
    if steps is not None:
        updates.append("steps_json = ?")
        params.append(json.dumps(steps, ensure_ascii=False) if isinstance(steps, list) else "[]")
    if model is not None:
        updates.append("model = ?")
        params.append(model)
    if effort is not None:
        updates.append("effort = ?")
        params.append(effort)
    if cost is not None:
        updates.append("cost = ?")
        params.append(cost)
    if session_id is not None:
        updates.append("session_id = ?")
        params.append(session_id)
    if created_at is not None:
        updates.append("created_at = ?")
        params.append(_parse_timestamp(created_at, default=time.time()))
    if not updates:
        return False

    with get_connection(db_path) as conn:
        cur = conn.cursor()
        cur.execute("SELECT room_id FROM messages WHERE id = ?;", (int(message_id),))
        row = cur.fetchone()
        if not row:
            return False
        room_id = row["room_id"]

        params.append(int(message_id))
        conn.execute(f"UPDATE messages SET {', '.join(updates)} WHERE id = ?;", params)

        if update_preview and text:
            now = time.time()
            preview = text.strip()[:60]
            conn.execute(
                "UPDATE rooms SET preview = ?, updated_at = ? WHERE id = ?;",
                (preview, now, room_id),
            )
        conn.commit()
    return True


def _normalize_room_dict_payload(d: dict) -> dict:
    d["isNewSession"] = not bool(d.get("sessionId"))
    d["isArchived"] = bool(d.get("isArchived"))
    d["isOrchestratorChild"] = bool(d.get("isOrchestratorChild"))
    if d["isOrchestratorChild"] and not d.get("parentAgentId"):
        d["parentAgentId"] = "__core__"
    raw_ad = d.get("addDirs")
    if isinstance(raw_ad, str) and raw_ad.strip():
        try:
            d["addDirs"] = json.loads(raw_ad)
        except Exception:
            d["addDirs"] = [p.strip() for p in raw_ad.splitlines() if p.strip()]
    elif not isinstance(raw_ad, list):
        d["addDirs"] = None
    return d


def _normalize_room_rows(rows: list[sqlite3.Row], limit: int) -> list[dict]:
    """SQLite rooms 조회 행을 정규화하고 빈 방 및 sessionId/id 중복을 제거한다."""
    result = []
    seen_keys = set()
    for row in rows:
        d = _normalize_room_dict_payload(dict(row))
        sid = (d.get("sessionId") or "").strip()
        rid = (d.get("id") or "").strip()

        # 메시지 0건이고 제목이 '새 대화'인 빈 방은 제외
        if d.get("messageCount", 0) == 0 and d.get("title") in ("새 대화", "대화방"):
            continue

        # sessionId 또는 id 기준 중복 제거
        if sid and sid in seen_keys:
            continue
        if rid and rid in seen_keys:
            continue

        if sid:
            seen_keys.add(sid)
        if rid:
            seen_keys.add(rid)

        result.append(d)
        if len(result) >= limit:
            break
    return result


def _extract_match_snippet(text: str, query: str, radius: int = 32) -> str:
    """검색어 주변 컨텍스트 스니펫을 추출한다."""
    clean = " ".join((text or "").split())
    if not clean:
        return ""
    idx = clean.lower().find(query.lower())
    if idx < 0:
        return clean[:80]
    start = max(0, idx - radius)
    end = min(len(clean), idx + len(query) + radius)
    snippet = clean[start:end]
    if start > 0:
        snippet = "…" + snippet
    if end < len(clean):
        snippet = snippet + "…"
    return snippet


def get_rooms(
    limit: int = 50,
    include_hidden: bool = False,
    include_archived: bool = True,
    archived_only: bool = False,
    db_path: str | None = None,
) -> list[dict]:
    """최신 갱신순으로 대화방 목록을 반환한다 (sessionId 중복 방지 및 0건 빈방 필터링)."""
    with get_connection(db_path) as conn:
        cur = conn.cursor()
        where_clauses: list[str] = []
        if not include_hidden:
            where_clauses.append("r.is_hidden = 0")
        if archived_only:
            where_clauses.append("r.is_archived = 1")
        elif not include_archived:
            where_clauses.append("r.is_archived = 0")

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        query = f"""
            SELECT
                r.id,
                r.id AS roomId,
                r.session_id AS sessionId,
                r.title,
                r.preview,
                r.created_at AS createdAt,
                r.updated_at AS updatedAt,
                r.last_channel AS lastChannel,
                r.is_hidden AS isHidden,
                COALESCE(r.is_archived, 0) AS isArchived,
                r.model,
                r.effort,
                r.workdir,
                r.parent_agent_id AS parentAgentId,
                (SELECT COUNT(*) FROM messages m WHERE m.room_id = r.id) AS messageCount,
                (r.parent_agent_id IS NOT NULL OR EXISTS(SELECT 1 FROM messages m WHERE m.room_id = r.id AND m.channel = 'orchestrator')) AS isOrchestratorChild
            FROM rooms r
            {where_sql}
            ORDER BY r.updated_at DESC
            LIMIT ?;
        """
        cur.execute(query, (limit * 2,))
        return _normalize_room_rows(cur.fetchall(), limit)


def search_rooms(
    query_text: str,
    limit: int = 100,
    include_hidden: bool = False,
    include_archived: bool = True,
    db_path: str | None = None,
) -> list[dict]:
    """대화방 제목·미리보기·메시지 본문 전체를 검색해 매칭된 대화방 목록을 반환한다."""
    q = (query_text or "").strip()
    if not q:
        return get_rooms(
            limit=limit,
            include_hidden=include_hidden,
            include_archived=include_archived,
            db_path=db_path,
        )

    q_lower = q.lower()
    with get_connection(db_path) as conn:
        cur = conn.cursor()
        where_clauses: list[str] = [
            "("
            "INSTR(LOWER(COALESCE(r.title, '')), ?) > 0 "
            "OR INSTR(LOWER(COALESCE(r.preview, '')), ?) > 0 "
            "OR EXISTS (SELECT 1 FROM messages m WHERE m.room_id = r.id AND INSTR(LOWER(COALESCE(m.text, '')), ?) > 0)"
            ")"
        ]
        params: list[Any] = [q_lower, q_lower, q_lower, q_lower]
        if not include_hidden:
            where_clauses.append("r.is_hidden = 0")
        if not include_archived:
            where_clauses.append("r.is_archived = 0")

        where_sql = f"WHERE {' AND '.join(where_clauses)}"
        sql = f"""
            SELECT
                r.id,
                r.id AS roomId,
                r.session_id AS sessionId,
                r.title,
                r.preview,
                r.created_at AS createdAt,
                r.updated_at AS updatedAt,
                r.last_channel AS lastChannel,
                r.is_hidden AS isHidden,
                COALESCE(r.is_archived, 0) AS isArchived,
                r.model,
                r.effort,
                r.workdir,
                r.parent_agent_id AS parentAgentId,
                (SELECT COUNT(*) FROM messages m WHERE m.room_id = r.id) AS messageCount,
                (r.parent_agent_id IS NOT NULL OR EXISTS(SELECT 1 FROM messages m WHERE m.room_id = r.id AND m.channel = 'orchestrator')) AS isOrchestratorChild,
                (
                    SELECT m.text
                    FROM messages m
                    WHERE m.room_id = r.id AND INSTR(LOWER(COALESCE(m.text, '')), ?) > 0
                    ORDER BY m.id DESC
                    LIMIT 1
                ) AS matchedText
            FROM rooms r
            {where_sql}
            ORDER BY r.updated_at DESC
            LIMIT ?;
        """
        # SELECT 절 서브쿼리의 ? 1개 + WHERE 절의 ? 3개 + LIMIT ? 1개
        params.append(limit * 2)
        cur.execute(sql, tuple(params))
        rooms = _normalize_room_rows(cur.fetchall(), limit)
        for room in rooms:
            matched_text = room.pop("matchedText", None)
            room["matchSnippet"] = _extract_match_snippet(matched_text or "", q) if matched_text else ""
        return rooms


_ROOM_SELECT_COLS = """
    r.id,
    r.id AS roomId,
    r.session_id AS sessionId,
    r.title,
    r.preview,
    r.created_at AS createdAt,
    r.updated_at AS updatedAt,
    r.last_channel AS lastChannel,
    r.is_hidden AS isHidden,
    COALESCE(r.is_archived, 0) AS isArchived,
    r.model,
    r.effort,
    r.workdir,
    r.parent_agent_id AS parentAgentId,
    r.add_dirs AS addDirs,
    (SELECT COUNT(*) FROM messages m WHERE m.room_id = r.id) AS messageCount,
    (r.parent_agent_id IS NOT NULL OR EXISTS(SELECT 1 FROM messages m WHERE m.room_id = r.id AND m.channel = 'orchestrator')) AS isOrchestratorChild
"""


def _row_to_room_dict(row: sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    return _normalize_room_dict_payload(dict(row))


def get_room(room_id_or_session_id: str, db_path: str | None = None) -> dict | None:
    """방 ID 또는 세션 ID로 단일 대화방 메타데이터를 조회한다."""
    target = str(room_id_or_session_id).strip()
    if not target:
        return None
    with get_connection(db_path) as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT {_ROOM_SELECT_COLS}
            FROM rooms r
            WHERE r.id = ? OR r.session_id = ?
            LIMIT 1;
            """,
            (target, target),
        )
        return _row_to_room_dict(cur.fetchone())


def archive_room(
    room_id_or_session_id: str,
    is_archived: bool = True,
    db_path: str | None = None,
) -> bool:
    """대화방을 아카이브(보관)하거나 보관 해제한다 (마스터 및 전문가 에이전트는 보관 불가)."""
    target = str(room_id_or_session_id).strip()
    if not target:
        return False
    # 보관 처리 시 보호 대상 룸은 보관 거부
    if is_archived and _is_protected_room_id(target):
        return False

    archived_val = 1 if is_archived else 0
    with get_connection(db_path) as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE rooms SET is_archived = ? WHERE id = ? OR session_id = ?;",
            (archived_val, target, target),
        )
        conn.commit()
        return cur.rowcount > 0


def archive_rooms_bulk(
    targets: list[str],
    is_archived: bool = True,
    db_path: str | None = None,
) -> int:
    """여러 대화방(방 ID 또는 세션 ID 목록)을 단일 트랜잭션으로 일괄 아카이브(보관) 또는 해제 처리한다 (마스터/전문가는 자동 제외)."""
    cleaned = [str(t).strip() for t in targets if t and str(t).strip()]
    cleaned = list(dict.fromkeys(cleaned))
    if not cleaned:
        return 0

    if is_archived:
        # 보관 처리 시 보호 대상 ID 제외
        cleaned = [t for t in cleaned if not _is_protected_room_id(t)]
        if not cleaned:
            return 0

        # 보호 대상 룸의 세션 ID도 제외
        protected_sids: set[str] = set()
        all_protected = set(PROTECTED_ROOM_IDS)
        try:
            import bot.core.agent_configs as agent_configs
            all_protected.update(agent_configs.get_orchestrator_room_ids())
        except Exception:
            pass
        for pid in all_protected:
            try:
                p_room = get_room(pid, db_path=db_path)
                if p_room and p_room.get("sessionId"):
                    protected_sids.add(str(p_room["sessionId"]).strip())
            except Exception:
                pass
        if protected_sids:
            cleaned = [t for t in cleaned if t not in protected_sids]
        if not cleaned:
            return 0

    archived_val = 1 if is_archived else 0
    placeholders = ", ".join("?" for _ in cleaned)
    params = [archived_val] + cleaned + cleaned
    with get_connection(db_path) as conn:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE rooms SET is_archived = ? WHERE id IN ({placeholders}) OR session_id IN ({placeholders});",
            params,
        )
        updated_count = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        conn.commit()
        return updated_count


def get_message(message_id: int, db_path: str | None = None) -> dict | None:
    """단일 메시지 ID로 메시지 레코드를 조회한다."""
    if not message_id or int(message_id) <= 0:
        return None
    with get_connection(db_path) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, room_id, role, channel, text, steps_json, attachments_json, model, effort, cost, session_id, created_at
            FROM messages
            WHERE id = ?;
            """,
            (int(message_id),),
        )
        row = cur.fetchone()
        if not row:
            return None
        steps = []
        if row["steps_json"]:
            try:
                steps = json.loads(row["steps_json"])
            except Exception:
                steps = []
        attachments = []
        try:
            raw_att = row["attachments_json"] if "attachments_json" in row.keys() else None
            if raw_att:
                attachments = json.loads(raw_att)
        except Exception:
            attachments = []
        resolved_role = _resolve_message_role(row["role"], row["channel"], row["text"])
        return {
            "id": row["id"],
            "roomId": row["room_id"],
            "role": resolved_role,
            "sender": resolved_role,
            "channel": row["channel"],
            "text": row["text"] or "",
            "steps": steps,
            "attachments": attachments,
            "model": row["model"],
            "effort": row["effort"] if "effort" in row.keys() else None,
            "cost": row["cost"],
            "sessionId": row["session_id"],
            "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(row["created_at"])),
        }


def get_messages(
    room_id_or_session_id: str,
    limit: int | None = None,
    db_path: str | None = None,
) -> list[dict]:
    """특정 방(또는 세션)의 대화 메시지 전체를 시간순으로 반환한다."""
    target = str(room_id_or_session_id).strip()
    if not target:
        return []

    room = get_room(target, db_path=db_path)
    if not room or room.get("isHidden") == 1:
        return []
    actual_room_id = room["id"]

    with get_connection(db_path) as conn:
        cur = conn.cursor()
        limit_clause = f"LIMIT {int(limit)}" if limit and limit > 0 else ""
        cur.execute(
            f"""
            SELECT id, room_id, role, channel, text, steps_json, attachments_json, model, effort, cost, session_id, created_at
            FROM messages
            WHERE room_id = ?
            ORDER BY id ASC
            {limit_clause};
            """,
            (actual_room_id,),
        )
        rows = cur.fetchall()
        messages = []
        for row in rows:
            steps = []
            if row["steps_json"]:
                try:
                    steps = json.loads(row["steps_json"])
                except Exception:
                    steps = []
            attachments = []
            try:
                raw_att = row["attachments_json"] if "attachments_json" in row.keys() else None
                if raw_att:
                    attachments = json.loads(raw_att)
            except Exception:
                attachments = []
            resolved_role = _resolve_message_role(row["role"], row["channel"], row["text"])
            messages.append({
                "id": row["id"],
                "role": resolved_role,
                "sender": resolved_role,
                "channel": row["channel"],
                "text": row["text"],
                "steps": steps,
                "attachments": attachments,
                "model": row["model"],
                "effort": row["effort"] if "effort" in row.keys() else None,
                "cost": row["cost"],
                "sessionId": row["session_id"],
                "createdAt": row["created_at"],
            })
        return messages


def clear_room_messages(
    room_id_or_session_id: str,
    db_path: str | None = None,
) -> int:
    """대화방의 모든 메시지를 삭제하고 방의 session_id를 초기화한다 (방 자체는 보존)."""
    target = str(room_id_or_session_id).strip()
    if not target:
        return 0
    room = get_room(target, db_path=db_path)
    if not room:
        return 0
    actual_room_id = room["id"]
    with get_connection(db_path) as conn:
        cursor = conn.execute("DELETE FROM messages WHERE room_id = ?;", (actual_room_id,))
        deleted_count = cursor.rowcount
        conn.execute(
            "UPDATE rooms SET session_id = NULL, updated_at = ? WHERE id = ?;",
            (time.time(), actual_room_id),
        )
    return deleted_count


def delete_room(
    room_id_or_session_id: str,
    hard_delete: bool = True,
    db_path: str | None = None,
    allow_custom_agent: bool = False,
) -> bool:
    """대화방을 삭제(기본: 완전 삭제, 외래키로 메시지도 함께 삭제) 처리한다 (마스터 및 전문가 에이전트는 삭제 불가)."""
    target = str(room_id_or_session_id).strip()
    if not target or target in PROTECTED_ROOM_IDS or (not allow_custom_agent and _is_protected_room_id(target)):
        return False

    room = get_room(target, db_path=db_path)
    if not room:
        return False
    actual_room_id = room["id"]
    if actual_room_id in PROTECTED_ROOM_IDS or (not allow_custom_agent and _is_protected_room_id(actual_room_id)):
        return False

    with get_connection(db_path) as conn:
        if hard_delete:
            conn.execute("DELETE FROM rooms WHERE id = ?;", (actual_room_id,))
        else:
            conn.execute("UPDATE rooms SET is_hidden = 1 WHERE id = ?;", (actual_room_id,))
        conn.commit()
        return True


def delete_rooms_bulk(
    targets: list[str],
    hard_delete: bool = True,
    db_path: str | None = None,
) -> int:
    """여러 대화방(방 ID 또는 세션 ID 목록)을 단일 트랜잭션으로 일괄 삭제 처리한다 (마스터 및 전문가 에이전트는 삭제 불가)."""
    cleaned = [str(t).strip() for t in targets if t and str(t).strip() and not _is_protected_room_id(str(t).strip())]
    cleaned = list(dict.fromkeys(cleaned))
    if not cleaned:
        return 0

    # 보호 대상 룸의 세션 ID도 삭제 대상에서 제외
    protected_sids: set[str] = set()
    all_protected = set(PROTECTED_ROOM_IDS)
    try:
        import bot.core.agent_configs as agent_configs
        all_protected.update(agent_configs.get_orchestrator_room_ids())
    except Exception:
        pass
    for pid in all_protected:
        try:
            p_room = get_room(pid, db_path=db_path)
            if p_room and p_room.get("sessionId"):
                protected_sids.add(str(p_room["sessionId"]).strip())
        except Exception:
            pass
    if protected_sids:
        cleaned = [t for t in cleaned if t not in protected_sids]
    if not cleaned:
        return 0

    placeholders = ", ".join("?" for _ in cleaned)
    params = cleaned + cleaned
    with get_connection(db_path) as conn:
        cur = conn.cursor()
        if hard_delete:
            cur.execute(
                f"DELETE FROM rooms WHERE id IN ({placeholders}) OR session_id IN ({placeholders});",
                params,
            )
        else:
            cur.execute(
                f"UPDATE rooms SET is_hidden = 1 WHERE id IN ({placeholders}) OR session_id IN ({placeholders});",
                params,
            )
        deleted_count = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        conn.commit()
        return deleted_count
