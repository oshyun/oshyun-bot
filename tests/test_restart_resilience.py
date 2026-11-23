"""test_restart_resilience.py — 서버 재시작 및 고아 프로세스 재부착(Reattachment), 원장 복구 검증."""

from __future__ import annotations

import asyncio
import copy
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import bot.core.bot_logger as bot_logger
import bot.core.chat_db as chat_db
import bot.engine.engine_settings as engine_settings
import bot.master.master_supervisor as master_supervisor
import bot.master.master_tasks as master_tasks
import bot.engine.stream_parser as stream_parser
import bot.web.execution as web_execution
import bot.web.server as web_server


class TestWebJobReattachment(unittest.IsolatedAsyncioTestCase):
    """서버 재시작 후 web_jobs 복구 및 재부착 로직 검증."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self._orig_settings = copy.deepcopy(engine_settings._settings)
        self.tasks_file = os.path.join(self.tmp_dir, "master_tasks.json")
        self.web_jobs_file = os.path.join(self.tmp_dir, "web_jobs.json")
        self.db_path = os.path.join(self.tmp_dir, "test_chat.db")
        self.jobs_dir = os.path.join(self.tmp_dir, "jobs")
        os.makedirs(self.jobs_dir, exist_ok=True)
        chat_db.init_db(self.db_path, migrate_legacy=False)

        self.patch_tasks = patch("bot.master.master_tasks.MASTER_TASKS_FILE", self.tasks_file)
        self.patch_memory = patch(
            'bot.master.master_memory.MASTER_MEMORY_FILE',
            os.path.join(self.tmp_dir, "master_memory.json"),
        )
        self.patch_jobs = patch.object(web_execution, "WEB_JOBS_FILE", self.web_jobs_file)
        self.patch_server_jobs = patch.object(web_server, "WEB_JOBS_FILE", self.web_jobs_file)
        self.patch_jobs_dir = patch.object(web_execution, "JOBS_DIR", self.jobs_dir)
        self.patch_server_jobs_dir = patch.object(web_server, "JOBS_DIR", self.jobs_dir)
        self.patch_db = patch("bot.core.chat_db.CHAT_DB_FILE", self.db_path)
        self.patch_settings = patch(
            'bot.engine.engine_settings._SETTINGS_FILE',
            os.path.join(self.tmp_dir, "bot_settings.json"),
        )
        self.patch_session_log = patch(
            'bot.engine.stream_parser._SESSION_LOG_FILE',
            os.path.join(self.tmp_dir, "session_log.jsonl"),
        )
        self.patch_vault_log_path = patch(
            'bot.core.bot_logger.BOT_LOG_PATH',
            os.path.join(self.tmp_dir, "bot-log.md"),
        )
        self.patch_run_vault_log = patch("bot.core.bot_logger._run_vault_log", AsyncMock())
        for p in (
            self.patch_tasks,
            self.patch_memory,
            self.patch_jobs,
            self.patch_server_jobs,
            self.patch_jobs_dir,
            self.patch_server_jobs_dir,
            self.patch_db,
            self.patch_settings,
            self.patch_session_log,
            self.patch_vault_log_path,
            self.patch_run_vault_log,
        ):
            p.start()

        # 전역 상태 초기화
        web_execution._room_live_snapshots.clear()
        web_execution._room_live_feeds.clear()
        web_execution._active_agent_pids.clear()
        web_execution._active_watch_tasks.clear()
        web_execution._room_workers.clear()
        web_execution._room_queues.clear()

    def tearDown(self):
        for p in (
            self.patch_run_vault_log,
            self.patch_vault_log_path,
            self.patch_session_log,
            self.patch_settings,
            self.patch_db,
            self.patch_server_jobs_dir,
            self.patch_jobs_dir,
            self.patch_server_jobs,
            self.patch_jobs,
            self.patch_memory,
            self.patch_tasks,
        ):
            p.stop()
        engine_settings._settings.clear()
        engine_settings._settings.update(self._orig_settings)
        if os.path.exists(engine_settings._SETTINGS_FILE):
            engine_settings._sync_from_disk()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        web_execution._room_live_snapshots.clear()
        web_execution._room_live_feeds.clear()
        web_execution._active_agent_pids.clear()
        web_execution._active_watch_tasks.clear()
        web_execution._room_workers.clear()
        web_execution._room_queues.clear()

    async def test_recover_single_web_job_with_alive_pid(self):
        """살아있는 PID 감지 시 라이브 스냅샷 및 작업 원장이 running 상태로 즉시 복원된다."""
        task = master_tasks.create_task(
            room_id="room-alive",
            instruction="긴 분석 작업 실행",
        )
        task_id = task["id"]

        dummy_out = os.path.join(self.tmp_dir, "alive.jsonl")
        with open(dummy_out, "w", encoding="utf-8") as f:
            f.write('{"type": "progress", "text": "계산 중"}\n')

        rec = {
            "job_id": "job-alive-1",
            "room_id": "room-alive",
            "msg_id": 101,
            "pid": 99999,
            "out_file": dummy_out,
            "err_file": None,
            "started_at": time.time() - 10,
            "session_id": "sess-alive",
            "task_id": task_id,
            "user_text": "긴 분석 작업 실행",
        }

        with patch.object(web_execution, "detached_pid_alive", return_value=True), \
             patch.object(web_execution, "_watch_and_finish_web_job", new_callable=AsyncMock) as mock_watch:

            await web_execution._recover_single_web_job("job-alive-1", rec)

            # 1. 활성 PID 및 라이브 스냅샷 복원 확인
            self.assertEqual(web_execution._active_agent_pids.get("room-alive"), 99999)
            snap = web_execution._room_live_snapshots.get("room-alive")
            self.assertIsNotNone(snap)
            self.assertEqual(snap.get("taskId"), task_id)
            self.assertEqual(snap.get("feed"), "실행 중 (재부착됨)…")

            # 2. 작업 원장 status=running 갱신 확인
            t_after = master_tasks.get_task(task_id)
            self.assertEqual(t_after["status"], "running")
            self.assertEqual(t_after["feed"], "실행 중 (재부착됨)…")

            # 3. 비동기 감시 태스크 생성 확인
            mock_watch.assert_called_once()

    async def test_recover_single_web_job_finished_during_downtime(self):
        """다운타임 중 완료된 작업은 DB를 갱신하고 master_tasks를 done으로 변경하며 마스터에 보고한다."""
        task = master_tasks.create_task(
            room_id="room-done",
            instruction="단문 질의",
        )
        task_id = task["id"]

        dummy_out = os.path.join(self.tmp_dir, "done.jsonl")
        with open(dummy_out, "w", encoding="utf-8") as f:
            f.write('{"type": "result", "result": "분석이 성공적으로 끝났습니다."}\n')

        real_msg_id = chat_db.add_message(
            room_id="room-done",
            role="assistant",
            text="",
            db_path=self.db_path,
        )

        rec = {
            "job_id": "job-done-1",
            "room_id": "room-done",
            "msg_id": real_msg_id,
            "pid": None,
            "out_file": dummy_out,
            "err_file": None,
            "started_at": time.time() - 30,
            "session_id": "sess-done",
            "task_id": task_id,
            "user_text": "단문 질의",
        }

        mock_notify = AsyncMock()

        fake_output = {
            "result": "분석이 성공적으로 끝났습니다.",
            "session_id": "sess-done",
            "cost": 0.001,
            "interrupted": False,
        }

        with patch.object(web_execution, "detached_pid_alive", return_value=False), \
             patch.object(web_execution, "watch_agent", new_callable=AsyncMock, return_value=fake_output), \
             patch.object(web_execution, "_notify_master_agent", mock_notify):

            await web_execution._recover_single_web_job("job-done-1", rec)

            # 1. DB 메시지 업데이트 확인 (실제 DB 조회)
            msg_row = chat_db.get_message(real_msg_id, db_path=self.db_path)
            self.assertIsNotNone(msg_row)
            self.assertEqual(msg_row["text"], "분석이 성공적으로 끝났습니다.")

            # 2. 작업 원장이 done으로 갱신되었는지 확인
            t_after = master_tasks.get_task(task_id)
            self.assertEqual(t_after["status"], "done")
            self.assertEqual(t_after["resultPreview"], "분석이 성공적으로 끝났습니다.")

            # 3. 마스터 에이전트 완료 보고 전달 확인
            mock_notify.assert_awaited_once_with(
                "room-done", task_id, "분석이 성공적으로 끝났습니다.", is_success=True
            )

    async def test_recover_single_web_job_interrupted(self):
        """서버 재시작으로 완전히 중단된 작업은 failed로 갱신되고 마스터에 실패 보고가 전달된다."""
        task = master_tasks.create_task(
            room_id="room-fail",
            instruction="중단될 작업",
        )
        task_id = task["id"]

        dummy_out = os.path.join(self.tmp_dir, "fail.jsonl")
        with open(dummy_out, "w", encoding="utf-8") as f:
            f.write("")

        real_msg_id = chat_db.add_message(
            room_id="room-fail",
            role="assistant",
            text="",
            db_path=self.db_path,
        )

        rec = {
            "job_id": "job-fail-1",
            "room_id": "room-fail",
            "msg_id": real_msg_id,
            "pid": None,
            "out_file": dummy_out,
            "err_file": None,
            "started_at": time.time() - 20,
            "session_id": "sess-fail",
            "task_id": task_id,
            "user_text": "중단될 작업",
        }

        mock_notify = AsyncMock()

        fake_output = {
            "result": "⚠️ 서버 재시작으로 답변이 중단되었습니다.",
            "session_id": "sess-fail",
            "interrupted": True,
        }

        with patch.object(web_execution, "detached_pid_alive", return_value=False), \
             patch.object(web_execution, "watch_agent", new_callable=AsyncMock, return_value=fake_output), \
             patch.object(web_execution, "_notify_master_agent", mock_notify):

            await web_execution._recover_single_web_job("job-fail-1", rec)

            # 1. 작업 원장 status=failed 갱신 확인
            t_after = master_tasks.get_task(task_id)
            self.assertEqual(t_after["status"], "failed")

            # 2. 마스터 보고 실패 전달 확인
            mock_notify.assert_awaited_once_with(
                "room-fail", task_id, "서버 재시작으로 작업 중단됨", is_success=False
            )

    async def test_recover_single_web_job_registers_active_watch_task_and_busy(self):
        """재부착 시 _active_watch_tasks 에 태스크가 등록되고 is_room_busy 가 True 를 반환한다."""
        dummy_out = os.path.join(self.tmp_dir, "busy.jsonl")
        with open(dummy_out, "w", encoding="utf-8") as f:
            f.write("")

        rec = {
            "job_id": "job-busy-1",
            "room_id": "room-busy",
            "msg_id": 202,
            "pid": 88888,
            "out_file": dummy_out,
            "err_file": None,
            "started_at": time.time() - 5,
            "session_id": "sess-busy",
            "user_text": "진행 중 작업",
        }

        gate = asyncio.Event()

        async def _fake_watch(*_args, **_kwargs):
            await gate.wait()

        try:
            with patch.object(web_execution, "detached_pid_alive", return_value=True), \
                 patch.object(web_execution, "_watch_and_finish_web_job", side_effect=_fake_watch):
                await web_execution._recover_single_web_job("job-busy-1", rec)
                self.assertIn("room-busy", web_execution._active_watch_tasks)
                self.assertTrue(web_execution.is_room_busy("room-busy"))
        finally:
            gate.set()
            task = web_execution._active_watch_tasks.pop("room-busy", None)
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    async def test_watch_and_finish_web_job_shutdown_preserves_job_for_next_restart(self):
        """재부착 감시 중 서버 재시작(CancelledError)이 발생해도 web_jobs.json 과 out_file 을 보존한다."""
        task = master_tasks.create_task(
            room_id="room-reattach-2",
            instruction="다중 재시작 테스트",
        )
        task_id = task["id"]
        master_tasks.update_task(task_id, status="running")

        dummy_out = os.path.join(self.tmp_dir, "reattach2.jsonl")
        with open(dummy_out, "w", encoding="utf-8") as f:
            f.write('{"type": "progress", "text": "진행 중"}\n')

        rec = {
            "job_id": "job-reattach-2",
            "room_id": "room-reattach-2",
            "msg_id": 303,
            "pid": 77777,
            "out_file": dummy_out,
            "err_file": None,
            "started_at": time.time() - 5,
            "session_id": "sess-reattach-2",
            "task_id": task_id,
            "user_text": "다중 재시작 테스트",
            "channel": "orchestrator",
        }
        web_execution._save_web_job("job-reattach-2", rec)

        async def _blocking_watch(*_args, **_kwargs):
            await asyncio.sleep(60)

        web_execution.set_shutting_down(True)
        try:
            with patch.object(web_execution, "watch_agent", side_effect=_blocking_watch):
                watch_task = asyncio.create_task(
                    web_execution._watch_and_finish_web_job("job-reattach-2", rec, [])
                )
                await asyncio.sleep(0.05)
                watch_task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await watch_task

            # web_jobs.json 과 out_file 이 삭제되지 않고 보존되어야 함
            saved_jobs = web_execution._load_web_jobs()
            self.assertIn("job-reattach-2", saved_jobs)
            self.assertTrue(os.path.exists(dummy_out))
            t_after = master_tasks.get_task(task_id)
            self.assertEqual(t_after["status"], "running")
        finally:
            web_execution.set_shutting_down(False)

    async def test_execute_chat_turn_shutdown_preserves_job_and_running_task(self):
        """execute_chat_turn 실행 중 서버 재시작 시 cancelled 로 오기하지 않고 job 을 보존한다."""
        task = master_tasks.create_task(
            room_id="room-exec-shutdown",
            instruction="서버 재시작 내성 테스트",
        )
        task_id = task["id"]

        fake_proc = MagicMock()
        fake_proc.pid = 66666

        async def _blocking_watch(*_args, **_kwargs):
            await asyncio.sleep(60)

        broadcast_calls = []

        async def _fake_broadcast(payload):
            broadcast_calls.append(payload)

        web_execution.set_shutting_down(True)
        try:
            mock_spawn = MagicMock(return_value=fake_proc)
            orig_get_mod_attr = web_execution._get_mod_attr

            def _fake_get_mod_attr(name, fallback):
                if name == "spawn_agent":
                    return mock_spawn
                if name == "watch_agent":
                    return _blocking_watch
                if name == "broadcast_ws_json":
                    return _fake_broadcast
                return orig_get_mod_attr(name, fallback)

            with patch.object(web_execution, "_get_mod_attr", side_effect=_fake_get_mod_attr):
                turn_task = asyncio.create_task(
                    web_execution.execute_chat_turn(
                        "서버 재시작 내성 테스트",
                        False,
                        room_id="room-exec-shutdown",
                        channel="orchestrator",
                        task_id=task_id,
                    )
                )
                await asyncio.sleep(0.05)
                turn_task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await turn_task

            # 1. master_tasks 상태가 cancelled 가 아니라 running 으로 유지되어야 함
            t_after = master_tasks.get_task(task_id)
            self.assertEqual(t_after["status"], "running")

            # 2. cancelled 브로드캐스트가 발생하지 않아야 함
            self.assertFalse(any(c.get("type") == "cancelled" for c in broadcast_calls))

            # 3. web_jobs.json 에 job 이 보존되어야 함
            saved_jobs = web_execution._load_web_jobs()
            self.assertEqual(len(saved_jobs), 1)
            only_job = next(iter(saved_jobs.values()))
            self.assertEqual(only_job["pid"], 66666)
        finally:
            web_execution.set_shutting_down(False)

    async def test_persist_and_restore_pending_room_turns(self):
        """서버 종료 시 대기열 미실행 턴이 영속화되고 기동 시 다시 큐에 복원된다."""
        q = asyncio.Queue()
        await q.put({
            "room_id": "room-queued-1",
            "user_text": "대기 중이던 후속 지시",
            "channel": "orchestrator",
            "task_id": "mt-q1",
        })
        web_execution._room_queues["room-queued-1"] = q

        count = web_execution.persist_pending_room_turns()
        self.assertEqual(count, 1)
        self.assertTrue(os.path.exists(web_execution._get_web_pending_turns_file()))

        mock_enqueue = AsyncMock(return_value=(False, 1))
        with patch.object(web_execution, "enqueue_room_turn", mock_enqueue):
            await web_execution._restore_pending_room_turns()

        mock_enqueue.assert_awaited_once()
        call_kwargs = mock_enqueue.call_args.kwargs
        self.assertEqual(call_kwargs["room_id"], "room-queued-1")
        self.assertEqual(call_kwargs["user_text"], "대기 중이던 후속 지시")
        self.assertEqual(call_kwargs["task_id"], "mt-q1")
        self.assertFalse(os.path.exists(web_execution._get_web_pending_turns_file()))

    async def test_detach_active_tasks_cancels_workers_and_watchers_cleanly(self):
        """detach_active_tasks 는 활성 워커와 감시 태스크를 중복 없이 취소하고 종료를 대기한다."""
        cancelled_flags = {"worker": False, "watch": False}

        async def _dummy_worker():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled_flags["worker"] = True
                raise

        async def _dummy_watch():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled_flags["watch"] = True
                raise

        w_task = asyncio.create_task(_dummy_worker())
        watch_task = asyncio.create_task(_dummy_watch())
        web_execution._room_workers["room-detach-1"] = w_task
        web_execution._active_watch_tasks["room-detach-1"] = watch_task
        await asyncio.sleep(0)

        detached = await web_execution.detach_active_tasks(timeout=1.0)
        self.assertEqual(detached, 2)
        self.assertTrue(cancelled_flags["worker"])
        self.assertTrue(cancelled_flags["watch"])
        self.assertTrue(w_task.done())
        self.assertTrue(watch_task.done())


class TestMasterSupervisorGracefulReconciliation(unittest.TestCase):
    """master_supervisor.reconcile_tasks의 Graceful Recovery 검증."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.tasks_file = os.path.join(self.tmp_dir, "master_tasks.json")
        self.patch_tasks = patch("bot.master.master_tasks.MASTER_TASKS_FILE", self.tasks_file)
        self.patch_tasks.start()

    def tearDown(self):
        self.patch_tasks.stop()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_reconcile_tasks_lost_grace_period(self):
        """실행 흔적이 없더라도 LOST_GRACE_SECONDS 이내이면 lost로 오인하지 않고 유예한다."""
        now = time.time()
        task = master_tasks.create_task(
            room_id="room-grace",
            instruction="테스트",
        )
        task["status"] = "running"
        task["updatedAt"] = now - 30  # 30초 전 업데이트 (LOST_GRACE_SECONDS=90초 이내)

        # live_runs에 흔적이 없음
        changes = master_supervisor.reconcile_tasks(
            [task],
            live_runs=[],
            now=now,
            stall_seconds=900,
            latest_reply=lambda r, s: None,
        )

        # 유예 기간이므로 변경 사항이 발생하지 않아야 함
        self.assertEqual(len(changes), 0)

    def test_reconcile_tasks_downtime_reply_recovery(self):
        """유예 시간 경과 후에도 DB에 정상 답변이 있으면 lost가 아니라 done으로 복구한다."""
        now = time.time()
        task = master_tasks.create_task(
            room_id="room-recovered",
            instruction="테스트",
        )
        task["status"] = "running"
        task["updatedAt"] = now - 120  # 유예 시간(90초) 경과

        # latest_reply 가 정상 답변을 반환
        changes = master_supervisor.reconcile_tasks(
            [task],
            live_runs=[],
            now=now,
            stall_seconds=900,
            latest_reply=lambda r, s: "결과가 성공적으로 생성되었습니다.",
        )

        self.assertEqual(len(changes), 1)
        tid, upd = changes[0]
        self.assertEqual(tid, task["id"])
        self.assertEqual(upd["status"], "done")
        self.assertIn("결과가 성공적으로 생성되었습니다", upd["resultPreview"])

    def test_reconcile_tasks_interrupted_reply_failed(self):
        """유예 시간 경과 후 DB 답변이 중단 문구이면 done이 아니라 failed로 판정한다."""
        now = time.time()
        task = master_tasks.create_task(
            room_id="room-int",
            instruction="테스트",
        )
        task["status"] = "running"
        task["updatedAt"] = now - 120

        changes = master_supervisor.reconcile_tasks(
            [task],
            live_runs=[],
            now=now,
            stall_seconds=900,
            latest_reply=lambda r, s: "⚠️ 서버 재시작으로 답변이 중단되었습니다.",
        )

        self.assertEqual(len(changes), 1)
        tid, upd = changes[0]
        self.assertEqual(tid, task["id"])
        self.assertEqual(upd["status"], "failed")


if __name__ == "__main__":
    unittest.main()
