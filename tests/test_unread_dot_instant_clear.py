"""tests/test_unread_dot_instant_clear.py — 확인 필요(unread/needs-review) 파란 점 즉시 해제 및 부활 방어 단위 테스트."""

import os
import unittest
from unittest.mock import patch, MagicMock
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

import bot.master.master_tasks as master_tasks
from bot.web.orchestrator import handle_orchestrator_room_mark_read, handle_orchestrator_task_update

STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "src", "bot", "static")


def read_static_bundle(static_dir: str) -> str:
    parts = []
    index_file = os.path.join(static_dir, "index.html")
    if os.path.isfile(index_file):
        with open(index_file, "r", encoding="utf-8") as f:
            parts.append(f.read())
    for sub in ("css", "js"):
        dir_path = os.path.join(static_dir, sub)
        if os.path.isdir(dir_path):
            for fname in sorted(os.listdir(dir_path)):
                if fname.endswith((".css", ".js")):
                    with open(os.path.join(dir_path, fname), "r", encoding="utf-8") as f:
                        parts.append(f.read())
    return "\n".join(parts)


class TestMasterTasksMarkRoomReviewed(unittest.TestCase):
    def setUp(self):
        self._orig_load = master_tasks._load
        self._orig_save = master_tasks._save
        self.fake_db = {
            "tasks": {
                "t1": {"id": "t1", "roomId": "room-target", "status": "done", "reviewed": False},
                "t2": {"id": "t2", "roomId": "room-target", "status": "failed", "reviewed": False},
                "t3": {"id": "t3", "roomId": "room-target", "status": "done", "reviewed": True},
                "t4": {"id": "t4", "roomId": "room-other", "status": "done", "reviewed": False},
            },
            "room_summaries": {}
        }
        master_tasks._load = lambda: self.fake_db
        master_tasks._save = lambda d: None

    def tearDown(self):
        master_tasks._load = self._orig_load
        master_tasks._save = self._orig_save

    def test_mark_room_reviewed_success(self):
        """특정 roomId 의 미검토 태스크들만 reviewed=True 처리되고 count 가 정확한지 검증."""
        marked = master_tasks.mark_room_reviewed("room-target")
        self.assertEqual(marked, 2)
        self.assertTrue(self.fake_db["tasks"]["t1"]["reviewed"])
        self.assertIsNotNone(self.fake_db["tasks"]["t1"]["reviewedAt"])
        self.assertTrue(self.fake_db["tasks"]["t2"]["reviewed"])
        self.assertFalse(self.fake_db["tasks"]["t4"]["reviewed"])

    def test_mark_room_reviewed_empty_or_none(self):
        """유효하지 않은 roomId 일 때 0 반환."""
        self.assertEqual(master_tasks.mark_room_reviewed(""), 0)
        self.assertEqual(master_tasks.mark_room_reviewed(None), 0)
        self.assertEqual(master_tasks.mark_room_reviewed("room-nonexistent"), 0)


class TestOrchestratorRoomMarkReadApi(AioHTTPTestCase):
    async def get_application(self):
        app = web.Application()
        app.router.add_post("/api/orchestrator/room/mark-read", handle_orchestrator_room_mark_read)
        app.router.add_post("/api/orchestrator/task/update", handle_orchestrator_task_update)
        return app

    async def test_room_mark_read_endpoint_success(self):
        """POST /api/orchestrator/room/mark-read 정상 호출 시 marked 개수와 200 반환 검증."""
        with patch("bot.master.master_tasks.mark_room_reviewed", return_value=3) as mock_mark:
            resp = await self.client.post("/api/orchestrator/room/mark-read", json={"roomId": "room-xyz"})
            self.assertEqual(resp.status, 200)
            data = await resp.json()
            self.assertTrue(data.get("ok"))
            self.assertEqual(data.get("roomId"), "room-xyz")
            self.assertEqual(data.get("marked"), 3)
            mock_mark.assert_called_once_with("room-xyz")

    async def test_room_mark_read_endpoint_missing_room_id(self):
        """roomId 가 비어있을 때 400 반환."""
        resp = await self.client.post("/api/orchestrator/room/mark-read", json={})
        self.assertEqual(resp.status, 400)
        data = await resp.json()
        self.assertFalse(data.get("ok"))
        self.assertIn("roomId 가 필요합니다", data.get("error"))

    async def test_task_update_endpoint_fallback_room_id(self):
        """taskId 없이 roomId 만 전달되었을 때도 mark_room_reviewed 로 정상 처리되는지 검증."""
        with patch("bot.master.master_tasks.mark_room_reviewed", return_value=1) as mock_mark:
            resp = await self.client.post("/api/orchestrator/task/update", json={"roomId": "room-fallback"})
            self.assertEqual(resp.status, 200)
            data = await resp.json()
            self.assertTrue(data.get("ok"))
            self.assertEqual(data.get("roomId"), "room-fallback")
            self.assertEqual(data.get("marked"), 1)
            mock_mark.assert_called_once_with("room-fallback")


class TestStaticBundleUnreadOptimisticAndResurrectionDefense(unittest.TestCase):
    def setUp(self):
        self.bundle = read_static_bundle(STATIC_DIR)

    def test_optimistic_ui_and_server_api_call_on_select_room(self):
        """selectRoom 에서 낙관적 unread 해제 및 서버 방 읽음 처리 API 비동기 호출 포함 검증."""
        self.assertIn("targetRoom.unread = false;", self.bundle)
        self.assertIn("t.reviewed = true;", self.bundle)
        self.assertIn("fetch('/api/orchestrator/room/mark-read'", self.bundle)

    def test_current_room_unread_defense_in_tree_and_list(self):
        """현재 보고 있는 방(!isCur)은 파란 점이 렌더링되지 않도록 방어하는 로직 검증."""
        self.assertIn("const isCur = (r.id === currentRoomId);", self.bundle)
        self.assertIn("const isUnread = !isCur && Boolean(r.unread || hasPendingTask);", self.bundle)

    def test_resurrection_defense_in_polling_and_ws(self):
        """태스크 폴링(loadMasterTasksFromServer) 및 방 동기화 시 현재 방의 unread/reviewed 상태 보존 검증."""
        self.assertIn("if (currentRoomId) {", self.bundle)
        self.assertIn("if (t && t.roomId === currentRoomId) {", self.bundle)
        self.assertIn("if (curRoomObj && curRoomObj.unread) {", self.bundle)
        self.assertIn("curRoomObj.unread = false;", self.bundle)

    def test_running_indicator_and_unread_dot_coexistence(self):
        """실행 중(isRunning) 표시와 확인 필요(isUnread) 파란 점이 분리되어 공존 및 우선순위 검증."""
        self.assertIn(".run-spinner", self.bundle)
        self.assertIn(".run-label", self.bundle)
        self.assertIn(".sb-tree-dot", self.bundle)
        self.assertIn("isRunning ? '<span class=\"run-label\">실행 중</span><span class=\"run-spinner\"", self.bundle)


if __name__ == "__main__":
    unittest.main()
