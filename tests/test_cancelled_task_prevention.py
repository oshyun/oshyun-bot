"""test_cancelled_task_prevention.py — 명시적 중단 작업의 상위 에이전트 강제 재실행 방지 검증 단위 테스트."""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import master_supervisor
import master_tasks
import web_execution


class TestMasterTasksCancellation(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_mt_cancel_")
        self.orig_file = master_tasks.MASTER_TASKS_FILE
        master_tasks.MASTER_TASKS_FILE = os.path.join(self.test_dir, "master_tasks.json")

    def tearDown(self):
        master_tasks.MASTER_TASKS_FILE = self.orig_file
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_is_task_aborted(self):
        self.assertFalse(master_tasks.is_task_aborted(None))
        self.assertFalse(master_tasks.is_task_aborted({}))
        self.assertFalse(master_tasks.is_task_aborted({"status": "running"}))
        self.assertTrue(master_tasks.is_task_aborted({"status": "cancelled"}))
        self.assertTrue(master_tasks.is_task_aborted({"status": "failed", "user_cancelled": True}))
        self.assertTrue(master_tasks.is_task_aborted({"status": "lost", "user_confirmed": True}))

    def test_needs_attention_excludes_aborted_tasks(self):
        # 일반 failed 태스크는 attention 필요
        task_failed = master_tasks.create_task(room_id="room-1", instruction="작업 1")
        master_tasks.update_task(task_failed["id"], status="failed", error="오류 발생")
        t = master_tasks.get_task(task_failed["id"])
        self.assertTrue(master_tasks.needs_attention(t))

        # user_cancelled 또는 user_confirmed 된 failed 태스크는 attention 대상에서 배제
        master_tasks.update_task(task_failed["id"], user_cancelled=True)
        t = master_tasks.get_task(task_failed["id"])
        self.assertFalse(master_tasks.needs_attention(t))

        # cancelled 태스크는 attention 대상에서 배제
        task_cancelled = master_tasks.create_task(room_id="room-2", instruction="작업 2")
        master_tasks.update_task(task_cancelled["id"], status="cancelled")
        t = master_tasks.get_task(task_cancelled["id"])
        self.assertFalse(master_tasks.needs_attention(t))
        self.assertTrue(t["user_cancelled"])
        self.assertTrue(t["user_confirmed"])
        self.assertTrue(t["reviewed"])

    def test_update_task_prevents_cancelled_task_resurrect(self):
        task = master_tasks.create_task(room_id="room-1", instruction="작업 1")
        tid = task["id"]
        # 사용자가 취소
        master_tasks.update_task(tid, status="cancelled")
        t = master_tasks.get_task(tid)
        self.assertEqual(t["status"], "cancelled")

        # 재기동 복구나 기타 루프에서 running 으로 덮어쓰기 시도 -> 차단
        master_tasks.update_task(tid, status="running", feed="실행 중…")
        t = master_tasks.get_task(tid)
        self.assertEqual(t["status"], "cancelled")
        self.assertTrue(t["user_cancelled"])

        # queued 로 덮어쓰기 시도 -> 차단
        master_tasks.update_task(tid, status="queued")
        t = master_tasks.get_task(tid)
        self.assertEqual(t["status"], "cancelled")

    def test_unreported_async_tasks_excludes_cancelled_and_aborted(self):
        now = time.time()
        # 일반 failed 작업은 unreported 대상에 포함
        t1 = master_tasks.create_task(room_id="room-1", instruction="작업 1", origin="dispatch")
        master_tasks.update_task(t1["id"], status="failed", finishedAt=now - 30)

        # 취소된 작업
        t2 = master_tasks.create_task(room_id="room-2", instruction="작업 2", origin="dispatch")
        master_tasks.update_task(t2["id"], status="cancelled", finishedAt=now - 30)

        # user_cancelled 된 작업
        t3 = master_tasks.create_task(room_id="room-3", instruction="작업 3", origin="dispatch")
        master_tasks.update_task(t3["id"], status="failed", user_cancelled=True, finishedAt=now - 30)

        unrep = master_tasks.unreported_async_tasks(now=now, max_age=3600, min_age=10)
        unrep_ids = [t["id"] for t in unrep]
        self.assertIn(t1["id"], unrep_ids)
        self.assertNotIn(t2["id"], unrep_ids)
        self.assertNotIn(t3["id"], unrep_ids)


class TestMasterSupervisorCancellation(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_sup_cancel_")
        self.orig_file = master_tasks.MASTER_TASKS_FILE
        master_tasks.MASTER_TASKS_FILE = os.path.join(self.test_dir, "master_tasks.json")

    def tearDown(self):
        master_tasks.MASTER_TASKS_FILE = self.orig_file
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_reconcile_tasks_detects_user_cancellation_in_reply(self):
        now = time.time()
        t = master_tasks.create_task(room_id="room-cancel", instruction="중단될 작업")
        # update_task 가 updatedAt 을 내부 now 로 덮어쓰므로 now 기준 과거로 설정
        future_now = now + 200

        tasks = [master_tasks.get_task(t["id"])]
        live_runs = []  # 실행 흔적 없음

        def mock_latest_reply(rid, since):
            return "🛑 사용자에 의해 실행이 중단되었습니다."

        changes = master_supervisor.reconcile_tasks(
            tasks, live_runs, now=future_now, latest_reply=mock_latest_reply
        )
        self.assertEqual(len(changes), 1)
        tid, upd = changes[0]
        self.assertEqual(tid, t["id"])
        self.assertEqual(upd["status"], "cancelled")
        self.assertTrue(upd["user_cancelled"])
        self.assertTrue(upd["user_confirmed"])
        self.assertTrue(upd["reviewed"])

    def test_reconcile_tasks_skips_already_aborted_tasks(self):
        now = time.time()
        t = master_tasks.create_task(room_id="room-cancel", instruction="이미 취소된 작업")
        master_tasks.update_task(t["id"], status="cancelled")

        tasks = [master_tasks.get_task(t["id"])]
        live_runs = []
        changes = master_supervisor.reconcile_tasks(tasks, live_runs, now=now)
        self.assertEqual(len(changes), 0)


class TestWebExecutionCancellation(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_exec_cancel_")
        self.orig_file = master_tasks.MASTER_TASKS_FILE
        master_tasks.MASTER_TASKS_FILE = os.path.join(self.test_dir, "master_tasks.json")

    def tearDown(self):
        master_tasks.MASTER_TASKS_FILE = self.orig_file
        shutil.rmtree(self.test_dir, ignore_errors=True)

    async def test_finalize_completion_skips_parent_notification_on_cancelled(self):
        task = master_tasks.create_task(room_id="room-child", instruction="하위 작업", origin="dispatch")
        tid = task["id"]

        mock_notify = AsyncMock()
        with patch.object(web_execution, "_notify_master_agent", mock_notify), \
             patch.object(web_execution, "_room_parent_id", return_value="__core__"):

            # status="cancelled" 로 종료 시
            await web_execution._finalize_orchestrator_completion(
                room_id="room-child",
                task_id=tid,
                channel="orchestrator",
                is_orchestrator=False,
                is_success=False,
                result_text="작업이 취소되었습니다.",
                status="cancelled",
                error_text="사용자 취소",
            )

            # 상위 에이전트(_notify_master_agent)에게 보고를 보내지 않아야 함
            mock_notify.assert_not_called()

            # 작업 원장은 cancelled 로 기록되고 user_cancelled, user_confirmed, reviewed 가 True
            t_after = master_tasks.get_task(tid)
            self.assertEqual(t_after["status"], "cancelled")
            self.assertTrue(t_after["user_cancelled"])
            self.assertTrue(t_after["user_confirmed"])
            self.assertTrue(t_after["reviewed"])

    def test_cancel_active_run_cancels_queued_and_snapshot_tasks(self):
        room_id = "test-cancel-room"
        task1 = master_tasks.create_task(room_id=room_id, instruction="실행 중 작업")
        task2 = master_tasks.create_task(room_id=room_id, instruction="대기 중 작업")

        # 스냅샷 등록
        web_execution._room_live_snapshots[room_id] = {
            "taskId": task1["id"],
            "roomId": room_id,
        }

        # 큐 등록
        q = asyncio.Queue()
        q.put_nowait({"task_id": task2["id"], "user_text": "대기 턴", "result_future": asyncio.Future()})
        web_execution._room_queues[room_id] = q

        try:
            with patch.object(web_execution, "kill_job", MagicMock()):
                res = web_execution.cancel_active_run(room_id)
                self.assertTrue(res)

            # 두 태스크 모두 즉시 cancelled 상태 및 user_cancelled 확정 확인
            t1 = master_tasks.get_task(task1["id"])
            t2 = master_tasks.get_task(task2["id"])
            self.assertEqual(t1["status"], "cancelled")
            self.assertTrue(t1["user_cancelled"])
            self.assertTrue(t1["reviewed"])
            self.assertEqual(t2["status"], "cancelled")
            self.assertTrue(t2["user_cancelled"])
            self.assertTrue(t2["reviewed"])
        finally:
            web_execution._room_live_snapshots.pop(room_id, None)
            web_execution._room_queues.pop(room_id, None)
            web_execution._user_cancelled_rooms.discard(room_id)

    async def test_reap_zombie_sessions_handles_aborted_tasks_without_error_notify(self):
        room_id = "test-reap-room"
        task = master_tasks.create_task(room_id=room_id, instruction="중단된 작업")
        master_tasks.update_task(task["id"], status="cancelled")

        web_execution._room_live_snapshots[room_id] = {
            "taskId": task["id"],
            "roomId": room_id,
            "startTimestamp": time.time() - 3600,  # 타임아웃
        }

        mock_notify = AsyncMock()
        with patch.object(web_execution, "_notify_master_agent", mock_notify), \
             patch.object(web_execution, "_load_web_jobs", return_value={}):
            reaped = await web_execution.reap_zombie_sessions(max_inactive_seconds=10, max_elapsed_seconds=20)
            self.assertIn(room_id, reaped)
            # 상위 에이전트 통지가 호출되지 않아야 함
            mock_notify.assert_not_called()

        t = master_tasks.get_task(task["id"])
        self.assertEqual(t["status"], "cancelled")

    async def test_restore_pending_room_turns_skips_cancelled_tasks(self):
        task = master_tasks.create_task(room_id="room-restore", instruction="취소된 작업")
        master_tasks.update_task(task["id"], status="cancelled")

        pending_items = [
            {"room_id": "room-restore", "user_text": "취소된 작업 재실행 시도", "task_id": task["id"]}
        ]
        mock_enqueue = AsyncMock()
        with patch.object(web_execution, "read_json", return_value=pending_items), \
             patch("os.path.exists", return_value=True), \
             patch("os.remove", MagicMock()), \
             patch.object(web_execution, "enqueue_room_turn", mock_enqueue):
            await web_execution._restore_pending_room_turns()
            # 취소된 태스크이므로 대기열 적재(enqueue_room_turn)가 호출되지 않아야 함
            mock_enqueue.assert_not_called()


if __name__ == "__main__":
    unittest.main()
