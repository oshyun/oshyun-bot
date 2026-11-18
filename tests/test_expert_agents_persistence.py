"""전문가 에이전트 대화 내역 영구 보존(Persistence & Self-Healing) 단위 테스트."""
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop

import chat_db
import web_rooms
import web_orchestrator


class TestExpertAgentsPersistence(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_expert_persist_")
        self.db_path = os.path.join(self.test_dir, "test_chat.db")
        chat_db.init_db(self.db_path)

        self.hidden_file = os.path.join(self.test_dir, "hidden_chat_rooms.json")
        self.orig_hidden_file = web_rooms.HIDDEN_ROOMS_FILE
        web_rooms.HIDDEN_ROOMS_FILE = self.hidden_file
        chat_db.HIDDEN_ROOMS_FILE = self.hidden_file

        import sys
        mod = sys.modules.get("web_server")
        self.orig_ws_hidden_file = getattr(mod, "HIDDEN_ROOMS_FILE", None) if mod else None
        if mod and hasattr(mod, "HIDDEN_ROOMS_FILE"):
            mod.HIDDEN_ROOMS_FILE = self.hidden_file

        # 보호 대상 룸 초기화
        for rid, title in [
            (chat_db.CORE_ORCHESTRATOR_ROOM_ID, "마스터 에이전트"),
            (chat_db.EXPERT_VAULT_ROOM_ID, "세컨브레인 에이전트"),
            (chat_db.EXPERT_CODING_ROOM_ID, "봇개발 에이전트"),
        ]:
            chat_db.get_or_create_room(
                rid,
                session_id=f"sid_{rid}",
                title=title,
                channel="web",
                model="flash-test",
                effort="medium",
                db_path=self.db_path,
            )

    def tearDown(self):
        web_rooms.HIDDEN_ROOMS_FILE = self.orig_hidden_file
        chat_db.HIDDEN_ROOMS_FILE = self.orig_hidden_file
        import sys
        mod = sys.modules.get("web_server")
        if mod and self.orig_ws_hidden_file is not None:
            mod.HIDDEN_ROOMS_FILE = self.orig_ws_hidden_file
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_protected_rooms_cannot_be_deleted_from_chat_db(self):
        """chat_db 에서 마스터 및 전문가 룸의 단독/일괄 삭제가 원천 차단되는지 검증."""
        # 1. 단독 삭제 시도 -> 실패 반환 및 레코드 보존
        for rid in chat_db.PROTECTED_ROOM_IDS:
            res = chat_db.delete_room(rid, hard_delete=True, db_path=self.db_path)
            self.assertFalse(res, f"{rid}는 삭제되지 않아야 함")
            room = chat_db.get_room(rid, db_path=self.db_path)
            self.assertIsNotNone(room)

        # 2. 일반 방 생성 후 일괄 삭제 시도
        chat_db.get_or_create_room("temp_room_1", title="임시방", db_path=self.db_path)
        chat_db.get_or_create_room("temp_room_2", title="임시방2", db_path=self.db_path)

        targets = [
            chat_db.CORE_ORCHESTRATOR_ROOM_ID,
            chat_db.EXPERT_VAULT_ROOM_ID,
            chat_db.EXPERT_CODING_ROOM_ID,
            "temp_room_1",
            "temp_room_2",
        ]
        deleted_count = chat_db.delete_rooms_bulk(targets, hard_delete=True, db_path=self.db_path)
        # 일반 임시방 2개만 삭제되고 보호 대상 3개는 보존되어야 함
        self.assertEqual(deleted_count, 2)
        for rid in chat_db.PROTECTED_ROOM_IDS:
            self.assertIsNotNone(chat_db.get_room(rid, db_path=self.db_path))
        self.assertIsNone(chat_db.get_room("temp_room_1", db_path=self.db_path))
        self.assertIsNone(chat_db.get_room("temp_room_2", db_path=self.db_path))

    def test_clear_room_messages(self):
        """clear_room_messages 가 방 레코드와 설정을 보존하면서 메시지만 삭제하고 sessionId를 초기화하는지 검증."""
        rid = chat_db.EXPERT_VAULT_ROOM_ID
        chat_db.add_message(rid, "user", "볼트 메모 정리해줘", db_path=self.db_path)
        chat_db.add_message(rid, "assistant", "정리 완료했습니다.", db_path=self.db_path)

        msgs_before = chat_db.get_messages(rid, db_path=self.db_path)
        self.assertEqual(len(msgs_before), 2)

        # 메시지 비우기 실행
        cleared = chat_db.clear_room_messages(rid, db_path=self.db_path)
        self.assertEqual(cleared, 2)

        msgs_after = chat_db.get_messages(rid, db_path=self.db_path)
        self.assertEqual(len(msgs_after), 0)

        # 방 레코드 자체는 모델, 타이틀, 설정 온전히 유지
        room = chat_db.get_room(rid, db_path=self.db_path)
        self.assertIsNotNone(room)
        self.assertEqual(room.get("title"), "세컨브레인 에이전트")
        self.assertEqual(room.get("model"), "flash-test")
        self.assertIsNone(room.get("sessionId"))

    def test_hidden_rooms_self_healing_and_save_guard(self):
        """hidden_chat_rooms.json 에 전문가 룸이 있어도 _load_hidden_rooms 에서 self-healing 되고 _save_hidden_rooms 에서 원천 차단되는지 검증."""
        # 파일에 강제로 전문가 룸 오염 기록
        with open(self.hidden_file, "w", encoding="utf-8") as f:
            json.dump([
                chat_db.CORE_ORCHESTRATOR_ROOM_ID,
                chat_db.EXPERT_VAULT_ROOM_ID,
                chat_db.EXPERT_CODING_ROOM_ID,
                "real_hidden_room",
            ], f)

        # 1. 로드 시 self-healing 발동 검증
        hidden = web_rooms._load_hidden_rooms()
        self.assertNotIn(chat_db.CORE_ORCHESTRATOR_ROOM_ID, hidden)
        self.assertNotIn(chat_db.EXPERT_VAULT_ROOM_ID, hidden)
        self.assertNotIn(chat_db.EXPERT_CODING_ROOM_ID, hidden)
        self.assertIn("real_hidden_room", hidden)

        # 파일에서도 실제로 제거되었는지 확인
        with open(self.hidden_file, "r", encoding="utf-8") as f:
            saved_on_disk = json.load(f)
        self.assertEqual(saved_on_disk, ["real_hidden_room"])

        # 2. _save_hidden_rooms 에 보호 대상 전달 시 원천 차단 검증
        web_rooms._save_hidden_rooms({
            chat_db.EXPERT_VAULT_ROOM_ID,
            chat_db.EXPERT_CODING_ROOM_ID,
            "another_hidden_room",
        })
        with open(self.hidden_file, "r", encoding="utf-8") as f:
            saved_again = json.load(f)
        self.assertNotIn(chat_db.EXPERT_VAULT_ROOM_ID, saved_again)
        self.assertNotIn(chat_db.EXPERT_CODING_ROOM_ID, saved_again)
        self.assertIn("another_hidden_room", saved_again)

    def test_append_and_load_room_messages_for_expert_rooms(self):
        """전문가 룸에 메시지 추가 및 조회가 hidden_rooms 영향 없이 완벽히 동작하는지 검증."""
        rid = chat_db.EXPERT_CODING_ROOM_ID
        msg = {
            "id": 101,
            "sender": "user",
            "text": "단위 테스트 실행해줘",
            "createdAt": time.time(),
        }
        with patch.object(chat_db, "CHAT_DB_FILE", self.db_path), \
             patch("paths.CHAT_DB_FILE", self.db_path):
            web_rooms._append_room_message(rid, msg)
            loaded = web_rooms._load_room_messages(rid)

        self.assertGreaterEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["text"], "단위 테스트 실행해줘")


class TestExpertAgentsRestApi(AioHTTPTestCase):
    async def get_application(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_expert_api_")
        self.db_path = os.path.join(self.test_dir, "test_chat.db")
        chat_db.init_db(self.db_path)

        self.hidden_file = os.path.join(self.test_dir, "hidden_chat_rooms.json")
        self.orig_hidden_file = web_rooms.HIDDEN_ROOMS_FILE
        web_rooms.HIDDEN_ROOMS_FILE = self.hidden_file
        chat_db.HIDDEN_ROOMS_FILE = self.hidden_file

        import sys
        mod = sys.modules.get("web_server")
        self.orig_ws_hidden_file = getattr(mod, "HIDDEN_ROOMS_FILE", None) if mod else None
        if mod and hasattr(mod, "HIDDEN_ROOMS_FILE"):
            mod.HIDDEN_ROOMS_FILE = self.hidden_file

        for rid, title in [
            (chat_db.CORE_ORCHESTRATOR_ROOM_ID, "마스터 에이전트"),
            (chat_db.EXPERT_VAULT_ROOM_ID, "세컨브레인 에이전트"),
            (chat_db.EXPERT_CODING_ROOM_ID, "봇개발 에이전트"),
        ]:
            chat_db.get_or_create_room(
                rid,
                session_id=f"sid_{rid}",
                title=title,
                channel="web",
                model="flash-test",
                effort="medium",
                db_path=self.db_path,
            )

        app = web.Application()
        app.router.add_delete("/api/session", web_rooms.handle_api_delete_session)
        app.router.add_get("/api/chat/history", web_rooms.handle_chat_history)
        app.router.add_post("/api/orchestrator/reset", web_orchestrator.handle_orchestrator_reset)
        return app

    async def asyncTearDown(self):
        await super().asyncTearDown()
        web_rooms.HIDDEN_ROOMS_FILE = self.orig_hidden_file
        chat_db.HIDDEN_ROOMS_FILE = self.orig_hidden_file
        import sys
        mod = sys.modules.get("web_server")
        if mod and self.orig_ws_hidden_file is not None:
            mod.HIDDEN_ROOMS_FILE = self.orig_ws_hidden_file
        shutil.rmtree(self.test_dir, ignore_errors=True)

    @unittest_run_loop
    async def test_handle_api_delete_session_does_not_hide_expert_rooms(self):
        """DELETE /api/session?sessionId=__expert_vault__ 호출 시 hidden_chat_rooms 에 들어가지 않고 세션만 리셋되는지 검증."""
        import sys
        ws_mod = sys.modules.get("web_server")
        from unittest.mock import MagicMock
        mock_clear = MagicMock()
        patches = [
            patch.object(chat_db, "CHAT_DB_FILE", self.db_path),
            patch("paths.CHAT_DB_FILE", self.db_path),
            patch("web_rooms.clear_session", mock_clear),
        ]
        if ws_mod and hasattr(ws_mod, "clear_session"):
            patches.append(patch.object(ws_mod, "clear_session", mock_clear))

        for p in patches:
            p.start()
        try:
            resp = await self.client.delete(f"/api/session?sessionId={chat_db.EXPERT_VAULT_ROOM_ID}")
            self.assertEqual(resp.status, 200)
            data = await resp.json()
            self.assertTrue(data.get("ok"))

            # vault_ 네임스페이스 세션 클리어 호출 확인
            mock_clear.assert_called_with(ns="vault_")

            # hidden_chat_rooms.json 에 등록되지 않았는지 검증
            hidden = web_rooms._load_hidden_rooms()
            self.assertNotIn(chat_db.EXPERT_VAULT_ROOM_ID, hidden)

            # DB 에서 방이 삭제되지 않고 남아있는지 검증
            room = chat_db.get_room(chat_db.EXPERT_VAULT_ROOM_ID, db_path=self.db_path)
            self.assertIsNotNone(room)
        finally:
            for p in reversed(patches):
                p.stop()

    @unittest_run_loop
    async def test_handle_orchestrator_reset_expert_room(self):
        """POST /api/orchestrator/reset 으로 전문가 룸 초기화 시 해당 방만 리셋되고 메시지가 비워지는지 검증."""
        rid = chat_db.EXPERT_CODING_ROOM_ID
        chat_db.add_message(rid, "user", "코드 리팩토링해줘", db_path=self.db_path)
        chat_db.add_message(rid, "assistant", "리팩토링 완료", db_path=self.db_path)

        import sys
        ws_mod = sys.modules.get("web_server")
        from unittest.mock import MagicMock
        mock_clear = MagicMock()
        patches = [
            patch.object(chat_db, "CHAT_DB_FILE", self.db_path),
            patch("paths.CHAT_DB_FILE", self.db_path),
            patch("web_orchestrator.clear_session", mock_clear),
            patch("web_execution.broadcast_ws_json", new_callable=AsyncMock),
        ]
        if ws_mod and hasattr(ws_mod, "clear_session"):
            patches.append(patch.object(ws_mod, "clear_session", mock_clear))

        for p in patches:
            p.start()
        try:
            resp = await self.client.post("/api/orchestrator/reset", json={
                "roomId": rid,
                "agentId": "coding",
                "memoryMode": "keep",
            })
            self.assertEqual(resp.status, 200)
            data = await resp.json()
            self.assertTrue(data.get("ok"))
            self.assertEqual(data.get("roomId"), rid)

            mock_clear.assert_called_with(ns="coding_")

            # 메시지는 비워졌는지 확인
            msgs = chat_db.get_messages(rid, db_path=self.db_path)
            self.assertEqual(len(msgs), 0)

            # 방은 여전히 존재하는지 확인
            room = chat_db.get_room(rid, db_path=self.db_path)
            self.assertIsNotNone(room)
        finally:
            for p in reversed(patches):
                p.stop()


if __name__ == "__main__":
    unittest.main()
