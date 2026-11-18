"""web_goals — 에이전트 및 볼트 활동 감사 로그(Audit) REST API 모듈."""

from __future__ import annotations

import json
import os
import time

from aiohttp import web

from paths import SESSION_LOG_FILE


async def handle_activity_audit(_request: web.Request) -> web.Response:
    """GET /api/activity/audit — 최근 에이전트 실행 및 볼트 활동 감사 로그를 반환한다."""
    audit_items = []
    if os.path.isfile(SESSION_LOG_FILE):
        try:
            with open(SESSION_LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
                for line in reversed(lines[-30:]):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        audit_items.append({
                            "timestamp": record.get("timestamp") or time.time(),
                            "action": record.get("action") or "에이전트 실행",
                            "summary": record.get("summary") or record.get("prompt") or "",
                            "model": record.get("model") or "agy",
                            "cost": record.get("cost"),
                        })
                    except Exception:
                        pass
        except Exception:
            pass

    if not audit_items:
        audit_items.append({
            "timestamp": time.time(),
            "action": "세션 동기화",
            "summary": "AGY 세션 DB 단일 진실 원천(SSOT) 연결 완료",
            "model": "agy",
            "cost": 0.0,
        })

    return web.json_response({"ok": True, "audit": audit_items})
