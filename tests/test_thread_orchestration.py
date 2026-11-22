"""tests/test_thread_orchestration.py — 마스터 에이전트 스레드 오케스트레이션 패널 및 실시간 동기화 단위 테스트."""

from __future__ import annotations

import os
import sys
import tempfile
import shutil
import unittest
from unittest.mock import AsyncMock, patch

# src/bot 경로 추가
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "bot"))

import chat_db
import web_execution


class TestThreadOrchestration(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_thread_orch_")
        self.db_path = os.path.join(self.test_dir, "test_chat.db")
        chat_db.init_db(self.db_path)

        # 루트 경로 및 static 파일 경로 확인
        self.repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        self.static_dir = os.path.join(self.repo_root, "src", "bot", "static")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_thread_slide_panel_dom_integrity(self):
        """index.html 에 스레드 오케스트레이션 슬라이드 패널 및 필수 하위 요소들이 완전한지 검증."""
        index_path = os.path.join(self.static_dir, "index.html")
        self.assertTrue(os.path.isfile(index_path), "index.html 파일이 존재해야 함")

        with open(index_path, "r", encoding="utf-8") as f:
            html = f.read()

        # 스레드 패널 컨테이너
        self.assertIn('id="thread-slide-panel"', html)
        self.assertIn('class="thread-sidebar"', html)

        # 헤더 및 닫기 버튼
        self.assertIn('id="thread-panel-title"', html)
        self.assertIn('id="thread-panel-status"', html)
        self.assertIn('onclick="closeThreadPanel()"', html)

        # 본문 및 푸터
        self.assertIn('id="thread-panel-body"', html)
        self.assertIn('id="thread-panel-footer"', html)
        self.assertIn('id="thread-user-input"', html)
        self.assertIn('id="btn-thread-send"', html)
        self.assertIn('onclick="sendThreadMessage()"', html)

        # 백드롭
        self.assertIn('id="thread-drawer-backdrop"', html)

    def test_thread_slide_panel_css_tokens(self):
        """master-inspector.css 에 스레드 패널 레이아웃 및 상태 뱃지, 마크다운 스타일이 선언되었는지 검증."""
        css_path = os.path.join(self.static_dir, "css", "master-inspector.css")
        self.assertTrue(os.path.isfile(css_path), "master-inspector.css 파일이 존재해야 함")

        with open(css_path, "r", encoding="utf-8") as f:
            css = f.read()

        self.assertIn(".thread-sidebar", css)
        self.assertIn(".thread-sidebar.open", css)
        self.assertIn(".thread-status-badge", css)
        self.assertIn(".thread-status-badge.running", css)
        self.assertIn(".thread-status-badge.done", css)
        self.assertIn(".thread-steps-section", css)
        self.assertIn(".thread-msg-bubble", css)
        self.assertIn(".thread-msg-bubble.markdown-body", css)

    def test_thread_js_functions_defined(self):
        """rooms-ui.js 에 스레드 패널 제어 함수들이 올바르게 정의되어 있는지 검증."""
        js_path = os.path.join(self.static_dir, "js", "rooms-ui.js")
        with open(js_path, "r", encoding="utf-8") as f:
            js = f.read()

        self.assertIn("function openThreadPanel(", js)
        self.assertIn("function closeThreadPanel(", js)
        self.assertIn("function renderThreadPanelBody(", js)
        self.assertIn("async function sendThreadMessage(", js)
        self.assertIn("function handleThreadInputKeyDown(", js)

    def test_thread_escape_key_binding(self):
        """init.js 의 Escape 키 핸들러에 closeThreadPanel 연동이 등록되어 있는지 검증."""
        init_js_path = os.path.join(self.static_dir, "js", "init.js")
        with open(init_js_path, "r", encoding="utf-8") as f:
            js = f.read()

        self.assertIn("closeThreadPanel", js)

    def test_thread_ws_realtime_events_integration(self):
        """ws.js 에서 하위 작업 실시간 이벤트(agent_step, stream_delta, result 등) 수신 시 스레드 패널 갱신이 호출되는지 검증."""
        ws_js_path = os.path.join(self.static_dir, "js", "ws.js")
        with open(ws_js_path, "r", encoding="utf-8") as f:
            js = f.read()

        self.assertIn("isThreadRoom", js)
        self.assertIn("renderThreadPanelBody()", js)


class TestThreadBackendExecution(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_thread_exec_")
        self.db_path = os.path.join(self.test_dir, "test_chat.db")
        chat_db.init_db(self.db_path)

        # 상위 마스터 및 하위 방 생성
        chat_db.get_or_create_room(
            "__core__",
            title="마스터 에이전트",
            channel="web",
            db_path=self.db_path
        )
        chat_db.get_or_create_room(
            "room-subtask-123",
            title="하위 데이터 분석 작업",
            channel="orchestrator",
            parent_agent_id="__core__",
            db_path=self.db_path
        )

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    async def test_thread_completion_hook_reports_to_parent(self):
        """스레드 패널에서 channel='orchestrator'로 실행된 작업이 완료되면 부모에게 완료 보고가 트리거되는지 검증."""
        notify_mock = AsyncMock()

        with patch.object(web_execution, "_notify_master_agent", notify_mock), \
             patch.object(web_execution, "chat_db", chat_db):
            await web_execution._finalize_orchestrator_completion(
                room_id="room-subtask-123",
                task_id="task-sub-1",
                channel="orchestrator",
                is_orchestrator=False,
                is_success=True,
                result_text="데이터 분석 결과 완료되었습니다.",
                status="done",
                model="gemini-3.8-flash",
                effort="low",
            )

            notify_mock.assert_awaited_once_with(
                "room-subtask-123",
                "task-sub-1",
                "데이터 분석 결과 완료되었습니다.",
                is_success=True
            )

    def test_room_parent_preservation_in_chat_db(self):
        """하위 방의 parentAgentId와 isOrchestratorChild 속성이 chat_db 에 정확히 보존되는지 검증."""
        room = chat_db.get_room("room-subtask-123", db_path=self.db_path)
        self.assertIsNotNone(room)
        self.assertEqual(room.get("parentAgentId"), "__core__")
        self.assertTrue(room.get("isOrchestratorChild"))
