"""하위 슬롯 플레이스홀더 타겟 룸 라우팅 및 고아/유실 슬롯 자가치유 검증 단위 테스트."""
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock
from aiohttp.test_utils import make_mocked_request

import bot.core.chat_db as chat_db
import bot.master.master_tasks as master_tasks
from bot.master.master_supervisor import MasterSupervisor
from bot.web.orchestrator import handle_orchestrator_dispatch
from bot.web.job_recovery import _recover_startup_placeholders


class TestSlotPollutionReconciliation(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_slot_reconcile_")
        self.db_path = os.path.join(self.test_dir, "test_chat.db")
        self.state_dir = os.path.join(self.test_dir, "state")
        os.makedirs(self.state_dir, exist_ok=True)
        self.tasks_path = os.path.join(self.state_dir, "master_tasks.json")

        os.environ["CHAT_DB_FILE"] = self.db_path
        os.environ["MASTER_TASKS_FILE"] = self.tasks_path

        self.patch_chat_db_file = patch("bot.core.paths.CHAT_DB_FILE", self.db_path)
        self.patch_chat_db_mod = patch("bot.core.chat_db.CHAT_DB_FILE", self.db_path)
        self.patch_tasks_file = patch("bot.core.paths.MASTER_TASKS_FILE", self.tasks_path)
        self.patch_tasks_mod = patch("bot.master.master_tasks.MASTER_TASKS_FILE", self.tasks_path)

        self.patch_chat_db_file.start()
        self.patch_chat_db_mod.start()
        self.patch_tasks_file.start()
        self.patch_tasks_mod.start()

        chat_db.init_db(self.db_path)

    def tearDown(self):
        self.patch_chat_db_file.stop()
        self.patch_chat_db_mod.stop()
        self.patch_tasks_file.stop()
        self.patch_tasks_mod.stop()
        os.environ.pop("CHAT_DB_FILE", None)
        os.environ.pop("MASTER_TASKS_FILE", None)
        shutil.rmtree(self.test_dir, ignore_errors=True)

    async def test_dispatch_from_expert_targets_expert_room_not_core(self):
        """parentAgentId가 __expert_coding__이고 originRoomId가 없을 때 __core__가 아닌 __expert_coding__에 슬롯이 등록된다."""
        chat_db.get_or_create_room("__expert_coding__", title="봇개발 에이전트", db_path=self.db_path)
        chat_db.get_or_create_room("__core__", title="마스터 에이전트", db_path=self.db_path)

        payload = {
            "action": "spawn",
            "title": "테스트 전문가 서브태스크",
            "message": "코딩 작업 수행해줘",
            "parentAgentId": "__expert_coding__",
            "wait": False,
        }
        async def mock_json():
            return payload

        req = make_mocked_request("POST", "/api/orchestrator/dispatch")
        req.json = mock_json

        with patch("bot.web.rooms.chat_db", chat_db), \
             patch("bot.web.orchestrator.chat_db", chat_db), \
             patch("bot.web.room_queue.enqueue_room_turn", return_value={"already_running": False, "queue_size": 1}):
            resp = await handle_orchestrator_dispatch(req)
            self.assertEqual(resp.status, 200)

        # __core__ 방에는 슬롯 메시지가 등록되지 않았어야 함
        core_msgs = chat_db.get_messages("__core__", db_path=self.db_path)
        self.assertEqual(len(core_msgs), 0)

        # __expert_coding__ 방에 슬롯 메시지가 등록되었어야 함
        coding_msgs = chat_db.get_messages("__expert_coding__", db_path=self.db_path)
        self.assertEqual(len(coding_msgs), 1)
        self.assertEqual(coding_msgs[0]["channel"], "thread_bypass")
        self.assertTrue(coding_msgs[0]["steps"][0]["isSlotPlaceholder"])

    def test_supervisor_reconcile_updates_slot_on_lost_task(self):
        """master_supervisor에서 작업이 lost/failed 상태가 되면 슬롯 메시지가 즉시 실패/중단 처리된다."""
        slot_id = chat_db.add_message(
            room_id="__core__",
            role="assistant",
            text="",
            channel="thread_bypass",
            steps=[{"kind": "subagent_bypass", "tool": "bypass", "isSlotPlaceholder": True, "roomId": "room-lost-1", "taskId": "mt-lost-1", "status": "running", "title": "유실 작업"}],
            db_path=self.db_path,
        )
        master_tasks.create_task(
            room_id="room-lost-1",
            instruction="유실 작업 지시",
            title="유실 작업",
            task_id="mt-lost-1",
            slot_msg_id=slot_id,
        )

        mock_host = MagicMock()
        mock_host.latest_reply = MagicMock(return_value=None)
        supervisor = MasterSupervisor(host=mock_host)
        now = time.time() + 1000
        reconciled = supervisor._reconcile_now(now=now, live_runs=[])
        self.assertGreaterEqual(reconciled, 1)

        # DB 상의 슬롯 메시지가 failed/lost로 갱신되었는지 확인
        updated_msg = chat_db.get_message(slot_id, db_path=self.db_path)
        self.assertIsNotNone(updated_msg)
        self.assertTrue(updated_msg["text"].startswith("⚠️"))
        self.assertFalse(updated_msg["steps"][0]["isSlotPlaceholder"])
        self.assertEqual(updated_msg["steps"][0]["status"], "lost")
        self.assertFalse(updated_msg["steps"][0]["isSlotPlaceholder"])
        self.assertEqual(updated_msg["steps"][0]["status"], "lost")

    async def test_recover_startup_placeholders_cleans_orphan_slots(self):
        """웹서버 기동 시 고아 슬롯(자식 방 메시지 0건)은 삭제되고, 실제 대화가 있는 방 슬롯은 중단으로 복구된다."""
        # 1. 고아 방
        chat_db.get_or_create_room("room-ghost-child", title="유령방", db_path=self.db_path)
        old_ts = time.time() - 45.0
        ghost_slot = chat_db.add_message(
            room_id="__core__",
            role="assistant",
            text="",
            channel="thread_bypass",
            steps=[{"kind": "subagent_bypass", "tool": "bypass", "isSlotPlaceholder": True, "roomId": "room-ghost-child", "taskId": "mt-ghost", "status": "running"}],
            created_at=old_ts,
            db_path=self.db_path,
        )

        # 2. 실제 대화 방
        chat_db.get_or_create_room("room-real-child", title="실제방", db_path=self.db_path)
        chat_db.add_message("room-real-child", role="user", text="안녕", created_at=old_ts, db_path=self.db_path)
        real_slot = chat_db.add_message(
            room_id="__core__",
            role="assistant",
            text="",
            channel="thread_bypass",
            steps=[{"kind": "subagent_bypass", "tool": "bypass", "isSlotPlaceholder": True, "roomId": "room-real-child", "taskId": "mt-real", "status": "running", "title": "실제작업"}],
            created_at=old_ts,
            db_path=self.db_path,
        )

        with patch("bot.web.job_recovery.chat_db", chat_db):
            fixed = await _recover_startup_placeholders()
            self.assertEqual(fixed, 2)

        # 유령 슬롯 및 유령방은 DB에서 완전 제거
        self.assertIsNone(chat_db.get_message(ghost_slot, db_path=self.db_path))
        self.assertIsNone(chat_db.get_room("room-ghost-child", db_path=self.db_path))

        # 실제 대화 방 슬롯은 중단 상태로 복구
        real_msg = chat_db.get_message(real_slot, db_path=self.db_path)
        self.assertIsNotNone(real_msg)
        self.assertTrue(real_msg["text"].startswith("⚠️"))
