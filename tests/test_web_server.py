"""test_web_server — 웹 API 서버 및 세컨드브레인 볼트 파일 관리 단위 테스트."""

from __future__ import annotations

import asyncio
import atexit
import copy
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC_DIR = os.path.join(_REPO_ROOT, "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

import bot.engine.agent_engine as agent_engine
import bot.core.bot_logger as bot_logger
import bot.core.chat_db as chat_db
import bot.engine.engine_settings as engine_settings
import bot.master.master_tasks as master_tasks
import bot.engine.stream_parser as stream_parser
import bot.web.execution as web_execution
import bot.web.server as web_server
import bot.web.auth as web_auth


_STATIC_BUNDLE_CACHE: dict[str, str] = {}
_STATIC_BUNDLE_FILE_CACHE: dict[str, str] = {}


def _read_static_bundle(static_dir: str | None = None) -> str:
    s_dir = static_dir or web_server.INTERNAL_STATIC_DIR
    if s_dir in _STATIC_BUNDLE_CACHE:
        return _STATIC_BUNDLE_CACHE[s_dir]
    index_file = os.path.join(s_dir, "index.html")
    if not os.path.isfile(index_file):
        return ""
    with open(index_file, "r", encoding="utf-8") as f:
        html = f.read()

    css_files = [
        "css/base.css",
        "css/layout.css",
        "css/chat.css",
        "css/views-modals.css",
        "css/responsive.css",
        "css/master-inspector.css",
    ]
    js_files = [
        "js/core.js",
        "js/rooms-sync.js",
        "js/master-memory.js",
        "js/ws.js",
        "js/markdown.js",
        "js/composer.js",
        "js/rooms-ui.js",
        "js/messages.js",
        "js/gestures.js",
        "js/voice.js",
        "js/vault.js",
        "js/settings.js",
        "js/init.js",
    ]

    css_parts = []
    for rel in css_files:
        p = os.path.join(s_dir, rel)
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as f:
                c = f.read()
                css_parts.append("\n".join(("    " + line if line else "") for line in c.splitlines()))

    js_parts = []
    for rel in js_files:
        p = os.path.join(s_dir, rel)
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as f:
                c = f.read()
                js_parts.append("\n".join(("    " + line if line else "") for line in c.splitlines()))

    bundled_css = "  <style>\n" + "\n".join(css_parts) + "\n  </style>\n"
    bundled_js = "  <script>\n" + "\n".join(js_parts) + "\n  </script>\n"

    res = re.sub(r'(?:  <link rel="stylesheet" href="/css/[^"]+">\n?)+', lambda m: bundled_css, html)
    res = re.sub(r'(?:  <script src="/js/[^"]+"></script>\n?)+', lambda m: bundled_js, res)
    _STATIC_BUNDLE_CACHE[s_dir] = res
    return res


def _get_static_bundle_path(static_dir: str | None = None) -> str:
    s_dir = static_dir or web_server.INTERNAL_STATIC_DIR
    if s_dir in _STATIC_BUNDLE_FILE_CACHE and os.path.exists(_STATIC_BUNDLE_FILE_CACHE[s_dir]):
        return _STATIC_BUNDLE_FILE_CACHE[s_dir]
    content = _read_static_bundle(s_dir)
    fd, path = tempfile.mkstemp(suffix=".html", prefix="static_bundle_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    _STATIC_BUNDLE_FILE_CACHE[s_dir] = path
    return path


def _cleanup_bundle_files():
    for p in _STATIC_BUNDLE_FILE_CACHE.values():
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass


atexit.register(_cleanup_bundle_files)


def _install_runtime_isolation(temp_dir: str):
    """테스트 중 운영 설정/로그/작업 원장 파일을 임시 경로로 격리하고 복구 콜백을 반환한다."""
    orig_settings = copy.deepcopy(engine_settings._settings)
    jobs_dir = os.path.join(temp_dir, "jobs")
    web_jobs_file = os.path.join(temp_dir, "web_jobs.json")
    os.makedirs(jobs_dir, exist_ok=True)
    patchers = [
        patch("bot.engine.engine_settings._SETTINGS_FILE", os.path.join(temp_dir, "bot_settings.json")),
        patch("bot.engine.stream_parser._SESSION_LOG_FILE", os.path.join(temp_dir, "session_log.jsonl")),
        patch("bot.core.bot_logger.BOT_LOG_PATH", os.path.join(temp_dir, "bot-log.md")),
        patch("bot.core.bot_logger._run_vault_log", AsyncMock()),
        patch("bot.master.master_tasks.MASTER_TASKS_FILE", os.path.join(temp_dir, "bot.master.master_tasks.json")),
        patch("bot.master.master_memory.MASTER_MEMORY_FILE", os.path.join(temp_dir, "bot.master.master_memory.json")),
        patch.object(web_execution, "JOBS_DIR", jobs_dir),
        patch.object(web_execution, "WEB_JOBS_FILE", web_jobs_file),
        patch.object(web_server, "JOBS_DIR", jobs_dir),
        patch.object(web_server, "WEB_JOBS_FILE", web_jobs_file),
    ]
    for p in patchers:
        p.start()

    def cleanup():
        for p in reversed(patchers):
            p.stop()
        engine_settings._settings.clear()
        engine_settings._settings.update(orig_settings)
        if os.path.exists(engine_settings._SETTINGS_FILE):
            engine_settings._sync_from_disk()

    return cleanup


class TestSpeechTextCleaning(unittest.TestCase):
    """TTS 발화용 마크다운·위키링크·코드블록 정제 함수를 검증한다."""

    def test_clean_text_removes_markdown_and_links(self):
        raw_markdown = (
            "✅ **오늘 일정** 안내입니다.\n"
            "- [[캘린더|일정 허브]] 및 [[개인 할일]] 확인\n"
            "- 공유 링크: [link:캘린더.md](https://obsid.net/?vault=oshyun&file=cal)\n"
            "- 괄호 문서: ( [link:스킨케어 루틴(아침).md](https://obsid.net/?vault=oshyun&file=skincare(am).md) )의 브리핑\n"
            "```python\nprint('hello')\n```"
        )
        speech_text = web_server.clean_text_for_speech(raw_markdown)
        self.assertIn("오늘 일정 안내입니다.", speech_text)
        self.assertIn("일정 허브 및 개인 할일 확인", speech_text)
        self.assertIn("캘린더.md", speech_text)
        self.assertIn("스킨케어 루틴(아침).md )의 브리핑", speech_text)
        self.assertNotIn("skincare(am).md", speech_text)
        self.assertNotIn("https://obsid.net", speech_text)
        self.assertNotIn("print", speech_text)
        self.assertIn("코드 블록은 화면을 참고해 주세요.", speech_text)

    def test_deduplicate_speech_text(self):
        """STT 단어 및 구문 연속 반복 버그 제거 정합성 검증."""
        # 1. 단일 단어 다중 반복
        self.assertEqual(
            web_server.deduplicate_speech_text("안녕 안녕 안녕하세요"),
            "안녕 안녕하세요",
        )
        self.assertEqual(
            web_server.deduplicate_speech_text("테스트 테스트 테스트"),
            "테스트",
        )
        # 2. 2단어 구문 반복
        self.assertEqual(
            web_server.deduplicate_speech_text("오늘 날씨 오늘 날씨 어때?"),
            "오늘 날씨 어때?",
        )
        # 3. 3단어 구문 반복
        self.assertEqual(
            web_server.deduplicate_speech_text("오늘 일정 확인 오늘 일정 확인 부탁해"),
            "오늘 일정 확인 부탁해",
        )
        # 4. 일반적인 정상 문장은 그대로 유지
        normal_text = "이것은 정상적인 문장입니다. 중복이 없습니다."
        self.assertEqual(
            web_server.deduplicate_speech_text(normal_text),
            normal_text,
        )
        # 5. 빈 문자열 및 None 안전성
        self.assertEqual(web_server.deduplicate_speech_text(""), "")
        self.assertEqual(web_server.deduplicate_speech_text(None), "")

    def test_is_session_stale_error(self):
        """세션 만료/유실/접근불가 오류 식별 검증."""
        from bot.engine.agent_engine import is_session_stale_error

        # 1. AGY trajectory 미존재
        self.assertTrue(
            is_session_stale_error(
                "failed to send message: not_found: trajectory not found: 85922edc-4e2b-4b6f-9d83-1df07c0329cb: not found"
            )
        )
        self.assertTrue(
            is_session_stale_error("failed to ensure trajectory loaded: not found")
        )
        self.assertTrue(is_session_stale_error("conversation 1234 not found locally"))
        # 2. 일반 에러 및 정상 답변은 False
        self.assertFalse(is_session_stale_error("오늘 서울 날씨는 맑음입니다."))
        self.assertFalse(is_session_stale_error("⚠️ API 호출 시간 초과"))
        self.assertFalse(is_session_stale_error(""))
        self.assertFalse(is_session_stale_error(None))


class TestStreamEventExtraction(unittest.TestCase):
    """stream-json 라인에서 thinking, tool_use, tool_result 추출 정합성을 검증한다."""

    def test_extract_thinking_and_tool_call(self):
        raw_line = json.dumps({
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "캘린더.md 노트를 읽어서 오늘 일정을 파악해야겠다.",
                    },
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {
                            "file_path": "/Users/user/obsidian/oshyun/_bot/캘린더.md"
                        },
                    },
                ]
            },
        })
        events = web_server.extract_stream_events_from_line(raw_line)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["kind"], "thinking")
        self.assertIn("캘린더.md 노트를 읽어서", events[0]["detail"])

        self.assertEqual(events[1]["kind"], "tool_call")
        self.assertEqual(events[1]["tool"], "Read")
        self.assertIn("캘린더.md", events[1]["title"])
        self.assertIn("/Users/user/obsidian/oshyun/_bot/캘린더.md", events[1]["detail"])

    def test_extract_tool_result(self):
        raw_line = json.dumps({
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "content": "## 2026-10-29\n- 14:00 팀 미팅\n- 16:30 치과 정기 검진",
                    }
                ]
            },
        })
        events = web_server.extract_stream_events_from_line(raw_line)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "tool_result")
        self.assertIn("치과 정기 검진", events[0]["detail"])

    def test_extract_agy_tool_call_human_friendly(self):
        read_line = json.dumps({
            "event": "step_update",
            "step_update": {
                "step_index": 32,
                "state": "ACTIVE",
                "step_type": "tool",
                "tool_name": "read_file",
                "tool_info": {
                    "name": "read_file",
                    "parameters": {
                        "Path": "/Users/user/obsidian/oshyun/개인/건강/약 복용 기록.md",
                        "StartLine": 1,
                        "EndLine": 100,
                    },
                },
            },
        })
        events = web_server.extract_stream_events_from_line(read_line)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "tool_call")
        self.assertEqual(events[0]["title"], "read_file: 약 복용 기록.md")
        self.assertEqual(events[0]["stepIndex"], 32)
        self.assertIn("범위: 1~100행", events[0]["detail"])

        grep_cmd_line = json.dumps({
            "event": "step_update",
            "step_update": {
                "step_index": 14,
                "state": "ACTIVE",
                "step_type": "tool",
                "tool_name": "run_command",
                "tool_info": {
                    "name": "run_command",
                    "parameters": {
                        "CommandLine": 'grep -rni "할인" /Users/user/obsidian/oshyun --include="*.md"'
                    },
                },
            },
        })
        grep_events = web_server.extract_stream_events_from_line(grep_cmd_line)
        self.assertEqual(len(grep_events), 1)
        self.assertEqual(grep_events[0]["title"], 'run_command: "할인"')


class TestVaultPathSecurity(unittest.TestCase):
    """경로 탈출(..) 공격 차단 및 볼트 상대 경로 해석 검증."""

    def setUp(self):
        self.temp_vault = tempfile.mkdtemp()
        self.orig_workdir = web_server.WORKDIR
        web_server.WORKDIR = self.temp_vault

    def tearDown(self):
        web_server.WORKDIR = self.orig_workdir
        shutil.rmtree(self.temp_vault, ignore_errors=True)

    def test_normal_relative_path(self):
        resolved = web_server._resolve_vault_absolute_path("개인/건강.md")
        self.assertTrue(resolved.startswith(os.path.realpath(self.temp_vault)))

    def test_forbidden_traversal(self):
        with self.assertRaises(web.HTTPForbidden):
            web_server._resolve_vault_absolute_path("../../etc/passwd")


class TestVoiceWebServerEndpoints(AioHTTPTestCase):
    """REST 엔드포인트(/api/status, /api/settings, /api/tts, /api/vault/*)와 WebSocket(/ws) 동작 검증."""

    async def get_application(self):
        self.test_vault_dir = tempfile.mkdtemp()
        self.test_db_dir = tempfile.mkdtemp()
        self._cleanup_runtime = _install_runtime_isolation(self.test_db_dir)
        self.orig_chat_db_file = getattr(chat_db, "CHAT_DB_FILE", None)
        self.orig_hidden_file = getattr(web_server, "HIDDEN_ROOMS_FILE", None)
        self.orig_chat_db_hidden_file = getattr(chat_db, "HIDDEN_ROOMS_FILE", None)
        self.orig_web_jobs_file = getattr(web_server, "WEB_JOBS_FILE", None)
        self.test_chat_db = os.path.join(self.test_db_dir, "test_chat.db")
        chat_db.CHAT_DB_FILE = self.test_chat_db
        test_hidden = os.path.join(self.test_db_dir, "hidden_rooms.json")
        web_server.HIDDEN_ROOMS_FILE = test_hidden
        chat_db.HIDDEN_ROOMS_FILE = test_hidden
        web_server.WEB_JOBS_FILE = os.path.join(self.test_db_dir, "web_jobs.json")
        chat_db.init_db(self.test_chat_db, migrate_legacy=False)

        web_server.WORKDIR = self.test_vault_dir
        web_auth.WEB_TOKEN = "test-secret-token-1234"
        web_auth.AUTH_DELAY_SECONDS = 0.0  # 테스트 실행 속도를 위해 딜레이 비활성화

        # 테스트용 볼트 파일 생성
        os.makedirs(os.path.join(self.test_vault_dir, "개인"), exist_ok=True)
        with open(os.path.join(self.test_vault_dir, "개인", "메모.md"), "w", encoding="utf-8") as f:
            f.write("# 개인 메모\n- 테스트 내용")

        return web_server.create_web_app(static_dir="/nonexistent")

    async def asyncSetUp(self):
        await super().asyncSetUp()
        cookie_val = web_server._create_session_cookie_value()
        self.client.session.cookie_jar.update_cookies(
            {web_server.AUTH_COOKIE_NAME: cookie_val}
        )

    async def tearDownAsync(self):
        if getattr(self, "orig_chat_db_file", None):
            chat_db.CHAT_DB_FILE = self.orig_chat_db_file
        if getattr(self, "orig_hidden_file", None):
            web_server.HIDDEN_ROOMS_FILE = self.orig_hidden_file
        if getattr(self, "orig_chat_db_hidden_file", None):
            chat_db.HIDDEN_ROOMS_FILE = self.orig_chat_db_hidden_file
        if getattr(self, "orig_web_jobs_file", None):
            web_server.WEB_JOBS_FILE = self.orig_web_jobs_file
        if hasattr(self, "_cleanup_runtime"):
            self._cleanup_runtime()
        shutil.rmtree(self.test_vault_dir, ignore_errors=True)
        shutil.rmtree(self.test_db_dir, ignore_errors=True)
        await super().tearDownAsync()

    async def test_get_status_returns_expected_fields(self):
        response = await self.client.request("GET", "/api/status")
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertEqual(payload["type"], "status")
        self.assertIn("engine", payload)
        self.assertIn("model", payload)
        self.assertIn("ttsVoices", payload)
        self.assertIn("ttsMode", payload)
        self.assertIn("ttsVoice", payload)
        self.assertIn("supportedTtsModes", payload)
        self.assertIn("clientIp", payload)
        self.assertIn("clientMac", payload)
        self.assertIn("ipType", payload)
        self.assertIn("ipDescription", payload)
        self.assertGreaterEqual(len(payload["ttsVoices"]), 1)

    async def test_vault_tree_and_recent_endpoints(self):
        response_tree = await self.client.request("GET", "/api/vault/tree")
        self.assertEqual(response_tree.status, 200)
        tree_payload = await response_tree.json()
        self.assertEqual(tree_payload.get("status"), "ok")
        self.assertTrue(tree_payload.get("ok"))
        self.assertIn("items", tree_payload)
        self.assertTrue(any(item["name"] == "개인" for item in tree_payload["items"]))

        response_recent = await self.client.request("GET", "/api/vault/recent?limit=10")
        self.assertEqual(response_recent.status, 200)
        recent_payload = await response_recent.json()
        self.assertEqual(recent_payload.get("status"), "ok")
        self.assertIn("items", recent_payload)
        self.assertIn("recentFiles", recent_payload)
        self.assertTrue(any("메모.md" in f["name"] for f in recent_payload["items"]))
        self.assertTrue(any("메모.md" in f["name"] for f in recent_payload["recentFiles"]))

    async def test_vault_file_read_write_and_create(self):
        # 1. 파일 읽기
        get_res = await self.client.request("GET", "/api/vault/file?path=개인/메모.md")
        self.assertEqual(get_res.status, 200)
        file_payload = await get_res.json()
        self.assertEqual(file_payload.get("status"), "ok")
        self.assertEqual(file_payload["name"], "메모.md")
        self.assertIn("# 개인 메모", file_payload["content"])

        # 2. 파일 수정 (VaultFileDetail 스키마 필드 전체 반환 검증)
        post_res = await self.client.request(
            "POST",
            "/api/vault/file",
            json={"path": "개인/메모.md", "content": "# 개인 메모\n- 수정된 내용"},
        )
        self.assertEqual(post_res.status, 200)
        post_payload = await post_res.json()
        self.assertEqual(post_payload.get("status"), "ok")
        self.assertTrue(post_payload.get("ok"))
        self.assertEqual(post_payload.get("name"), "메모.md")
        self.assertIn("수정된 내용", post_payload.get("content", ""))
        self.assertIn("obsidUrl", post_payload)

        verify_res = await self.client.request("GET", "/api/vault/file?path=개인/메모.md")
        verify_payload = await verify_res.json()
        self.assertIn("수정된 내용", verify_payload["content"])

        # 3. 새 파일 생성 및 is_directory 호환성 검증
        create_res = await self.client.request(
            "POST",
            "/api/vault/create",
            json={"path": "할일/오늘.md", "content": "# 오늘 할일\n- [ ] 플러터 앱 테스트"},
        )
        self.assertEqual(create_res.status, 200)
        create_payload = await create_res.json()
        self.assertEqual(create_payload.get("status"), "ok")

        dir_create_res = await self.client.request(
            "POST",
            "/api/vault/create",
            json={"path": "새폴더_테스트", "is_directory": True},
        )
        self.assertEqual(dir_create_res.status, 200)
        dir_payload = await dir_create_res.json()
        self.assertEqual(dir_payload.get("type"), "directory")

        new_file_res = await self.client.request("GET", "/api/vault/file?path=할일/오늘.md")
        self.assertEqual(new_file_res.status, 200)
        new_payload = await new_file_res.json()
        self.assertIn("플러터 앱 테스트", new_payload["content"])

    async def test_settings_endpoint_returns_ok_flag(self):
        with patch("bot.web.server.set_effort"):
            res = await self.client.request(
                "POST",
                "/api/settings",
                json={"effort": "medium"},
            )
            self.assertEqual(res.status, 200)
            data = await res.json()
            self.assertTrue(data.get("ok"))
            self.assertEqual(data.get("status"), "ok")

    async def test_settings_endpoint_updates_tts_mode_and_voice(self):
        res = await self.client.request(
            "POST",
            "/api/settings",
            json={"ttsMode": "off", "ttsVoice": "ko-KR-InJoonNeural"},
        )
        self.assertEqual(res.status, 200)
        data = await res.json()
        self.assertEqual(data.get("ttsMode"), "off")
        self.assertEqual(data.get("ttsVoice"), "ko-KR-InJoonNeural")

    async def test_tts_endpoint_returns_mp3_bytes(self):
        with patch(
            'bot.web.server.synthesize_edge_tts_mp3',
            new_callable=AsyncMock,
            return_value=b"ID3-mock-mp3-audio",
        ) as mock_synth:
            response = await self.client.request(
                "POST",
                "/api/tts",
                json={"text": "안녕하세요 **테스트**입니다.", "voice": "ko-KR-SunHiNeural"},
            )
            self.assertEqual(response.status, 200)
            self.assertEqual(response.content_type, "audio/mpeg")
            audio_body = await response.read()
            self.assertEqual(audio_body, b"ID3-mock-mp3-audio")
            mock_synth.assert_awaited_once_with(
                "안녕하세요 테스트 입니다.",
                voice_id="ko-KR-SunHiNeural",
                speech_rate="+0%",
            )

    async def test_websocket_chat_and_result_flow(self):
        mock_proc = MagicMock()
        mock_proc.pid = 99999
        mock_watch_return = {
            "result": "[테스트] 오늘 오후 3시에 치과 예약이 있습니다.",
            "session_id": "sess-voice-123",
            "cost": 0.001,
            "model": "sonnet",
            "interrupted": False,
        }
        with patch("bot.web.server.spawn_agent", return_value=mock_proc), patch(
            'bot.web.server.watch_agent',
            new_callable=AsyncMock,
            return_value=mock_watch_return,
        ), patch("bot.web.server.set_session"), patch(
            'bot.web.server.update_last_activity'
        ), patch("bot.web.server.vault_log"):
            async with self.client.ws_connect("/ws") as ws_conn:
                initial_msg = await ws_conn.receive_json()
                self.assertEqual(initial_msg["type"], "status")

                await ws_conn.send_json(
                    {
                        "type": "chat",
                        "text": "[테스트] 오늘 일정 알려줘",
                        "voiceMode": True,
                        "roomId": "room-101",
                    }
                )
                started_msg = await ws_conn.receive_json()
                self.assertEqual(started_msg["type"], "started")
                self.assertEqual(started_msg["roomId"], "room-101")
                self.assertTrue(started_msg["voiceMode"])

                result_msg = await ws_conn.receive_json()
                self.assertEqual(result_msg["type"], "result")
                self.assertEqual(result_msg["roomId"], "room-101")
                self.assertEqual(
                    result_msg["text"], "[테스트] 오늘 오후 3시에 치과 예약이 있습니다."
                )
                self.assertEqual(result_msg["sessionId"], "sess-voice-123")

    async def test_websocket_chat_duplicate_debounce_guard(self):
        """음성 STT나 네트워크로 인해 동일 질문이 짧은 시간 내 재전송되어도 기존 실행을 죽이지 않고 유지하는지 검증."""
        mock_proc = MagicMock()
        mock_proc.pid = 99998
        mock_watch_return = {
            "result": "[테스트] 안전하게 완료된 음성 답변입니다.",
            "session_id": "sess-debounce-1",
            "cost": 0.001,
            "model": "sonnet",
            "interrupted": False,
        }
        with patch("bot.web.server.spawn_agent", return_value=mock_proc) as mock_spawn, patch(
            'bot.web.server.watch_agent',
            new_callable=AsyncMock,
            return_value=mock_watch_return,
        ), patch("bot.web.server.set_session"), patch(
            'bot.web.server.update_last_activity'
        ), patch("bot.web.server.vault_log"):
            async with self.client.ws_connect("/ws") as ws_conn:
                initial_msg = await ws_conn.receive_json()
                self.assertEqual(initial_msg["type"], "status")

                # 첫 번째 음성 질문 전송
                await ws_conn.send_json(
                    {
                        "type": "chat",
                        "text": "[테스트] 오늘 날씨 어때",
                        "voiceMode": True,
                        "roomId": "room-debounce-test",
                    }
                )
                started_msg = await ws_conn.receive_json()
                self.assertEqual(started_msg["type"], "started")

                # 0.1초 뒤 동일 텍스트 재전송 (중복 트리거)
                await ws_conn.send_json(
                    {
                        "type": "chat",
                        "text": "[테스트] 오늘 날씨 어때",
                        "voiceMode": True,
                        "roomId": "room-debounce-test",
                    }
                )

                # 첫 번째 실행 결과가 정상 도착해야 함 (취소되지 않고 1회 실행 보장)
                result_msg = await ws_conn.receive_json()
                self.assertEqual(result_msg["type"], "result")
                self.assertEqual(result_msg["text"], "[테스트] 안전하게 완료된 음성 답변입니다.")
                self.assertEqual(mock_spawn.call_count, 1)

    async def test_websocket_room_session_isolation(self):
        """새 대화방에서 sessionId가 비어 있을 때 이전 방의 전역 세션과 섞이지 않는지 검증."""
        mock_proc = MagicMock()
        mock_proc.pid = 88888
        mock_watch_return = {
            "result": "[테스트] 새 대화방 응답입니다.",
            "session_id": "sess-room-new",
            "cost": 0.001,
            "model": "flash",
            "interrupted": False,
        }
        with patch("bot.web.server.spawn_agent", return_value=mock_proc) as mock_spawn, patch(
            'bot.web.server.watch_agent',
            new_callable=AsyncMock,
            return_value=mock_watch_return,
        ), patch("bot.web.server.get_session", return_value="old-global-session"), patch(
            'bot.web.server.set_session'
        ), patch("bot.web.server.update_last_activity"), patch("bot.web.server.vault_log"):
            async with self.client.ws_connect("/ws") as ws_conn:
                await ws_conn.receive_json()  # initial status
                await ws_conn.send_json(
                    {
                        "type": "chat",
                        "text": "[테스트] 새 방 질문",
                        "roomId": "room-202",
                        "sessionId": None,
                        "newSession": False,
                    }
                )
                await ws_conn.receive_json()  # started
                result_msg = await ws_conn.receive_json()  # result
                self.assertEqual(result_msg["roomId"], "room-202")
                # roomId가 지정되고 sessionId가 None이면 old-global-session을 물려받지 않고 None으로 시작해야 함
                self.assertIsNone(mock_spawn.call_args[0][1])

    async def test_websocket_new_session_on_web_room_preserves_global_session(self):
        """웹 대화방에서 new_session을 요청해도 전역(코어) 세션(ns='')은 초기화되지 않는다."""
        engine_settings.set_engine("agy")
        engine_settings.set_session("tg-live-sess-123", ns="")
        web_server._room_session_ids["room-web-iso-1"] = "web-room-sess-old"
        try:
            async with self.client.ws_connect("/ws") as ws_conn:
                await ws_conn.receive_json()  # initial status
                await ws_conn.send_json({"type": "new_session", "roomId": "room-web-iso-1"})
                status_evt = await ws_conn.receive_json()
                self.assertEqual(status_evt.get("type"), "status")

            self.assertNotIn("room-web-iso-1", web_server._room_session_ids)
            self.assertEqual(engine_settings.get_session(""), "tg-live-sess-123")
        finally:
            web_server._room_session_ids.pop("room-web-iso-1", None)

    async def test_web_execution_on_room_preserves_global_session(self):
        """특정 웹 대화방 실행(execute_chat_turn) 완료 시 전역(코어) 세션이 덮어쓰이지 않는다."""
        engine_settings.set_engine("agy")
        engine_settings.set_session("tg-live-sess-999", ns="")
        mock_proc = MagicMock(pid=77777)
        mock_watch_return = {
            "result": "[테스트] 웹 방 답변입니다.",
            "session_id": "web-turn-sess-888",
            "cost": 0.0,
            "model": "gemini-3.8-flash",
            "interrupted": False,
        }
        with patch("bot.web.server.spawn_agent", return_value=mock_proc), patch(
            'bot.web.server.watch_agent',
            new_callable=AsyncMock,
            return_value=mock_watch_return,
        ):
            res = await web_execution.execute_chat_turn(
                "[테스트] 웹 질문",
                is_voice_mode=False,
                room_id="room-web-iso-2",
                channel="web",
            )

        self.assertEqual(res["sessionId"], "web-turn-sess-888")
        self.assertEqual(engine_settings.get_session(""), "tg-live-sess-999")
        web_server._room_session_ids.pop("room-web-iso-2", None)

    async def test_websocket_delete_session_message(self):
        """WebSocket 을 통해 delete_session 메시지 수신 시 세션 정리 및 session_deleted 이벤트 회신 검증."""
        async with self.client.ws_connect("/ws") as ws_conn:
            await ws_conn.receive_json()  # initial status
            await ws_conn.send_json(
                {
                    "type": "delete_session",
                    "roomId": "room-del-1",
                    "sessionId": "mock-sess-to-delete",
                }
            )
            deleted_evt = await ws_conn.receive_json()
            self.assertEqual(deleted_evt.get("type"), "session_deleted")
            self.assertEqual(deleted_evt.get("roomId"), "room-del-1")
            self.assertEqual(deleted_evt.get("sessionId"), "mock-sess-to-delete")
            self.assertTrue(deleted_evt.get("ok"))
            status_evt = await ws_conn.receive_json()  # follow-up status
            self.assertEqual(status_evt.get("type"), "status")

    async def test_websocket_cancel_with_terminate_session(self):
        """WebSocket 을 통해 cancel(terminateSession=True) 수신 시 세션 정리 및 sessionTerminated 회신 검증."""
        with patch("bot.web.server.clear_session") as mock_clear, patch("bot.web.server.vault_log"):
            async with self.client.ws_connect("/ws") as ws_conn:
                await ws_conn.receive_json()  # initial status
                await ws_conn.send_json(
                    {
                        "type": "cancel",
                        "roomId": "room-voice-abort-1",
                        "terminateSession": True,
                    }
                )
                cancel_evt = await ws_conn.receive_json()
                self.assertEqual(cancel_evt.get("type"), "cancelled")
                self.assertEqual(cancel_evt.get("roomId"), "room-voice-abort-1")
                self.assertTrue(cancel_evt.get("sessionTerminated"))
                mock_clear.assert_called_once()

    def test_web_static_dir_resolution(self):
        self.assertTrue(
            web_server.DEFAULT_WEB_BUILD_DIR.endswith(os.path.join("src", "bot", "web", "static"))
        )


class TestAuthGateway(AioHTTPTestCase):
    """비밀 토큰 게이트웨이, 서명 쿠키, 브루트포스 차단 등 보안 검증."""

    async def get_application(self):
        self.test_vault_dir = tempfile.mkdtemp()
        self._cleanup_runtime = _install_runtime_isolation(self.test_vault_dir)
        self.orig_chat_db_file = getattr(chat_db, "CHAT_DB_FILE", None)
        self.test_chat_db = os.path.join(self.test_vault_dir, "test_chat.db")
        chat_db.CHAT_DB_FILE = self.test_chat_db
        chat_db.init_db(self.test_chat_db, migrate_legacy=False)
        web_server.WORKDIR = self.test_vault_dir
        web_auth.WEB_TOKEN = "super-secret-password-999"
        web_auth.AUTH_DELAY_SECONDS = 0.0
        web_server._failed_auth_attempts.clear()
        return web_server.create_web_app(static_dir="/nonexistent")

    async def tearDownAsync(self):
        if getattr(self, "orig_chat_db_file", None):
            chat_db.CHAT_DB_FILE = self.orig_chat_db_file
        if hasattr(self, "_cleanup_runtime"):
            self._cleanup_runtime()
        shutil.rmtree(self.test_vault_dir, ignore_errors=True)
        web_server._failed_auth_attempts.clear()
        await super().tearDownAsync()

    async def test_unauthenticated_api_request_returns_401(self):
        """인증 정보 없이 API 요청 시 401 반환."""
        res = await self.client.request("GET", "/api/status")
        self.assertEqual(res.status, 401)
        data = await res.json()
        self.assertEqual(data.get("error"), "Unauthorized")

    async def test_unauthenticated_page_request_redirects_to_login(self):
        """인증 정보 없이 웹 루트(/) 요청 시 /login 으로 302 리다이렉트."""
        res = await self.client.request("GET", "/", allow_redirects=False)
        self.assertEqual(res.status, 302)
        self.assertEqual(res.headers.get("Location"), "/login")

    async def test_login_page_renders_html(self):
        """GET /login 요청 시 로그인 폼 HTML 정상 렌더링."""
        res = await self.client.request("GET", "/login")
        self.assertEqual(res.status, 200)
        self.assertIn("text/html", res.headers.get("Content-Type", ""))
        text = await res.text()
        self.assertIn("oshyun-bot Gateway", text)
        self.assertIn("SECRET TOKEN", text)

    async def test_login_failure_with_wrong_token(self):
        """잘못된 토큰 제출 시 401 및 실패 메시지 반환."""
        res = await self.client.request(
            "POST",
            "/login",
            json={"token": "wrong-token"},
        )
        self.assertEqual(res.status, 401)
        data = await res.json()
        self.assertFalse(data.get("ok"))
        self.assertIn("올바르지 않습니다", data.get("error", ""))

    async def test_login_failure_with_non_ascii_token(self):
        """한글 등 non-ASCII 토큰이 들어와도 TypeError 없이 안전하게 401 반환."""
        res = await self.client.request(
            "POST",
            "/login",
            json={"token": "잘못된한글토큰123"},
        )
        self.assertEqual(res.status, 401)
        data = await res.json()
        self.assertFalse(data.get("ok"))

    async def test_login_success_and_cookie_auth(self):
        """올바른 토큰 제출 시 200 OK + 세션 쿠키 발급 및 이후 API 접근 통과."""
        login_res = await self.client.request(
            "POST",
            "/login",
            json={"token": "super-secret-password-999"},
        )
        self.assertEqual(login_res.status, 200)
        login_data = await login_res.json()
        self.assertTrue(login_data.get("ok"))
        self.assertIn(web_server.AUTH_COOKIE_NAME, self.client.session.cookie_jar.filter_cookies(self.client.make_url("/")))

        # 쿠키가 포함된 상태로 API 요청 시 정상 200 OK
        status_res = await self.client.request("GET", "/api/status")
        self.assertEqual(status_res.status, 200)

    async def test_bearer_token_authorization(self):
        """Authorization: Bearer <token> 헤더로 쿠키 없이도 정상 접근."""
        headers = {"Authorization": "Bearer super-secret-password-999"}
        res = await self.client.request("GET", "/api/status", headers=headers)
        self.assertEqual(res.status, 200)

    async def test_url_token_auto_sets_session_cookie(self):
        """URL 쿼리 ?token=<token> 으로 접속 시 세션 쿠키가 자동 발급되어 후속 요청이 인가됨."""
        res = await self.client.request("GET", "/?token=super-secret-password-999")
        self.assertEqual(res.status, 200)
        cookies = self.client.session.cookie_jar.filter_cookies(self.client.make_url("/"))
        self.assertIn(web_server.AUTH_COOKIE_NAME, cookies)

        # 발급된 쿠키 덕분에 토큰 파라미터 없는 후속 API 요청도 200 OK
        status_res = await self.client.request("GET", "/api/status")
        self.assertEqual(status_res.status, 200)

    async def test_brute_force_lockout_after_max_failures(self):
        """동일 IP에서 5회 실패 시 429 Too Many Requests 로 차단."""
        client_ip = "127.0.0.1"
        for i in range(web_server.AUTH_MAX_FAILURES):
            res = await self.client.request("POST", "/login", json={"token": f"bad-try-{i}"})
            self.assertEqual(res.status, 401)

        # 6번째 시도: 429 차단 응답
        lockout_res = await self.client.request("POST", "/login", json={"token": "super-secret-password-999"})
        self.assertEqual(lockout_res.status, 429)
        lockout_data = await lockout_res.json()
        self.assertIn("차단", lockout_data.get("error", ""))

    async def test_delete_session_api_endpoint(self):
        """POST /api/session/delete 로 특정 세션 ID 삭제 요청 시 정상 처리 확인."""
        headers = {"Authorization": "Bearer super-secret-password-999"}
        res = await self.client.request(
            "POST",
            "/api/session/delete",
            headers=headers,
            json={"roomId": "room-test-123", "sessionId": "mock-agy-session-777"},
        )
        self.assertEqual(res.status, 200)
        data = await res.json()
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("roomId"), "room-test-123")
        self.assertEqual(data.get("sessionId"), "mock-agy-session-777")
        self.assertTrue(data.get("result", {}).get("deleted"))

    async def test_delete_session_files_removes_agy_disk_targets(self):
        """delete_session_files 가 AGY 디스크 저장소(brain, conversations, annotations) 파일들을 실제로 삭제하는지 검증."""
        test_session_id = f"test-agy-sess-{uuid.uuid4().hex[:8]}"
        brain_dir = os.path.expanduser(f"~/.gemini/antigravity/brain/{test_session_id}")
        conv_file = os.path.expanduser(f"~/.gemini/antigravity/conversations/{test_session_id}.db")
        anno_file = os.path.expanduser(f"~/.gemini/antigravity/annotations/{test_session_id}.pbtxt")

        # 임시 모의 파일 및 디렉토리 생성
        os.makedirs(brain_dir, exist_ok=True)
        with open(os.path.join(brain_dir, "test.txt"), "w") as f:
            f.write("mock")
        os.makedirs(os.path.dirname(conv_file), exist_ok=True)
        with open(conv_file, "w") as f:
            f.write("mock db")
        os.makedirs(os.path.dirname(anno_file), exist_ok=True)
        with open(anno_file, "w") as f:
            f.write("mock proto")

        self.assertTrue(os.path.isdir(brain_dir))
        self.assertTrue(os.path.isfile(conv_file))
        self.assertTrue(os.path.isfile(anno_file))

        # 삭제 함수 실행
        result = agent_engine.delete_session_files(test_session_id)
        self.assertTrue(result.get("deleted"))

        # 디스크에서 완전히 사라졌는지 확인
        self.assertFalse(os.path.exists(brain_dir))
        self.assertFalse(os.path.exists(conv_file))
        self.assertFalse(os.path.exists(anno_file))

    async def test_logout_clears_session(self):
        """로그아웃 시 세션 쿠키가 삭제되고 /login 으로 리다이렉트."""
        # 1. 로그인
        await self.client.request("POST", "/login", json={"token": "super-secret-password-999"})
        self.assertEqual((await self.client.request("GET", "/api/status")).status, 200)

        # 2. 로그아웃
        logout_res = await self.client.request("POST", "/logout", json={})
        self.assertEqual(logout_res.status, 200)

        # 3. 로그아웃 후 API 요청 시 401 거부
        res_after = await self.client.request("GET", "/api/status")
        self.assertEqual(res_after.status, 401)


class TestPwaSupport(AioHTTPTestCase):
    """PWA 필수 메타데이터(manifest.json), Service Worker(sw.js), 아이콘 서빙 검증."""

    async def get_application(self):
        self.test_vault_dir = tempfile.mkdtemp()
        self._cleanup_runtime = _install_runtime_isolation(self.test_vault_dir)
        self.orig_chat_db_file = getattr(chat_db, "CHAT_DB_FILE", None)
        chat_db.CHAT_DB_FILE = os.path.join(self.test_vault_dir, "test_chat.db")
        web_server.WORKDIR = self.test_vault_dir
        web_auth.WEB_TOKEN = "super-secret-password-999"
        web_auth.AUTH_DELAY_SECONDS = 0.0
        web_server._failed_auth_attempts.clear()
        self.orig_hidden_file = web_server.HIDDEN_ROOMS_FILE
        self.orig_chat_db_hidden_file = getattr(chat_db, "HIDDEN_ROOMS_FILE", None)
        self.orig_backup_file = web_server.CHAT_ROOMS_BACKUP_FILE
        test_hidden = os.path.join(self.test_vault_dir, "hidden.json")
        web_server.HIDDEN_ROOMS_FILE = test_hidden
        chat_db.HIDDEN_ROOMS_FILE = test_hidden
        web_server.CHAT_ROOMS_BACKUP_FILE = os.path.join(self.test_vault_dir, "backup.json")
        static_dir = os.path.join(_REPO_ROOT, "src", "bot", "web", "static")
        return web_server.create_web_app(static_dir=static_dir)

    def tearDown(self):
        super().tearDown()
        if getattr(self, "orig_chat_db_file", None):
            chat_db.CHAT_DB_FILE = self.orig_chat_db_file
        if hasattr(self, "orig_hidden_file"):
            web_server.HIDDEN_ROOMS_FILE = self.orig_hidden_file
        if getattr(self, "orig_chat_db_hidden_file", None):
            chat_db.HIDDEN_ROOMS_FILE = self.orig_chat_db_hidden_file
        if hasattr(self, "orig_backup_file"):
            web_server.CHAT_ROOMS_BACKUP_FILE = self.orig_backup_file
        if hasattr(self, "_cleanup_runtime"):
            self._cleanup_runtime()
        if hasattr(self, "test_vault_dir") and os.path.isdir(self.test_vault_dir):
            shutil.rmtree(self.test_vault_dir, ignore_errors=True)

    async def test_pwa_manifest_and_sw_accessible_without_auth(self):
        """PWA manifest.json 및 sw.js, icons는 비인증 상태에서도 브라우저가 정상 로드할 수 있어야 함."""
        # 1. manifest.json 요청
        res = await self.client.request("GET", "/manifest.json")
        self.assertEqual(res.status, 200)
        data = await res.json()
        self.assertIn("name", data)
        self.assertIn("icons", data)
        self.assertGreaterEqual(len(data["icons"]), 2)
        sizes = [icon["sizes"] for icon in data["icons"]]
        self.assertIn("192x192", sizes)
        self.assertIn("512x512", sizes)

        # 2. sw.js 서비스워커 요청 및 Service-Worker-Allowed 헤더 검증
        sw_res = await self.client.request("GET", "/sw.js")
        self.assertEqual(sw_res.status, 200)
        self.assertEqual(sw_res.headers.get("Service-Worker-Allowed"), "/")
        self.assertIn("javascript", sw_res.headers.get("Content-Type", "").lower())

        # 3. icons 아이콘 파일 요청
        icon_res = await self.client.request("GET", "/icons/icon-192.png")
        self.assertEqual(icon_res.status, 200)
        self.assertEqual(icon_res.headers.get("Content-Type"), "image/png")


class TestHubApi(AioHTTPTestCase):
    """ohbot 스타일 Goals, Ideas, Activity Audit 및 대화방 전용 영속화 API 검증."""

    async def get_application(self):
        self.test_vault_dir = tempfile.mkdtemp()
        self._cleanup_runtime = _install_runtime_isolation(self.test_vault_dir)
        web_server.WORKDIR = self.test_vault_dir
        web_auth.WEB_TOKEN = "super-secret-password-999"
        web_auth.AUTH_DELAY_SECONDS = 0.0
        web_server._failed_auth_attempts.clear()
        self.orig_hidden_file = web_server.HIDDEN_ROOMS_FILE
        self.orig_chat_db_hidden_file = getattr(chat_db, "HIDDEN_ROOMS_FILE", None)
        self.orig_backup_file = web_server.CHAT_ROOMS_BACKUP_FILE
        self.orig_chats_dir = web_server.CHATS_DIR
        self.orig_chat_rooms_file = web_server.CHAT_ROOMS_FILE
        self.orig_chat_db_file = getattr(chat_db, "CHAT_DB_FILE", None)

        test_hidden = os.path.join(self.test_vault_dir, "hidden.json")
        web_server.HIDDEN_ROOMS_FILE = test_hidden
        chat_db.HIDDEN_ROOMS_FILE = test_hidden
        web_server.CHAT_ROOMS_BACKUP_FILE = os.path.join(self.test_vault_dir, "backup.json")
        web_server.CHATS_DIR = os.path.join(self.test_vault_dir, "chats")
        web_server.CHAT_ROOMS_FILE = os.path.join(self.test_vault_dir, "chat_rooms.json")
        test_db = os.path.join(self.test_vault_dir, "test_chat.db")
        chat_db.CHAT_DB_FILE = test_db
        chat_db.init_db(test_db, migrate_legacy=False)
        os.makedirs(web_server.CHATS_DIR, exist_ok=True)

        static_dir = os.path.join(_REPO_ROOT, "src", "bot", "web", "static")
        return web_server.create_web_app(static_dir=static_dir)

    def tearDown(self):
        super().tearDown()
        if hasattr(self, "orig_hidden_file"):
            web_server.HIDDEN_ROOMS_FILE = self.orig_hidden_file
        if getattr(self, "orig_chat_db_hidden_file", None):
            chat_db.HIDDEN_ROOMS_FILE = self.orig_chat_db_hidden_file
        if hasattr(self, "orig_backup_file"):
            web_server.CHAT_ROOMS_BACKUP_FILE = self.orig_backup_file
        if hasattr(self, "orig_chats_dir"):
            web_server.CHATS_DIR = self.orig_chats_dir
        if hasattr(self, "orig_chat_rooms_file"):
            web_server.CHAT_ROOMS_FILE = self.orig_chat_rooms_file
        if getattr(self, "orig_chat_db_file", None):
            chat_db.CHAT_DB_FILE = self.orig_chat_db_file
        if hasattr(self, "_cleanup_runtime"):
            self._cleanup_runtime()
        if hasattr(self, "test_vault_dir") and os.path.isdir(self.test_vault_dir):
            shutil.rmtree(self.test_vault_dir, ignore_errors=True)

    async def test_chat_rooms_and_history_api(self):
        """GET /api/chat/rooms 및 /api/chat/history 응답 검증."""
        headers = {"Authorization": "Bearer super-secret-password-999"}
        res = await self.client.request("GET", "/api/chat/rooms", headers=headers)
        self.assertEqual(res.status, 200)
        data = await res.json()
        self.assertTrue(data.get("ok"))
        self.assertIsInstance(data.get("rooms"), list)

        # 없는 세션 ID 히스토리 요청 시 빈 배열 반환
        hist_res = await self.client.request(
            "GET", "/api/chat/history?sessionId=non-existent-sid", headers=headers
        )
        self.assertEqual(hist_res.status, 200)
        hist_data = await hist_res.json()
        self.assertTrue(hist_data.get("ok"))
        self.assertEqual(hist_data.get("messages"), [])
        self.assertIn("workdir", hist_data)

    async def test_chat_rooms_sorted_by_recency(self):
        """GET /api/chat/rooms 가 최근 사용(갱신) 순서로 정렬되어 반환되는지 검증."""
        headers = {"Authorization": "Bearer super-secret-password-999"}
        chat_db.get_or_create_room("room-older", title="[테스트] 오래된 방")
        chat_db.add_message("room-older", "user", "[테스트] 과거 질문", created_at=1700000000.0)
        chat_db.update_room("room-older", updated_at=1700000000.0)

        chat_db.get_or_create_room("room-newer", title="[테스트] 최신 방")
        chat_db.add_message("room-newer", "user", "[테스트] 최신 질문", created_at=1730000000000.0)
        chat_db.update_room("room-newer", updated_at=1730000000000.0)

        chat_db.get_or_create_room("room-mid", title="[테스트] 중간 방")
        chat_db.add_message("room-mid", "user", "[테스트] 중간 질문", created_at=1715000000.0)
        chat_db.update_room("room-mid", updated_at=1715000000.0)

        get_res = await self.client.request("GET", "/api/chat/rooms", headers=headers)
        self.assertEqual(get_res.status, 200)
        data = await get_res.json()
        self.assertTrue(data.get("ok"))
        rooms = data.get("rooms", [])

        # room-newer, room-mid, room-older 순서인지 확인
        matched_ids = [r["id"] for r in rooms if r["id"] in ("room-newer", "room-mid", "room-older")]
        self.assertEqual(matched_ids, ["room-newer", "room-mid", "room-older"])

    async def test_chat_rooms_backup_and_merge_lifecycle(self):
        """SQLite SSOT 기반 GET /api/chat/rooms 조회, 삭제 및 클라이언트 백업 역동기화 차단 검증."""
        headers = {"Authorization": "Bearer super-secret-password-999"}

        # 1. 초기 백업 조회
        res = await self.client.request("GET", "/api/chat/rooms/backup", headers=headers)
        self.assertEqual(res.status, 200)
        data = await res.json()
        self.assertTrue(data.get("ok"))
        self.assertIsInstance(data.get("rooms"), list)

        # 2. SQLite SSOT에 대화방 등록
        chat_db.get_or_create_room("room-test-sync-1", session_id="test-session-sync-1", title="[테스트] 로컬 테스트 대화방")
        chat_db.add_message("room-test-sync-1", "user", "[테스트] 테스트 질문", session_id="test-session-sync-1")

        # 3. GET /api/chat/rooms 에 SSOT 방이 노출되는지 확인
        merged_res = await self.client.request("GET", "/api/chat/rooms", headers=headers)
        self.assertEqual(merged_res.status, 200)
        merged_data = await merged_res.json()
        self.assertTrue(merged_data.get("ok"))
        room_ids = [r.get("id") for r in merged_data.get("rooms", [])]
        self.assertIn("room-test-sync-1", room_ids)

        # 4. 삭제 시 SSOT 목록에서도 제거되는지 확인
        del_res = await self.client.request(
            "POST",
            "/api/session/delete",
            headers=headers,
            json={"roomId": "room-test-sync-1", "sessionId": "test-session-sync-1"},
        )
        self.assertEqual(del_res.status, 200)

        check_res = await self.client.request("GET", "/api/chat/rooms/backup", headers=headers)
        check_data = await check_res.json()
        remaining_ids = [r.get("id") for r in check_data.get("rooms", [])]
        self.assertNotIn("room-test-sync-1", remaining_ids)

        # 5. 삭제된 방이 클라이언트의 오래된 캐시로 인해 다시 백업 전송되더라도 부활하지 않는지 검증
        sample_rooms = [
            {
                "id": "room-test-sync-1",
                "title": "[테스트] 로컬 테스트 대화방",
                "messages": [{"sender": "user", "text": "[테스트] 테스트 질문"}],
                "sessionId": "test-session-sync-1",
            }
        ]
        zombie_res = await self.client.request(
            "POST",
            "/api/chat/rooms/backup",
            headers=headers,
            json={"rooms": sample_rooms},
        )
        self.assertEqual(zombie_res.status, 200)

        check_res2 = await self.client.request("GET", "/api/chat/rooms", headers=headers)
        check_data2 = await check_res2.json()
        rooms_after = [r.get("id") for r in check_data2.get("rooms", [])]
        self.assertNotIn("room-test-sync-1", rooms_after)

    async def test_room_stream_jsonl_storage_and_history_api(self):
        """data/chats/<roomId>.jsonl 실시간 스트림 영속화 및 히스토리 조회, 방 삭제 검증."""
        headers = {"Authorization": "Bearer super-secret-password-999"}
        from bot.web.server import _append_room_message, _update_room_metadata, _load_room_messages

        room_id = "room-stream-jsonl-test"

        # 1. 턴 메시지 추가 (유저 + 어시스턴트)
        _append_room_message(room_id, {
            "type": "user",
            "text": "오늘 날씨 어때?",
            "createdAt": "2026-11-02T10:00:00Z"
        })
        _update_room_metadata(room_id, title="오늘 날씨 어때?", delta_msgs=1)

        _append_room_message(room_id, {
            "type": "assistant",
            "text": "오늘은 맑고 쾌청합니다.",
            "steps": [{"type": "tool", "name": "weather", "preview": "seoul"}],
            "model": "gemini-3.8-flash",
            "cost": 0.0001,
            "sessionId": "sid-weather-1",
            "createdAt": "2026-11-02T10:00:02Z"
        })
        _update_room_metadata(room_id, preview="오늘은 맑고 쾌청합니다.", delta_msgs=1, session_id="sid-weather-1")

        # 2. GET /api/chat/rooms 에서 방 목록 및 메타데이터 확인
        res_rooms = await self.client.request("GET", "/api/chat/rooms", headers=headers)
        self.assertEqual(res_rooms.status, 200)
        rooms_data = await res_rooms.json()
        self.assertTrue(rooms_data.get("ok"))
        target = next((r for r in rooms_data.get("rooms", []) if r.get("id") == room_id), None)
        self.assertIsNotNone(target)
        self.assertEqual(target.get("title"), "오늘 날씨 어때?")
        self.assertEqual(target.get("messageCount"), 2)

        # 3. GET /api/chat/history?roomId=... 에서 전체 히스토리 파싱 검증
        res_hist = await self.client.request("GET", f"/api/chat/history?roomId={room_id}", headers=headers)
        self.assertEqual(res_hist.status, 200)
        hist_data = await res_hist.json()
        self.assertTrue(hist_data.get("ok"))
        messages = hist_data.get("messages", [])
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["role"], "user")
        self.assertEqual(messages[0]["text"], "오늘 날씨 어때?")
        self.assertEqual(messages[1]["role"], "assistant")
        self.assertEqual(messages[1]["text"], "오늘은 맑고 쾌청합니다.")
        self.assertEqual(len(messages[1]["steps"]), 1)

        # 4. 방 삭제 시 chats/<roomId>.jsonl 파일 및 인덱스에서 완전 제거되는지 확인
        del_res = await self.client.request(
            "POST",
            "/api/session/delete",
            headers=headers,
            json={"roomId": room_id, "sessionId": "sid-weather-1"},
        )
        self.assertEqual(del_res.status, 200)

        # 삭제 후 목록 및 히스토리 재확인
        res_after = await self.client.request("GET", "/api/chat/rooms", headers=headers)
        after_ids = [r.get("id") for r in (await res_after.json()).get("rooms", [])]
        self.assertNotIn(room_id, after_ids)

        hist_after = await self.client.request("GET", f"/api/chat/history?roomId={room_id}", headers=headers)
        self.assertEqual((await hist_after.json()).get("messages"), [])

    async def test_removed_audit_goals_ideas(self):
        """제거된 감사 로그(Audit), 목표, 아이디어 엔드포인트가 라우팅되지 않고 404/405 반환되는지 검증."""
        headers = {"Authorization": "Bearer super-secret-password-999"}

        # 제거된 Audit / Goals / Ideas 엔드포인트 (add_static("/") 로 인해 미등록 GET=404, POST=405)
        for method, path in (
            ("GET", "/api/activity/audit"),
            ("GET", "/api/goals"),
            ("POST", "/api/goals"),
            ("POST", "/api/goals/delete"),
            ("GET", "/api/ideas"),
        ):
            res = await self.client.request(method, path, headers=headers)
            self.assertIn(res.status, (404, 405), f"{method} {path} should not be routed")

    async def test_chat_rooms_excludes_zero_user_messages_and_hidden_rooms(self):
        """GET /api/chat/rooms 가 유저 메시지 0건 세션을 제외하고 숨김 처리된 세션을 올바르게 제외하는지 검증."""
        headers = {"Authorization": "Bearer super-secret-password-999"}
        from bot.web.server import _save_hidden_room, _load_hidden_rooms

        res = await self.client.request("GET", "/api/chat/rooms", headers=headers)
        self.assertEqual(res.status, 200)
        data = await res.json()
        self.assertTrue(data.get("ok"))
        rooms = data.get("rooms", [])

        # 0건 메시지인 방은 목록에 포함되지 않아야 함
        for r in rooms:
            self.assertGreater(r.get("messageCount", 0), 0)

        # 숨김 처리 테스트
        if rooms:
            target_id = rooms[0]["id"]
            _save_hidden_room(target_id)
            self.assertIn(target_id, _load_hidden_rooms())

            # 다시 방 목록 조회 시 target_id 가 제외되어야 함
            res2 = await self.client.request("GET", "/api/chat/rooms", headers=headers)
            rooms2 = (await res2.json()).get("rooms", [])
            self.assertNotIn(target_id, [r["id"] for r in rooms2])


class TestWebServerStandalone(unittest.TestCase):
    """웹 서버 단독 프로세스 실행 및 로깅, 락 단위 테스트."""

    def test_setup_web_logger(self):
        from bot.core.bot_logger import setup_web_logger
        from bot.core.paths import WEB_LOG_FILE

        logger = setup_web_logger()
        self.assertEqual(logger.name, "oshyun-web")
        paths_in_handlers = [getattr(h, "log_path", "") for h in logger.handlers]
        self.assertIn(WEB_LOG_FILE, paths_in_handlers)

    def test_instance_lock(self):
        import bot.web.server as web_server
        with tempfile.TemporaryDirectory() as tmp_dir:
            with patch.object(web_server, "LOGS_DIR", tmp_dir):
                self.assertTrue(web_server._acquire_instance_lock())
                if web_server._instance_lock_file:
                    web_server._instance_lock_file.close()
                    web_server._instance_lock_file = None

    def test_main_disabled_exits_zero(self):
        import bot.web.server as web_server
        with patch.object(web_server, "WEB_ENABLED", False):
            with self.assertRaises(SystemExit) as cm:
                web_server.main()
            self.assertEqual(cm.exception.code, 0)

    def test_static_index_html_empty_room_selection_renders_messages(self):
        """메시지가 없는 새 대화방 선택 시 기존 대화가 노출되지 않도록 renderMessagesForCurrentRoom 호출을 보장하는지 검증."""
        import os
        from bot.web.server import INTERNAL_STATIC_DIR

        content = _read_static_bundle(INTERNAL_STATIC_DIR)

        # selectRoom 내부에서 이전 메시지 잔상이 남지 않도록 renderMessagesForCurrentRoom 이 즉시 호출되는지 검증
        self.assertRegex(
            content,
            r"async function selectRoom\(roomId[^\)]*\)\s*\{[\s\S]*?renderMessagesForCurrentRoom\(\);",
        )
        # loadChatHistoryFromServer 에서 빈 배열이나 에러 시에도 renderMessagesForCurrentRoom 호출이 보장되는지 검증
        self.assertRegex(
            content,
            r"async function loadChatHistoryFromServer\(roomId, sessionId\)\s*\{[\s\S]*?renderMessagesForCurrentRoom\(\);",
        )

    def test_static_index_html_voice_input_clears_prompt_and_has_no_overlay(self):
        """음성 바로가기 오버레이가 제거되고, 음성 입력 전송 시 프롬프트창이 초기화되며, voice 주소 접속 시 원래 페이지에서 음성 입력이 활성화되는지 검증."""
        import os
        from bot.web.server import INTERNAL_STATIC_DIR

        content = _read_static_bundle(INTERNAL_STATIC_DIR)

        # 1. 풀스크린 음성 바로가기 오버레이(voice-overlay)가 제거되었는지 검증
        self.assertNotIn('id="voice-overlay"', content)
        self.assertNotIn("enterVoiceMode", content)

        # 2. sendUserMessage 에서 textOverride 유무와 무관하게 입력창을 초기화하는지 검증
        self.assertNotIn("if (!textOverride && input)", content)
        self.assertRegex(
            content,
            r"function sendUserMessage\(textOverride,\s*isVoiceTurn\s*=\s*false(?:\s*,\s*options\s*=\s*\{\})?\)\s*\{[\s\S]*?setChatInputText\(''\);",
        )

        # 3. /voice 경로 또는 ?voice=1 파라미터 접속 시 tryStartVoice 호출을 통해 원래 페이지에서 음성 입력이 활성화되는지 검증
        self.assertIn("window.location.pathname === '/voice'", content)
        self.assertIn("tryStartVoice(false)", content)

    def test_static_index_html_retry_skips_input_focus(self):
        """다시 실행(retryMessageByIndex) 시 입력창 focus()가 호출되지 않고 blur 처리되는지 검증."""
        import os
        from bot.web.server import INTERNAL_STATIC_DIR

        content = _read_static_bundle(INTERNAL_STATIC_DIR)

        self.assertIn("sendUserMessage(targetPrompt, false, { skipFocus: true })", content)
        self.assertIn("const skipFocus = Boolean(isVoiceTurn || (options && options.skipFocus));", content)
        self.assertIn("if (!skipFocus) {\n        focusChatInput();", content)

    def test_static_index_html_abort_preserves_chat_input(self):
        """답변 생성 중단 버튼 클릭 시 작성 중이던 입력창 텍스트가 지워지지 않고 보존되는지 검증."""
        import os
        from bot.web.server import INTERNAL_STATIC_DIR

        content = _read_static_bundle(INTERNAL_STATIC_DIR)

        # abortCurrentSession 함수 내에서 inputEl.value = '' 초기화가 수행되지 않는지 확인
        self.assertNotIn("abortCurrentSession(roomId = currentRoomId) {\n      currentSpeechTranscript = '';\n      hasDispatchedSpeech = true;\n      stopVoiceRecording(true);\n      const inputEl = document.getElementById('chat-input');\n      if (inputEl) { inputEl.value = '';", content)
        self.assertRegex(
            content,
            r"function abortCurrentSession\(roomId\s*=\s*currentRoomId\)\s*\{[\s\S]*?if \(inputEl\)\s*\{\s*isImeComposing\s*=\s*false;\s*onChatInput\(\);\s*\}",
        )

    def test_image_attach_icon_and_direct_open_in_index_html(self):
        """index.html 에 이미지 첨부 전용 아이콘 및 파일 선택기 원클릭 직접 실행, 클립보드 Paste 호환성이 올바르게 구현되어 있는지 검증."""
        import os
        from bot.web.server import INTERNAL_STATIC_DIR

        html = _read_static_bundle(INTERNAL_STATIC_DIR)

        # 1. 원클릭 이미지 첨부 버튼 (#btn-attach 및 triggerFileInput 직접 호출)
        self.assertIn('id="btn-attach"', html)
        self.assertIn('onclick="triggerFileInput()"', html)
        self.assertIn('title="이미지 첨부"', html)

        # 2. 이미지/사진 전용 SVG 아이콘 (Lucide 규격: rect + circle + path 능선)
        self.assertIn('<rect width="18" height="18" x="3" y="3" rx="2" ry="2"/>', html)
        self.assertIn('<circle cx="9" cy="9" r="2"/>', html)
        self.assertIn('<path d="m21 15-3.086-3.086a2 2 0 0 0-2.828 0L6 21"/>', html)

        # 3. 팝오버 메뉴 및 불필요한 레거시 팝업 로직 제거 검증
        self.assertNotIn('id="attach-menu-popover"', html)
        self.assertNotIn('class="attach-menu-popover"', html)
        self.assertNotIn("toggleAttachMenu", html)
        self.assertNotIn("onAttachClipboardOptionClick", html)
        self.assertNotIn("onAttachFileOptionClick", html)

        # 4. 파일 입력창 속성 및 원클릭 트리거 함수 구현
        self.assertIn('id="file-input"', html)
        self.assertIn('accept="image/*"', html)
        self.assertIn("function triggerFileInput(", html)
        self.assertIn("function handleFileSelect(", html)
        self.assertIn("function addAttachmentFiles(", html)

        # 5. 전역 클립보드 붙여넣기(Paste) 및 드래그앤드롭 호환성 검증
        self.assertIn("extractImagesFromDataTransfer", html)
        self.assertIn("addEventListener('paste'", html)
        self.assertIn("개의 클립보드 이미지가 첨부되었습니다.", html)
        self.assertIn("--radius", html)


class TestVoiceRouteServing(AioHTTPTestCase):
    """/voice 주소 접속 시 원래 페이지가 정상 서빙되는지 검증."""

    async def get_application(self):
        self.test_db_dir = tempfile.mkdtemp()
        self._cleanup_runtime = _install_runtime_isolation(self.test_db_dir)
        self.orig_chat_db_file = getattr(chat_db, "CHAT_DB_FILE", None)
        chat_db.CHAT_DB_FILE = os.path.join(self.test_db_dir, "test_chat.db")
        chat_db.init_db(chat_db.CHAT_DB_FILE, migrate_legacy=False)
        web_auth.WEB_AUTH_REQUIRED = False
        return web_server.create_web_app(static_dir=web_server.INTERNAL_STATIC_DIR)

    async def tearDownAsync(self):
        web_auth.WEB_AUTH_REQUIRED = True
        if getattr(self, "orig_chat_db_file", None):
            chat_db.CHAT_DB_FILE = self.orig_chat_db_file
        if hasattr(self, "_cleanup_runtime"):
            self._cleanup_runtime()
        shutil.rmtree(self.test_db_dir, ignore_errors=True)
        await super().tearDownAsync()

    async def test_voice_route_serves_main_index(self):
        for path in ("/voice", "/voice/", "/?voice=1"):
            res = await self.client.request("GET", path)
            self.assertEqual(res.status, 200)
            html = await res.text()
            self.assertIn('id="chat-input"', html)
            self.assertNotIn('id="voice-overlay"', html)

    async def test_static_css_and_js_modular_routes(self):
        """분리된 css/*.css 및 js/*.js 모듈 정적 파일이 200 및 올바른 Content-Type으로 서빙되는지 검증."""
        for css_rel in ("css/base.css", "css/layout.css", "css/chat.css"):
            res = await self.client.request("GET", f"/{css_rel}?v=1")
            self.assertEqual(res.status, 200, f"Failed to serve /{css_rel}")
            self.assertIn("text/css", res.headers.get("Content-Type", ""))
            body = await res.text()
            self.assertTrue(len(body) > 0)

        for js_rel in ("js/core.js", "js/ws.js", "js/composer.js"):
            res = await self.client.request("GET", f"/{js_rel}?v=1")
            self.assertEqual(res.status, 200, f"Failed to serve /{js_rel}")
            self.assertIn("javascript", res.headers.get("Content-Type", "").lower())
            body = await res.text()
            self.assertTrue(len(body) > 0)

    def test_static_index_html_chat_turn_queue(self):
        """index.html 의 대기열(Queue) UI 동작 로직이 올바르게 구현되어 있는지 검증."""
        import os
        from bot.web.server import INTERNAL_STATIC_DIR

        content = _read_static_bundle(INTERNAL_STATIC_DIR)

        # 1. handleWsEvent 에서 data.type === 'queued' 처리 확인
        self.assertIn("data.type === 'queued'", content)
        self.assertIn("대기열에 추가되었습니다", content)

        # 2. updateSendButtonState 에서 생성 중이어도 텍스트가 있으면 대기열 추가 버튼 활성화
        self.assertIn("btn.title = isRunning ? '대기열에 추가' : '전송'", content)

        # 3. onSendButtonClick 에서 텍스트가 있을 때는 abort 하지 않고 sendUserMessage 호출
        self.assertIn("if (live && live.isRunning && !hasText)", content)

        # 4. 대기열 메시지가 답변 생성 중 말풍선(liveRow) 아래쪽에 렌더링되고 시각적 배지/구분선을 포함하는지 검증
        for marker in (
            "function insertMessageBeforeQueued(",
            "function cancelQueuedMessageByIndex(",
            "function cancelAllQueuedMessages(",
            ".msg.user.queued .bubble",
            "queue-status-tag",
            "queue-divider",
            'id="live-queue-badge"',
            "답변 완료 후 순차 실행",
        ):
            self.assertIn(marker, content)

        # renderMessagesForCurrentRoom 내부에서 liveRow(답변 생성 중)가 activeEntries 뒤, queuedEntries 앞에 배치되는 순서 검증
        render_start = content.index("function renderMessagesForCurrentRoom(")
        render_block = content[render_start : render_start + 25000]
        active_pos = render_block.index("activeEntries.forEach")
        live_pos = render_block.index("liveRow.className = 'msg assistant live-generating'")
        queued_pos = render_block.index("queuedEntries.forEach")
        self.assertLess(active_pos, live_pos)
        self.assertLess(live_pos, queued_pos)

    def test_static_index_html_pull_to_refresh_bottom(self):
        """index.html 의 상단 및 하단 당겨서 새로고침(Pull to Refresh) 로직과 스타일 검증."""
        content = _read_static_bundle(web_server.INTERNAL_STATIC_DIR)

        # 상단 및 하단 당김 스타일 및 로직 검증 (대폭 엄격해진 판정 기준 포함)
        for marker in (
            ".ptr-indicator.from-bottom",
            "getDistFromBottom",
            "pullDirection = 'bottom'",
            "올려서 새로고침",
            "pullDirection = 'top'",
            "당겨서 새로고침",
        ):
            self.assertIn(marker, content)

    def test_static_index_html_effort_display(self):
        """index.html 의 응답 모델명 옆 Effort 표시 검증."""
        content = _read_static_bundle(web_server.INTERNAL_STATIC_DIR)

        for marker in (
            "metaParts.push(effortVal ? `${m.model} (${effortVal})` : m.model)",
            "const headEffort = m.effort",
            "liveEffort",
        ):
            self.assertIn(marker, content)



class TestChatTurnQueueWebSocket(AioHTTPTestCase):
    """WebSocket /ws 상에서 에이전트 응답 진행 중 인입된 채팅 요청이 큐(FIFO)로 적재되고 순차 실행되는지 검증."""

    async def get_application(self):
        web_auth.WEB_AUTH_REQUIRED = False
        self.test_db_dir = tempfile.mkdtemp()
        self._cleanup_runtime = _install_runtime_isolation(self.test_db_dir)
        self.orig_chat_db_file = getattr(chat_db, "CHAT_DB_FILE", None)
        self.orig_hidden_file = getattr(web_server, "HIDDEN_ROOMS_FILE", None)
        self.orig_chat_db_hidden_file = getattr(chat_db, "HIDDEN_ROOMS_FILE", None)
        self.orig_web_jobs_file = getattr(web_server, "WEB_JOBS_FILE", None)
        self.test_chat_db = os.path.join(self.test_db_dir, "test_chat.db")
        chat_db.CHAT_DB_FILE = self.test_chat_db
        test_hidden = os.path.join(self.test_db_dir, "hidden_rooms.json")
        web_server.HIDDEN_ROOMS_FILE = test_hidden
        chat_db.HIDDEN_ROOMS_FILE = test_hidden
        web_server.WEB_JOBS_FILE = os.path.join(self.test_db_dir, "web_jobs.json")
        chat_db.init_db(self.test_chat_db, migrate_legacy=False)
        return web_server.create_web_app(static_dir=web_server.INTERNAL_STATIC_DIR)

    async def tearDownAsync(self):
        web_auth.WEB_AUTH_REQUIRED = True
        if getattr(self, "orig_chat_db_file", None):
            chat_db.CHAT_DB_FILE = self.orig_chat_db_file
        if getattr(self, "orig_hidden_file", None):
            web_server.HIDDEN_ROOMS_FILE = self.orig_hidden_file
        if getattr(self, "orig_chat_db_hidden_file", None):
            chat_db.HIDDEN_ROOMS_FILE = self.orig_chat_db_hidden_file
        if getattr(self, "orig_web_jobs_file", None):
            web_server.WEB_JOBS_FILE = self.orig_web_jobs_file
        if hasattr(self, "_cleanup_runtime"):
            self._cleanup_runtime()
        shutil.rmtree(self.test_db_dir, ignore_errors=True)
        await super().tearDownAsync()

    async def test_websocket_chat_turn_queue_and_sequential_execution(self):
        """진행 중 턴이 있을 때 새 질문이 queued 로 적재되고 직전 턴 완료 후 순차 처리되는지 검증."""
        turn1_gate = asyncio.Event()

        fake_proc_1 = MagicMock(pid=1001)
        fake_proc_2 = MagicMock(pid=1002)

        async def fake_watch_agent(out_file, **kwargs):
            pid = kwargs.get("pid")
            if pid == 1001:
                await turn1_gate.wait()
                return {"result": "[테스트] 턴1 답변 완료", "session_id": "session-turn-1", "model": "agy"}
            else:
                return {"result": "[테스트] 턴2 답변 완료", "session_id": "session-turn-1", "model": "agy"}

        with patch("bot.web.server.spawn_agent") as mock_spawn, patch(
            'bot.web.server.watch_agent', side_effect=fake_watch_agent
        ):
            mock_spawn.side_effect = [fake_proc_1, fake_proc_2]

            ws = await self.client.ws_connect("/ws")

            # 1. 초기 상태 메시지 수신 (status)
            init_msg = await ws.receive_json(timeout=3.0)
            self.assertEqual(init_msg.get("type"), "status")

            # 2. 턴 1 요청 발송
            await ws.send_json({
                "type": "chat",
                "text": "[테스트] 질문 1",
                "roomId": "test-room-q",
                "newSession": True,
            })

            # 턴 1 started 수신
            started_1 = await ws.receive_json(timeout=3.0)
            self.assertEqual(started_1.get("type"), "started")
            self.assertEqual(started_1.get("userText"), "[테스트] 질문 1")

            # 3. 턴 1이 아직 끝나지 않은 상태에서 턴 2 요청 발송
            await ws.send_json({
                "type": "chat",
                "text": "[테스트] 질문 2",
                "roomId": "test-room-q",
                "newSession": False,
            })

            # 4. 턴 2가 큐에 등록되어 queued 이벤트가 즉시 회신되는지 검증
            queued_msg = await ws.receive_json(timeout=3.0)
            self.assertEqual(queued_msg.get("type"), "queued")
            self.assertEqual(queued_msg.get("roomId"), "test-room-q")
            self.assertEqual(queued_msg.get("text"), "[테스트] 질문 2")
            self.assertEqual(queued_msg.get("queueSize"), 1)

            # 5. 이제 턴 1 완료 게이트 오픈
            turn1_gate.set()

            # 턴 1 result 수신
            result_1 = await ws.receive_json(timeout=3.0)
            self.assertEqual(result_1.get("type"), "result")
            self.assertEqual(result_1.get("text"), "[테스트] 턴1 답변 완료")

            # status 업데이트 수신
            status_after_1 = await ws.receive_json(timeout=3.0)
            self.assertEqual(status_after_1.get("type"), "status")

            # 6. 큐에 있던 턴 2가 자동으로 started 및 result 까지 이어지는지 검증
            started_2 = await ws.receive_json(timeout=3.0)
            self.assertEqual(started_2.get("type"), "started")
            self.assertEqual(started_2.get("userText"), "[테스트] 질문 2")

            result_2 = await ws.receive_json(timeout=3.0)
            self.assertEqual(result_2.get("type"), "result")
            self.assertEqual(result_2.get("text"), "[테스트] 턴2 답변 완료")

            await ws.close()

    async def test_websocket_cancel_clears_queue_and_cancels_active(self):
        """생성 중단(cancel) 요청 시 현재 실행 작업과 큐에 대기 중이던 작업들이 모두 정리되는지 검증."""
        turn1_gate = asyncio.Event()
        fake_proc_1 = MagicMock(pid=2001)

        async def fake_watch_agent(out_file, **kwargs):
            try:
                await turn1_gate.wait()
                return {"result": "[테스트] 정상완료"}
            except asyncio.CancelledError:
                raise

        with patch("bot.web.server.spawn_agent", return_value=fake_proc_1), patch(
            'bot.web.server.watch_agent', side_effect=fake_watch_agent
        ), patch("bot.web.server.kill_job") as mock_kill:
            ws = await self.client.ws_connect("/ws")
            await ws.receive_json(timeout=3.0)  # status

            # 턴 1 시작
            await ws.send_json({
                "type": "chat",
                "text": "[테스트] 질문 1",
                "roomId": "room-cancel-test",
            })
            await ws.receive_json(timeout=3.0)  # started

            # 턴 2 큐에 적재
            await ws.send_json({
                "type": "chat",
                "text": "[테스트] 질문 2",
                "roomId": "room-cancel-test",
            })
            queued = await ws.receive_json(timeout=3.0)  # queued
            self.assertEqual(queued.get("type"), "queued")

            # cancel 발송
            await ws.send_json({
                "type": "cancel",
                "roomId": "room-cancel-test",
            })

            # cancelled 이벤트 수신
            cancelled_msg = await ws.receive_json(timeout=3.0)
            self.assertEqual(cancelled_msg.get("type"), "cancelled")
            self.assertEqual(cancelled_msg.get("roomId"), "room-cancel-test")

            # kill_job 호출되었는지 확인
            mock_kill.assert_called_with(2001)

            await ws.close()

    async def test_websocket_cancel_queued_item_preserves_running_turn(self):
        """대기열 개별 취소(cancel_queued) 요청 시 현재 진행 중인 턴은 중단되지 않고 대기열 항목만 제거되는지 검증."""
        turn1_gate = asyncio.Event()
        fake_proc_1 = MagicMock(pid=3001)

        async def fake_watch_agent(out_file, **kwargs):
            await turn1_gate.wait()
            return {"result": "[테스트] 턴1 정상 답변", "session_id": "sess-3001", "model": "agy"}

        with patch("bot.web.server.spawn_agent", return_value=fake_proc_1) as mock_spawn, patch(
            'bot.web.server.watch_agent', side_effect=fake_watch_agent
        ):
            ws = await self.client.ws_connect("/ws")
            await ws.receive_json(timeout=3.0)  # status

            # 1. 턴 1 시작
            await ws.send_json({
                "type": "chat",
                "text": "[테스트] 첫 번째 질문",
                "roomId": "room-cancel-q-item",
            })
            started_1 = await ws.receive_json(timeout=3.0)
            self.assertEqual(started_1.get("type"), "started")

            # 2. 턴 2, 턴 3 대기열 적재
            await ws.send_json({
                "type": "chat",
                "text": "[테스트] 두 번째 대기 질문",
                "roomId": "room-cancel-q-item",
            })
            q1 = await ws.receive_json(timeout=3.0)
            self.assertEqual(q1.get("type"), "queued")
            self.assertEqual(q1.get("queueSize"), 1)

            await ws.send_json({
                "type": "chat",
                "text": "[테스트] 세 번째 대기 질문",
                "roomId": "room-cancel-q-item",
            })
            q2 = await ws.receive_json(timeout=3.0)
            self.assertEqual(q2.get("type"), "queued")
            self.assertEqual(q2.get("queueSize"), 2)

            # 3. 두 번째 대기 질문 개별 취소 (cancel_queued)
            await ws.send_json({
                "type": "cancel_queued",
                "roomId": "room-cancel-q-item",
                "text": "[테스트] 두 번째 대기 질문",
            })
            q_cancel_1 = await ws.receive_json(timeout=3.0)
            self.assertEqual(q_cancel_1.get("type"), "queued_cancelled")
            self.assertEqual(q_cancel_1.get("removedCount"), 1)
            self.assertEqual(q_cancel_1.get("queueSize"), 1)

            # 4. 남은 대기열 전체 취소 (clearAll: True)
            await ws.send_json({
                "type": "cancel_queued",
                "roomId": "room-cancel-q-item",
                "clearAll": True,
            })
            q_cancel_all = await ws.receive_json(timeout=3.0)
            self.assertEqual(q_cancel_all.get("type"), "queued_cancelled")
            self.assertEqual(q_cancel_all.get("queueSize"), 0)

            # 5. 진행 중이던 턴 1은 중단되지 않고 정상 완료되어야 함
            turn1_gate.set()
            result_1 = await ws.receive_json(timeout=3.0)
            self.assertEqual(result_1.get("type"), "result")
            self.assertEqual(result_1.get("text"), "[테스트] 턴1 정상 답변")

            # spawn_agent 는 턴 1에 대해 딱 1번만 호출되었는지 검증
            self.assertEqual(mock_spawn.call_count, 1)

            await ws.close()


class TestMobileUiAndBatchRoomManagement(AioHTTPTestCase):
    """모바일 최적화 UI, 대화방 길게 누르기 다중 선택/일괄 삭제/이름 변경 API 및 정적 HTML 검증."""

    async def get_application(self):
        import bot.core.chat_db as chat_db

        self._tmp_dir = tempfile.TemporaryDirectory()
        self._cleanup_runtime = _install_runtime_isolation(self._tmp_dir.name)
        self.db_path = os.path.join(self._tmp_dir.name, "chat_test.db")
        self._db_patcher = patch("bot.core.chat_db.CHAT_DB_FILE", self.db_path)
        self._db_patcher.start()
        test_hidden = os.path.join(self._tmp_dir.name, "hidden_rooms.json")
        self._hidden_patcher = patch(
            'bot.web.server.HIDDEN_ROOMS_FILE',
            test_hidden,
        )
        self._hidden_patcher.start()
        self._chat_db_hidden_patcher = patch(
            'bot.core.chat_db.HIDDEN_ROOMS_FILE',
            test_hidden,
        )
        self._chat_db_hidden_patcher.start()
        chat_db.init_db(db_path=self.db_path, migrate_legacy=False)
        web_auth.WEB_AUTH_REQUIRED = False
        return web_server.create_web_app(static_dir=web_server.INTERNAL_STATIC_DIR)

    async def tearDownAsync(self):
        web_auth.WEB_AUTH_REQUIRED = True
        self._hidden_patcher.stop()
        if hasattr(self, "_chat_db_hidden_patcher"):
            self._chat_db_hidden_patcher.stop()
        self._db_patcher.stop()
        if hasattr(self, "_cleanup_runtime"):
            self._cleanup_runtime()
        self._tmp_dir.cleanup()
        await super().tearDownAsync()

    async def test_api_batch_delete_rooms(self):
        """POST /api/session/batch-delete 호출 시 선택된 여러 대화방이 한 번에 삭제되는지 검증."""
        import bot.core.chat_db as chat_db

        chat_db.get_or_create_room("room-a", session_id="sess-a", title="[테스트] 대화방 A")
        chat_db.get_or_create_room("room-b", session_id="sess-b", title="[테스트] 대화방 B")
        chat_db.get_or_create_room("room-c", session_id="sess-c", title="[테스트] 대화방 C")

        res = await self.client.post(
            "/api/session/batch-delete",
            json={
                "roomIds": ["room-a", "room-b"],
                "sessionIds": ["sess-a", "sess-b"],
            },
        )
        self.assertEqual(res.status, 200)
        data = await res.json()
        self.assertTrue(data.get("ok"))
        self.assertGreaterEqual(data.get("deletedCount", 0), 2)

        remaining = chat_db.get_rooms()
        remaining_ids = [r["id"] for r in remaining]
        self.assertNotIn("room-a", remaining_ids)
        self.assertNotIn("room-b", remaining_ids)
        self.assertIn("room-c", remaining_ids)

    async def test_api_rename_room(self):
        """POST /api/chat/room/rename 호출 시 대화방 제목이 변경되는지 검증."""
        import bot.core.chat_db as chat_db

        chat_db.get_or_create_room("room-rename-1", session_id="sess-rename-1", title="[테스트] 기존 제목")

        res = await self.client.post(
            "/api/chat/room/rename",
            json={
                "roomId": "room-rename-1",
                "sessionId": "sess-rename-1",
                "title": "[테스트] 변경된 새 제목",
            },
        )
        self.assertEqual(res.status, 200)
        data = await res.json()
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("title"), "[테스트] 변경된 새 제목")

        room = chat_db.get_room("room-rename-1")
        self.assertIsNotNone(room)
        self.assertEqual(room["title"], "[테스트] 변경된 새 제목")

    def test_static_index_html_mobile_polish_and_long_press_selection(self):
        """index.html 에 대화방 롱프레스 다중 선택, 일괄 삭제/고정/이름변경 및 모바일 최적화 기능이 포함되어 있는지 검증."""
        content = _read_static_bundle(web_server.INTERNAL_STATIC_DIR)

        # 대화방 롱프레스 다중 선택 및 일괄 액션 함수 확인
        for fn_sig in (
            "function enterRoomSelectionMode(",
            "function exitRoomSelectionMode(",
            "function toggleRoomSelection(",
            "function toggleSelectAllRooms(",
            "function deleteSelectedRooms(",
            "async function executeBatchDeleteRooms(",
            "function togglePinSelectedRooms(",
            "async function submitRenameRoom(",
            "function bindRoomLongPressEvents(",
        ):
            self.assertIn(fn_sig, content)

        # 메시지 롱프레스 즉시 복사(바텀시트 모달은 제거됨), 스와이프 제스처, 키보드 뷰포트 핸들러 확인
        self.assertNotIn('id="mobile-bottom-nav"', content)
        self.assertNotIn('id="sheet-msg-actions"', content)
        self.assertNotIn('openMsgActionSheet', content)
        for marker in (
            'id="room-bulk-bar"',
            'id="btn-scroll-bottom"',
            "function bindMessageLongPressEvents(",
            "function initMobileSwipeGestures(",
            "function initVisualViewportHandler(",
        ):
            self.assertIn(marker, content)


class TestCoreOrchestratorAgent(AioHTTPTestCase):
    """총괄 에이전트(Core Orchestrator Agent) API, AGY 세션 제어 및 UI 슬롯 검증."""

    async def get_application(self):
        import bot.core.chat_db as chat_db

        self._tmp_dir = tempfile.TemporaryDirectory()
        self._cleanup_runtime = _install_runtime_isolation(self._tmp_dir.name)
        self.db_path = os.path.join(self._tmp_dir.name, "chat_orch_test.db")
        self._db_patcher = patch("bot.core.chat_db.CHAT_DB_FILE", self.db_path)
        self._db_patcher.start()
        test_hidden = os.path.join(self._tmp_dir.name, "hidden_rooms.json")
        self._hidden_patcher = patch(
            'bot.web.server.HIDDEN_ROOMS_FILE',
            test_hidden,
        )
        self._hidden_patcher.start()
        self._chat_db_hidden_patcher = patch(
            'bot.core.chat_db.HIDDEN_ROOMS_FILE',
            test_hidden,
        )
        self._chat_db_hidden_patcher.start()
        self._chats_dir_patcher = patch(
            'bot.web.server.CHATS_DIR',
            os.path.join(self._tmp_dir.name, "chats"),
        )
        self._chats_dir_patcher.start()
        self._rooms_file_patcher = patch(
            'bot.web.server.CHAT_ROOMS_FILE',
            os.path.join(self._tmp_dir.name, "chat_rooms.json"),
        )
        self._rooms_file_patcher.start()
        self._rooms_backup_patcher = patch(
            'bot.web.server.CHAT_ROOMS_BACKUP_FILE',
            os.path.join(self._tmp_dir.name, "chat_rooms_backup.json"),
        )
        self._rooms_backup_patcher.start()
        self._migrate_patcher = patch("bot.core.chat_db.migrate_legacy_files_if_needed")
        self._migrate_patcher.start()
        self._tasks_patcher = patch(
            'bot.master.master_tasks.MASTER_TASKS_FILE',
            os.path.join(self._tmp_dir.name, "master_tasks.json"),
        )
        self._tasks_patcher.start()
        self._memory_patcher = patch(
            'bot.master.master_memory.MASTER_MEMORY_FILE',
            os.path.join(self._tmp_dir.name, "master_memory.json"),
        )
        self._memory_patcher.start()
        chat_db.init_db(db_path=self.db_path, migrate_legacy=False)
        web_auth.WEB_AUTH_REQUIRED = False
        return web_server.create_web_app(static_dir=web_server.INTERNAL_STATIC_DIR)

    async def tearDownAsync(self):
        web_auth.WEB_AUTH_REQUIRED = True
        web_server._room_session_ids.pop("__core__", None)
        self._memory_patcher.stop()
        self._tasks_patcher.stop()
        self._migrate_patcher.stop()
        self._rooms_backup_patcher.stop()
        self._rooms_file_patcher.stop()
        self._chats_dir_patcher.stop()
        self._hidden_patcher.stop()
        if hasattr(self, "_chat_db_hidden_patcher"):
            self._chat_db_hidden_patcher.stop()
        self._db_patcher.stop()
        if hasattr(self, "_cleanup_runtime"):
            self._cleanup_runtime()
        self._tmp_dir.cleanup()
        await super().tearDownAsync()

    async def test_orchestrator_sessions_list_and_read(self):
        """GET /api/orchestrator/sessions 및 /api/orchestrator/session/read 로 AGY 세션 조회 검증."""
        import bot.core.chat_db as chat_db

        chat_db.get_or_create_room("sess-alpha-111", session_id="sess-alpha-111", title="[테스트] 주식 포트폴리오 점검")
        chat_db.add_message("sess-alpha-111", "user", "[테스트] 현재 보유 주식 정리해줘", session_id="sess-alpha-111")
        chat_db.add_message("sess-alpha-111", "assistant", "[테스트] 보유 종목은 3개입니다.", session_id="sess-alpha-111", model="agy")

        # 1. 세션 목록 조회
        res_list = await self.client.get("/api/orchestrator/sessions?limit=20")
        self.assertEqual(res_list.status, 200)
        list_data = await res_list.json()
        self.assertTrue(list_data.get("ok"))
        sessions = list_data.get("sessions", [])
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["roomId"], "sess-alpha-111")
        self.assertEqual(sessions[0]["sessionId"], "sess-alpha-111")
        self.assertEqual(sessions[0]["title"], "[테스트] 주식 포트폴리오 점검")
        self.assertIn("agy --conversation sess-alpha-111", sessions[0]["agyCmd"])

        # 2. 제목 부분 일치로 세션 대화 내용 열람
        res_read = await self.client.get("/api/orchestrator/session/read?target=주식&limit=10")
        self.assertEqual(res_read.status, 200)
        read_data = await res_read.json()
        self.assertTrue(read_data.get("ok"))
        self.assertEqual(read_data["session"]["roomId"], "sess-alpha-111")
        self.assertEqual(len(read_data["messages"]), 2)
        self.assertEqual(read_data["messages"][0]["text"], "[테스트] 현재 보유 주식 정리해줘")
        self.assertEqual(read_data["messages"][1]["text"], "[테스트] 보유 종목은 3개입니다.")

    async def test_orchestrator_dispatch_send_and_spawn(self):
        """POST /api/orchestrator/dispatch 로 기존 세션 메시지 전송 및 신규 AGY 세션 스폰 검증."""
        import bot.core.chat_db as chat_db

        chat_db.get_or_create_room("sess-target-1", session_id="sess-target-1", title="[테스트] 캘린더 일정 방")

        mock_proc = MagicMock(pid=54321, out_file="/tmp/mock_orch_out.jsonl")
        mock_watch_return = {
            "result": "[테스트] 일정 업데이트를 완료했습니다.",
            "session_id": "sess-target-1",
            "model": "g3-flash",
            "interrupted": False,
        }

        with patch("bot.web.server.spawn_agent", return_value=mock_proc), patch(
            'bot.web.server.watch_agent',
            new_callable=AsyncMock,
            return_value=mock_watch_return,
        ), patch("bot.web.server.set_session"), patch("bot.web.server.update_last_activity"), patch(
            'bot.web.server.vault_log'
        ):
            # 1) 기존 세션에 동기(wait=True) 지시 전송
            res_send = await self.client.post(
                "/api/orchestrator/dispatch",
                json={
                    "action": "send",
                    "target": "캘린더 일정 방",
                    "message": "[테스트] 오늘 오후 3시 미팅 추가해줘",
                    "wait": True,
                },
            )
            self.assertEqual(res_send.status, 200)
            send_data = await res_send.json()
            self.assertTrue(send_data.get("ok"))
            self.assertEqual(send_data.get("status"), "completed")
            self.assertEqual(send_data.get("reply"), "[테스트] 일정 업데이트를 완료했습니다.")

            msgs = chat_db.get_messages("sess-target-1")
            self.assertEqual(len(msgs), 2)
            self.assertEqual(msgs[0]["channel"], "orchestrator")
            self.assertEqual(msgs[0]["text"], "[테스트] 오늘 오후 3시 미팅 추가해줘")
            self.assertEqual(msgs[1]["text"], "[테스트] 일정 업데이트를 완료했습니다.")

        # 2) 새 세션 스폰 (wait=True)
        mock_spawn_watch = {
            "result": "[테스트] 새 보험 청구 세션이 생성되었습니다.",
            "session_id": "new-spawned-agy-uuid",
            "model": "g3-flash",
            "interrupted": False,
        }
        with patch("bot.web.server.spawn_agent", return_value=mock_proc), patch(
            'bot.web.server.watch_agent',
            new_callable=AsyncMock,
            return_value=mock_spawn_watch,
        ), patch("bot.web.server.set_session"), patch("bot.web.server.update_last_activity"), patch(
            'bot.web.server.vault_log'
        ):
            res_spawn = await self.client.post(
                "/api/orchestrator/dispatch",
                json={
                    "action": "spawn",
                    "title": "[테스트] 실손보험 청구 작업방",
                    "message": "[테스트] 최근 진료 내역 확인해줘",
                    "wait": True,
                },
            )
            self.assertEqual(res_spawn.status, 200)
            spawn_data = await res_spawn.json()
            self.assertTrue(spawn_data.get("ok"))
            self.assertEqual(spawn_data.get("sessionId"), "new-spawned-agy-uuid")
            self.assertTrue(str(spawn_data.get("roomId") or "").startswith("room-"))
            self.assertEqual(spawn_data.get("title"), "[테스트] 실손보험 청구 작업방")
            self.assertEqual(spawn_data.get("reply"), "[테스트] 새 보험 청구 세션이 생성되었습니다.")

    async def test_spawned_room_immediately_appears_in_chat_rooms_index(self):
        """마스터 에이전트가 비동기(wait=False)로 스폰한 채팅방이 /api/chat/rooms 목록에 즉시 잡히는지 검증."""
        import bot.core.chat_db as chat_db

        mock_proc = MagicMock(pid=65432, out_file="/tmp/mock_spawn_async.jsonl")
        mock_watch = {
            "result": "[테스트] 비동기 세션 작업 완료",
            "session_id": "async-spawned-uuid",
            "model": "g3-flash",
            "interrupted": False,
        }

        with patch("bot.web.server.spawn_agent", return_value=mock_proc), patch(
            'bot.web.server.watch_agent',
            new_callable=AsyncMock,
            return_value=mock_watch,
        ), patch("bot.web.server.set_session"), patch("bot.web.server.update_last_activity"), patch(
            'bot.web.server.vault_log'
        ), patch("bot.web.execution._notify_master_agent", new_callable=AsyncMock):
            # 1. 비동기 스폰 요청 (마스터 완료보고 턴은 mock 이 같은 session_id 를 돌려줘 방이 병합되므로 격리)
            res = await self.client.post(
                "/api/orchestrator/dispatch",
                json={
                    "action": "spawn",
                    "title": "[테스트] 모바일 하단 탭 제거",
                    "message": "[요청: 모바일 하단 탭 제거] 관련 CSS와 HTML 정리해줘",
                    "wait": False,
                },
            )
            self.assertEqual(res.status, 200)
            data = await res.json()
            self.assertTrue(data.get("ok"))
            self.assertEqual(data.get("status"), "started")
            spawned_rid = data.get("roomId")
            self.assertTrue(spawned_rid and spawned_rid.startswith("room-"))

            # 2. 스폰 직후 /api/chat/rooms 호출 시 해당 방이 사이드바 목록에 즉시 잡혀야 함
            res_rooms = await self.client.get("/api/chat/rooms")
            self.assertEqual(res_rooms.status, 200)
            rooms_data = await res_rooms.json()
            self.assertTrue(rooms_data.get("ok"))
            found = next((r for r in rooms_data.get("rooms", []) if r["id"] == spawned_rid), None)
            self.assertIsNotNone(found, f"스폰된 방 {spawned_rid}이 /api/chat/rooms 목록에 잡히지 않음")
            self.assertEqual(found["title"], "[테스트] 모바일 하단 탭 제거")
            self.assertGreaterEqual(found["messageCount"], 1)

            # 3. 백그라운드 워커가 턴을 완료할 때까지 잠시 대기
            await asyncio.sleep(0.1)

            # 4. 완료 후 메시지 중복 없이 사용자 지시 + 어시스턴트 답변 총 2개 존재 확인
            msgs = chat_db.get_messages(spawned_rid)
            self.assertEqual(len(msgs), 2)
            self.assertEqual(msgs[0]["role"], "user")
            self.assertEqual(msgs[0]["channel"], "orchestrator")
            self.assertEqual(msgs[1]["role"], "assistant")
            self.assertIn("비동기 세션 작업 완료", msgs[1]["text"])

    async def test_orchestrator_spawn_reuses_similar_room(self):
        """유사한 주제/질문을 다루던 기존 채팅방이 있으면 spawn 시 새 방을 만들지 않고 기존 방을 이어서 진행하는지 검증."""
        import bot.core.chat_db as chat_db
        import bot.web.orchestrator as web_orchestrator

        # 기존 방 1: 카드 혜택 방
        chat_db.get_or_create_room(
            "room-card-11",
            session_id="sess-card-11",
            title="11월 카드 혜택 및 체리피킹 실적 정리",
        )
        chat_db.add_message(
            "room-card-11",
            "user",
            "이번 달 카드 혜택과 프로모션 실적 달성 현황 정리해줘",
            session_id="sess-card-11",
        )
        chat_db.add_message(
            "room-card-11",
            "assistant",
            "KB 굿데이카드 실적 달성률 및 주유 혜택 요약입니다.",
            session_id="sess-card-11",
            model="agy",
        )
        # 기존 방 2: UI 레이아웃 방 (범용 단어 '웹/UI' 만 겹치는 오탐 방지 검증용)
        chat_db.get_or_create_room(
            "room-ui-layout",
            session_id="sess-ui-layout",
            title="웹 UI 볼트 뷰 레이아웃 버그 수정",
        )
        chat_db.add_message(
            "room-ui-layout",
            "user",
            "볼트 탐색기와 대화창이 겹치는 CSS 레이아웃 버그 수정해줘",
            session_id="sess-ui-layout",
        )

        mock_proc = MagicMock(pid=71111, out_file="/tmp/mock_reuse_out.jsonl")
        mock_watch_reuse = {
            "result": "카드 혜택 달성률을 최신 기준으로 다시 확인했습니다.",
            "session_id": "sess-card-11",
            "model": "g3-flash",
            "interrupted": False,
        }

        with patch("bot.web.server.spawn_agent", return_value=mock_proc) as mock_spawn, patch(
            'bot.web.server.watch_agent',
            new_callable=AsyncMock,
            return_value=mock_watch_reuse,
        ), patch("bot.web.server.set_session"), patch("bot.web.server.update_last_activity"), patch(
            'bot.web.server.vault_log'
        ):
            # 1) 매우 유사한 주제로 spawn 요청 -> 기존 room-card-11 재사용
            res_reuse = await self.client.post(
                "/api/orchestrator/dispatch",
                json={
                    "action": "spawn",
                    "title": "카드 혜택 및 실적 추가 확인",
                    "message": "이번 달 KB 굿데이 카드 혜택 달성률 다시 확인해줘",
                    "wait": True,
                },
            )
            self.assertEqual(res_reuse.status, 200)
            reuse_data = await res_reuse.json()
            self.assertTrue(reuse_data.get("ok"))
            self.assertTrue(reuse_data.get("reused"))
            self.assertGreaterEqual(float(reuse_data.get("matchedSimilarity") or 0), 0.68)
            self.assertEqual(reuse_data.get("roomId"), "room-card-11")
            self.assertEqual(reuse_data.get("sessionId"), "sess-card-11")
            self.assertEqual(mock_spawn.call_args.args[1], "sess-card-11")
            # 기존 방과 새 요청 주제가 결합된 통합 제목 검증
            expected_merged_title = web_orchestrator._merge_room_titles(
                "11월 카드 혜택 및 체리피킹 실적 정리", "카드 혜택 및 실적 추가 확인"
            )
            self.assertEqual(reuse_data.get("title"), expected_merged_title)
            db_room_after = chat_db.get_room("room-card-11")
            self.assertIsNotNone(db_room_after)
            self.assertEqual(db_room_after.get("title"), expected_merged_title)
            rsum_after = master_tasks.get_room_summary("room-card-11")
            self.assertIsNotNone(rsum_after)
            self.assertEqual(rsum_after.get("title"), expected_merged_title)

        # 2) 범용 단어('UI')만 겹치고 주제가 다른 요청 -> 새 방 생성(reused=False)
        mock_watch_new = {
            "result": "관제 패널 닫기 버튼을 정리했습니다.",
            "session_id": "sess-panel-new",
            "model": "g3-flash",
            "interrupted": False,
        }
        with patch("bot.web.server.spawn_agent", return_value=mock_proc), patch(
            'bot.web.server.watch_agent',
            new_callable=AsyncMock,
            return_value=mock_watch_new,
        ), patch("bot.web.server.set_session"), patch("bot.web.server.update_last_activity"), patch(
            'bot.web.server.vault_log'
        ):
            res_unrelated = await self.client.post(
                "/api/orchestrator/dispatch",
                json={
                    "action": "spawn",
                    "title": "관제 패널 투명 UI 닫기 버튼 정리",
                    "message": "모바일 드로어 닫기 아이콘 스타일과 터치 영역 개선해줘",
                    "wait": True,
                },
            )
            self.assertEqual(res_unrelated.status, 200)
            unrelated_data = await res_unrelated.json()
            self.assertTrue(unrelated_data.get("ok"))
            self.assertFalse(unrelated_data.get("reused"))
            self.assertNotIn("matchedSimilarity", unrelated_data)
            self.assertTrue(str(unrelated_data.get("roomId") or "").startswith("room-"))
            self.assertNotEqual(unrelated_data.get("roomId"), "room-ui-layout")

            # 3) 유사한 방이 있어도 forceNew=True 지정 시 새 방 강제 생성
            res_forced = await self.client.post(
                "/api/orchestrator/dispatch",
                json={
                    "action": "spawn",
                    "title": "카드 혜택 및 실적 추가 확인",
                    "message": "이번 달 KB 굿데이 카드 혜택 달성률 다시 확인해줘",
                    "forceNew": True,
                    "wait": True,
                },
            )
            self.assertEqual(res_forced.status, 200)
            forced_data = await res_forced.json()
            self.assertTrue(forced_data.get("ok"))
            self.assertFalse(forced_data.get("reused"))
            self.assertTrue(str(forced_data.get("roomId") or "").startswith("room-"))
            self.assertNotEqual(forced_data.get("roomId"), "room-card-11")

    def test_orchestrator_room_similarity_scoring_and_target_fallback(self):
        """한국어 복합어·조사 정규화, 시간축 분리 및 target 유사 검색 폴백 검증."""
        import bot.core.chat_db as chat_db
        import bot.web.orchestrator as web_orchestrator

        # 복합명사/조사 매칭: '실손보험 청구' <-> '보험청구 내역 업데이트' (임계값 0.68 상향 후에도 충분히 높은 0.85로 통과)
        score_insurance = web_orchestrator._score_room_similarity(
            "보험청구 내역 업데이트",
            "바른세상병원 족부 진료 실손보험금 청구 결과 반영해줘",
            {
                "id": "r-ins",
                "title": "실손보험 청구 작업방",
                "preview": "최근 진료 내역과 농협/KB 실손보험 청구 현황입니다.",
            },
        )
        self.assertGreaterEqual(score_insurance, 0.68)

        # 시간축 분리: '내일 할일 및 일정 조사' vs '12월 일정 및 주요 마일스톤 조사'
        score_schedule_diff = web_orchestrator._score_room_similarity(
            "다음 달(12월) 일정 및 주요 마일스톤 조사",
            "12월 주요 이벤트와 마감 작업 정리해줘",
            {
                "id": "r-tmr",
                "title": "내일 할일 및 일정 조사",
                "preview": "내일(11/14 토) 등록된 캘린더 일정은 없습니다.",
            },
        )
        self.assertLess(score_schedule_diff, 0.68)

        # 느슨한 유사도(약 0.64 수준)의 경우 이전(0.52)에는 매칭되었으나 상향된 기준(0.68)에서는 분리되는지 검증
        score_loose = web_orchestrator._score_room_similarity(
            "11월 주유비 카드 혜택",
            "이번 달 주유소 이용 내역과 주유비 결제 카드 혜택 좀 확인해줘",
            {
                "id": "room-card-11",
                "title": "11월 카드 혜택 및 체리피킹 실적 정리",
                "preview": "KB 굿데이카드 실적 달성률 및 주유 혜택 요약입니다.",
            },
        )
        self.assertLess(score_loose, 0.68)
        self.assertGreater(score_loose, 0.52)

        # target 키워드 표현이 약간 달라도 _resolve_orchestrator_target_room 5단계 폴백으로 탐지
        chat_db.get_or_create_room(
            "room-gas-promo",
            session_id="sess-gas-promo",
            title="오늘 주유소에서 네이버페이 그걸로 그 카드로",
        )
        chat_db.update_room(
            "room-gas-promo",
            preview="네이버페이 GS칼텍스 주유 프로모션 종료일(~2027-04-21) 반영 완료",
        )
        resolved = web_orchestrator._resolve_orchestrator_target_room("네이버페이 GS칼텍스 주유 프로모션")
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.get("id"), "room-gas-promo")

        # _merge_room_titles 단위 검증: 동일/포함 관계 유지, 상이 주제 결합 및 36자 제한
        self.assertEqual(
            web_orchestrator._merge_room_titles("실손보험 청구", "실손보험 청구"),
            "실손보험 청구",
        )
        self.assertEqual(
            web_orchestrator._merge_room_titles("식재료 관리", "식재료 관리 허브"),
            "식재료 관리 허브",
        )
        merged_two = web_orchestrator._merge_room_titles("식재료 관리", "양배추 보관법")
        self.assertEqual(merged_two, "식재료 관리 · 양배추 보관법")
        self.assertLessEqual(len(merged_two), 36)

        long_merged = web_orchestrator._merge_room_titles(
            "11월 카드 혜택 및 체리피킹 실적 정리", "주유비 결제 카드 혜택 추가 확인"
        )
        self.assertLessEqual(len(long_merged), 36)
        self.assertIn(" · ", long_merged)

    async def test_orchestrator_room_summaries_matching_and_apis(self):
        """room_summaries 가 preview 덮어쓰기 상황에서도 유사 방 매칭에 활용되며 summaries/summary API가 정상 동작하는지 검증."""
        import bot.core.chat_db as chat_db

        # 기존 방 생성 후 preview 가 일반 문구로 덮어쓰여진 상태 시뮬레이션
        chat_db.get_or_create_room(
            "room-ins-summary",
            session_id="sess-ins-summary",
            title="병원 상담",
        )
        chat_db.update_room(
            "room-ins-summary",
            preview="웹 서버를 재기동했습니다. 무엇을 도와드릴까요?",
        )
        master_tasks.upsert_room_summary(
            "room-ins-summary",
            title="병원 상담",
            session_id="sess-ins-summary",
            query="바른세상병원 족부 진료 실손보험 청구 계산해줘",
            result_summary="농협 실손보험 예상환급액은 42,500원입니다.",
            status="done",
        )

        # 아카이브 방 (기본 summaries 목록에서 제외되어야 함)
        chat_db.get_or_create_room(
            "room-arch-summary",
            session_id="sess-arch-summary",
            title="과거 보관 방",
        )
        chat_db.archive_room("room-arch-summary", is_archived=True)
        master_tasks.upsert_room_summary(
            "room-arch-summary",
            title="과거 보관 방",
            summary="보관된 채팅방 요약본",
        )

        # 1. GET /api/orchestrator/summaries — 활성 요약본만 조회 & includeArchived=1 시 보관 방 포함
        res_sum = await self.client.get("/api/orchestrator/summaries")
        self.assertEqual(res_sum.status, 200)
        data_sum = await res_sum.json()
        self.assertTrue(data_sum.get("ok"))
        sum_ids = [s["roomId"] for s in data_sum.get("summaries", [])]
        self.assertIn("room-ins-summary", sum_ids)
        self.assertNotIn("room-arch-summary", sum_ids)

        res_sum_arch = await self.client.get("/api/orchestrator/summaries?includeArchived=1")
        self.assertEqual(res_sum_arch.status, 200)
        arch_ids = [s["roomId"] for s in (await res_sum_arch.json()).get("summaries", [])]
        self.assertIn("room-arch-summary", arch_ids)

        # 2. POST /api/orchestrator/session/summary — 요약본 명시적 갱신 및 read/sessions 노출 확인
        res_upd = await self.client.post(
            "/api/orchestrator/session/summary",
            json={
                "target": "room-ins-summary",
                "summary": "바른세상병원 족부 진료 실손보험금 농협 청구 및 예상환급액 42,500원 산출 완료",
                "note": "유사 질문 이어쓰기 기준 방",
            },
        )
        self.assertEqual(res_upd.status, 200)
        upd_data = await res_upd.json()
        self.assertTrue(upd_data.get("ok"))
        self.assertEqual(upd_data.get("roomId"), "room-ins-summary")
        self.assertIn("바른세상병원 족부 진료 실손보험금", upd_data.get("summary", ""))

        res_read = await self.client.get("/api/orchestrator/session/read?target=room-ins-summary")
        self.assertEqual(res_read.status, 200)
        read_data = await res_read.json()
        self.assertIn("바른세상병원 족부 진료 실손보험금", read_data["session"].get("summary", ""))
        self.assertEqual(read_data["roomSummary"].get("lastNote"), "유사 질문 이어쓰기 기준 방")

        # 3. preview 가 무관해도 room_summaries 를 통해 spawn 시 기존 방(room-ins-summary) 자동 재사용
        mock_proc = MagicMock(pid=72222, out_file="/tmp/mock_sum_reuse.jsonl")
        mock_watch = {
            "result": "실손보험 입금 내역까지 반영했습니다.",
            "session_id": "sess-ins-summary",
            "model": "g3-flash",
            "interrupted": False,
        }
        with patch("bot.web.server.spawn_agent", return_value=mock_proc), patch(
            'bot.web.server.watch_agent',
            new_callable=AsyncMock,
            return_value=mock_watch,
        ), patch("bot.web.server.set_session"), patch("bot.web.server.update_last_activity"), patch(
            'bot.web.server.vault_log'
        ):
            res_spawn = await self.client.post(
                "/api/orchestrator/dispatch",
                json={
                    "action": "spawn",
                    "title": "실손보험 확인",
                    "message": "바른세상병원 족부 진료 농협 실손보험금 예상환급액 다시 확인해줘",
                    "wait": True,
                },
            )
            self.assertEqual(res_spawn.status, 200)
            spawn_data = await res_spawn.json()
            self.assertTrue(spawn_data.get("ok"))
            self.assertTrue(spawn_data.get("reused"))
            self.assertEqual(spawn_data.get("roomId"), "room-ins-summary")
            self.assertIn("바른세상병원", spawn_data.get("matchedSummary", ""))

    async def test_core_orchestrator_room_turn_passes_orchestrator_flag(self):
        """roomId='__core__' 대화 턴 실행 시 spawn_agent 에 orchestrator=True 가 전달되고 ID가 유지되는지 검증."""
        mock_proc = MagicMock(pid=77777, out_file="/tmp/mock_core_out.jsonl")
        mock_watch_return = {
            "result": "전체 3개 세션을 점검했습니다.",
            "session_id": "core-agy-session-999",
            "model": "g3-flash",
            "interrupted": False,
        }
        with patch("bot.web.server.spawn_agent", return_value=mock_proc) as mock_spawn, patch(
            'bot.web.server.watch_agent',
            new_callable=AsyncMock,
            return_value=mock_watch_return,
        ), patch("bot.web.server.set_session"), patch("bot.web.server.update_last_activity"), patch(
            'bot.web.server.vault_log'
        ):
            async with self.client.ws_connect("/ws") as ws_conn:
                await ws_conn.receive_json()  # initial status
                await ws_conn.send_json(
                    {
                        "type": "chat",
                        "text": "현재 진행 중인 모든 채팅방 요약해줘",
                        "roomId": "__core__",
                        "sessionId": None,
                        "newSession": True,
                    }
                )
                started = await ws_conn.receive_json()
                self.assertEqual(started.get("type"), "started")
                self.assertEqual(started.get("roomId"), "__core__")

                result_msg = await ws_conn.receive_json()
                self.assertEqual(result_msg.get("type"), "result")
                self.assertEqual(result_msg.get("roomId"), "__core__")
                self.assertEqual(result_msg.get("sessionId"), "core-agy-session-999")
                self.assertTrue(mock_spawn.call_args.kwargs.get("orchestrator"))
                self.assertIn("OSHYUN_ORCHESTRATOR_API_URL", mock_spawn.call_args.kwargs.get("extra_env", {}))

    def test_agy_engine_orchestrator_prompt_and_step_formatting(self):
        """AGYEngine(orchestrator=True) 호출 시 instructions/common 및 instructions/orchestrator 디렉토리가 --add-dir로 주입되고 프롬프트는 순수하게 전달되는지 검증."""
        import bot.engine.agent_engine as agent_engine

        engine = agent_engine.AGYEngine()
        inv = engine.build_cmd(
            "모든 방 상태 알려줘",
            session_id="core-conv-1",
            model="g3-flash",
            effort="medium",
            workdir="/Users/user/obsidian/oshyun",
            extra_args=[],
            slash_commands=None,
            sandbox=False,
            coding=False,
            orchestrator=True,
        )
        cmd_joined = " ".join(inv["cmd"])
        # 순수 프롬프트 확인 (-p 바로 뒤에 원래 질문만 위치)
        p_idx = inv["cmd"].index("-p")
        self.assertEqual(inv["cmd"][p_idx + 1], "모든 방 상태 알려줘")

        # --add-dir로 common과 orchestrator 지침 디렉토리가 주입되었는지 검증
        common_dir = agent_engine.get_instruction_dir("common")
        orch_dir = agent_engine.get_instruction_dir("orchestrator")
        self.assertIn(f"--add-dir {common_dir}", cmd_joined)
        self.assertIn(f"--add-dir {orch_dir}", cmd_joined)

        # 지침 파일에 마스터 에이전트 지침 및 즉시 하위 채팅 스폰/위임, 사전 탐색 금지, session-ctl 도구 지침이 포함되어 있는지 검증
        orch_agents_md = os.path.join(orch_dir, "AGENTS.md")
        with open(orch_agents_md, "r", encoding="utf-8") as f:
            orch_content = f.read()
        self.assertIn("마스터 에이전트(Master Agent", orch_content)
        self.assertIn("session-ctl", orch_content)
        self.assertIn("즉시 하위 채팅 스폰/위임", orch_content)
        self.assertIn("즉시 비동기 위임", orch_content)
        self.assertIn("--model gemini-3.8-flash --effort low", orch_content)

        # common 지침 파일에 볼트 최우선 및 Gemini API 자문 원칙이 포함되어 있는지 검증
        common_agents_md = os.path.join(common_dir, "AGENTS.md")
        with open(common_agents_md, "r", encoding="utf-8") as f:
            common_content = f.read()
        self.assertIn("볼트 최우선", common_content)
        self.assertIn("Gemini API", common_content)

        step_list_info = web_server._format_tool_step_info("run_command", {"CommandLine": "session-ctl list"})
        self.assertEqual(step_list_info["title"], "session-ctl list: all")

        step_send_info = web_server._format_tool_step_info(
            "run_command",
            {"CommandLine": 'session-ctl send "주식방" "포트폴리오 업데이트해줘" --wait'},
        )
        self.assertTrue(step_send_info["title"].startswith("session-ctl send:"))

        html = _read_static_bundle(web_server.INTERNAL_STATIC_DIR)
        self.assertIn('id="core-agent-slot"', html)
        self.assertIn("CORE_ROOM_ID = '__core__'", html)
        self.assertIn("function ensureCoreRoom(", html)
        self.assertIn("if (i === startIdx)", html)

    def test_agy_engine_web_prompt_and_vault_first(self):
        """AGYEngine(web=True) 호출 시 common 및 web 디렉토리 주입, 볼트 최우선 지침 및 웹 마크다운 허용 검증."""
        import bot.engine.agent_engine as agent_engine

        engine = agent_engine.AGYEngine()
        inv_web = engine.build_cmd(
            "연저펀 이벤트 알려줘",
            session_id="web-conv-1",
            model="gemini-3.8-flash",
            effort="medium",
            workdir="/Users/user/obsidian/oshyun",
            extra_args=[],
            slash_commands=False,
            sandbox=False,
            coding=False,
            web=True,
        )
        web_cmd = " ".join(inv_web["cmd"])
        # 순수 프롬프트 확인
        p_idx = inv_web["cmd"].index("-p")
        self.assertEqual(inv_web["cmd"][p_idx + 1], "연저펀 이벤트 알려줘")

        common_dir = agent_engine.get_instruction_dir("common")
        web_dir = agent_engine.get_instruction_dir("web")

        self.assertIn(f"--add-dir {common_dir}", web_cmd)
        self.assertIn(f"--add-dir {web_dir}", web_cmd)

        # web 지침 파일 내용 검증
        with open(os.path.join(web_dir, "AGENTS.md"), "r", encoding="utf-8") as f:
            web_content = f.read()
        self.assertIn("웹 UI에서는 마크다운", web_content)
        self.assertNotIn("마크다운이나 HTML 서식은 렌더링되지 않으니 사용하지 마라", web_content)

        # 채널 미지정(web=False) 폴백 실행도 웹 지침 디렉토리를 사용한다
        inv_default = engine.build_cmd(
            "연저펀 이벤트 알려줘",
            session_id="default-conv-1",
            model="gemini-3.8-flash",
            effort="medium",
            workdir="/Users/user/obsidian/oshyun",
            extra_args=[],
            slash_commands=False,
            sandbox=False,
            coding=False,
            web=False,
        )
        default_cmd = " ".join(inv_default["cmd"])
        # 순수 프롬프트 확인
        default_p_idx = inv_default["cmd"].index("-p")
        self.assertEqual(inv_default["cmd"][default_p_idx + 1], "연저펀 이벤트 알려줘")

        self.assertIn(f"--add-dir {common_dir}", default_cmd)
        self.assertIn(f"--add-dir {web_dir}", default_cmd)

    def test_expert_agents_system_prompts_and_directives(self):
        """전문가 에이전트(세컨브레인, 봇개발) 전용 시스템 프롬프트, 인스트럭션 디렉토리, UI 계층 구조 검증."""
        import bot.engine.agent_engine as agent_engine
        import bot.engine.engine_drivers as engine_drivers

        # 1. _resolve_system_prompt 검증
        vault_prompt = engine_drivers._resolve_system_prompt(expert_vault=True)
        self.assertIn("세컨브레인 전문가 에이전트", vault_prompt)
        self.assertIn("5대 도메인", vault_prompt)
        self.assertIn("__expert_vault__", vault_prompt)

        coding_prompt = engine_drivers._resolve_system_prompt(expert_coding=True)
        self.assertIn("봇개발 전문가 에이전트", coding_prompt)
        self.assertIn("워크트리", coding_prompt)
        self.assertIn("__expert_coding__", coding_prompt)
        self.assertIn("session-ctl spawn", coding_prompt)
        self.assertIn("하위 코딩 세션", coding_prompt)

        # 2. AGYEngine 빌드 검증 - 세컨브레인
        engine = agent_engine.AGYEngine()
        inv_vault = engine.build_cmd(
            "건강검진 일정 확인해줘",
            session_id="vault-sess-1",
            model="gemini-3.8-flash",
            effort="medium",
            workdir="/Users/user/obsidian/oshyun",
            extra_args=[],
            slash_commands=False,
            sandbox=True,
            coding=False,
            expert_vault=True,
        )
        vault_cmd_str = " ".join(inv_vault["cmd"])
        vault_dir = agent_engine.get_instruction_dir("expert_vault")
        self.assertIn(f"--add-dir {vault_dir}", vault_cmd_str)
        self.assertNotIn("--sandbox", inv_vault["cmd"])
        self.assertTrue(any(inv_vault["env"]["PATH"].startswith(p) for p in [os.path.join(engine_drivers._ROOT_DIR, "scripts")]))

        # ClaudeDriver 세컨브레인 샌드박스 제외 검증
        claude_engine = agent_engine.ClaudeEngine()
        inv_claude_vault = claude_engine.build_cmd(
            "건강검진 일정 확인해줘",
            session_id="vault-sess-1",
            model="claude-3-5-sonnet",
            effort=None,
            workdir="/Users/user/obsidian/oshyun",
            extra_args=[],
            slash_commands=False,
            sandbox=True,
            coding=False,
            expert_vault=True,
        )
        self.assertNotIn("sandbox-exec", " ".join(inv_claude_vault["cmd"]))

        # 3. AGYEngine 빌드 검증 - 봇개발 (샌드박스 제외 및 scripts PATH 확인)
        inv_coding = engine.build_cmd(
            "새 기능 단위 테스트 작성",
            session_id="coding-sess-1",
            model="gemini-4-hydrogen",
            effort="medium",
            workdir=engine_drivers._ROOT_DIR,
            extra_args=[],
            slash_commands=False,
            sandbox=True,
            coding=True,
            expert_coding=True,
        )
        coding_cmd_str = " ".join(inv_coding["cmd"])
        coding_dir = agent_engine.get_instruction_dir("expert_coding")
        self.assertIn(f"--add-dir {coding_dir}", coding_cmd_str)
        self.assertNotIn("--sandbox", inv_coding["cmd"])

        # 4. index.html의 계층형 트리 UI 요소 검증
        html = _read_static_bundle(web_server.INTERNAL_STATIC_DIR)
        self.assertIn("sb-master-branch", html)
        self.assertIn("expert-agent-node", html)
        self.assertIn('id="expert-vault-subtask-tree"', html)
        self.assertIn('id="expert-coding-subtask-tree"', html)
        self.assertIn('id="master-subtask-tree"', html)
        self.assertIn("ORCHESTRATOR_ROOM_IDS.includes(rid)", html)
        self.assertIn("parentAgentId: sRoom.parentAgentId", html)
        self.assertIn("isOrchRoom", html)

    async def test_notify_master_agent_bypasses_direct_message_without_enqueue(self):
        """_notify_master_agent 가 완료 시 2차 턴(enqueue_room_turn) 없이 긴 응답도 부모 방에 직접 바이패스 저장 및 브로드캐스트하는지 검증."""
        import bot.web.execution as web_execution

        long_response = "상세 분석 결과: " + ("나" * 900)
        mock_enqueue = AsyncMock()
        mock_db = MagicMock()
        mock_db.get_room.return_value = {"id": "room-summary-test", "parentAgentId": "__core__", "title": "분석 작업"}
        mock_broadcast = AsyncMock()

        with patch.object(web_execution, "enqueue_room_turn", mock_enqueue), \
             patch.object(web_execution, "chat_db", mock_db), \
             patch.object(web_execution, "broadcast_ws_json", mock_broadcast), \
             patch.object(web_execution.master_memory, "add_memory"):
            await web_execution._notify_master_agent(
                "room-summary-test",
                None,
                long_response,
                is_success=True,
            )

        # 마스터의 2차 중복 턴은 차단됨
        mock_enqueue.assert_not_awaited()
        # 부모 방에 어시스턴트 메시지로 직접 바이패스 저장됨
        mock_db.add_message.assert_called_once()
        kw = mock_db.add_message.call_args.kwargs
        self.assertEqual(kw["room_id"], "__core__")
        self.assertEqual(kw["role"], "assistant")
        self.assertEqual(kw["channel"], "thread_bypass")
        self.assertEqual(kw["text"], long_response)
        # 웹소켓 브로드캐스트 검증
        mock_broadcast.assert_awaited_once()
        b_payload = mock_broadcast.call_args[0][0]
        self.assertEqual(b_payload["type"], "result")
        self.assertEqual(b_payload["text"], long_response)
        self.assertEqual(b_payload["roomId"], "__core__")

    async def test_orchestrator_graph_endpoint(self):
        """GET /api/orchestrator/graph 로 실시간 마스터-슬레이브 관계도 및 감독 상태 반환 검증."""
        res = await self.client.get("/api/orchestrator/graph")
        self.assertEqual(res.status, 200)
        data = await res.json()
        self.assertTrue(data.get("ok"))
        graph = data.get("graph", {})
        self.assertEqual(graph.get("master", {}).get("id"), "__core__")
        self.assertIn("nodes", graph)
        self.assertIn("edges", graph)
        self.assertIn("summary", graph)
        self.assertIn("patrol", graph)

    async def test_orchestrator_tasks_and_update_lifecycle(self):
        """POST /api/orchestrator/dispatch 위임 작업 등록 -> GET /api/orchestrator/tasks 조회 -> POST /api/orchestrator/task/update 판정 및 종결 검증."""
        import bot.core.chat_db as chat_db

        chat_db.get_or_create_room("sub-room-1", session_id="sub-sess-1", title="[테스트] 세무 신고")

        mock_proc = MagicMock(pid=88881, out_file="/tmp/mock_out.jsonl")
        mock_watch = {
            "result": "세무 일정 정리 완료",
            "session_id": "sub-sess-1",
            "model": "g3-flash",
            "interrupted": False,
        }
        with patch("bot.web.server.spawn_agent", return_value=mock_proc), patch(
            'bot.web.server.watch_agent',
            new_callable=AsyncMock,
            return_value=mock_watch,
        ), patch("bot.web.server.set_session"), patch("bot.web.server.update_last_activity"), patch(
            'bot.web.server.vault_log'
        ):
            # 1. dispatch 호출 시 taskId 반환 확인
            res_disp = await self.client.post(
                "/api/orchestrator/dispatch",
                json={
                    "action": "send",
                    "target": "sub-room-1",
                    "message": "종합소득세 신고 기한 확인해줘",
                    "wait": True,
                },
            )
            self.assertEqual(res_disp.status, 200)
            disp_data = await res_disp.json()
            task_id = disp_data.get("taskId")
            self.assertTrue(task_id and task_id.startswith("mt-"))

            # 2. tasks 목록 조회 시 등록된 작업 및 완료 상태 확인
            res_tasks = await self.client.get("/api/orchestrator/tasks?limit=20")
            self.assertEqual(res_tasks.status, 200)
            tasks_data = await res_tasks.json()
            tasks = tasks_data.get("tasks", [])
            target_t = next((t for t in tasks if t["id"] == task_id), None)
            self.assertIsNotNone(target_t)
            self.assertEqual(target_t["status"], "done")
            self.assertIn("세무 일정 정리 완료", target_t["resultPreview"])

            # 3. 작업 판정 메모 등록 및 종결
            res_upd = await self.client.post(
                "/api/orchestrator/task/update",
                json={
                    "taskId": task_id,
                    "note": "기한 확인 완료되어 종결함",
                    "close": True,
                },
            )
            self.assertEqual(res_upd.status, 200)
            upd_data = await res_upd.json()
            self.assertTrue(upd_data.get("ok"))
            self.assertEqual(upd_data["task"]["status"], "closed")
            self.assertTrue(upd_data["task"]["reviewed"])
            self.assertEqual(upd_data["task"]["notes"][-1]["text"], "기한 확인 완료되어 종결함")

    async def test_orchestrator_patrol_trigger_endpoint(self):
        """POST /api/orchestrator/patrol/trigger 로 수동 점검 트리거 검증."""
        with patch.object(
            web_server._master_supervisor,
            "start_patrol",
            new_callable=AsyncMock,
            return_value={"ok": True, "reason": "점검 시작"},
        ):
            res = await self.client.post("/api/orchestrator/patrol/trigger")
            self.assertEqual(res.status, 200)
            data = await res.json()
            self.assertTrue(data.get("ok"))

    async def test_orchestrator_memory_and_reset_endpoints(self):
        """GET/POST /api/orchestrator/memory 4계층 연쇄 이동 및 POST /api/orchestrator/reset 초기화 검증."""
        import bot.core.chat_db as chat_db
        import bot.master.master_memory as master_memory
        import bot.master.master_tasks as master_tasks

        # 1. 초기 상태 확인
        res_empty = await self.client.get("/api/orchestrator/memory")
        self.assertEqual(res_empty.status, 200)
        d_empty = await res_empty.json()
        self.assertEqual(d_empty["tierOrder"], ["hot", "warm", "cold", "freeze"])
        self.assertEqual(d_empty["totalItems"], 0)

        # 2. 작은 한도로 설정 후 연쇄 하위 이동 검증 (hot -> warm -> cold -> freeze)
        res_lim = await self.client.post(
            "/api/orchestrator/memory",
            json={
                "action": "set_limits",
                "limits": {
                    "hot": {"maxChars": 20, "maxItems": 1},
                    "warm": {"maxChars": 20, "maxItems": 1},
                    "cold": {"maxChars": 20, "maxItems": 1},
                    "freeze": {"maxChars": 20, "maxItems": 1},
                },
            },
        )
        self.assertEqual(res_lim.status, 200)

        for idx in range(1, 5):
            r_add = await self.client.post(
                "/api/orchestrator/memory",
                json={"action": "add", "title": f"K{idx}", "text": f"기억항목-{idx}"},
            )
            self.assertEqual(r_add.status, 200)

        res_graph = await self.client.get("/api/orchestrator/graph")
        self.assertEqual(res_graph.status, 200)
        g_data = await res_graph.json()
        mem_snap = g_data["graph"]["memory"]
        self.assertEqual(mem_snap["tiers"]["hot"]["items"][0]["title"], "K4")
        self.assertEqual(mem_snap["tiers"]["warm"]["items"][0]["title"], "K3")
        self.assertEqual(mem_snap["tiers"]["cold"]["items"][0]["title"], "K2")
        self.assertEqual(mem_snap["tiers"]["freeze"]["items"][0]["title"], "K1")

        # 3. __core__ 대화방, 하위 방, 태스크 준비 후 cascade 초기화
        chat_db.get_or_create_room("__core__", session_id="core-sess-1", title="마스터 에이전트", model="agy-3", effort="high")
        chat_db.add_message("__core__", "user", "이전 마스터 질문", session_id="core-sess-1")
        chat_db.get_or_create_room("sub-keep-1", session_id="sub-sess-1", title="유지될 하위방")
        t_done = master_tasks.create_task(room_id="sub-keep-1", instruction="완료된 작업")
        master_tasks.update_task(t_done["id"], status="done")
        t_run = master_tasks.create_task(room_id="sub-keep-1", instruction="진행중 작업")
        master_tasks.update_task(t_run["id"], status="running")

        # warm 한도를 넉넉히 늘린 뒤 cascade 초기화 -> K4 가 warm 으로 내려오고 K3 는 cold 로 이동해야 함
        master_memory.update_tier_limits({"warm": {"maxChars": 500, "maxItems": 5}, "cold": {"maxChars": 500, "maxItems": 5}})
        with patch("bot.web.server.clear_session") as mock_clear_sid, \
             patch("bot.web.server.broadcast_ws_json", new_callable=AsyncMock) as mock_bcast:
            res_rst1 = await self.client.post("/api/orchestrator/reset", json={"memoryMode": "cascade"})
            self.assertEqual(res_rst1.status, 200)
            d_rst1 = await res_rst1.json()
            self.assertTrue(d_rst1["ok"])
            self.assertEqual(d_rst1["memoryMode"], "cascade")
            self.assertEqual(d_rst1["memory"]["tiers"]["hot"]["itemCount"], 0)
            self.assertGreaterEqual(d_rst1["memory"]["tiers"]["warm"]["itemCount"], 1)
            mock_clear_sid.assert_called_with(ns="core_")
            bcast_types = [c.args[0].get("type") for c in mock_bcast.await_args_list]
            self.assertIn("session_reset", bcast_types)

        # __core__ 메시지는 비워졌지만 모델/effort 및 하위 방·태스크는 유지
        self.assertEqual(chat_db.get_messages("__core__"), [])
        core_row = chat_db.get_room("__core__")
        self.assertIsNotNone(core_row)
        self.assertEqual(core_row.get("model"), "agy-3")
        self.assertEqual(core_row.get("effort"), "high")
        self.assertIsNotNone(chat_db.get_room("sub-keep-1"))
        self.assertIsNotNone(master_tasks.get_task(t_done["id"]))

        # 4. clear 초기화 + clearDoneTasks=True 시 전 계층 삭제 및 종결 태스크만 정리
        master_memory.add_memory("초기화 전 핫 기억", title="TMP")
        res_rst2 = await self.client.post(
            "/api/orchestrator/reset",
            json={"memoryMode": "clear", "clearDoneTasks": True},
        )
        self.assertEqual(res_rst2.status, 200)
        d_rst2 = await res_rst2.json()
        self.assertEqual(d_rst2["memory"]["totalItems"], 0)
        self.assertEqual(d_rst2["clearedTasks"], 1)
        self.assertIsNone(master_tasks.get_task(t_done["id"]))
        self.assertIsNotNone(master_tasks.get_task(t_run["id"]))

        # 4-1. 기본 초기화(memoryMode 미지정 시 기본 "keep") -> 메모리가 삭제되거나 강등되지 않고 그대로 유지
        master_memory.add_memory("유지되어야 할 핫 기억", title="GLOBAL_HOT")
        res_rst_default = await self.client.post("/api/orchestrator/reset", json={})
        self.assertEqual(res_rst_default.status, 200)
        d_rst_def = await res_rst_default.json()
        self.assertEqual(d_rst_def["memoryMode"], "keep")
        self.assertEqual(d_rst_def["memory"]["tiers"]["hot"]["itemCount"], 1)

        # 5. __core__ WS 턴 실행 시 4계층 메모리 컨텍스트가 프롬프트에 주입되고 턴 응답이 hot 에 저장되는지 검증
        master_memory.update_tier_limits({"hot": {"maxChars": 2400, "maxItems": 8}})
        master_memory.add_memory("핵심 기억: 세금 신고 기한 5월", title="세무일정")
        fake_proc = MagicMock(pid=54321, out_file=os.path.join(self._tmp_dir.name, "core_mem.jsonl"))
        captured_prompts = []

        def fake_spawn(prompt, *args, **kwargs):
            captured_prompts.append(prompt)
            return fake_proc

        async def fake_watch(*args, **kwargs):
            return {
                "result": "메모리 확인 후 답변드립니다.",
                "session_id": "core-sess-new",
                "model": "g3-flash",
                "interrupted": False,
            }

        with patch("bot.web.server.spawn_agent", side_effect=fake_spawn), \
             patch("bot.web.server.watch_agent", side_effect=fake_watch):
            async with self.client.ws_connect("/ws") as ws_conn:
                _ = await asyncio.wait_for(ws_conn.receive_json(), timeout=3)
                await ws_conn.send_json({"type": "chat", "text": "남은 일정 알려줘", "roomId": "__core__"})
                msg_types = []
                for _ in range(10):
                    frame = await asyncio.wait_for(ws_conn.receive_json(), timeout=3)
                    ftype = frame.get("type")
                    msg_types.append(ftype)
                    if ftype == "result":
                        break
                self.assertIn("started", msg_types)
                self.assertIn("result", msg_types)

        self.assertTrue(captured_prompts)
        self.assertIn("세무일정", captured_prompts[0])
        self.assertIn("세금 신고 기한 5월", captured_prompts[0])
        hot_texts = [it["text"] for it in master_memory.get_memory_snapshot(tier="hot")["tiers"]["hot"]["items"]]
        self.assertTrue(any("남은 일정 알려줘" in txt and "메모리 확인 후 답변드립니다." in txt for txt in hot_texts))

    async def test_orchestrator_skill_endpoint(self):
        """POST /api/orchestrator/skill 이 프롬프트 삽입 없이 /<skill> 만 순수 실행하는지 검증."""
        captured = []

        async def fake_run_agent(prompt, session_id=None, **kwargs):
            captured.append((prompt, session_id, kwargs))
            return {"result": "스킬 실행 완료", "model": kwargs.get("model"), "session_id": None}

        with patch("bot.engine.agent_engine.run_agent", side_effect=fake_run_agent):
            res_bad = await self.client.post("/api/orchestrator/skill", json={"skill": "../bad"})
            self.assertEqual(res_bad.status, 400)

            # 1) 기본 비동기 실행: 즉시 status="started" 반환 (마스터 턴 실행 중에도 블로킹 없음)
            res_async = await self.client.post(
                "/api/orchestrator/skill",
                json={"skill": "/memory-rebalance", "model": "gemini-3.8-flash", "effort": "low"},
            )
            self.assertEqual(res_async.status, 200)
            data_async = await res_async.json()
            self.assertTrue(data_async["ok"])
            self.assertEqual(data_async["status"], "started")
            await asyncio.sleep(0.05)

            # 2) wait=True 동기 실행 검증
            res = await self.client.post(
                "/api/orchestrator/skill",
                json={"skill": "/memory-rebalance", "model": "gemini-3.8-flash", "effort": "low", "wait": True},
            )
            self.assertEqual(res.status, 200)
            data = await res.json()
            self.assertTrue(data["ok"])
            self.assertEqual(data["skill"], "memory-rebalance")
            self.assertEqual(data["prompt"], "/memory-rebalance")
            self.assertEqual(data["result"], "스킬 실행 완료")
            self.assertIn("memory", data)

        self.assertEqual(len(captured), 2)
        prompt, sess_id, kw = captured[0]
        self.assertEqual(prompt, "/memory-rebalance")
        self.assertIsNone(sess_id)
        self.assertTrue(kw.get("slash_commands"))
        self.assertTrue(kw.get("orchestrator"))
        self.assertEqual(kw.get("model"), "gemini-3.8-flash")
        self.assertEqual(kw.get("effort"), "low")

    async def test_ws_raw_prompt_skill_chat_turn(self):
        """WS chat 턴에서 rawPrompt=True, slashCommands=True 시 프롬프트 삽입 없이 원문만 실행되는지 검증."""
        import bot.master.master_memory as master_memory
        master_memory.add_memory("주입되면 안 되는 기억", title="노출금지")
        fake_proc = MagicMock(pid=54322, out_file=os.path.join(self._tmp_dir.name, "raw_skill.jsonl"))
        captured = []

        def fake_spawn(prompt, *args, **kwargs):
            captured.append((prompt, kwargs))
            return fake_proc

        async def fake_watch(*args, **kwargs):
            return {"result": "리밸런싱 완료", "session_id": "sess-raw-1", "model": "gemini-3.8-flash", "interrupted": False}

        with patch("bot.web.server.spawn_agent", side_effect=fake_spawn), \
             patch("bot.web.server.watch_agent", side_effect=fake_watch):
            async with self.client.ws_connect("/ws") as ws_conn:
                _ = await asyncio.wait_for(ws_conn.receive_json(), timeout=3)
                await ws_conn.send_json({
                    "type": "chat",
                    "text": "/memory-rebalance",
                    "roomId": "__core__",
                    "rawPrompt": True,
                    "slashCommands": True,
                    "orchestrator": True,
                    "model": "gemini-3.8-flash",
                    "effort": "low",
                })
                for _ in range(10):
                    frame = await asyncio.wait_for(ws_conn.receive_json(), timeout=3)
                    if frame.get("type") == "result":
                        break

        self.assertEqual(len(captured), 1)
        prompt, kw = captured[0]
        self.assertEqual(prompt, "/memory-rebalance")
        self.assertNotIn("노출금지", prompt)
        self.assertTrue(kw.get("slash_commands"))
        self.assertTrue(kw.get("orchestrator"))

    async def test_user_preemption_over_patrol(self):
        """정기 점검 턴 실행 중 사용자가 __core__에 직접 질문을 보내면 점검 턴이 즉시 취소(양보)되고 사용자 질문이 먼저 처리되는지 검증."""
        mock_proc_patrol = MagicMock(pid=99911, out_file="/tmp/mock_patrol.jsonl")
        mock_proc_user = MagicMock(pid=99922, out_file="/tmp/mock_user.jsonl")

        # patrol turn 이 대기하는 상황 시뮬레이션
        patrol_wait_event = asyncio.Event()
        call_count = 0

        async def fake_watch(*args, **kwargs):
            nonlocal call_count
            if call_count == 0:
                call_count += 1
                await patrol_wait_event.wait()
                return {"result": "점검 완료", "session_id": "core-sess", "model": "g3-flash"}
            return {"result": "사용자 질문에 답변합니다.", "session_id": "core-sess", "model": "g3-flash"}

        with patch("bot.web.server.spawn_agent", side_effect=[mock_proc_patrol, mock_proc_user]), patch(
            'bot.web.server.watch_agent',
            side_effect=fake_watch,
        ), patch("bot.web.server.set_session"), patch("bot.web.server.update_last_activity"), patch(
            'bot.web.server.vault_log'
        ):
            # 1. 배경 점검 턴 적재 (channel="master_patrol")
            patrol_fut = asyncio.get_running_loop().create_future()
            is_run = (await web_server.enqueue_room_turn(
                room_id="__core__",
                user_text="[정기 점검] 하위 세션 감시",
                channel="master_patrol",
                result_future=patrol_fut,
            ))["already_running"]
            await asyncio.sleep(0.05)

            # 2. 사용자 요청 도착 (channel="web")
            user_fut = asyncio.get_running_loop().create_future()
            await web_server.enqueue_room_turn(
                room_id="__core__",
                user_text="지금 하위 작업 전부 중지해줘",
                channel="web",
                result_future=user_fut,
            )

            # 점검 태스크는 CancelledError 로 취소(양보)되어야 함
            with self.assertRaises(asyncio.CancelledError):
                await patrol_fut

            # 사용자 태스크는 정상 완료되어야 함
            user_result = await user_fut
            self.assertTrue(user_result.get("ok"))
            self.assertEqual(user_result.get("reply"), "사용자 질문에 답변합니다.")

    async def test_backup_does_not_duplicate_messages_in_db(self):
        """POST /api/chat/rooms/backup 호출 시 클라이언트 캐시가 DB SSOT를 오염시키거나 중복 INSERT 하지 않는지 검증."""
        import bot.core.chat_db as chat_db

        chat_db.get_or_create_room("room-no-dup", session_id="sess-no-dup", title="[테스트] 중복 방지 테스트")
        chat_db.add_message("room-no-dup", "user", "[테스트] 첫 질문", session_id="sess-no-dup")
        chat_db.add_message("room-no-dup", "assistant", "[테스트] 첫 답변", session_id="sess-no-dup")

        backup_payload = {
            "rooms": [
                {
                    "id": "room-no-dup",
                    "sessionId": "sess-no-dup",
                    "title": "[테스트] 중복 방지 테스트",
                    "updatedAt": 1794066000,
                    "messages": [
                        {"role": "user", "text": "[테스트] 첫 질문"},
                        {"role": "assistant", "text": "[테스트] 첫 답변"},
                    ],
                },
                {
                    "id": "room-stale-deleted",
                    "sessionId": "sess-stale-deleted",
                    "title": "[테스트] 부활하면 안 되는 캐시 방",
                    "updatedAt": 1794066000,
                    "messages": [
                        {"role": "user", "text": "[테스트] 캐시 질문"},
                    ],
                },
            ]
        }
        res1 = await self.client.post("/api/chat/rooms/backup", json=backup_payload)
        self.assertEqual(res1.status, 200)
        res2 = await self.client.post("/api/chat/rooms/backup", json=backup_payload)
        self.assertEqual(res2.status, 200)

        msgs = chat_db.get_messages("room-no-dup")
        self.assertEqual(len(msgs), 2)
        self.assertIsNone(chat_db.get_room("room-stale-deleted"))

    async def test_live_run_snapshot_on_refresh_during_execution(self):
        """응답 생성 중 새로고침 시 /api/status, /api/chat/rooms, /api/chat/history 에서 진행 중인 스텝·스트리밍 텍스트가 즉시 복원되는지 검증."""
        finish_gate = asyncio.Event()
        stream_ready = asyncio.Event()

        def fake_spawn(prompt, session_id, **kwargs):
            out_file = kwargs.get("out_file")
            if out_file:
                with open(out_file, "w", encoding="utf-8") as f:
                    f.write(
                        json.dumps({
                            "event": "step_update",
                            "step_update": {
                                "step_type": "tool",
                                "state": "ACTIVE",
                                "step_index": 1,
                                "tool_name": "read_file",
                                "tool_info": {
                                    "parameters": {"Path": "/Users/user/obsidian/oshyun/_bot/컨텍스트.md"},
                                },
                            },
                        }, ensure_ascii=False)
                        + "\n"
                        + json.dumps({
                            "event": "step_update",
                            "step_update": {
                                "step_type": "agent_response",
                                "state": "ACTIVE",
                                "text_delta": "### 실시간 중간 응답\n컨텍스트 확인 중입니다...",
                            },
                        }, ensure_ascii=False)
                        + "\n"
                    )
            mock_proc = MagicMock()
            mock_proc.pid = 77777
            return mock_proc

        async def fake_watch(out_file_path, **kwargs):
            await asyncio.sleep(0.25)
            stream_ready.set()
            await finish_gate.wait()
            return {
                "result": "### 실시간 중간 응답\n컨텍스트 확인 완료했습니다!",
                "session_id": "sess-live-refresh-1",
                "model": "g3-flash",
                "cost": 0.0,
                "interrupted": False,
            }

        headers = {"Authorization": "Bearer super-secret-password-999"}
        with (
            patch.object(web_server, "spawn_agent", side_effect=fake_spawn),
            patch.object(web_server, "watch_agent", side_effect=fake_watch),
        ):
            try:
                ws1 = await self.client.ws_connect("/ws?token=super-secret-password-999")
                await ws1.receive_json()  # initial status

                await ws1.send_json({
                    "type": "chat",
                    "roomId": "room-live-1",
                    "text": "컨텍스트 브리핑해줘",
                    "newSession": True,
                })

                await asyncio.wait_for(stream_ready.wait(), timeout=3.0)

                # 새로고침 시나리오: 기존 WS 종료 후 HTTP API 및 신규 WS 접속 시 진행 중 스냅샷 검증
                await ws1.close()

                status_res = await self.client.get("/api/status", headers=headers)
                self.assertEqual(status_res.status, 200)
                status_data = await status_res.json()
                active_runs = status_data.get("activeRuns", [])
                self.assertEqual(len(active_runs), 1)
                run_snap = active_runs[0]
                self.assertEqual(run_snap.get("roomId"), "room-live-1")
                self.assertEqual(run_snap.get("userText"), "컨텍스트 브리핑해줘")
                self.assertTrue(run_snap.get("isRunning"))
                self.assertEqual(len(run_snap.get("steps", [])), 1)
                self.assertIn("read_file", run_snap["steps"][0].get("title", ""))
                self.assertIn("### 실시간 중간 응답", run_snap.get("streamText", ""))

                hist_res = await self.client.get("/api/chat/history?roomId=room-live-1", headers=headers)
                self.assertEqual(hist_res.status, 200)
                hist_data = await hist_res.json()
                self.assertIsNotNone(hist_data.get("liveRun"))
                self.assertIn("컨텍스트 확인 중입니다", hist_data["liveRun"].get("streamText", ""))
                # 새로고침 시 이중 렌더링 방지: 진행 중인 미완결 어시스턴트 메시지가 messages에 포함되지 않음을 검증
                hist_msgs = hist_data.get("messages", [])
                self.assertTrue(all(m.get("role") != "assistant" or bool(m.get("text")) for m in hist_msgs))

                # 신규 WS 연결 시 초기 status 페이로드에도 activeRuns 포함 및 완료 시 result 수신 확인
                ws2 = await self.client.ws_connect("/ws?token=super-secret-password-999")
                ws2_init = await ws2.receive_json()
                self.assertEqual(ws2_init.get("type"), "status")
                self.assertEqual(len(ws2_init.get("activeRuns", [])), 1)

                finish_gate.set()

                result_msg = None
                for _ in range(10):
                    msg = await ws2.receive_json(timeout=3.0)
                    if msg.get("type") == "result":
                        result_msg = msg
                        break
                self.assertIsNotNone(result_msg)
                self.assertIn("컨텍스트 확인 완료했습니다!", result_msg.get("text", ""))
                await ws2.close()

                # 완료 후에는 activeRuns 가 비어야 함
                after_res = await self.client.get("/api/status", headers=headers)
                after_data = await after_res.json()
                self.assertEqual(after_data.get("activeRuns"), [])
            finally:
                finish_gate.set()

    async def test_api_upload_and_serve_attachment(self):
        """사진/파일 업로드 및 /api/attachments 서빙 정상 동작 검증."""
        headers = {"Authorization": "Bearer super-secret-password-999"}
        # 1. Base64 JSON 업로드 테스트
        payload = {
            "files": [
                {
                    "name": "receipt_test.png",
                    "data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
                    "type": "image/png"
                }
            ]
        }
        res = await self.client.post("/api/upload", json=payload, headers=headers)
        self.assertEqual(res.status, 200)
        data = await res.json()
        self.assertTrue(data.get("ok"))
        files = data.get("files") or []
        self.assertEqual(len(files), 1)
        uploaded = files[0]
        self.assertEqual(uploaded["name"], "receipt_test.png")
        self.assertTrue(os.path.isfile(uploaded["path"]))

        # 2. 서빙 엔드포인트 열람 검증
        get_res = await self.client.get(uploaded["url"], headers=headers)
        self.assertEqual(get_res.status, 200)
        body = await get_res.read()
        self.assertGreater(len(body), 0)

        # 3. 경로 트래버설 공격 방어 검증
        bad_res = await self.client.get("/api/attachments/..%2F..%2Fetc%2Fpasswd", headers=headers)
        self.assertIn(bad_res.status, (400, 404))

    def test_agent_timeout_default_is_unlimited(self):
        """시간초과 없이 무제한으로 안전하게 대기하는지 검증."""
        import inspect
        # watch_agent의 기본 timeout이 0(무제한)인지 확인
        sig = inspect.signature(agent_engine.watch_agent)
        self.assertEqual(sig.parameters["timeout"].default, 0)

    async def test_orchestrator_spawn_ws_events_ids_and_preview_preservation(self):
        """spawn 시 room_created·orchestrator_dispatch·started·result 의 메시지 ID 일관성과 빈 플레이스홀더 preview 보존을 검증."""
        import bot.core.chat_db as chat_db

        headers = {"Authorization": "Bearer super-secret-password-999"}
        long_prompt = (
            "사용자가 '택시비 할인카드 빠르게 찾아'라고 요청했습니다. 다음 순서로 빠르게 조사해 요약해 주세요: "
            "1) 먼저 볼트 내 _bot/컨텍스트.md 및 '개인/금융 💰/카드 보유현황.md', '개인/금융 💰/카드 혜택 정리.md'를 확인하여 "
            "현재 보유 중인 카드 중 택시비 할인·적립 혜택이 있는 카드가 있는지 확인하세요. 2) 필요 시 웹 검색으로 비교해 주세요."
        )
        self.assertGreater(len(long_prompt), 120)

        mock_proc = MagicMock(pid=88111, out_file="/tmp/mock_spawn_id_check.jsonl")
        mock_watch = {
            "result": "보유 카드 및 시중 택시 할인카드 TOP 3 비교 결과입니다.",
            "session_id": "sess-taxi-card-1",
            "model": "gemini-3.8-flash",
            "interrupted": False,
        }

        ws = await self.client.ws_connect("/ws?token=super-secret-password-999")
        try:
            init_msg = await ws.receive_json(timeout=3.0)
            self.assertEqual(init_msg.get("type"), "status")

            with patch.object(web_server, "spawn_agent", return_value=mock_proc), patch.object(
                web_server, "watch_agent", new_callable=AsyncMock, return_value=mock_watch
            ), patch.object(web_server, "set_session"), patch.object(
                web_server, "update_last_activity"
            ), patch.object(
                web_server, "vault_log"
            ), patch(
                'bot.web.execution._notify_master_agent', new_callable=AsyncMock
            ):
                res = await self.client.post(
                    "/api/orchestrator/dispatch",
                    json={
                        "action": "spawn",
                        "title": "택시비 할인카드 조회",
                        "message": long_prompt,
                        "wait": False,
                    },
                    headers=headers,
                )
                self.assertEqual(res.status, 200)
                resp_data = await res.json()
                room_id = resp_data["roomId"]

                events = []
                for _ in range(10):
                    ev = await ws.receive_json(timeout=3.0)
                    events.append(ev)
                    if ev.get("type") == "result" and ev.get("roomId") == room_id:
                        break

            ev_by_type = {e.get("type"): e for e in events if e.get("type")}
            self.assertIn("room_created", ev_by_type)
            self.assertIn("orchestrator_dispatch", ev_by_type)
            self.assertIn("started", ev_by_type)
            self.assertIn("result", ev_by_type)

            room_created = ev_by_type["room_created"]["room"]
            dispatch_ev = ev_by_type["orchestrator_dispatch"]
            started_ev = ev_by_type["started"]
            result_ev = ev_by_type["result"]

            self.assertTrue(room_created.get("isOrchestratorChild"))
            self.assertEqual(dispatch_ev.get("text"), long_prompt)
            self.assertIsNotNone(dispatch_ev.get("msgId"))
            self.assertTrue(dispatch_ev.get("clientMsgId"))
            self.assertFalse(dispatch_ev.get("queued"))

            # dispatch·started·DB 간 userMsgId 및 clientMsgId 완전 일치
            self.assertEqual(started_ev.get("userMsgId"), dispatch_ev.get("msgId"))
            self.assertEqual(started_ev.get("clientMsgId"), dispatch_ev.get("clientMsgId"))
            self.assertIsNotNone(result_ev.get("msgId"))

            # 빈 플레이스홀더 assistant 생성으로 room preview 가 빈 문자열로 덮어써지지 않았는지 검증
            placeholder_room_id = "room-preview-preserve"
            chat_db.get_or_create_room(placeholder_room_id, title="프리뷰 보존 테스트")
            chat_db.add_message(placeholder_room_id, "assistant", "기존 답변 프리뷰입니다")
            before_room = chat_db.get_room(placeholder_room_id)
            self.assertEqual(before_room["preview"], "기존 답변 프리뷰입니다")
            chat_db.add_message(placeholder_room_id, "assistant", "")
            after_room = chat_db.get_room(placeholder_room_id)
            self.assertEqual(after_room["preview"], "기존 답변 프리뷰입니다")

            # 최종 DB에는 user 1개 + assistant 1개 총 2개만 존재
            db_msgs = chat_db.get_messages(room_id)
            self.assertEqual(len(db_msgs), 2)
            self.assertEqual(db_msgs[0]["id"], dispatch_ev["msgId"])
            self.assertEqual(db_msgs[0]["text"], long_prompt)
            self.assertEqual(db_msgs[1]["id"], result_ev["msgId"])
        finally:
            await ws.close()

    async def test_orchestrator_queued_dispatch_client_id_and_same_text_turns(self):
        """실행 중 방에 orchestrator send 시 queued/started 간 clientMsgId가 일치하고 동일 문구 반복 발화도 DB에 모두 기록된다."""
        import time
        import bot.core.chat_db as chat_db
        import bot.web.execution as web_execution

        headers = {"Authorization": "Bearer super-secret-password-999"}
        room_id = "room-orch-queue-test"
        chat_db.get_or_create_room(room_id, session_id="sess-orch-q", title="대기열 ID 테스트")

        turn1_gate = asyncio.Event()
        turn1_started = asyncio.Event()
        proc1 = MagicMock(pid=88201)
        proc2 = MagicMock(pid=88202)
        proc3 = MagicMock(pid=88203)

        async def fake_watch(out_file, **kwargs):
            pid = kwargs.get("pid")
            if pid == 88201:
                turn1_started.set()
                await turn1_gate.wait()
                return {"result": "첫 턴 완료", "session_id": "sess-orch-q", "model": "g3-flash"}
            return {"result": f"후속 완료 ({pid})", "session_id": "sess-orch-q", "model": "g3-flash"}

        ws = await self.client.ws_connect("/ws?token=super-secret-password-999")
        try:
            await ws.receive_json(timeout=3.0)  # initial status

            with patch.object(
                web_server, "spawn_agent", side_effect=[proc1, proc2, proc3]
            ), patch.object(
                web_server, "watch_agent", side_effect=fake_watch
            ), patch.object(
                web_server, "set_session"
            ), patch.object(
                web_server, "update_last_activity"
            ), patch.object(
                web_server, "vault_log"
            ), patch(
                'bot.web.execution._notify_master_agent', new_callable=AsyncMock
            ):
                # 1. 첫 턴 시작
                await ws.send_json({
                    "type": "chat",
                    "roomId": room_id,
                    "sessionId": "sess-orch-q",
                    "text": "계속해",
                    "clientMsgId": "c-repeat-1",
                })
                await asyncio.wait_for(turn1_started.wait(), timeout=3.0)

                # 2. 실행 중일 때 동일 방에 마스터 지시(send) 전송 -> queued=True
                res_send = await self.client.post(
                    "/api/orchestrator/dispatch",
                    json={
                        "action": "send",
                        "target": room_id,
                        "message": "추가 조사 지시",
                        "wait": False,
                    },
                    headers=headers,
                )
                self.assertEqual(res_send.status, 200)
                send_body = await res_send.json()
                self.assertEqual(send_body.get("status"), "queued")

                # 3. 턴 1·2 완료 후 디바운스 창(3초) 이후 동일 문구('계속해')를 다시 질문해도 DB에 정상 기록되는지 검증
                turn1_gate.set()
                collected = []
                result_count = 0
                for _ in range(20):
                    ev = await ws.receive_json(timeout=3.0)
                    collected.append(ev)
                    if ev.get("type") == "result" and ev.get("roomId") == room_id:
                        result_count += 1
                        if result_count >= 2:
                            break

                web_execution._last_chat_requests[room_id] = ("계속해", time.time() - 5.0)
                await ws.send_json({
                    "type": "chat",
                    "roomId": room_id,
                    "sessionId": "sess-orch-q",
                    "text": "계속해",
                    "clientMsgId": "c-repeat-2",
                })
                for _ in range(10):
                    ev = await ws.receive_json(timeout=3.0)
                    collected.append(ev)
                    if ev.get("type") == "result" and ev.get("roomId") == room_id:
                        break

            dispatch_ev = next(e for e in collected if e.get("type") == "orchestrator_dispatch")
            self.assertTrue(dispatch_ev.get("queued"))
            orch_cid = dispatch_ev.get("clientMsgId")
            self.assertTrue(orch_cid)

            queued_orch = next(
                e for e in collected if e.get("type") == "queued" and e.get("clientMsgId") == orch_cid
            )
            self.assertEqual(queued_orch.get("text"), "추가 조사 지시")

            started_orch = next(
                e for e in collected if e.get("type") == "started" and e.get("clientMsgId") == orch_cid
            )
            self.assertIsNotNone(started_orch.get("userMsgId"))

            # 동일 문구("계속해") 두 번 모두 누락 없이 DB에 총 6개 메시지(질문3 + 답변3) 기록 확인
            msgs = chat_db.get_messages(room_id)
            user_texts = [m["text"] for m in msgs if m["role"] == "user"]
            self.assertEqual(user_texts, ["계속해", "추가 조사 지시", "계속해"])
        finally:
            turn1_gate.set()
            await ws.close()

    async def test_orchestrator_dispatch_spawn_parent_and_slash_commands(self):
        """POST /api/orchestrator/dispatch 스폰 시 parentAgentId 귀속 및 rawPrompt/slashCommands 전달 검증."""
        import bot.web.execution as web_execution

        enqueue_calls = []

        async def fake_enqueue(**kwargs):
            enqueue_calls.append(kwargs)
            return {"already_running": False, "queue_size": 0}

        with (
            patch.object(web_server, "enqueue_room_turn", new=fake_enqueue),
            patch.object(web_server, "broadcast_ws_json", new=AsyncMock()),
            patch.object(web_execution, "enqueue_room_turn", new=fake_enqueue),
            patch.object(web_execution, "broadcast_ws_json", new=AsyncMock()),
        ):
            for agent_id, parent_id in [
                ("master", "__core__"),
                ("second_brain", "__expert_vault__"),
                ("bot_dev", "__expert_coding__"),
            ]:
                res = await self.client.post(
                    "/api/orchestrator/dispatch",
                    json={
                        "action": "spawn",
                        "forceNew": True,
                        "parentAgentId": parent_id,
                        "title": f"{agent_id} 메모리 리밸런싱",
                        "prompt": f"/memory-rebalance --agent-id {agent_id}",
                        "rawPrompt": True,
                        "slashCommands": True,
                        "model": "gemini-3.8-flash",
                        "effort": "low",
                        "wait": False,
                    },
                )
                self.assertEqual(res.status, 200)
                body = await res.json()
                self.assertTrue(body.get("ok"))
                self.assertEqual(body.get("parentAgentId"), parent_id)

        self.assertEqual(len(enqueue_calls), 3)
        for call_kwargs in enqueue_calls:
            self.assertTrue(call_kwargs.get("raw_prompt"))
            self.assertTrue(call_kwargs.get("slash_commands"))


class TestWebCrashRecovery(unittest.TestCase):
    """서버 재시작 및 비정상 종료 시 미완결 작업 자가 복구 단위 테스트."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="web_recovery_test_")
        self._cleanup_runtime = _install_runtime_isolation(self.test_dir)
        self.orig_jobs_file = web_server.WEB_JOBS_FILE
        self.orig_jobs_dir = web_server.JOBS_DIR
        self.orig_chat_db_file = chat_db.CHAT_DB_FILE
        self.orig_hidden_file = getattr(web_server, "HIDDEN_ROOMS_FILE", None)
        self.orig_chat_db_hidden_file = getattr(chat_db, "HIDDEN_ROOMS_FILE", None)

        web_server.WEB_JOBS_FILE = os.path.join(self.test_dir, "web_jobs.json")
        web_server.JOBS_DIR = os.path.join(self.test_dir, "jobs")
        os.makedirs(web_server.JOBS_DIR, exist_ok=True)
        self.db_path = os.path.join(self.test_dir, "test_chat.db")
        chat_db.CHAT_DB_FILE = self.db_path
        test_hidden = os.path.join(self.test_dir, "hidden_chat_rooms.json")
        web_server.HIDDEN_ROOMS_FILE = test_hidden
        chat_db.HIDDEN_ROOMS_FILE = test_hidden
        chat_db.init_db(self.db_path, migrate_legacy=False)

    def tearDown(self):
        web_server.WEB_JOBS_FILE = self.orig_jobs_file
        web_server.JOBS_DIR = self.orig_jobs_dir
        chat_db.CHAT_DB_FILE = self.orig_chat_db_file
        if self.orig_hidden_file:
            web_server.HIDDEN_ROOMS_FILE = self.orig_hidden_file
        if self.orig_chat_db_hidden_file:
            chat_db.HIDDEN_ROOMS_FILE = self.orig_chat_db_hidden_file
        if hasattr(self, "_cleanup_runtime"):
            self._cleanup_runtime()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_save_load_remove_web_job(self):
        job_id = "test-job-1"
        rec = {"room_id": "r1", "msg_id": 10, "pid": 1234}
        web_server._save_web_job(job_id, rec)
        loaded = web_server._load_web_jobs()
        self.assertIn(job_id, loaded)
        self.assertEqual(loaded[job_id]["msg_id"], 10)

        web_server._remove_web_job(job_id)
        loaded2 = web_server._load_web_jobs()
        self.assertNotIn(job_id, loaded2)

    def test_recover_finished_job(self):
        async def _run():
            engine_settings.set_engine("agy")
            engine_settings.set_session("tg-live-before-rec", ns="")
            msg_id = chat_db.add_message("room-rec", "assistant", "", steps=[], db_path=self.db_path)
            job_id = "job-finished"
            out_file = os.path.join(web_server.JOBS_DIR, f"{job_id}.jsonl")
            with open(out_file, "w", encoding="utf-8") as f:
                f.write(json.dumps({
                    "event": "step_update",
                    "step_update": {
                        "step_type": "tool",
                        "state": "ACTIVE",
                        "tool_name": "read_file",
                        "tool_info": {"parameters": {"Path": "test.md"}}
                    }
                }) + "\n")
                f.write(json.dumps({
                    "event": "result",
                    "result": "작업이 성공적으로 완료되었습니다.",
                    "session_id": "sid-rec-1"
                }) + "\n")

            rec = {
                "job_id": job_id,
                "room_id": "room-rec",
                "msg_id": msg_id,
                "pid": None,
                "out_file": out_file,
                "started_at": 1000.0,
                "session_id": None,
            }
            web_server._save_web_job(job_id, rec)

            await web_server._recover_web_jobs()

            msg = chat_db.get_message(msg_id, db_path=self.db_path)
            self.assertEqual(msg["text"], "작업이 성공적으로 완료되었습니다.")
            self.assertEqual(msg["sessionId"], "sid-rec-1")
            self.assertEqual(web_server._load_web_jobs(), {})
            self.assertFalse(os.path.exists(out_file))
            self.assertEqual(engine_settings.get_session(""), "tg-live-before-rec")

        asyncio.run(_run())

    def test_recover_interrupted_job(self):
        async def _run():
            msg_id = chat_db.add_message("room-int", "assistant", "", steps=[], db_path=self.db_path)
            job_id = "job-interrupted"
            out_file = os.path.join(web_server.JOBS_DIR, f"{job_id}.jsonl")
            with open(out_file, "w", encoding="utf-8") as f:
                f.write(json.dumps({
                    "event": "step_update",
                    "step_update": {
                        "step_type": "tool",
                        "state": "ACTIVE",
                        "tool_name": "run_command",
                        "tool_info": {"parameters": {"CommandLine": "ls"}}
                    }
                }) + "\n")

            rec = {
                "job_id": job_id,
                "room_id": "room-int",
                "msg_id": msg_id,
                "pid": 99999999,
                "out_file": out_file,
                "started_at": 1000.0,
                "session_id": None,
            }
            web_server._save_web_job(job_id, rec)

            await web_server._recover_web_jobs()

            msg = chat_db.get_message(msg_id, db_path=self.db_path)
            self.assertIn("⚠️ 서버 재시작으로 답변이 중단되었습니다.", msg["text"])
            self.assertEqual(len(msg["steps"]), 1)
            self.assertEqual(web_server._load_web_jobs(), {})
            self.assertFalse(os.path.exists(out_file))

        asyncio.run(_run())

    def test_recover_job_with_empty_text_falls_back(self):
        async def _run():
            msg_id = chat_db.add_message("room-empty", "assistant", "", steps=[], db_path=self.db_path)
            job_id = "job-empty-text"
            out_file = os.path.join(web_server.JOBS_DIR, f"{job_id}.jsonl")
            with open(out_file, "w", encoding="utf-8") as f:
                f.write(json.dumps({
                    "event": "step_update",
                    "step_update": {
                        "step_type": "tool",
                        "state": "ACTIVE",
                        "tool_name": "run_command",
                        "tool_info": {"parameters": {"CommandLine": "git status"}}
                    }
                }) + "\n")
                f.write(json.dumps({
                    "event": "result",
                    "result": "",
                    "session_id": "sid-empty"
                }) + "\n")

            rec = {
                "job_id": job_id,
                "room_id": "room-empty",
                "msg_id": msg_id,
                "pid": None,
                "out_file": out_file,
                "started_at": 1000.0,
                "session_id": None,
            }
            web_server._save_web_job(job_id, rec)

            await web_server._recover_web_jobs()

            msg = chat_db.get_message(msg_id, db_path=self.db_path)
            self.assertTrue(len(msg["text"]) > 0)
            self.assertEqual(msg["text"], "(도구 실행이 완료되었습니다)")
            self.assertEqual(web_server._load_web_jobs(), {})
            self.assertFalse(os.path.exists(out_file))

        asyncio.run(_run())

    def test_static_index_html_ios_ipad_keyboard_layout_protection(self):
        """Mac iPad(iOS) 하드웨어 키보드 및 터치 환경에서 강제 채팅키보드 호출과 레이아웃 깨짐 방지 로직 검증."""
        content = _read_static_bundle(web_server.INTERNAL_STATIC_DIR)

        # 1. isTouchOrMobileDevice 디바이스 감지 함수 및 iPadOS(MacIntel + maxTouchPoints) 판별
        self.assertIn("function isTouchOrMobileDevice()", content)
        self.assertIn("MacIntel", content)
        self.assertIn("maxTouchPoints", content)
        self.assertIn("(pointer: coarse)", content)

        # 2. focusChatInput 에서 첫 실행 시 모바일 과도한 팝업 방지 및 preventScroll 적용
        self.assertRegex(
            content,
            r"function focusChatInput\(options\s*=\s*\{\}\)\s*\{[\s\S]*?if\s*\(isInitial\s*&&\s*isTouchOrMobileDevice\(\)\)\s*\{\s*return;",
        )
        self.assertIn("preventScroll: true", content)

        # 3. toggleVoiceRecording 에서 inputEl.focus() 가 제거되어 음성 입력 시 키보드 미호출
        self.assertRegex(
            content,
            r"function toggleVoiceRecording\(\)\s*\{[\s\S]*?const SpeechRecognition",
        )
        voice_rec_block = content.split("function toggleVoiceRecording()")[1].split("function startVoiceRecording")[0]
        self.assertNotIn("inputEl.focus()", voice_rec_block)

        # 4. 터치 디바이스 전역 폰트 16px 강제로 iOS Safari 자동 줌인 방지
        self.assertIn("@media (hover: none), (pointer: coarse)", content)
        self.assertIn("font-size: 16px !important;", content)

        # 5. initVisualViewportHandler 에서 visualViewport 높이 추적 및 하단 흰 여백 방지
        self.assertIn("window.scrollTo(0, 0);", content)
        self.assertIn("interactive-widget=resizes-content", content)
        self.assertIn("position: fixed;", content)
        self.assertIn("--app-height", content)
        self.assertIn("document.addEventListener('focusout'", content)

        # 6. iPad 및 태블릿 세로 해상도(1024px) 대응 및 화면 너비/데스크톱 크기 무관 당겨서 새로고침(PTR) 상시 유지
        self.assertIn("@media (max-width: 1024px)", content)
        self.assertIn("데스크톱/태블릿/모바일 크기와 무관하게 당겨서 새로고침(PTR) 상시 유지", content)
        self.assertIn("#room-sidebar, .sidebar", content)

        # 7. iOS/iPad 터치 환경에서 사용자 제스처 콜스택 내 동기적 포커스 호출로 가상 키보드 자동 팝업 보장
        self.assertIn('id="btn-send" class="send-btn" onpointerdown="event.preventDefault()"', content)
        focus_chat_def = content.split("function focusChatInput(options = {})")[1].split("function updateSessionBar")[0]
        self.assertNotIn("requestAnimationFrame", focus_chat_def)
        self.assertIn("input.focus({ preventScroll: true })", focus_chat_def)
        self.assertIn("function createNewChat(force = false, options = {})", content)
        self.assertIn("function selectRoom(roomId, options = {})", content)

        # 8. 모바일 대화방 선택 시 키보드(자동 포커스) 팝업 여부 사용자 설정 제어
        self.assertIn("oshyun_mobile_autofocus_v1", content)
        self.assertIn("function getMobileAutofocus()", content)
        self.assertIn("function setMobileAutofocus(", content)
        self.assertIn("function onMobileAutofocusChange()", content)
        self.assertIn('id="select-mobile-autofocus"', content)
        self.assertIn("isTouchOrMobileDevice() && !isForce && !getMobileAutofocus()", content)

        # 8. iPad 및 터치 디바이스에서 왼쪽 사이드바 1회 터치 즉시 전환 보장 (더블 탭 방지)
        self.assertIn("@media (hover: hover) and (pointer: fine)", content)
        self.assertIn(".room-item.active .room-actions-inline { display: inline-flex; }", content)
        self.assertIn("touch-action: manipulation;", content)
        self.assertIn("Date.now() - lastTouchTime < 600", content)
        self.assertIn("targetItem.classList.add('pressing');\n        }, 260);", content)

    def test_status_payload_contains_model_supported_efforts(self):
        """status 페이로드의 availableModels 각 항목이 모델별 실제 supportedEfforts를 포함하는지 검증."""
        payload = web_server.build_status_payload()
        self.assertIn("availableModels", payload)
        models = payload["availableModels"]
        self.assertTrue(len(models) > 0)

        # 모델별 supportedEfforts 검증
        model_map = {m["id"]: m for m in models}
        for m_id, m_data in model_map.items():
            self.assertIn("supportedEfforts", m_data, f"{m_id}에 supportedEfforts 누락")
            self.assertIsInstance(m_data["supportedEfforts"], list)

        # Gemini 3.8 Flash는 low, medium, high만 지원해야 하며 max는 없어야 함
        if "gemini-3.8-flash" in model_map:
            efforts = model_map["gemini-3.8-flash"]["supportedEfforts"]
            self.assertEqual(efforts, ["low", "medium", "high"])
            self.assertNotIn("max", efforts)

        # gpt-oss-120b는 medium만 지원해야 함
        if "gpt-oss-120b" in model_map:
            efforts = model_map["gpt-oss-120b"]["supportedEfforts"]
            self.assertEqual(efforts, ["medium"])
            self.assertNotIn("max", efforts)

        # Gemini 4 Hydrogen은 low, medium, high, xhigh를 지원해야 함
        self.assertIn("gemini-4-hydrogen", model_map)
        self.assertEqual(
            model_map["gemini-4-hydrogen"]["supportedEfforts"],
            ["low", "medium", "high", "xhigh"],
        )

        # Orchestrator (Argon) 모델 포함 및 effort 미지원 검증
        self.assertIn("gemini-pro-orchestrator-g1-pro", model_map)
        self.assertEqual(
            model_map["gemini-pro-orchestrator-g1-pro"]["supportedEfforts"],
            [],
        )

    def test_agy_xhigh_suffix_stripping_and_resolution(self):
        """-xhigh 접미사가 -high로 잘못 잘리지 않고 정확히 정규화되는지 검증."""
        from bot.engine.engine_drivers import AGYEngine, strip_agy_effort_suffix

        self.assertEqual(strip_agy_effort_suffix("gemini-4-hydrogen-xhigh"), "gemini-4-hydrogen")
        self.assertEqual(strip_agy_effort_suffix("gemini-4-hydrogen-high"), "gemini-4-hydrogen")
        eng = AGYEngine()
        base, eff = eng.resolve_model_and_effort("gemini-4-hydrogen-xhigh", "xhigh")
        self.assertEqual(base, "gemini-4-hydrogen")
        self.assertEqual(eff, "xhigh")

    def test_index_html_dynamic_effort_rendering(self):
        """index.html에서 고정된 5개 버튼 대신 모델별 지원 단계가 동적으로 렌더링되는지 검증."""
        content = _read_static_bundle(web_server.INTERNAL_STATIC_DIR)

        # 1. 모델별 동적 effort 추출 함수 및 축약 표시 함수 존재
        self.assertIn("function getSupportedEffortsForModel(", content)
        self.assertIn("function formatShortModelName(", content)
        self.assertIn("effort-unsupported-note", content)

        # 2. HTML 마크업에 max 버튼 하드코딩이 제거되고 동적 생성 컨테이너로 준비됨
        seg_bar_markup = content.split('id="effort-segmented-bar"')[1].split('</div>')[0]
        self.assertNotIn('data-effort="max"', seg_bar_markup)
        self.assertNotIn('data-effort="low"', seg_bar_markup)

    def test_master_agent_graph_and_sidebar_features(self):
        """마스터 에이전트 온디맨드 관제 인스펙터, 미니멀 네비/슬롯, 불필요 요소 제거, 아카이브 및 sub-agent 정리 검증."""
        content = _read_static_bundle(web_server.INTERNAL_STATIC_DIR)

        # 1. 온디맨드 메모리 인스펙터 및 메모리 토글 버튼 마크업 (불필요한 모니터/스폰/4계층 섹션 헤더 제거 확인)
        self.assertIn('id="master-sidebar"', content)
        self.assertIn('id="btn-topbar-memory-pill"', content)
        self.assertIn('id="msb-memory-section"', content)
        self.assertIn('function getActiveScrollable(touchTarget)', content)
        self.assertNotIn('#view-chat:not(.master-mode) .master-sidebar', content)
        self.assertIn('/memory-rebalance', content)
        self.assertIn('rawPrompt: true', content)
        self.assertNotIn('class="msb-section-head"', content)
        self.assertNotIn('id="btn-toggle-master-sidebar"', content)
        self.assertNotIn('id="msb-summary-text"', content)
        self.assertNotIn('id="btn-msb-mem-promote"', content)
        self.assertNotIn('id="btn-msb-mem-demote"', content)
        self.assertNotIn('id="msb-mem-cascade-stat"', content)
        self.assertIn('class="chat-main-area"', content)
        self.assertIn('id="master-drawer-backdrop"', content)

        # 2. 중복/난잡한 관제·이원화 요소(전체 관계도 뷰, 목표, 아이디어, 상단 상태 스트립, 미니 그래프, 컴포저 힌트) 제거 확인
        self.assertNotIn('id="view-graph"', content)
        self.assertNotIn('id="tab-graph"', content)
        self.assertNotIn('id="tab-goals"', content)
        self.assertNotIn('id="tab-ideas"', content)
        self.assertNotIn('id="view-goals"', content)
        self.assertNotIn('id="view-ideas"', content)
        self.assertNotIn('id="modal-new-goal"', content)
        self.assertNotIn('id="master-status-strip"', content)
        self.assertNotIn('id="master-mini-graph-canvas"', content)
        self.assertNotIn('drawDynamicGraphSvg', content)
        self.assertNotIn('class="composer-hint"', content)

        # 3. 좌측 사이드바 보관된 대화 (아카이브) 섹션 및 2개 단일 행 탭바(대화·볼트) 마크업
        self.assertIn('id="tab-chat"', content)
        self.assertIn('id="tab-vault"', content)
        self.assertIn('grid-template-columns: repeat(3, 1fr)', content)
        self.assertIn('id="sb-archived-section"', content)
        self.assertIn('id="sb-archived-list"', content)
        self.assertIn('function toggleArchivedSection()', content)
        self.assertIn('function toggleArchiveRoom(', content)

        # 4. 마스터 인스펙터 렌더링 및 새로고침 시 마스터 모드 복원 동기화 검증
        self.assertIn('function applyMasterModeElements(', content)
        self.assertIn('function toggleMasterSidebar(', content)
        self.assertIn('function syncMasterModeUi(', content)
        self.assertNotIn('function renderMsbSubsessions(', content)
        self.assertNotIn('function renderMsbTasks(', content)
        self.assertIn('function updateMasterTopbarBadge(', content)

        apply_master_block = content.split('function applyMasterModeElements(')[1].split('function toggleMasterSidebar(')[0]
        self.assertIn('const isMaster = (currentRoomId === CORE_ROOM_ID)', apply_master_block)
        self.assertIn("viewChat.classList.toggle('master-mode', isMaster)", apply_master_block)

        update_session_bar_block = content.split('function updateSessionBar(')[1].split('function toggleRoomConfigPopover(')[0]
        self.assertIn('syncMasterModeUi(', update_session_bar_block)

        toggle_master_block = content.split('function toggleMasterSidebar(')[1].split('async function loadMasterGraphFromServer(')[0]
        self.assertIn('applyMasterModeElements()', toggle_master_block)
        self.assertIn("const willOpen = (forceState !== null) ? Boolean(forceState) : !sidebar.classList.contains('open');", toggle_master_block)

        load_rooms_block = content.split('async function loadChatRoomsFromServer(')[1].split('async function syncRoomsWithServer(')[0]
        self.assertIn('currentRoomId !== CORE_ROOM_ID', load_rooms_block)
        self.assertIn('ensureCoreRoom()', load_rooms_block)
        self.assertIn('syncMasterModeUi({ loadData: true })', load_rooms_block)

        ptr_block = content.split('function initPullToRefresh(')[1].split('function initVisualViewportHandler(')[0]
        self.assertIn("document.getElementById('master-sidebar')", ptr_block)
        self.assertIn('.master-drawer-backdrop', ptr_block)
        # 스크롤할 내용이 없는 짧은 대화방 및 빈 화면에서도 당겨서 새로고침(PTR)이 정상 트리거되도록 하는 판정 로직 및 상향된 임계값/원복 로직 검증
        self.assertIn('startedAtTop = scrollTop <= 1;', ptr_block)
        self.assertIn('startedAtBottom = distFromBottom <= 1;', ptr_block)
        self.assertIn('hasScrolledDuringTouch', ptr_block)
        self.assertIn('const PTR_DEADZONE_PX = 80;', content)
        self.assertIn('const PTR_TRIGGER_THRESHOLD_PX = 180;', content)
        self.assertIn('signedDy >= PTR_TRIGGER_THRESHOLD_PX', ptr_block)
        self.assertIn('resetPullToRefreshIndicator(', ptr_block)
        self.assertIn('restoreScrollerAnchor()', ptr_block)
        self.assertIn('.ptr-indicator.returning', content)

        # 5. sub-agent 보고 메시지 좌측 발화 UI 및 내부 메타데이터 필터링 검증
        self.assertIn('function isSubAgentReportMessage(', content)
        self.assertIn('function parseSubAgentReport(', content)
        self.assertIn('sub-agent-avatar', content)
        self.assertIn('sub-agent-md', content)
        self.assertIn('sub-agent-room-btn', content)
        self.assertIn('sub-agent-details', content)

        # 6. 마스터 하위 태스크 트리 뷰, 초기화 버튼 및 4계층 메모리 시각화 파이프라인 검증
        self.assertIn('id="master-subtask-tree"', content)
        self.assertIn('id="btn-core-agent-reset"', content)
        self.assertIn('function isMasterSubtaskRoom(', content)
        self.assertIn('function renderMasterSubtaskTree(', content)
        self.assertIn('id="msb-mem-pipeline"', content)
        self.assertIn('id="btn-topbar-memory-pill"', content)
        self.assertIn('function openMasterMemoryInspector(', content)

    def test_sub_agent_report_role_and_migration(self):
        """task_report 및 기존 보고 메시지가 sub-agent 역할로 저장·마이그레이션·조회되는지 검증."""
        import asyncio
        import sqlite3

        chat_db.get_or_create_room("__core__", title="마스터 에이전트")

        # 1. 레거시 user 역할로 저장된 보고 메시지가 init_db 마이그레이션 시 sub-agent 로 전환되는지 검증
        with sqlite3.connect(chat_db.CHAT_DB_FILE) as conn:
            conn.execute(
                "INSERT INTO messages (room_id, role, channel, text, created_at) VALUES (?, ?, ?, ?, ?);",
                ("__core__", "user", "task_report", "[하위 작업 완료 보고] [방A] 세션 작업 완료\n• 내용: 끝", 1000.0),
            )
            conn.execute(
                "INSERT INTO messages (room_id, role, channel, text, created_at) VALUES (?, ?, ?, ?, ?);",
                ("__core__", "user", "web", "[하위 작업 중단/오류 보고] [방B] 세션 작업 중단\n• 내용: 실패", 1001.0),
            )
            conn.execute(
                "INSERT INTO messages (room_id, role, channel, text, created_at) VALUES (?, ?, ?, ?, ?);",
                ("__core__", "user", "web", "[System Event] 서브 태스크 task_1의 보고가 도착했습니다 (COMPLETED, Attempt #1)", 1002.0),
            )
            conn.execute(
                "INSERT INTO messages (room_id, role, channel, text, created_at) VALUES (?, ?, ?, ?, ?);",
                ("__core__", "user", "web", "일반 사용자 질문입니다.", 1003.0),
            )
            conn.commit()

        chat_db.init_db(db_path=chat_db.CHAT_DB_FILE, migrate_legacy=False)

        msgs = chat_db.get_messages("__core__")
        self.assertEqual(len(msgs), 4)
        self.assertEqual(msgs[0]["role"], "sub-agent")
        self.assertEqual(msgs[0]["sender"], "sub-agent")
        self.assertEqual(msgs[1]["role"], "sub-agent")
        self.assertEqual(msgs[2]["role"], "sub-agent")
        self.assertEqual(msgs[3]["role"], "user")
        self.assertEqual(msgs[3]["sender"], "user")

        # DB 실제 컬럼도 sub-agent 로 보정되었는지 확인
        with sqlite3.connect(chat_db.CHAT_DB_FILE) as conn:
            roles = [r[0] for r in conn.execute("SELECT role FROM messages WHERE room_id='__core__' ORDER BY id ASC;").fetchall()]
        self.assertEqual(roles, ["sub-agent", "sub-agent", "sub-agent", "user"])

        # 2. channel="task_report" 로 신규 저장 시 sub-agent 역할로 저장되고 execute_chat_turn 도 동일하게 기록하는지 검증
        mid = chat_db.add_message(
            "__core__",
            "user",
            "[하위 작업 완료 보고] 직접 호출 테스트",
            channel="task_report",
        )
        single = chat_db.get_message(mid)
        self.assertIsNotNone(single)
        self.assertEqual(single["role"], "sub-agent")
        self.assertEqual(single["sender"], "sub-agent")

        mock_proc = MagicMock(pid=77712, out_file="/tmp/mock_sub_report.jsonl")
        mock_watch = {
            "result": "보고를 확인하고 종결했습니다.",
            "session_id": "core-sess-sub-ui",
            "model": "claude-sonnet-5-5",
            "interrupted": False,
        }
        with patch("bot.web.server.spawn_agent", return_value=mock_proc), patch(
            'bot.web.server.watch_agent', new_callable=AsyncMock, return_value=mock_watch
        ), patch("bot.web.server.set_session"), patch("bot.web.server.update_last_activity"), patch(
            'bot.web.server.vault_log'
        ), patch("bot.web.server.broadcast_ws_json", new_callable=AsyncMock):
            asyncio.run(
                web_server.execute_chat_turn(
                    "[하위 작업 중단/오류 보고] [sub-x] 세션 작업 중단\n• 내용: 타임아웃",
                    False,
                    room_id="__core__",
                    channel="task_report",
                )
            )

        updated_msgs = chat_db.get_messages("__core__")
        self.assertEqual(updated_msgs[-2]["role"], "sub-agent")
        self.assertEqual(updated_msgs[-2]["sender"], "sub-agent")
        self.assertEqual(updated_msgs[-2]["channel"], "task_report")
        self.assertEqual(updated_msgs[-1]["role"], "assistant")
        self.assertEqual(updated_msgs[-1]["text"], "보고를 확인하고 종결했습니다.")

    def test_archive_room_api(self):
        """POST /api/chat/rooms/{room_id}/archive 및 /api/orchestrator/session/archive API 검증."""
        import asyncio
        from aiohttp.test_utils import make_mocked_request

        chat_db.get_or_create_room("room-api-arch-1", title="API 아카이브 테스트")
        chat_db.add_message("room-api-arch-1", "user", "안녕하세요")

        # 1. 일반 방 아카이브 처리
        req = make_mocked_request(
            "POST",
            "/api/chat/rooms/room-api-arch-1/archive",
            match_info={"room_id": "room-api-arch-1"},
        )
        req._read_bytes = b'{"isArchived": true}'
        resp = asyncio.run(web_server.handle_archive_chat_room(req))
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.text)
        self.assertTrue(data.get("ok"))
        self.assertTrue(data.get("isArchived"))

        r = chat_db.get_room("room-api-arch-1")
        self.assertTrue(r["isArchived"])

        # 2. 마스터 관제 세션 아카이브 해제 (복원)
        req_restore = make_mocked_request(
            "POST",
            "/api/orchestrator/session/archive",
        )
        req_restore._read_bytes = b'{"roomId": "room-api-arch-1", "isArchived": false}'
        resp_restore = asyncio.run(web_server.handle_orchestrator_archive_session(req_restore))
        self.assertEqual(resp_restore.status, 200)
        data_restore = json.loads(resp_restore.text)
        self.assertTrue(data_restore.get("ok"))
        self.assertFalse(data_restore.get("isArchived"))

        r_restored = chat_db.get_room("room-api-arch-1")
        self.assertFalse(r_restored["isArchived"])

        # 3. 마스터 에이전트(__core__)는 아카이브 불가 (400 오류)
        req_core = make_mocked_request(
            "POST",
            "/api/chat/rooms/__core__/archive",
            match_info={"room_id": "__core__"},
        )
        req_core._read_bytes = b'{"isArchived": true}'
        resp_core = asyncio.run(web_server.handle_archive_chat_room(req_core))
        self.assertEqual(resp_core.status, 400)

    def test_get_ssl_context_configurations(self):
        """web_server.get_ssl_context 환경변수 및 기본 경로 처리 검증."""
        # 1. WEB_SSL_ENABLED=0 이면 항상 None
        with patch.dict(os.environ, {"WEB_SSL_ENABLED": "0"}):
            self.assertIsNone(web_server.get_ssl_context())

        # 2. 존재하지 않는 인증서 파일 지정 시 None 반환
        with patch.dict(os.environ, {"WEB_SSL_ENABLED": "1", "WEB_SSL_CERT": "/non/existent/cert.pem", "WEB_SSL_KEY": "/non/existent/key.pem"}):
            self.assertIsNone(web_server.get_ssl_context())

        # 3. 유효한 인증서가 존재하는 경우 SSLContext 생성 확인
        from bot.core.paths import DEFAULT_SSL_CERT_FILE, DEFAULT_SSL_KEY_FILE
        if os.path.exists(DEFAULT_SSL_CERT_FILE) and os.path.exists(DEFAULT_SSL_KEY_FILE):
            ctx = web_server.get_ssl_context()
            self.assertIsNotNone(ctx)
            import ssl
            self.assertIsInstance(ctx, ssl.SSLContext)

    def test_static_index_html_pretty_link_chips(self):
        """index.html 에 Gemini 스타일 프리티 링크 칩 CSS·렌더링 및 Bare URL/obsid.net 디코딩 로직이 있는지 검증."""
        content = _read_static_bundle(web_server.INTERNAL_STATIC_DIR)
        bundle_path = _get_static_bundle_path(web_server.INTERNAL_STATIC_DIR)

        self.assertIn(".md .md-link-chip", content)
        self.assertIn(".md .md-link-chip.is-vault", content)
        self.assertIn("function decodeSafeUrlPart(", content)
        self.assertIn("function buildPrettyLinkHtml(", content)
        self.assertIn("function renderInline(", content)

        node_bin = shutil.which("node")
        if node_bin:
            import subprocess

            js_script = r"""
const fs = require("fs");
const html = fs.readFileSync(process.argv[1], "utf8");
const iconsMatch = html.match(/const ICONS = \{[\s\S]*?\};/);
const escapeMatch = html.match(/function escapeHtml\(str\) \{[\s\S]*?\n    \}/);
const renderBlockMatch = html.match(/function decodeSafeUrlPart[\s\S]*?(?=\s*async function onWikilinkClick)/);
const fmtMatch = html.match(/function formatMarkdown\(text\) \{[\s\S]*?\n    \}/);
const code = [iconsMatch[0], escapeMatch[0], renderBlockMatch[0], fmtMatch[0], "module.exports = { buildPrettyLinkHtml, renderInline, formatMarkdown };"].join("\n");
const mod = {};
new Function("module", code)(mod);
const { formatMarkdown } = mod.exports;

const out1 = formatMarkdown("확인 링크: https://obsid.net/?vault=oshyun&file=%EA%B0%9C%EC%9D%B8%2F%EC%9E%90%EB%8F%99%EC%B0%A8%20%EA%B4%80%EB%A6%AC");
if (!out1.includes('class="md-link-chip is-vault"') || !out1.includes(">자동차 관리.md<") || !out1.includes(">· 개인<")) {
  throw new Error("Unexpected obsid bare URL output: " + out1);
}

const out2 = formatMarkdown("[link:캘린더.md](https://obsid.net/?vault=oshyun&file=%EC%BA%98%EB%A6%B0%EB%8D%94)");
if (!out2.includes('class="md-link-chip is-vault"') || !out2.includes(">캘린더.md<") || out2.includes("link:")) {
  throw new Error("Unexpected markdown obsid link output: " + out2);
}

const out3 = formatMarkdown("- Genius: https://genius.com/Eminem-lose-yourself-lyrics (가사)");
if (!out3.includes('class="md-link-chip"') || !out3.includes(">genius.com<") || !out3.includes(">· /Eminem-lose-yourself-lyrics<") || !out3.includes("(가사)")) {
  throw new Error("Unexpected external bare URL output: " + out3);
}

const out4 = formatMarkdown("`https://raw-code.example.com`");
if (out4.includes("md-link-chip") || !out4.includes("<code>https://raw-code.example.com</code>")) {
  throw new Error("Inline code URL should stay code: " + out4);
}

const out5 = formatMarkdown('| 항목 | 절감 |\n|---|---|\n| 보험 | <span style="color:#2e9b3f">**-3,990원 절감**</span> |');
if (!out5.includes('<span style="color:#2e9b3f"><strong>-3,990원 절감</strong></span>') || out5.includes('&lt;span')) {
  throw new Error("Span tag inside table failed to render: " + out5);
}

const out6 = formatMarkdown('<span onclick="alert(1)" style="color:#e03131; background-image:url(javascript:alert(1))">경고</span>');
if (!out6.includes('<span style="color:#e03131">경고</span>') || out6.includes('onclick') || out6.includes('javascript:')) {
  throw new Error("Unsafe attributes on span were not sanitized: " + out6);
}

const out7 = formatMarkdown('`<span style="color:#2e9b3f">코드</span>` <script>alert(1)</script>');
if (!out7.includes('<code>&lt;span style=&quot;color:#2e9b3f&quot;&gt;코드&lt;/span&gt;</code>') || !out7.includes('&lt;script&gt;alert(1)&lt;/script&gt;')) {
  throw new Error("Inline code span or script escaping failed: " + out7);
}

const out8 = formatMarkdown("스킨케어 루틴 ( [link:스킨케어 루틴(아침).md](https://obsid.net/?vault=oshyun&file=%EA%B0%9C%EC%9D%B8%2F%EC%8A%A4%ED%82%A8%EC%BC%80%EC%96%B4%20%EB%A3%A8%ED%8B%B4(%EC%95%84%EC%B9%A8).md) )의 브리핑");
if (!out8.includes('>스킨케어 루틴(아침).md<') || !out8.includes('>· 개인<') || out8.includes('스킨케어 루틴)의') || !out8.includes(')의 브리핑')) {
  throw new Error("Bracket in link failed: " + out8);
}
"""
            res = subprocess.run(
                [node_bin, "-e", js_script, bundle_path],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(res.returncode, 0, res.stderr or res.stdout)

    def test_static_index_html_mobile_paste_and_collapsible_bubble(self):
        """index.html 에 모바일 긴 붙여넣기 최적화(pendingLongPastes, textContent) 및 긴 말풍선 축약/확대 UI가 있는지 검증."""
        content = _read_static_bundle(web_server.INTERNAL_STATIC_DIR)

        self.assertIn("let pendingLongPastes = []", content)
        self.assertIn("LONG_PASTE_CHAR_THRESHOLD", content)
        self.assertIn("function getRawChatInputTypedText(", content)
        self.assertIn("function removeLongPaste(", content)
        self.assertIn('autocorrect="off"', content)
        self.assertIn(".msg.user .bubble.is-collapsed .bubble-text-body", content)
        self.assertIn(".bubble-expand-btn", content)
        self.assertIn("bubble-expand-btn is-top", content)
        self.assertIn("bubble-expand-btn is-bottom", content)

    def test_static_index_html_font_size_setting(self):
        """index.html 에 UI 전체 크기 및 채팅방 본문 폰트 슬라이더, CSS 변수 동적 제어, localStorage 저장 로직이 있는지 검증."""
        content = _read_static_bundle(web_server.INTERNAL_STATIC_DIR)

        # UI 전체 크기 슬라이더 검증
        self.assertIn('id="range-ui-font-size"', content)
        self.assertIn('min="12" max="18"', content)
        self.assertIn('oninput="onUiFontSizeInput(this.value)"', content)
        self.assertIn('id="ui-font-size-value"', content)
        self.assertIn('id="btn-ui-font-size-reset"', content)

        # 채팅방 본문 폰트 슬라이더 및 실시간 미리보기 검증
        self.assertIn('id="range-font-size"', content)
        self.assertIn('type="range"', content)
        self.assertIn('min="12"', content)
        self.assertIn('max="22"', content)
        self.assertIn('step="0.5"', content)
        self.assertIn('oninput="onFontSizeInput(this.value)"', content)
        self.assertIn('id="font-size-value"', content)
        self.assertIn('id="btn-all-font-size-reset"', content)
        self.assertIn('class="font-size-preview"', content)

        # CSS 변수 및 동적 제어 검증
        self.assertIn('--ui-font-size: 14px;', content)
        self.assertIn('--ui-scale: 1;', content)
        self.assertIn('--chat-font-size: 15px;', content)
        self.assertIn('--body-font-size: var(--ui-font-size, 14px);', content)
        self.assertIn('var(--chat-font-size, 15px)', content)
        self.assertIn('var(--ui-scale, 1)', content)
        self.assertIn("document.documentElement.style.setProperty('--ui-scale'", content)
        self.assertIn("document.documentElement.style.setProperty('--chat-font-size'", content)

        # JS 함수, 레거시 마이그레이션 및 로컬스토리지 키 검증
        self.assertIn("STORAGE_KEY_UI_FONT_SIZE = 'oshyun_ui_font_size_v1'", content)
        self.assertIn("STORAGE_KEY_FONT_SIZE = 'oshyun_font_size_v1'", content)
        self.assertIn("LEGACY_FONT_SIZE_MAP", content)
        self.assertIn("function getUiFontSize(", content)
        self.assertIn("function setUiFontSize(", content)
        self.assertIn("function applyUiFontSize(", content)
        self.assertIn("function onUiFontSizeInput(", content)
        self.assertIn("function getFontSize(", content)
        self.assertIn("function setFontSize(", content)
        self.assertIn("function applyFontSize(", content)
        self.assertIn("function updateFontSizeUi(", content)
        self.assertIn("function onFontSizeInput(", content)
        self.assertIn("function onFontSizeChange(", content)
        self.assertIn("function resetAllFontSizes(", content)

    def test_static_index_html_ssot_message_upsert_and_no_duplicate_runtime(self):
        """index.html 의 room_created + orchestrator_dispatch 중복 방지 및 큐/방 격리 런타임 동작 검증."""
        content = _read_static_bundle(web_server.INTERNAL_STATIC_DIR)
        bundle_path = _get_static_bundle_path(web_server.INTERNAL_STATIC_DIR)

        self.assertIn("function upsertRoomMessage(", content)
        self.assertIn("if (room && !room.userCreatedEmpty)", content)

        node_bin = shutil.which("node")
        if not node_bin:
            self.skipTest("node 바이너리가 없어 JS 런타임 시뮬레이션을 건너뜁니다.")

        import subprocess

        js_script = r"""
const fs = require("fs");
const assert = require("assert");
const html = fs.readFileSync(process.argv[1], "utf8");

function extractFn(name) {
  const m = html.match(new RegExp(`function ${name}\\([^\\)]*\\) \\{[\\s\\S]*?\\n    \\}`));
  if (!m) throw new Error("Missing function: " + name);
  return m[0];
}

const code = `
const CORE_ROOM_ID = '__core__';
const CORE_ROOM_TITLE = '마스터 에이전트';
const EXPERT_VAULT_ROOM_ID = '__expert_vault__';
const EXPERT_CODING_ROOM_ID = '__expert_coding__';
const ORCHESTRATOR_ROOM_IDS = [CORE_ROOM_ID, EXPERT_VAULT_ROOM_ID, EXPERT_CODING_ROOM_ID];
let chatRooms = [];
let currentRoomId = 'room-other';
let lastRequestedRoomId = 'room-other';
let roomLiveStates = {};
let selectedRoomIds = new Set();
let isVoiceRecording = false;
let roomsRefreshCount = 0;

function sortChatRooms() {}
function saveChatRoomsToStorage() {}
function renderRoomList() {}
function renderMessagesForCurrentRoom() {}
function updateRoomConfigBar() {}
function updateComposerStatusBar() {}
function updateSendButtonState() {}
function syncLiveElapsedTimer() {}
function setAgentAvatarStatus() {}
function setOrbState() {}
function showToast() {}
function filterToolSteps(s) { return Array.isArray(s) ? s : []; }
function updateSessionBar() {}
function playTts() {}
function getTtsMode() { return 'off'; }
function loadChatHistoryFromServer() {}
function scheduleRoomsRefreshFromEvent() { roomsRefreshCount++; }
function ensureCoreRoom() { return null; }
function handleAgentResponseNotification() {}
function updateInlineChatChipsStatus() {}
function updateAllInlineChatChips() {}

${extractFn("mergeToolSteps")}
${extractFn("deduplicateMessagesList")}
${extractFn("getQueuedMessagesCount")}
${extractFn("insertMessageBeforeQueued")}
${extractFn("upsertRoomMessage")}
${extractFn("renumberQueuedMessages")}
${extractFn("getRoomById")}
${extractFn("getCurrentRoom")}
${extractFn("ensureRoomLiveState")}
${extractFn("applyActiveRunsSnapshot")}
${extractFn("resolveEventRoom")}
${extractFn("handleWsEvent")}

module.exports = {
  state: () => ({ chatRooms, currentRoomId, roomLiveStates, roomsRefreshCount }),
  setCurrentRoom: (id) => { currentRoomId = id; lastRequestedRoomId = id; },
  handleWsEvent,
  resolveEventRoom,
  applyActiveRunsSnapshot,
  deduplicateMessagesList,
};
`;

const mod = {};
new Function("module", code)(mod);
const app = mod.exports;

const fullMsg = "사용자가 '택시비 할인카드 빠르게 찾아'라고 요청했습니다. 다음 순서로 빠르게 조사해 요약해 주세요: 1) 먼저 볼트 내 _bot/컨텍스트.md 및 카드 보유현황 문서를 확인하고 2) 시중 인기 카드 3~5개를 비교해 주세요.";
const preview60 = fullMsg.slice(0, 60);

// 시나리오 1: spawn -> room_created(60자 preview) -> orchestrator_dispatch(전체 원문) -> started -> activeRuns -> result
app.handleWsEvent({
  type: "room_created",
  byOrchestrator: true,
  room: {
    id: "room-taxi-1",
    roomId: "room-taxi-1",
    title: "택시비 할인카드 조회",
    preview: preview60,
    messageCount: 1,
    isNewSession: true,
    isOrchestratorChild: true,
    byOrchestrator: true,
  },
});

const afterCreated = app.state().chatRooms.find(r => r.id === "room-taxi-1");
assert.ok(afterCreated, "room_created must register room");
assert.strictEqual(afterCreated.isOrchestratorChild, true);
assert.strictEqual(afterCreated.messages.length, 0, "room_created must NOT inject truncated preview into messages");

app.handleWsEvent({
  type: "orchestrator_dispatch",
  action: "spawn",
  roomId: "room-taxi-1",
  title: "택시비 할인카드 조회",
  text: fullMsg,
  msgId: 101,
  clientMsgId: "orch-abc123",
  queued: false,
});
app.handleWsEvent({
  type: "started",
  roomId: "room-taxi-1",
  userText: fullMsg,
  userMsgId: 101,
  clientMsgId: "orch-abc123",
  channel: "orchestrator",
});
app.applyActiveRunsSnapshot([
  {
    roomId: "room-taxi-1",
    jobId: "web-job-1",
    userMsgId: 101,
    clientMsgId: "orch-abc123",
    userText: fullMsg,
    channel: "orchestrator",
    isRunning: true,
  }
], false);
app.handleWsEvent({
  type: "result",
  roomId: "room-taxi-1",
  msgId: 102,
  userMsgId: 101,
  clientMsgId: "orch-abc123",
  text: "보유 카드 및 추천 카드 비교 요약입니다.",
  sessionId: "sess-taxi-1",
});

const taxiRoom = app.state().chatRooms.find(r => r.id === "room-taxi-1");
assert.strictEqual(taxiRoom.messages.length, 2, "Expected exactly 1 user + 1 assistant message, got " + JSON.stringify(taxiRoom.messages));
assert.strictEqual(taxiRoom.messages[0].text, fullMsg);
assert.strictEqual(taxiRoom.messages[0].id, 101);
assert.strictEqual(taxiRoom.messages[1].id, 102);

// 시나리오 2: 실행 중인 방에 orchestrator send -> queued -> 이전 턴 result -> started -> 후속 result
app.handleWsEvent({
  type: "started",
  roomId: "room-taxi-1",
  userText: "두 번째 질문",
  userMsgId: 103,
  clientMsgId: "c-2",
  channel: "web",
});
app.handleWsEvent({
  type: "orchestrator_dispatch",
  action: "send",
  roomId: "room-taxi-1",
  text: "세 번째 마스터 지시",
  clientMsgId: "orch-q-3",
  queued: true,
});
app.handleWsEvent({
  type: "queued",
  roomId: "room-taxi-1",
  text: "세 번째 마스터 지시",
  channel: "orchestrator",
  clientMsgId: "orch-q-3",
  position: 1,
});
app.handleWsEvent({
  type: "result",
  roomId: "room-taxi-1",
  msgId: 104,
  text: "두 번째 답변",
});
app.handleWsEvent({
  type: "started",
  roomId: "room-taxi-1",
  userText: "세 번째 마스터 지시",
  userMsgId: 105,
  clientMsgId: "orch-q-3",
  channel: "orchestrator",
});
app.handleWsEvent({
  type: "result",
  roomId: "room-taxi-1",
  msgId: 106,
  text: "세 번째 답변",
});

assert.deepStrictEqual(
  taxiRoom.messages.map(m => m.text),
  [fullMsg, "보유 카드 및 추천 카드 비교 요약입니다.", "두 번째 질문", "두 번째 답변", "세 번째 마스터 지시", "세 번째 답변"]
);

// 시나리오 3: 알 수 없는 roomId 이벤트가 현재 방으로 오염되지 않아야 함
app.setCurrentRoom("room-taxi-1");
const leaked = app.resolveEventRoom({ type: "result", roomId: "unknown-room-999" });
assert.strictEqual(leaked, null, "Unknown roomId must not fallback to currentRoom");
app.handleWsEvent({ type: "result", roomId: "unknown-room-999", text: "잘못된 방 답변" });
assert.strictEqual(taxiRoom.messages.length, 6);
assert.strictEqual(app.state().roomsRefreshCount, 1);
"""
        res = subprocess.run(
            [node_bin, "-e", js_script, bundle_path],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 0, res.stderr or res.stdout)

    def test_file_picker_and_visibilitychange_room_context_preservation(self):
        """파일 피커/이미지 첨부 및 visibilitychange 복귀 시 전문가/마스터 에이전트 룸 컨텍스트 유지 방어 로직 검증."""
        content = _read_static_bundle(web_server.INTERNAL_STATIC_DIR)

        # 1. 파일 피커 활성화 플래그 및 핸들러 바인딩
        self.assertIn("let isFilePickerActive = false;", content)
        self.assertIn("isFilePickerActive = true;", content)
        self.assertIn("isFilePickerActive = false;", content)
        self.assertIn("fi.addEventListener('cancel'", content)

        # 2. cleanUnusedEmptyRooms 및 cleanCurrentIfUnusedEmpty의 파일 피커 가드 & ORCHESTRATOR_ROOM_IDS 보존
        clean_rooms_block = content.split("function cleanUnusedEmptyRooms(")[1].split("function cleanCurrentIfUnusedEmpty(")[0]
        self.assertIn("if (typeof isFilePickerActive !== 'undefined' && isFilePickerActive) return;", clean_rooms_block)
        self.assertIn("!orchIds.includes(currentRoomId)", clean_rooms_block)

        # 3. loadChatRoomsFromServer의 ensureCoreRoom 상시 호출 및 오케스트레이터 방 fallback 차단
        load_rooms_block = content.split("async function loadChatRoomsFromServer(")[1].split("async function syncRoomsWithServer(")[0]
        self.assertIn("ensureCoreRoom();", load_rooms_block)
        self.assertIn("isOrchRoom", load_rooms_block)
        self.assertIn("currentRoomId !== CORE_ROOM_ID && !isOrchRoom", load_rooms_block)

        # 4. getRoomById 및 getCurrentRoom의 전문가/오케스트레이터 룸 보장
        get_room_block = content.split("function getRoomById(")[1].split("function ensureRoomLiveState(")[0]
        self.assertIn("orchIds.includes(roomId)", get_room_block)

        cur_room_block = content.split("function getCurrentRoom(")[1].split("function getWorkdirForRoom(")[0]
        self.assertIn("orchIds.includes(currentRoomId)", cur_room_block)

        # 5. visibilitychange 이벤트 리스너의 룸 컨텍스트 보존(preservedRoomId) 및 복구
        self.assertIn("const preservedRoomId = currentRoomId;", content)
        self.assertIn("currentRoomId = preservedRoomId;", content)

    def test_memory_rebalance_dynamic_parent_ui_wiring(self):
        """웹 UI 메모리 리밸런싱이 선택 에이전트(master/second_brain/bot_dev)에 따라 동적 parentAgentId로 스폰하는지 검증."""
        content = _read_static_bundle(web_server.INTERNAL_STATIC_DIR)

        self.assertIn("const MEMORY_AGENT_PARENT_MAP =", content)
        self.assertIn("master: CORE_ROOM_ID", content)
        self.assertIn("second_brain: EXPERT_VAULT_ROOM_ID", content)
        self.assertIn("bot_dev: EXPERT_CODING_ROOM_ID", content)

        rebalance_block = content.split("async function rebalanceMasterMemory()")[1].split("function applyOptimisticMemoryMove(")[0]
        self.assertIn("fetch('/api/orchestrator/dispatch'", rebalance_block)
        self.assertIn("parentAgentId: parentRoomId", rebalance_block)
        self.assertIn("rawPrompt: true", rebalance_block)
        self.assertIn("slashCommands: true", rebalance_block)
        self.assertIn("forceNew: true", rebalance_block)

    def test_browser_and_system_notification_on_agent_reply(self):
        """에이전트 응답 완료 시 브라우저/시스템 레벨 알림 및 PWA/SW 지원 로직 검증."""
        content = _read_static_bundle(web_server.INTERNAL_STATIC_DIR)

        sw_path = os.path.join(web_server.INTERNAL_STATIC_DIR, "sw.js")
        with open(sw_path, "r", encoding="utf-8") as f:
            sw_content = f.read()

        # 1. 설정 UI 마크업
        self.assertIn('id="settings-group-notification"', content)
        self.assertIn('id="select-notification-mode"', content)
        self.assertIn('id="select-notification-sound"', content)
        self.assertIn('id="btn-request-notification"', content)
        self.assertIn('id="btn-test-notification"', content)

        # 2. 알림 함수 및 핵심 로직
        self.assertIn("function handleAgentResponseNotification(", content)
        self.assertIn("function triggerSystemNotification(", content)
        self.assertIn("function playNotificationChime(", content)
        self.assertIn("function cleanNotificationText(", content)
        self.assertIn("function updateNotificationUi(", content)
        self.assertIn("function maybeRequestNotificationPermission(", content)

        # 3. result 및 error 처리 시 알림 연동
        self.assertIn("handleAgentResponseNotification(targetRoom, replyText)", content)
        self.assertIn("handleAgentResponseNotification(targetRoom, errMsg, { isError: true })", content)

        # 4. 사용자 발화(전송) 시 선제 권한 요청 연동
        send_block = content.split("async function sendUserMessage(")[1].split("lastSentMessageTime = now;")[0]
        self.assertIn("maybeRequestNotificationPermission();", send_block)

        # 5. Service Worker 알림 클릭 처리
        self.assertIn("self.addEventListener('notificationclick'", sw_content)
        self.assertIn("NAVIGATE_ROOM", sw_content)
        self.assertIn("event.notification.close()", sw_content)

    def test_sidebar_layout_search_toolbar_position_and_content_search(self):
        """사이드바 flex 고정/스크롤 구조, 검색창 상단 배치, 본문 검색 연동 검증."""
        content = _read_static_bundle(web_server.INTERNAL_STATIC_DIR)

        # 1. 사이드바 flex + overflow: hidden 및 독립 스크롤 컨테이너 CSS 검증
        sidebar_css = content.split(".sidebar {")[1].split("}")[0]
        self.assertIn("flex-direction: column", sidebar_css)
        self.assertIn("overflow: hidden", sidebar_css)
        self.assertIn("height: 100%", sidebar_css)

        scroll_css = content.split(".sb-scroll-area {")[1].split("}")[0]
        self.assertIn("flex: 1", scroll_css)
        self.assertIn("min-height: 0", scroll_css)
        self.assertIn("overflow-y: auto", scroll_css)

        footer_css = content.split("\n    .sb-footer {")[1].split("}")[0]
        self.assertIn("flex-shrink: 0", footer_css)

        # 2. HTML 배치 순서: sb-nav -> sb-rooms-toolbar(검색창) -> sb-scroll-area(sb-core-section + room-list-pc) -> sb-footer
        nav_pos = content.find('<nav class="sb-nav">')
        toolbar_pos = content.find('<div class="sb-rooms-toolbar">')
        scroll_pos = content.find('<div class="sb-scroll-area" id="sb-scroll-area">')
        core_pos = content.find('<div class="sb-core-section" id="sb-core-section">')
        rooms_pos = content.find('<div class="sb-rooms" id="room-list-pc">')
        footer_pos = content.find('<div class="sb-footer">')

        self.assertGreater(toolbar_pos, nav_pos)
        self.assertGreater(scroll_pos, toolbar_pos)
        self.assertGreater(core_pos, scroll_pos)
        self.assertGreater(rooms_pos, core_pos)
        self.assertGreater(footer_pos, rooms_pos)

        # 3. 본문 검색 API 연동 및 스니펫 렌더링 검증
        self.assertIn("/api/chat/rooms/search?q=", content)
        self.assertIn("roomServerSearchResults", content)
        self.assertIn("room-search-snippet", content)

    def test_chat_chip_status_integration_and_no_bottom_chips(self):
        """메시지 하단 하위 채팅 영역 제거 및 본문 인라인 링크 칩 상태 표시 통합 검증."""
        content = _read_static_bundle(web_server.INTERNAL_STATIC_DIR)

        # 1. 메시지 하단에 별도로 appendChild 되던 하위 채팅 영역 제거 검증
        self.assertNotIn("row.appendChild(chipsDom)", content)
        self.assertNotIn("liveRow.appendChild(liveChipsDom)", content)

        # 2. 본문 인라인 채팅 칩 상태 해석 및 실시간 갱신 함수 존재 검증
        self.assertIn("function resolveChatRoomStatus(", content)
        self.assertIn("function updateInlineChatChipsStatus(", content)
        self.assertIn("function updateAllInlineChatChips()", content)

        # 3. buildChatRoomChipHtml 에서 상태 인디케이터 및 data-room-id / data-status 속성 렌더링 검증
        self.assertIn('class="chip-status-wrap"', content)
        self.assertIn('class="chip-spin-wheel"', content)
        self.assertIn('class="chip-status-icon is-running"', content)
        self.assertIn('class="chip-status-icon is-done"', content)
        self.assertIn('class="chip-status-icon is-failed"', content)

        # 4. CSS 상태 스타일링 검증 (스핀휠 애니메이션, 완료 체크, 실패 아이콘)
        css_path = os.path.join(web_server.INTERNAL_STATIC_DIR, "css", "chat.css")
        with open(css_path, "r", encoding="utf-8") as f:
            css_content = f.read()
        self.assertIn('.md .md-link-chip.is-room .chip-status-wrap', css_content)
        self.assertIn('.md .md-link-chip.is-room .chip-spin-wheel', css_content)
        self.assertIn('.md .md-link-chip.is-room[data-status="running"]', css_content)
        self.assertIn('.md .md-link-chip.is-room[data-status="done"]', css_content)
        self.assertIn('.md .md-link-chip.is-room[data-status="failed"]', css_content)
        self.assertIn('@keyframes chip-spin', css_content)

    def test_api_chat_rooms_search_endpoint(self):
        """GET /api/chat/rooms/search 엔드포인트가 chat_db.search_rooms 결과를 반환하는지 검증."""
        import json as _json
        from aiohttp.test_utils import make_mocked_request

        fake_rooms = [{"id": "room-1", "title": "테스트", "matchSnippet": "…매칭 본문…"}]
        with unittest.mock.patch.object(web_server.chat_db, "search_rooms", return_value=fake_rooms) as mock_search:
            req = make_mocked_request("GET", "/api/chat/rooms/search?q=%EB%B3%B8%EB%AC%B8")
            resp = asyncio.run(web_server.handle_search_chat_rooms(req))
            self.assertEqual(resp.status, 200)
            body = _json.loads(resp.text)
            self.assertTrue(body["ok"])
            self.assertEqual(body["query"], "본문")
            self.assertEqual(body["rooms"], fake_rooms)
            mock_search.assert_called_once_with("본문", limit=100)


if __name__ == "__main__":
    unittest.main()
