"""test_server_on_complete.py — 서버 내장 완료 훅(on_complete) 및 멱등성(dedup) 검증."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp.test_utils import AioHTTPTestCase

import agent_engine
import web_execution
import web_orchestrator
import web_server


class TestAgentEngineWatchOnComplete(unittest.IsolatedAsyncioTestCase):
    """agent_engine.watch_agent 의 on_complete 훅 발동 검증."""

    async def test_watch_agent_invokes_async_on_complete_on_result(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_file = os.path.join(tmpdir, "out.jsonl")
            with open(out_file, "w", encoding="utf-8") as f:
                f.write(json.dumps({"type": "result", "result": "완료 응답", "session_id": "sid-1"}) + "\n")

            proc = MagicMock()
            proc.poll.return_value = 0
            proc.returncode = 0
            proc.pid = 99999

            captured: list[dict] = []

            async def on_complete(res: dict) -> None:
                captured.append(res)

            result = await agent_engine.watch_agent(
                out_file,
                pid=99999,
                start_ts=100.0,
                on_complete=on_complete,
                proc=proc,
            )

            self.assertFalse(result["interrupted"])
            self.assertEqual(result["result"], "완료 응답")
            self.assertEqual(len(captured), 1)
            self.assertEqual(captured[0]["result"], "완료 응답")

    async def test_watch_agent_invokes_on_complete_on_interrupted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_file = os.path.join(tmpdir, "out.jsonl")
            with open(out_file, "w", encoding="utf-8") as f:
                f.write("")

            proc = MagicMock()
            proc.poll.return_value = 1
            proc.returncode = 1
            proc.pid = 99998

            captured: list[dict] = []

            def on_complete_sync(res: dict) -> None:
                captured.append(res)

            result = await agent_engine.watch_agent(
                out_file,
                pid=99998,
                start_ts=100.0,
                on_complete=on_complete_sync,
                proc=proc,
            )

            self.assertTrue(result["interrupted"])
            self.assertEqual(len(captured), 1)
            self.assertTrue(captured[0]["interrupted"])


class TestWebExecutionOnComplete(unittest.IsolatedAsyncioTestCase):
    """web_execution.execute_chat_turn 의 모든 종료 경로(성공/실패/취소/재시작 분리)에서 on_complete 동작 검증."""

    def setUp(self):
        web_execution._recent_reported_rooms.clear()
        web_execution._user_cancelled_rooms.clear()
        web_execution._shutting_down = False

    def _enter_execution_patches(self, stack: ExitStack, *, spawn_mock=None, watch_mock=None):
        mock_db = MagicMock()
        mock_db.get_room.return_value = {"id": "room-sub", "title": "하위방"}
        mock_db.add_message.return_value = 10
        mock_notify = AsyncMock()
        mock_broadcast = AsyncMock()
        mock_tasks = MagicMock()
        mock_tasks.get_task.return_value = {"id": "mt-1", "wait": False}

        for mod in (web_execution, web_server):
            if spawn_mock is not None:
                stack.enter_context(patch.object(mod, "spawn_agent", spawn_mock))
            if watch_mock is not None:
                stack.enter_context(patch.object(mod, "watch_agent", watch_mock))
            stack.enter_context(patch.object(mod, "broadcast_ws_json", mock_broadcast))
            stack.enter_context(patch.object(mod, "chat_db", mock_db))
            stack.enter_context(patch.object(mod, "vault_log", MagicMock()))
        stack.enter_context(patch.object(web_execution, "_notify_master_agent", mock_notify))
        stack.enter_context(patch.object(web_execution, "master_tasks", mock_tasks))
        return mock_tasks, mock_notify, mock_db

    async def test_execute_chat_turn_normal_completion_fires_on_complete_and_notifies(self):
        captured: list[dict] = []

        async def on_complete(ev: dict) -> None:
            captured.append(ev)

        fake_proc = MagicMock()
        fake_proc.pid = 42424
        watch_mock = AsyncMock(
            return_value={"result": "정상 처리 결과", "session_id": "sid-ok", "interrupted": False}
        )

        with ExitStack() as stack:
            _, mock_notify, _ = self._enter_execution_patches(
                stack,
                spawn_mock=MagicMock(return_value=fake_proc),
                watch_mock=watch_mock,
            )
            res = await web_execution.execute_chat_turn(
                "작업 해줘",
                False,
                room_id="room-sub-1",
                channel="orchestrator",
                task_id="mt-ok-1",
                on_complete=on_complete,
            )

        self.assertTrue(res["ok"])
        self.assertEqual(len(captured), 1)
        self.assertTrue(captured[0]["ok"])
        self.assertEqual(captured[0]["status"], "done")
        self.assertEqual(captured[0]["taskId"], "mt-ok-1")
        self.assertEqual(captured[0]["reply"], "정상 처리 결과")
        mock_notify.assert_awaited_once_with("room-sub-1", "mt-ok-1", "정상 처리 결과", is_success=True)

    async def test_execute_chat_turn_spawn_failure_fires_on_complete_and_notifies(self):
        captured: list[dict] = []

        async def on_complete(ev: dict) -> None:
            captured.append(ev)

        with ExitStack() as stack:
            mock_tasks, mock_notify, _ = self._enter_execution_patches(
                stack,
                spawn_mock=MagicMock(side_effect=RuntimeError("실행 파일 없음")),
            )
            res = await web_execution.execute_chat_turn(
                "실패할 작업",
                False,
                room_id="room-sub-fail",
                channel="orchestrator",
                task_id="mt-fail-1",
                on_complete=on_complete,
            )

        self.assertFalse(res["ok"])
        self.assertEqual(len(captured), 1)
        self.assertFalse(captured[0]["ok"])
        self.assertEqual(captured[0]["status"], "failed")
        self.assertIn("실행 파일 없음", captured[0]["error"])
        mock_tasks.update_task.assert_called()
        self.assertEqual(mock_tasks.update_task.call_args.kwargs["status"], "failed")
        mock_notify.assert_awaited_once()
        self.assertFalse(mock_notify.call_args.kwargs["is_success"])

    async def test_execute_chat_turn_user_cancel_fires_on_complete_and_skips_notify(self):
        captured: list[dict] = []

        async def on_complete(ev: dict) -> None:
            captured.append(ev)

        fake_proc = MagicMock()
        fake_proc.pid = 51515

        async def cancelled_watch(*args, **kwargs):
            web_execution._user_cancelled_rooms.add("room-sub-cancel")
            raise asyncio.CancelledError()

        with ExitStack() as stack:
            _, mock_notify, _ = self._enter_execution_patches(
                stack,
                spawn_mock=MagicMock(return_value=fake_proc),
                watch_mock=AsyncMock(side_effect=cancelled_watch),
            )
            with self.assertRaises(asyncio.CancelledError):
                await web_execution.execute_chat_turn(
                    "취소할 작업",
                    False,
                    room_id="room-sub-cancel",
                    channel="orchestrator",
                    task_id="mt-cancel-1",
                    on_complete=on_complete,
                )

        self.assertEqual(len(captured), 1)
        self.assertFalse(captured[0]["ok"])
        self.assertEqual(captured[0]["status"], "cancelled")
        # 사용자가 명시적으로 중단한 작업은 상위 에이전트 강제 재실행을 막기 위해 부모 통지를 건너뛴다
        mock_notify.assert_not_called()

    async def test_execute_chat_turn_shutdown_detach_skips_on_complete(self):
        captured: list[dict] = []

        async def on_complete(ev: dict) -> None:
            captured.append(ev)

        fake_proc = MagicMock()
        fake_proc.pid = 61616

        async def shutdown_watch(*args, **kwargs):
            web_execution._shutting_down = True
            raise asyncio.CancelledError()

        try:
            with ExitStack() as stack:
                mock_tasks, mock_notify, _ = self._enter_execution_patches(
                    stack,
                    spawn_mock=MagicMock(return_value=fake_proc),
                    watch_mock=AsyncMock(side_effect=shutdown_watch),
                )
                with self.assertRaises(asyncio.CancelledError):
                    await web_execution.execute_chat_turn(
                        "서버 재시작 중 작업",
                        False,
                        room_id="room-sub-detach",
                        channel="orchestrator",
                        task_id="mt-detach-1",
                        on_complete=on_complete,
                    )

            self.assertEqual(len(captured), 0)
            mock_notify.assert_not_awaited()
            mock_tasks.update_task.assert_any_call("mt-detach-1", feed="서버 재시작 중 (백그라운드 유지)…")
        finally:
            web_execution._shutting_down = False


class TestOrchestratorNotifyDedupAndRouting(AioHTTPTestCase):
    """/api/orchestrator/notify 의 중복 보고 방지(dedup) 및 parentAgentId 라우팅 검증."""

    async def get_application(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._patchers = [
            patch("chat_db.CHAT_DB_FILE", os.path.join(self._tmp_dir.name, "chat.db")),
            patch("master_tasks.MASTER_TASKS_FILE", os.path.join(self._tmp_dir.name, "master_tasks.json")),
            patch("master_memory.MASTER_MEMORY_FILE", os.path.join(self._tmp_dir.name, "master_memory.json")),
            patch.object(web_execution, "WEB_JOBS_FILE", os.path.join(self._tmp_dir.name, "web_jobs.json")),
            patch.object(web_server, "WEB_JOBS_FILE", os.path.join(self._tmp_dir.name, "web_jobs.json")),
        ]
        for p in self._patchers:
            p.start()
        web_server.WEB_TOKEN = "test-secret-token-1234"
        web_server.AUTH_DELAY_SECONDS = 0.0
        return web_server.create_web_app(static_dir="/nonexistent")

    async def tearDownAsync(self):
        await super().tearDownAsync()
        for p in reversed(getattr(self, "_patchers", [])):
            p.stop()
        if hasattr(self, "_tmp_dir"):
            self._tmp_dir.cleanup()

    async def asyncSetUp(self):
        await super().asyncSetUp()
        web_execution._recent_reported_rooms.clear()
        cookie_val = web_server._create_session_cookie_value()
        self.client.session.cookie_jar.update_cookies(
            {web_server.AUTH_COOKIE_NAME: cookie_val}
        )

    async def test_notify_dedup_by_recent_room_without_task_id(self):
        web_execution.mark_room_completion_reported("room-dedup-no-tid")
        mock_enqueue = AsyncMock()
        with (
            patch.object(web_execution, "enqueue_room_turn", mock_enqueue),
            patch.object(web_server, "enqueue_room_turn", mock_enqueue),
        ):
            res = await self.client.post(
                "/api/orchestrator/notify",
                json={"roomId": "room-dedup-no-tid", "message": "중복 보고"},
            )
            self.assertEqual(res.status, 200)
            data = await res.json()
            self.assertTrue(data["ok"])
            self.assertEqual(data["status"], "duplicate")
            mock_enqueue.assert_not_awaited()

    async def test_notify_routes_to_explicit_parent_agent(self):
        mock_enqueue = AsyncMock(return_value={"already_running": False, "queue_size": 0})
        mock_broadcast = AsyncMock()
        with (
            patch.object(web_execution, "enqueue_room_turn", mock_enqueue),
            patch.object(web_server, "enqueue_room_turn", mock_enqueue),
            patch.object(web_execution, "broadcast_ws_json", mock_broadcast),
            patch.object(web_server, "broadcast_ws_json", mock_broadcast),
            patch.object(web_orchestrator, "master_memory"),
        ):
            res = await self.client.post(
                "/api/orchestrator/notify",
                json={
                    "roomId": "room-coding-child",
                    "parentAgentId": "__expert_coding__",
                    "message": "코딩 하위 작업 완료",
                },
            )
            self.assertEqual(res.status, 200)
            data = await res.json()
            self.assertTrue(data["ok"])
            self.assertEqual(data["target"], "__expert_coding__")
            mock_enqueue.assert_awaited_once()
            self.assertEqual(mock_enqueue.call_args.kwargs["room_id"], "__expert_coding__")
