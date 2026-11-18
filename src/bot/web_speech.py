"""web_speech — 웹/음성 비서 TTS 발화 정제 및 edge-tts 음성 합성 모듈."""

from __future__ import annotations

import os
import re
import sys
from aiohttp import web
import edge_tts

from bot_logger import logger

AVAILABLE_TTS_VOICES: tuple[dict[str, str], ...] = (
    {"id": "ko-KR-SunHiNeural", "label": "선희 (여성 · 자연스러운 한국어)"},
    {"id": "ko-KR-InJoonNeural", "label": "인준 (남성 · 차분한 한국어)"},
    {"id": "ko-KR-HyunsuMultilingualNeural", "label": "현수 (남성 · 다국어)"},
)
VALID_TTS_VOICE_IDS: frozenset[str] = frozenset(
    voice_item["id"] for voice_item in AVAILABLE_TTS_VOICES
)
DEFAULT_TTS_VOICE = "ko-KR-SunHiNeural"
VALID_TTS_MODES = frozenset({"voice_only", "always", "off"})
DEFAULT_TTS_MODE = "off"

_current_tts_voice = os.getenv("WEB_TTS_VOICE", DEFAULT_TTS_VOICE)
if _current_tts_voice not in VALID_TTS_VOICE_IDS:
    _current_tts_voice = DEFAULT_TTS_VOICE

_current_tts_mode = os.getenv("WEB_TTS_MODE", DEFAULT_TTS_MODE)
if _current_tts_mode not in VALID_TTS_MODES:
    _current_tts_mode = DEFAULT_TTS_MODE

VOICE_MODE_PROMPT_SUFFIX = (
    "\n\n[음성 대화 모드 지침: 사용자가 실시간 음성으로 대화 중이다. "
    "볼트 조회·수정은 평소처럼 정확히 수행하되, 답변은 귀로 듣기 편하도록 핵심 위주로 "
    "자연스러운 구어체 2~4문장 이내로 간결하게 말하라. 표나 긴 코드 블록은 피하라.]"
)


def _get_mod_val(attr_name: str, fallback: any) -> any:
    mod = sys.modules.get("web_server")
    if mod and hasattr(mod, attr_name):
        return getattr(mod, attr_name)
    return fallback


def clean_text_for_speech(markdown_text: str) -> str:
    """마크다운·위키링크·URL·코드펜스를 제거해 TTS가 자연스럽게 읽을 수 있는 평문으로 변환한다."""
    if not markdown_text:
        return ""

    cleaned = markdown_text
    cleaned = re.sub(
        r"```[\s\S]*?```",
        " 코드 블록은 화면을 참고해 주세요. ",
        cleaned,
    )
    cleaned = re.sub(r"`([^`]+)`", r"\1", cleaned)
    cleaned = re.sub(
        r"\[(?:link:)?([^\]]+)\]\([^)]+\)",
        r"\1",
        cleaned,
    )
    cleaned = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", cleaned)
    cleaned = re.sub(r"\[\[([^\]]+)\]\]", r"\1", cleaned)
    cleaned = re.sub(r"https?://\S+", "", cleaned)
    cleaned = re.sub(r"<[^>]+>", "", cleaned)
    cleaned = re.sub(r"(?m)^#{1,6}\s*", "", cleaned)
    cleaned = re.sub(r"[*_~>|]", " ", cleaned)
    cleaned = re.sub(
        r"[✅🔴⚠️📌⏳🔄🆕💬🎙️🌐📊📅🔒⛔❓•]",
        " ",
        cleaned,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def deduplicate_speech_text(text: str) -> str:
    """STT에서 흔히 발생하는 연속 단어/구문 반복 버그를 제거한다.
    예: '오늘 날씨 오늘 날씨 어때' -> '오늘 날씨 어때'
        '안녕 안녕 안녕하세요' -> '안녕하세요'
        '테스트 테스트' -> '테스트'
    """
    if not text:
        return ""
    cleaned = re.sub(r"\s+", " ", text).strip()
    words = cleaned.split(" ")

    for length in range(4, 0, -1):
        if len(words) < length * 2:
            continue
        new_words: list[str] = []
        i = 0
        while i < len(words):
            if i + length * 2 <= len(words):
                p1 = " ".join(words[i : i + length]).lower()
                p2 = " ".join(words[i + length : i + length * 2]).lower()
                if p1 == p2:
                    new_words.extend(words[i : i + length])
                    i += length * 2
                    while (
                        i + length <= len(words)
                        and " ".join(words[i : i + length]).lower() == p1
                    ):
                        i += length
                    continue
            new_words.append(words[i])
            i += 1
        words = new_words

    return " ".join(words).strip()


async def synthesize_edge_tts_mp3(
    speech_text: str,
    voice_id: str = DEFAULT_TTS_VOICE,
    speech_rate: str = "+0%",
) -> bytes:
    """edge-tts를 사용해 한국어 신경망 음성 MP3 바이트를 생성한다."""
    selected_voice = voice_id if voice_id in VALID_TTS_VOICE_IDS else DEFAULT_TTS_VOICE
    communicator = edge_tts.Communicate(
        speech_text,
        voice=selected_voice,
        rate=speech_rate,
    )
    audio_chunks: list[bytes] = []
    async for stream_chunk in communicator.stream():
        if stream_chunk.get("type") == "audio" and stream_chunk.get("data"):
            audio_chunks.append(stream_chunk["data"])
    return b"".join(audio_chunks)


async def handle_tts(request: web.Request) -> web.Response:
    """GET/POST /api/tts — 텍스트를 자연스러운 신경망 한국어 음성(MP3)으로 변환해 반환한다."""
    curr_voice = _get_mod_val("_current_tts_voice", DEFAULT_TTS_VOICE)
    if request.method == "POST":
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"error": "유효하지 않은 JSON 요청입니다."}, status=400)
        raw_text = str(payload.get("text") or "").strip()
        voice_id = str(payload.get("voice") or curr_voice).strip()
        speech_rate = str(payload.get("rate") or "+0%").strip()
    else:
        raw_text = str(request.query.get("text") or "").strip()
        voice_id = str(request.query.get("voice") or curr_voice).strip()
        speech_rate = str(request.query.get("rate") or "+0%").strip()

    clean_fn = _get_mod_val("clean_text_for_speech", clean_text_for_speech)
    speech_text = clean_fn(raw_text)
    if not speech_text:
        return web.json_response({"error": "읽을 텍스트가 비어 있습니다."}, status=400)

    if len(speech_text) > 1500:
        speech_text = speech_text[:1500] + " 이하 내용은 화면을 참고해 주세요."

    synth_fn = _get_mod_val("synthesize_edge_tts_mp3", synthesize_edge_tts_mp3)
    try:
        mp3_bytes = await synth_fn(
            speech_text,
            voice_id=voice_id,
            speech_rate=speech_rate,
        )
    except Exception as exc:
        logger.warning("[voice-web] edge-tts 합성 실패: %s", exc)
        return web.json_response({"error": f"TTS 합성 실패: {exc}"}, status=502)

    return web.Response(
        body=mp3_bytes,
        content_type="audio/mpeg",
        headers={"Cache-Control": "no-store"},
    )
