"""하위 세션 스폰 시 상설/관리형 에이전트 오매칭 방지 및 부모 에이전트 격리 단위 테스트."""
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import bot.core.agent_configs as agent_configs
import bot.core.chat_db as chat_db
from bot.web.orchestrator import (
    CORE_ORCHESTRATOR_ROOM_ID,
    _find_similar_orchestrator_room,
    _collect_orchestrator_candidate_rooms,
)


class TestSpawnSimilarityIsolation(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_spawn_isolation_")
        self.db_path = os.path.join(self.test_dir, "test_chat.db")
        self.state_dir = os.path.join(self.test_dir, "state")
        os.makedirs(self.state_dir, exist_ok=True)
        self.patch_chat_db_file = patch("bot.core.paths.CHAT_DB_FILE", self.db_path)
        self.patch_chat_db_file.start()
        chat_db.init_db(self.db_path)
        self.custom_agent_id = "__custom_ebb5f245__"

    def tearDown(self):
        self.patch_chat_db_file.stop()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_permanent_and_custom_agents_excluded_from_spawn_similarity(self):
        """하위 세션 스폰 시 상설 전문가 및 커스텀 에이전트 룸이 유사도 재사용 후보에서 원천 배제되는지 검증."""
        # 1. 상설 커스텀 에이전트(화장품 에이전트) 등록
        custom_agent = {
            "id": self.custom_agent_id,
            "title": "화장품 에이전트",
            "name": "화장품 전문가",
            "systemPrompt": "스킨케어 전문",
        }
        with patch.object(agent_configs, "_load_all_unlocked", return_value={self.custom_agent_id: custom_agent}):
            # 방 목록 모의 구성: 상설 마스터, 코딩 전문가, 커스텀 화장품 에이전트
            candidate_rooms = [
                {"id": CORE_ORCHESTRATOR_ROOM_ID, "title": "마스터 에이전트", "updatedAt": 1000},
                {"id": "__expert_coding__", "title": "봇개발 에이전트", "updatedAt": 1000},
                {"id": self.custom_agent_id, "title": "화장품 에이전트", "updatedAt": 1000},
            ]

            # 2. '화장품 전문가 하위 채팅 미생성 버그 분석'이라는 작업으로 유사도 탐색 수행
            title = "화장품 전문가 하위 채팅 미생성 버그 분석 및 수정"
            prompt = "화장품 전문가 에이전트에게 하위 채팅을 만들도록 지시/설정했음에도 안 만드는 버그"

            # exclude_orchestrator_rooms=True(기본값)일 때, 화장품 에이전트 방과 유사도가 높더라도 매칭되지 않아야 함
            best_room, score = _find_similar_orchestrator_room(
                title,
                prompt,
                rooms=candidate_rooms,
                exclude_orchestrator_rooms=True,
            )
            self.assertIsNone(best_room, "상설/커스텀 에이전트 룸은 하위 세션 스폰 후보로 선택되어서는 안 됩니다.")
            self.assertEqual(score, 0.0)

    def test_parent_agent_id_isolation(self):
        """parent_agent_id가 지정된 경우 동일 부모 산하 하위 세션만 매칭되는지 검증."""
        candidate_rooms = [
            {
                "id": "room-coding-sub-1",
                "title": "화장품 버그 코드 분석",
                "parentAgentId": "__expert_coding__",
                "updatedAt": 1000,
            },
            {
                "id": "room-vault-sub-1",
                "title": "화장품 버그 볼트 기록",
                "parentAgentId": "__expert_vault__",
                "updatedAt": 1000,
            },
        ]

        title = "화장품 버그 코드 분석 작업"
        prompt = "코드 리포지토리 버그 수정"

        # 1. __expert_coding__ 산하로 스폰 요청 시: 코딩 하위 방만 매칭
        best_room, score = _find_similar_orchestrator_room(
            title,
            prompt,
            rooms=candidate_rooms,
            parent_agent_id="__expert_coding__",
        )
        self.assertIsNotNone(best_room)
        self.assertEqual(best_room["id"], "room-coding-sub-1")

        # 2. __expert_vault__ 산하로 스폰 요청 시: 볼트 하위 방만 매칭
        best_room_vault, score_vault = _find_similar_orchestrator_room(
            "화장품 버그 볼트 기록 작업",
            "볼트 문서 정리",
            rooms=candidate_rooms,
            parent_agent_id="__expert_vault__",
        )
        self.assertIsNotNone(best_room_vault)
        self.assertEqual(best_room_vault["id"], "room-vault-sub-1")

        # 3. 다른 부모 에이전트(예: __expert_other__)로 요청 시 매칭되지 않아야 함
        best_room_none, score_none = _find_similar_orchestrator_room(
            title,
            prompt,
            rooms=candidate_rooms,
            parent_agent_id="__expert_other__",
        )
        self.assertIsNone(best_room_none)


    async def test_dispatch_spawn_does_not_hijack_permanent_agent(self):
        """dispatch spawn 호출 시 상설 커스텀 에이전트 룸이 가로채지지 않고 신규 워커 방이 생성되는지 검증."""
        import json
        from aiohttp.test_utils import make_mocked_request
        from bot.web.orchestrator import handle_orchestrator_dispatch

        # 1. DB에 상설 커스텀 에이전트(화장품 에이전트) 방 등록
        chat_db.get_or_create_room(
            self.custom_agent_id,
            title="화장품 에이전트",
            db_path=self.db_path,
        )

        custom_agent = {
            "id": self.custom_agent_id,
            "title": "화장품 에이전트",
            "name": "화장품 전문가",
            "systemPrompt": "스킨케어 전문",
        }

        # 2. spawn 요청 생성
        payload = {
            "action": "spawn",
            "title": "화장품 전문가 하위 채팅 미생성 버그 분석 및 수정",
            "message": "화장품 전문가 에이전트의 하위 세션 스폰 버그 원인 분석 및 수정",
            "parentAgentId": "__expert_coding__",
            "wait": False,
        }
        async def mock_json():
            return payload

        req = make_mocked_request("POST", "/api/orchestrator/dispatch", payload=json.dumps(payload).encode("utf-8"))
        req.json = mock_json

        with patch.object(agent_configs, "_load_all_unlocked", return_value={self.custom_agent_id: custom_agent}), \
             patch("bot.web.orchestrator.chat_db", chat_db), \
             patch("bot.web.rooms.chat_db", chat_db), \
             patch("bot.web.rooms._append_room_message", return_value=123), \
             patch("bot.web.room_queue.enqueue_room_turn", return_value={"already_running": False, "queue_size": 1}):

            resp = await handle_orchestrator_dispatch(req)
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.text)
            self.assertTrue(data.get("ok"))
            # 상설 방(__custom_ebb5f245__)이 재사용되지 않았어야 함
            self.assertFalse(data.get("reused", False))
            self.assertNotEqual(data.get("roomId"), self.custom_agent_id)
            self.assertTrue(str(data.get("roomId", "")).startswith("room-"))

            # 테스트 격리: 생성된 임시 하위 방 정리
            created_rid = data.get("roomId")
            if created_rid:
                with chat_db.get_connection() as conn:
                    conn.execute("DELETE FROM rooms WHERE id = ?;", (created_rid,))
                    conn.commit()


if __name__ == "__main__":
    unittest.main()
