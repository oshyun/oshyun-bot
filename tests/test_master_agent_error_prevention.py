"""test_master_agent_error_prevention — 마스터 에이전트 빈번한 응답 에러 근본 원인 재발 방지 단위 테스트."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp.test_utils import AioHTTPTestCase

import chat_db
import engine_sandbox
import master_supervisor
import master_tasks
import web_execution
import web_server


class TestMasterAgentErrorPrevention(unittest.IsolatedAsyncioTestCase):
    """마스터 에이전트(__core__) 에러 재발 방지를 위한 5대 가드레일 검증."""

    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp_dir.name, "test_chat.db")
        self.jobs_file = os.path.join(self._tmp_dir.name, "web_jobs.json")
        self.tasks_file = os.path.join(self._tmp_dir.name, "master_tasks.json")
        self.jobs_dir = os.path.join(self._tmp_dir.name, "jobs")
        os.makedirs(self.jobs_dir, exist_ok=True)
        chat_db.init_db(self.db_path, migrate_legacy=False)

        self._patchers = [
            patch("chat_db.CHAT_DB_FILE", self.db_path),
            patch("master_tasks.MASTER_TASKS_FILE", self.tasks_file),
            patch("master_memory.MASTER_MEMORY_FILE", os.path.join(self._tmp_dir.name, "master_memory.json")),
            patch.object(web_execution, "WEB_JOBS_FILE", self.jobs_file),
            patch.object(web_server, "WEB_JOBS_FILE", self.jobs_file),
            patch.object(web_execution, "JOBS_DIR", self.jobs_dir),
            patch.object(web_server, "JOBS_DIR", self.jobs_dir),
        ]
        for p in self._patchers:
            p.start()

        web_execution._room_live_snapshots.clear()
        web_execution._room_live_feeds.clear()
        web_execution._active_agent_pids.clear()
        web_execution._active_watch_tasks.clear()
        web_execution._room_queues.clear()
        web_execution._room_workers.clear()
        web_execution._user_cancelled_rooms.clear()
        web_execution.set_shutting_down(False)

    def tearDown(self) -> None:
        for p in reversed(self._patchers):
            p.stop()
        self._tmp_dir.cleanup()

    def test_sandbox_profile_permits_agy_cli_home_installation_id(self) -> None:
        """샌드박스 프로파일(sandbox.sb)과 _sandbox_prefix가 ~/.gemini/antigravity-cli 접근을 허용하는지 검증."""
        prefix = engine_sandbox._sandbox_prefix("/tmp/vault")
        joined = " ".join(prefix)
        self.assertIn("AGY_CLI_HOME=", joined)
        self.assertIn("AGY_HOME=", joined)
        self.assertIn(".gemini/antigravity-cli", joined)

        with open(engine_sandbox._PROFILE, encoding="utf-8") as f:
            sb_content = f.read()
        self.assertIn('(subpath (param "AGY_CLI_HOME"))', sb_content)
        self.assertIn('(subpath (param "AGY_HOME"))', sb_content)

    async def test_execute_chat_turn_auto_retries_once_on_empty_response(self) -> None:
        """AGY 프로세스가 에러/도구 호출 없이 빈 응답('(결과 없음)' -> '(응답 없음)')으로 종료되면 1회 자동 재시도하여 복구한다."""
        proc1 = MagicMock(pid=11111)
        proc2 = MagicMock(pid=22222)
        spawn_mock = MagicMock(side_effect=[proc1, proc2])
        watch_mock = AsyncMock(
            side_effect=[
                {"result": "(결과 없음)", "session_id": "sess-master-1", "interrupted": False},
                {"result": "재시도 후 정상 복구된 마스터 브리핑입니다.", "session_id": "sess-master-1", "interrupted": False},
            ]
        )

        with (
            patch.object(web_execution, "spawn_agent", spawn_mock),
            patch.object(web_server, "spawn_agent", spawn_mock),
            patch.object(web_execution, "watch_agent", watch_mock),
            patch.object(web_server, "watch_agent", watch_mock),
            patch.object(web_execution, "broadcast_ws_json", AsyncMock()),
            patch.object(web_server, "broadcast_ws_json", AsyncMock()),
            patch.object(web_execution, "set_session", MagicMock()),
            patch.object(web_server, "set_session", MagicMock()),
            patch.object(web_execution, "update_last_activity", MagicMock()),
            patch.object(web_server, "update_last_activity", MagicMock()),
            patch.object(web_execution, "vault_log", MagicMock()),
            patch.object(web_server, "vault_log", MagicMock()),
            patch("web_execution.extract_agy_transcript_final_response", return_value=None),
        ):
            res = await web_execution.execute_chat_turn(
                "하위 작업 상태 확인해줘",
                False,
                room_id="__core__",
                channel="task_report",
            )

        self.assertTrue(res["ok"])
        self.assertEqual(res["reply"], "재시도 후 정상 복구된 마스터 브리핑입니다.")
        self.assertEqual(spawn_mock.call_count, 2)
        self.assertEqual(watch_mock.call_count, 2)

        msgs = chat_db.get_messages("__core__", db_path=self.db_path)
        asst_msgs = [m for m in msgs if m["role"] == "assistant"]
        self.assertEqual(len(asst_msgs), 1)
        self.assertEqual(asst_msgs[0]["text"], "재시도 후 정상 복구된 마스터 브리핑입니다.")

    async def test_notify_master_agent_guards_against_magicmock_room_record(self) -> None:
        """chat_db.get_room 이 MagicMock을 반환해도 보고 메시지에 '<MagicMock' 문자열이 혼입되지 않는다."""
        mock_db = MagicMock()
        # get_room returns a default MagicMock (not a real dict)
        enqueue_mock = AsyncMock(return_value=(False, 1))

        with (
            patch.object(web_execution, "chat_db", mock_db),
            patch.object(web_server, "chat_db", mock_db),
            patch.object(web_execution, "enqueue_room_turn", enqueue_mock),
            patch.object(web_server, "enqueue_room_turn", enqueue_mock),
            patch("web_rooms._load_chat_rooms_index", return_value=[]),
        ):
            await web_execution._notify_master_agent(
                "room-test-magic",
                None,
                "하위 작업 실패 테스트",
                is_success=False,
            )

        enqueue_mock.assert_awaited_once()
        report_text = enqueue_mock.call_args.kwargs["user_text"]
        self.assertNotIn("MagicMock", report_text)
        self.assertIn("room-test-magic", report_text)
        self.assertIn("(기본값)", report_text)

    async def test_master_supervisor_wake_event_rebinds_across_event_loops(self) -> None:
        """MasterSupervisor 가 다른 이벤트 루프에서 생성되었더라도 run_forever/poke 시 루프 충돌 RuntimeError 가 발생하지 않는다."""
        host = MagicMock()
        host.snapshot.return_value = {"rooms": [], "liveRuns": []}
        host.core_busy.return_value = False
        host.broadcast = AsyncMock()

        sup = master_supervisor.MasterSupervisor(host, tick_seconds=1, interval_seconds=0)
        # 기존 _wake 에 다른 루프가 바인딩된 상황을 시뮬레이션
        sup._wake_loop = MagicMock()
        sup.poke()

        task = asyncio.create_task(sup.run_forever())
        await asyncio.sleep(0.05)
        sup.poke()
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task


class TestWebAppTestRunnerIsolation(AioHTTPTestCase):
    """unittest 환경에서 create_web_app 기동 시 백그라운드 루프(_recover_web_jobs 등)가 자동 격리되는지 검증."""

    async def get_application(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._patch_db = patch("chat_db.CHAT_DB_FILE", os.path.join(self._tmp_dir.name, "test.db"))
        self._patch_db.start()
        self._orig_auth = getattr(web_server, "WEB_AUTH_REQUIRED", True)
        web_server.WEB_AUTH_REQUIRED = False
        return web_server.create_web_app(static_dir="/nonexistent")

    async def tearDownAsync(self):
        await super().tearDownAsync()
        web_server.WEB_AUTH_REQUIRED = getattr(self, "_orig_auth", True)
        self._patch_db.stop()
        self._tmp_dir.cleanup()

    async def test_background_loops_not_started_in_unittest_by_default(self) -> None:
        self.assertIsNone(web_server._master_supervisor_task)
        self.assertIsNone(web_server._zombie_reaper_task)
        self.assertIsNone(web_server._scheduler_task)


if __name__ == "__main__":
    unittest.main()
