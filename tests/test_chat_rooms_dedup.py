import os
import sys
import unittest
from unittest.mock import patch

# 프로젝트 루트 경로 추가
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_BOT = os.path.join(REPO_ROOT, "src")
if SRC_BOT not in sys.path:
    sys.path.insert(0, SRC_BOT)

import bot.web.server as web_server


class TestChatRoomsDeduplication(unittest.TestCase):
    """대화방 목록 중복 제거 및 단일 방 병합 테스트."""

    def test_deduplicate_same_id_different_session_id(self):
        """동일한 id(roomId)를 가지고 sessionId가 각각 UUID와 roomId인 경우 1개로 병합된다."""
        raw_rooms = [
            {
                "id": "room-1794227759421",
                "roomId": "room-1794227759421",
                "sessionId": "7ec3c12e-b482-4fd4-9960-3719b5c1f9f2",
                "title": "대기열 1건 관련 UI 가 지저분해 왜 위쪽",
                "preview": "답변 프리뷰 내용입니다.",
                "messageCount": 3,
                "updatedAt": 1794244792.0,
            },
            {
                "id": "room-1794227759421",
                "roomId": "room-1794227759421",
                "sessionId": "room-1794227759421",
                "title": "대기열 1건 관련 UI 가 지저분해 왜 위쪽",
                "preview": "짧은 프리뷰",
                "messageCount": 2,
                "updatedAt": 1794244700.0,
            },
        ]

        with patch("bot.web.server._load_hidden_rooms", return_value=set()), \
             patch("bot.web.server.chat_db.get_rooms", return_value=[]), \
             patch("os.path.isfile", return_value=False), \
             patch("os.path.isdir", return_value=False):
            # _load_chat_rooms_index는 내부에서 raw_rooms를 취합하므로
            # raw_rooms를 직접 주입하는 모의 함수로 검증하거나 내부 함수 테스트
            pass

        # _load_chat_rooms_index의 핵심 그룹화 로직을 직접 테스트하기 위해
        # chat_db.get_rooms가 raw_rooms를 반환하도록 모의
        with patch("bot.web.server._load_hidden_rooms", return_value=set()), \
             patch("bot.web.server.chat_db.get_rooms", return_value=raw_rooms), \
             patch("os.path.isfile", return_value=False), \
             patch("os.path.isdir", return_value=False):
            result = web_server._load_chat_rooms_index()

        self.assertEqual(len(result), 1)
        merged = result[0]
        self.assertEqual(merged["id"], "room-1794227759421")
        self.assertEqual(merged["sessionId"], "7ec3c12e-b482-4fd4-9960-3719b5c1f9f2")
        self.assertEqual(merged["title"], "대기열 1건 관련 UI 가 지저분해 왜 위쪽")
        self.assertEqual(merged["messageCount"], 3)
        self.assertEqual(merged["preview"], "답변 프리뷰 내용입니다.")
        self.assertEqual(merged["updatedAt"], 1794244792.0)

    def test_deduplicate_cross_id_and_session_id(self):
        """한 레코드의 id가 다른 레코드의 sessionId와 동일할 때 1개로 병합되며 room- 접두사 id가 우선된다."""
        raw_rooms = [
            {
                "id": "7ec3c12e-b482-4fd4-9960-3719b5c1f9f2",
                "roomId": "7ec3c12e-b482-4fd4-9960-3719b5c1f9f2",
                "sessionId": "7ec3c12e-b482-4fd4-9960-3719b5c1f9f2",
                "title": "새 대화",
                "preview": "",
                "messageCount": 1,
                "updatedAt": 1794244000.0,
            },
            {
                "id": "room-1794227759421",
                "roomId": "room-1794227759421",
                "sessionId": "7ec3c12e-b482-4fd4-9960-3719b5c1f9f2",
                "title": "실제 질문 제목",
                "preview": "에이전트 답변",
                "messageCount": 2,
                "updatedAt": 1794244500.0,
            },
        ]

        with patch("bot.web.server._load_hidden_rooms", return_value=set()), \
             patch("bot.web.server.chat_db.get_rooms", return_value=raw_rooms), \
             patch("os.path.isfile", return_value=False), \
             patch("os.path.isdir", return_value=False):
            result = web_server._load_chat_rooms_index()

        self.assertEqual(len(result), 1)
        merged = result[0]
        self.assertEqual(merged["id"], "room-1794227759421")
        self.assertEqual(merged["sessionId"], "7ec3c12e-b482-4fd4-9960-3719b5c1f9f2")
        self.assertEqual(merged["title"], "실제 질문 제목")
        self.assertEqual(merged["messageCount"], 2)

    def test_deduplicate_core_orchestrator(self):
        """__core__ 방과 session_id가 분리되어 들어와도 항상 __core__ 단 1개로 유지된다."""
        raw_rooms = [
            {
                "id": "__core__",
                "roomId": "__core__",
                "sessionId": "core-session-uuid-1234",
                "title": "마스터 에이전트",
                "preview": "마스터 응답",
                "messageCount": 5,
                "updatedAt": 1794244000.0,
            },
            {
                "id": "core-session-uuid-1234",
                "roomId": "core-session-uuid-1234",
                "sessionId": "core-session-uuid-1234",
                "title": "새 대화",
                "preview": "",
                "messageCount": 1,
                "updatedAt": 1794243000.0,
            },
        ]

        with patch("bot.web.server._load_hidden_rooms", return_value=set()), \
             patch("bot.web.server.chat_db.get_rooms", return_value=raw_rooms), \
             patch("os.path.isfile", return_value=False), \
             patch("os.path.isdir", return_value=False):
            result = web_server._load_chat_rooms_index()

        self.assertEqual(len(result), 1)
        merged = result[0]
        self.assertEqual(merged["id"], "__core__")
        self.assertEqual(merged["title"], web_server.CORE_ORCHESTRATOR_TITLE)
        self.assertEqual(merged["sessionId"], "core-session-uuid-1234")
        self.assertEqual(merged["messageCount"], 5)
        self.assertEqual(merged["workdir"], web_server.WORKDIR)

    def test_room_workdir_resolution(self):
        """방 목록 로드 시 workdir가 없는 방은 기본 workdir가 부여되고, 커스텀 workdir는 유지된다."""
        raw_rooms = [
            {
                "id": "room-custom",
                "roomId": "room-custom",
                "sessionId": "sess-custom",
                "title": "커스텀 디렉토리 방",
                "preview": "내용",
                "messageCount": 1,
                "workdir": "/custom/path/repo",
                "updatedAt": 1794244000.0,
            },
            {
                "id": "room-default",
                "roomId": "room-default",
                "sessionId": "sess-default",
                "title": "기본 디렉토리 방",
                "preview": "내용",
                "messageCount": 1,
                "workdir": None,
                "updatedAt": 1794243000.0,
            },
        ]

        with patch("bot.web.server._load_hidden_rooms", return_value=set()), \
             patch("bot.web.server.chat_db.get_rooms", return_value=raw_rooms), \
             patch("os.path.isfile", return_value=False), \
             patch("os.path.isdir", return_value=False):
            result = web_server._load_chat_rooms_index()

        self.assertEqual(len(result), 2)
        custom_room = next(r for r in result if r["id"] == "room-custom")
        default_room = next(r for r in result if r["id"] == "room-default")

        self.assertEqual(custom_room["workdir"], "/custom/path/repo")
        self.assertIsNotNone(default_room["workdir"])
        self.assertEqual(default_room["workdir"], web_server.WORKDIR)


if __name__ == "__main__":
    unittest.main()
