"""turn_title — 독립 Gemini API(gemini-3.8-flash)를 사용한 턴 제목 동적 요약 모듈."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import ssl
import urllib.request
from typing import Any, Awaitable, Callable

logger = logging.getLogger("osh-bot.turn_title")

_DEFAULT_MODEL = "gemini-3.8-flash"
_KEY_FILES = (
    os.path.expanduser("~/.config/gemini/api_key"),
    os.path.expanduser("~/.gemini/api_key"),
    os.path.expanduser("~/obsidian/oshyun/개인/비밀번호/Gemini API 키.md"),
)
_SYSTEM_PROMPT = (
    "대화 내용을 읽고 핵심 주제를 자연스러운 한국어 명사구 한 줄(10~22자 이내)로 요약하세요. "
    "따옴표, 마침표, 불릿, 부연 설명 없이 제목 텍스트만 출력하세요."
)


def resolve_gemini_api_key() -> str | None:
    """환경변수 또는 로컬 키 파일/볼트 문서에서 Gemini API 키를 조회한다."""
    env_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if env_key:
        return env_key

    for key_file in _KEY_FILES:
        if not os.path.exists(key_file):
            continue
        try:
            with open(key_file, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if "\n" not in content and len(content) >= 30 and ("AQ." in content or content.startswith("AIza")):
                return content
            match = re.search(r"`(AQ\.[A-Za-z0-9_-]+|AIza[A-Za-z0-9_-]{35})`", content)
            if match:
                return match.group(1).strip()
            match = re.search(r"\b(AQ\.[A-Za-z0-9_-]{30,}|AIzaSy[A-Za-z0-9_-]{33})\b", content)
            if match:
                return match.group(1).strip()
        except Exception as e:
            logger.debug("[turn_title] 키 파일 읽기 실패 (%s): %s", key_file, e)
    return None


def _build_ssl_context() -> ssl.SSLContext:
    """certifi 번들을 우선 로드해 macOS SSL 검증 오류를 방지한다."""
    ctx = ssl.create_default_context()
    try:
        import certifi
        ca_path = certifi.where()
        if ca_path and os.path.exists(ca_path):
            ctx.load_verify_locations(cafile=ca_path)
    except Exception:
        pass
    return ctx


def _build_turn_transcript(messages_for_group: list[dict[str, Any]]) -> str:
    """턴 내 최근 발화들을 저토큰 요약용 텍스트로 압축한다."""
    lines: list[str] = []
    for m in messages_for_group[-6:]:
        role = str(m.get("role") or m.get("sender") or "assistant").strip()
        text = str(m.get("text") or "").strip()
        if not text or text.startswith("⚠️"):
            continue
        compact = re.sub(r"\s+", " ", text)[:300]
        label = "사용자" if role == "user" else "어시스턴트"
        lines.append(f"{label}: {compact}")
    return "\n".join(lines)


def _clean_title(raw: str) -> str:
    """모델 응답에서 따옴표/개행/불필요 문자를 제거하고 길이를 제한한다."""
    line = (raw or "").strip().splitlines()[0] if (raw or "").strip() else ""
    line = line.strip(" \"'`“”‘’.-•*")
    line = re.sub(r"\s+", " ", line).strip()
    if len(line) > 36:
        line = line[:35].rstrip() + "…"
    return line


def summarize_turn_title_sync(
    messages_for_group: list[dict[str, Any]],
    model: str = _DEFAULT_MODEL,
    api_key: str | None = None,
) -> str | None:
    """독립 Gemini API를 동기 호출해 턴 제목을 생성한다."""
    key = api_key or resolve_gemini_api_key()
    if not key:
        return None
    transcript = _build_turn_transcript(messages_for_group)
    if not transcript:
        return None

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {
        "systemInstruction": {"parts": [{"text": _SYSTEM_PROMPT}]},
        "contents": [{"parts": [{"text": transcript}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 48,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-goog-api-key": key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, context=_build_ssl_context(), timeout=12) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        candidates = body.get("candidates") or []
        if not candidates:
            return None
        parts = ((candidates[0].get("content") or {}).get("parts")) or []
        raw_text = "".join(str(p.get("text") or "") for p in parts if not p.get("thought"))
        title = _clean_title(raw_text)
        return title or None
    except Exception as e:
        logger.debug("[turn_title] Gemini 제목 요약 실패: %s", e)
        return None


def schedule_turn_title_update(
    room_id: str,
    group_id: str | None,
    chat_db_mod: Any,
    broadcast_fn: Callable[[dict[str, Any]], Awaitable[Any]] | None,
) -> None:
    """턴 완료 후 비동기로 턴 제목을 생성·저장·브로드캐스트한다."""
    if not room_id or not group_id or not chat_db_mod:
        return
    if not hasattr(chat_db_mod, "set_turn_title"):
        return

    async def _run() -> None:
        try:
            all_msgs = chat_db_mod.get_messages(room_id)
            group_msgs = [
                m for m in (all_msgs or [])
                if (m.get("groupId") or m.get("group_id")) == group_id
            ]
            if not group_msgs:
                return
            title = await asyncio.to_thread(summarize_turn_title_sync, group_msgs)
            if not title:
                return
            chat_db_mod.set_turn_title(group_id, room_id, title)
            if broadcast_fn:
                await broadcast_fn({
                    "type": "turn_title",
                    "roomId": room_id,
                    "groupId": group_id,
                    "title": title,
                })
        except Exception as e:
            logger.debug("[turn_title] 백그라운드 제목 갱신 오류: %s", e)

    try:
        asyncio.create_task(_run())
    except RuntimeError:
        pass
