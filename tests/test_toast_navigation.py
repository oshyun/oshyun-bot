"""test_toast_navigation.py — 작업 토스트 클릭 시 해당 대화방 이동 및 닫기 버튼 분리 검증."""

import os
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATIC_DIR = os.path.join(_REPO_ROOT, "src", "bot", "static")


class TestToastRoomClickNavigation(unittest.TestCase):
    """작업 알림 토스트 클릭 시 대상 대화방 이동 및 UX/이벤트 격리 검증."""

    def setUp(self):
        self.css_path = os.path.join(_STATIC_DIR, "css", "views-modals.css")
        self.sync_path = os.path.join(_STATIC_DIR, "js", "rooms-sync.js")
        self.ws_path = os.path.join(_STATIC_DIR, "js", "ws.js")
        self.memory_path = os.path.join(_STATIC_DIR, "js", "master-memory.js")
        self.html_path = os.path.join(_STATIC_DIR, "index.html")

        with open(self.css_path, "r", encoding="utf-8") as f:
            self.css = f.read()
        with open(self.sync_path, "r", encoding="utf-8") as f:
            self.sync_js = f.read()
        with open(self.ws_path, "r", encoding="utf-8") as f:
            self.ws_js = f.read()
        with open(self.memory_path, "r", encoding="utf-8") as f:
            self.memory_js = f.read()
        with open(self.html_path, "r", encoding="utf-8") as f:
            self.html = f.read()

    def test_css_toast_clickable_and_hover_styles(self):
        """토스트 표시 시 pointer-events 활성화 및 클릭 가능 토스트 호버 스타일 존재 검증."""
        self.assertIn("#toast.show", self.css)
        self.assertIn("pointer-events: auto", self.css)
        self.assertIn("#toast.clickable", self.css)
        self.assertIn("cursor: pointer", self.css)
        self.assertIn("#toast.clickable:hover", self.css)
        self.assertIn(".toast-action-badge", self.css)
        self.assertIn(".toast-close-btn", self.css)

    def test_rooms_sync_toast_implementation(self):
        """showToast 확장 구현(targetRoomId/taskId 매핑, openRoom/selectRoom 연동, stopPropagation 분리)."""
        self.assertIn("function showToast(msg, options = {})", self.sync_js)
        self.assertIn("findRoomIdByTaskId", self.sync_js)
        self.assertIn("window.openRoom", self.sync_js)
        self.assertIn("toast.classList.add('clickable')", self.sync_js)
        self.assertIn("selectRoom(targetRoomId)", self.sync_js)
        self.assertIn("openRoom(targetRoomId)", self.sync_js)
        # 닫기 버튼 stopPropagation 확인
        self.assertIn("e.stopPropagation()", self.sync_js)
        self.assertIn("hideToast()", self.sync_js)

    def test_ws_events_pass_room_id_to_toast(self):
        """웹소켓 하위 세션 생성, 시작, 답변 도착, 실행 오류, 스케줄러 공지에서 roomId 전달 검증."""
        # 세션 생성 (byOrchestrator)
        self.assertIn("showToast(`${creatorLabel}가 새 세션", self.ws_js)
        self.assertIn("roomId: sRoom.id || rId", self.ws_js)

        # 하위 작업 시작 (started)
        self.assertIn("작업이 시작되었습니다.", self.ws_js)
        self.assertIn("roomId: targetRoom.id", self.ws_js)

        # 답변 도착 (result)
        self.assertIn("답변이 도착했습니다.", self.ws_js)

        # 실행 오류 (error)
        self.assertIn("실행 오류가 발생했습니다.", self.ws_js)

        # 스케줄러 알림 (scheduler_notice)
        self.assertIn("scheduler_notice", self.ws_js)

    def test_master_memory_rebalance_passes_room_id(self):
        """메모리 리밸런싱 시작 시 하위 세션 roomId 전달 검증."""
        self.assertIn("메모리 리밸런싱을 시작했습니다.", self.memory_js)
        self.assertIn("roomId: data.roomId || parentRoomId", self.memory_js)


if __name__ == "__main__":
    unittest.main()
