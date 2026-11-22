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

    def test_thread_backdrop_no_blur(self):
        """스레드 백드롭이 마스터 대화창을 가리거나 블러 처리하지 않고 투명하게 유지되는지 검증."""
        css_path = os.path.join(self.static_dir, "css", "master-inspector.css")
        with open(css_path, "r", encoding="utf-8") as f:
            css = f.read()

        self.assertIn(".thread-drawer-backdrop", css)
        self.assertIn("backdrop-filter: none !important", css)
        self.assertIn("background: transparent", css)

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

    async def test_notify_master_agent_bypasses_direct_assistant_message(self):
        """_notify_master_agent 가 하위 작업 결과를 부모 방에 assistant 바이패스 메시지로 직접 영속화하는지 검증."""
        broadcast_mock = AsyncMock()
        with patch.object(chat_db, "CHAT_DB_FILE", self.db_path), \
             patch.object(web_execution, "chat_db", chat_db), \
             patch.object(web_execution, "broadcast_ws_json", broadcast_mock), \
             patch.object(web_execution.master_tasks, "claim_report", return_value=True), \
             patch.object(web_execution.master_memory, "add_memory"):
            await web_execution._notify_master_agent(
                room_id="room-subtask-123",
                task_id="task-sub-1",
                result_text="최종 바이패스 응답 본문입니다.",
                is_success=True,
                model="gemini-3.8-flash",
                effort="low",
            )

        # __core__ 방 메시지 확인
        msgs = chat_db.get_messages("__core__", db_path=self.db_path)
        asst_msgs = [m for m in msgs if m["role"] == "assistant"]
        self.assertEqual(len(asst_msgs), 1)
        self.assertEqual(asst_msgs[0]["text"], "최종 바이패스 응답 본문입니다.")
        self.assertEqual(asst_msgs[0]["channel"], "thread_bypass")
        self.assertEqual(asst_msgs[0]["model"], "gemini-3.8-flash")

        # steps 검증
        steps = asst_msgs[0].get("steps", [])
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].get("kind"), "subagent_bypass")
        self.assertEqual(steps[0].get("roomId"), "room-subtask-123")
        self.assertEqual(steps[0].get("status"), "done")

        # 웹소켓 브로드캐스트 검증
        broadcast_mock.assert_awaited_once()
        b_payload = broadcast_mock.call_args[0][0]
        self.assertEqual(b_payload["type"], "result")
        self.assertEqual(b_payload["roomId"], "__core__")
        self.assertEqual(b_payload["text"], "최종 바이패스 응답 본문입니다.")
        self.assertEqual(b_payload["bypassedFromRoomId"], "room-subtask-123")

    def test_thread_panel_autofocus_and_bypass_in_frontend_js(self):
        """rooms-ui.js 및 messages.js에 모바일 자동 포커스 방지 및 subagent_bypass 추출이 정확히 구현되었는지 검증."""
        base_dir = os.path.dirname(os.path.dirname(__file__))
        rooms_ui_js = os.path.join(base_dir, "src", "bot", "static", "js", "rooms-ui.js")
        with open(rooms_ui_js, "r", encoding="utf-8") as f:
            rooms_ui_content = f.read()

        # openThreadPanel 에서 모바일 설정 검사 가드 확인
        self.assertIn("!isTouchOrMobileDevice() || (typeof getMobileAutofocus === 'function' && getMobileAutofocus())", rooms_ui_content)

        messages_js = os.path.join(base_dir, "src", "bot", "static", "js", "messages.js")
        with open(messages_js, "r", encoding="utf-8") as f:
            messages_content = f.read()

        # extractDelegatedRoomsFromSteps 에서 subagent_bypass 지원 확인
        self.assertIn("s.kind === 'subagent_bypass'", messages_content)
        self.assertIn("bypassStep.agentName", messages_content)

    def test_thread_panel_layout_and_emoji_cleanliness(self):
        """스레드 패널에서 🧵 이모지가 제거되었고, 수신된 지시 프롬프트가 실행 단계보다 최상단에 우선 배치되는지 검증."""
        base_dir = os.path.dirname(os.path.dirname(__file__))
        index_html_path = os.path.join(base_dir, "src", "bot", "static", "index.html")
        with open(index_html_path, "r", encoding="utf-8") as f:
            index_html = f.read()

        # index.html 에 🧵 이모지 제거 확인
        self.assertNotIn("🧵", index_html)

        rooms_ui_js = os.path.join(base_dir, "src", "bot", "static", "js", "rooms-ui.js")
        with open(rooms_ui_js, "r", encoding="utf-8") as f:
            rooms_ui_content = f.read()

        # rooms-ui.js 에 🧵 이모지 제거 확인
        self.assertNotIn("🧵", rooms_ui_content)

        # 지시 프롬프트가 steps 및 나머지 메시지보다 먼저 push 되는지 순서 검증
        prompt_card_pos = rooms_ui_content.find("thread-prompt-card")
        steps_pos = rooms_ui_content.find("thread-steps-section")
        msgs_pos = rooms_ui_content.find("thread-messages-list")

        self.assertGreater(prompt_card_pos, 0, "thread-prompt-card 가 존재해야 함")
        self.assertGreater(steps_pos, 0, "thread-steps-section 이 존재해야 함")
        self.assertGreater(msgs_pos, 0, "thread-messages-list 가 존재해야 함")
        self.assertLess(prompt_card_pos, steps_pos, "지시 프롬프트가 실행 단계보다 상단에 위치해야 함")
        self.assertLess(steps_pos, msgs_pos, "실행 단계가 하위 메시지 목록보다 상단에 위치해야 함")

    def test_thread_panel_open_resilience_and_inline_button(self):
        """완료된 하위 작업에서도 패널이 즉시 열리고, steps 복원 및 인라인 스레드 버튼이 존재하는지 검증."""
        base_dir = os.path.dirname(os.path.dirname(__file__))
        rooms_ui_js = os.path.join(base_dir, "src", "bot", "static", "js", "rooms-ui.js")
        with open(rooms_ui_js, "r", encoding="utf-8") as f:
            rooms_ui_content = f.read()

        # openThreadPanel 내부에서 패널이 비동기 fetch 전에 즉시 열리는지 확인
        open_fn_pos = rooms_ui_content.find("function openThreadPanel")
        self.assertGreater(open_fn_pos, 0)
        open_panel_pos = rooms_ui_content.find("panel.classList.add('open')", open_fn_pos)
        fetch_history_pos = rooms_ui_content.find("loadChatHistoryFromServer", open_fn_pos)
        self.assertGreater(open_panel_pos, 0)
        self.assertGreater(fetch_history_pos, 0)
        self.assertLess(open_panel_pos, fetch_history_pos, "패널 open 이 fetch 보다 먼저 실행되어야 함")

        # 완료 상태 steps 복원 확인
        self.assertIn("rawSteps = m.steps", rooms_ui_content)

        # 마스터 방에서 좌측 트리 클릭 시 openThreadPanel 연동 확인
        self.assertIn("openThreadPanel(null, roomId", rooms_ui_content)

        messages_js = os.path.join(base_dir, "src", "bot", "static", "js", "messages.js")
        with open(messages_js, "r", encoding="utf-8") as f:
            messages_content = f.read()

        # 바이패스 메시지 헤더 인라인 스레드 열기 버튼 확인
        self.assertIn("btn-open-thread-inline", messages_content)
        self.assertIn("스레드 열기", messages_content)
