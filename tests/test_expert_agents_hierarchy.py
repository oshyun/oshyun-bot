"""전문가 에이전트 계층(Expert Agents Hierarchy) 및 분리 메모리 단위 테스트."""
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import chat_db
import master_memory


class TestExpertAgentsHierarchy(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_expert_hier_")
        self.db_path = os.path.join(self.test_dir, "test_chat.db")
        chat_db.init_db(self.db_path)

        self.orig_mem_file = master_memory.MASTER_MEMORY_FILE
        master_memory.MASTER_MEMORY_FILE = os.path.join(self.test_dir, "master_memory.json")

    def tearDown(self):
        master_memory.MASTER_MEMORY_FILE = self.orig_mem_file
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_chat_db_parent_agent_id_persistence(self):
        # 1. 자식 방 생성 및 parent_agent_id 저장
        room = chat_db.get_or_create_room(
            "sub_room_1",
            title="하위 태스크",
            parent_agent_id="__expert_vault__",
            db_path=self.db_path,
        )
        self.assertEqual(room.get("parent_agent_id"), "__expert_vault__")

        # 2. 조회 시 parentAgentId 및 isOrchestratorChild 검증
        fetched = chat_db.get_room("sub_room_1", db_path=self.db_path)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.get("parentAgentId"), "__expert_vault__")
        self.assertTrue(fetched.get("isOrchestratorChild"))

        # 3. update_room 으로 parent_agent_id 갱신
        chat_db.update_room("sub_room_1", parent_agent_id="__expert_coding__", db_path=self.db_path)
        updated = chat_db.get_room("sub_room_1", db_path=self.db_path)
        self.assertEqual(updated.get("parentAgentId"), "__expert_coding__")

    def test_isolated_memory_files_and_upward_reporting(self):
        # 1. 에이전트별 메모리 파일 경로 분리 확인
        m_file = master_memory._get_memory_file("master")
        v_file = master_memory._get_memory_file("second_brain")
        c_file = master_memory._get_memory_file("bot_dev")
        self.assertTrue(m_file.endswith("master_memory.json"))
        self.assertTrue(v_file.endswith("master_memory_second_brain.json"))
        self.assertTrue(c_file.endswith("master_memory_bot_dev.json"))

        # 2. 세컨브레인 메모리에 항목 추가
        master_memory.add_memory("개인 볼트 가계부 정리", agent_id="second_brain", tier="hot")
        v_snap = master_memory.get_memory_snapshot(agent_id="second_brain")
        m_snap = master_memory.get_memory_snapshot(agent_id="master")
        self.assertEqual(v_snap["totalItems"], 1)
        self.assertEqual(m_snap["totalItems"], 0)

        # 3. 봇개발 에이전트 코드 수정 이력 보존 검증 (잡음 필터 예외)
        dev_text = "test_chat_db.py 단위 테스트 통과 및 bug fix 완료"
        master_memory.add_memory(dev_text, agent_id="bot_dev", tier="hot", filter_noise=True)
        c_snap = master_memory.get_memory_snapshot(agent_id="bot_dev")
        self.assertEqual(c_snap["totalItems"], 1)
        self.assertIn("test_chat_db.py", c_snap["tiers"]["hot"]["items"][0]["text"])

        # 4. 세컨브레인의 하위 작업 완료 응답 턴 -> 마스터 WARM 상향 보고 검증
        master_memory.record_orchestrator_turn_memory(
            user_text="[하위 작업 완료 보고] 가계부 지출 대조 완료",
            reply_text="총 지출 150만원 일치 확인 완료",
            channel="task_report",
            room_id="__expert_vault__",
            agent_id="second_brain",
            upward_report_to_master=True,
        )
        # 마스터 메모리에 [세컨브레인] 태그로 WARM 적재 확인
        m_snap_after = master_memory.get_memory_snapshot(agent_id="master")
        self.assertGreaterEqual(m_snap_after["totalItems"], 1)
        warm_items = m_snap_after["tiers"]["warm"]["items"]
        self.assertTrue(any("[세컨브레인]" in it["text"] for it in warm_items))


if __name__ == "__main__":
    unittest.main()
