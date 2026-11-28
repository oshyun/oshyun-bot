"""메시지 0건인 빈 방(empty room)이 하위 세션 스폰 유사도 매칭에서 배제되는지 검증."""
import os
import shutil
import tempfile
import unittest

import bot.core.chat_db as chat_db
from bot.web.orchestrator import _find_similar_orchestrator_room


class TestEmptyRoomSimilarityFilter(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_empty_room_")
        self.db_path = os.path.join(self.test_dir, "test_chat.db")
        chat_db.init_db(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_empty_room_excluded_from_similarity_matching(self):
        """메시지 수가 0건인 방은 유사도가 1.0(완전 일치)이어도 재사용 후보에서 제외되어야 함."""
        # 1. 메시지 0건인 빈 방 생성
        empty_room = {
            "id": "room-empty-1",
            "title": "화장품 전문가 하위 채팅 미생성 버그 분석 및 수정",
            "preview": "화장품 전문가 에이전트의 하위 세션 스폰 버그 원인 분석 및 수정",
            "messageCount": 0,
            "messages": [],
            "parentAgentId": "__expert_coding__",
            "isArchived": False,
        }

        # 2. 메시지 2건인 정상 방 생성
        valid_room = {
            "id": "room-valid-1",
            "title": "화장품 전문가 하위 채팅 미생성 버그 분석 및 수정",
            "preview": "화장품 전문가 에이전트의 하위 세션 스폰 버그 원인 분석 및 수정",
            "messageCount": 2,
            "messages": [{"id": 1, "text": "시작"}, {"id": 2, "text": "완료"}],
            "parentAgentId": "__expert_coding__",
            "isArchived": False,
        }

        # Case A: 빈 방만 존재할 경우 매칭되지 않아야 함 (None 반환)
        matched_empty, score_empty = _find_similar_orchestrator_room(
            "화장품 전문가 하위 채팅 미생성 버그 분석 및 수정",
            "화장품 전문가 에이전트의 하위 세션 스폰 버그 원인 분석 및 수정",
            rooms=[empty_room],
            parent_agent_id="__expert_coding__",
        )
        self.assertIsNone(matched_empty)
        self.assertEqual(score_empty, 0.0)

        # Case B: 정상 방이 존재할 경우 해당 방이 매칭되어야 함
        matched_valid, score_valid = _find_similar_orchestrator_room(
            "화장품 전문가 하위 채팅 미생성 버그 분석 및 수정",
            "화장품 전문가 에이전트의 하위 세션 스폰 버그 원인 분석 및 수정",
            rooms=[empty_room, valid_room],
            parent_agent_id="__expert_coding__",
        )
        self.assertIsNotNone(matched_valid)
        self.assertEqual(matched_valid["id"], "room-valid-1")
        self.assertGreater(score_valid, 0.7)


if __name__ == "__main__":
    unittest.main()
