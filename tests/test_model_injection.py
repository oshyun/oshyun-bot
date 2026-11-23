"""test_model_injection — 웹 및 에이전트 엔진의 모델/Effort 주입 무결성 검증 테스트."""

from __future__ import annotations

import asyncio
import copy
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC_DIR = os.path.join(_REPO_ROOT, "src", "bot")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

import bot.engine.agent_engine as agent_engine
import bot.core.bot_logger as bot_logger
import bot.core.chat_db as chat_db
import bot.engine.engine_settings as engine_settings
import bot.master.master_tasks as master_tasks
import bot.engine.stream_parser as stream_parser
import bot.web.execution as web_execution
import bot.web.server as web_server


class TestModelInjection(unittest.TestCase):
    """에이전트 호출 및 웹 세션에서 모델과 추론 강도(effort)가 정확히 주입되는지 검증."""

    def setUp(self):
        self._test_dir = tempfile.mkdtemp()
        self._orig_settings = copy.deepcopy(engine_settings._settings)
        jobs_dir = os.path.join(self._test_dir, "jobs")
        os.makedirs(jobs_dir, exist_ok=True)
        self._isolation_patchers = [
            patch(
                "engine_settings._SETTINGS_FILE",
                os.path.join(self._test_dir, "bot_settings.json"),
            ),
            patch(
                "stream_parser._SESSION_LOG_FILE",
                os.path.join(self._test_dir, "session_log.jsonl"),
            ),
            patch(
                "bot_logger.BOT_LOG_PATH",
                os.path.join(self._test_dir, "bot-log.md"),
            ),
            patch("bot.core.bot_logger._run_vault_log", AsyncMock()),
            patch(
                "master_tasks.MASTER_TASKS_FILE",
                os.path.join(self._test_dir, "master_tasks.json"),
            ),
            patch(
                "master_memory.MASTER_MEMORY_FILE",
                os.path.join(self._test_dir, "master_memory.json"),
            ),
            patch.object(web_execution, "JOBS_DIR", jobs_dir),
        ]
        for p in self._isolation_patchers:
            p.start()

        self._test_db = os.path.join(self._test_dir, "test_chat.db")
        chat_db.init_db(self._test_db, migrate_legacy=False)
        self._orig_chat_db_file = chat_db.CHAT_DB_FILE
        chat_db.CHAT_DB_FILE = self._test_db

    def tearDown(self):
        chat_db.CHAT_DB_FILE = self._orig_chat_db_file
        for p in reversed(self._isolation_patchers):
            p.stop()
        engine_settings._settings.clear()
        engine_settings._settings.update(self._orig_settings)
        if os.path.exists(engine_settings._SETTINGS_FILE):
            engine_settings._sync_from_disk()
        shutil.rmtree(self._test_dir, ignore_errors=True)

    def test_build_invocation_injects_model_and_effort_when_omitted(self):
        """model과 effort가 빈 문자열/None으로 전달되어도 get_model()과 get_effort()가 주입된다."""
        agent_engine.set_engine("agy")
        agent_engine.set_model("gemini-3.8-flash")
        agent_engine.set_effort("medium")

        invocation = agent_engine._build_invocation(
            "테스트 질문",
            None,
            workdir=_REPO_ROOT,
            model="",
            effort=None,
            extra_args=None,
            slash_commands=False,
            sandbox=False,
            coding=False,
        )
        cmd = invocation["cmd"]
        self.assertIn("--model", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "gemini-3.8-flash")
        self.assertIn("--effort", cmd)
        self.assertEqual(cmd[cmd.index("--effort") + 1], "medium")

    def test_build_invocation_with_explicit_model_and_effort(self):
        """명시적으로 지정된 모델과 effort가 build_cmd에 올바르게 전달된다."""
        agent_engine.set_engine("agy")
        invocation = agent_engine._build_invocation(
            "테스트 질문",
            None,
            workdir=_REPO_ROOT,
            model="claude-sonnet-5-5",
            effort="high",
            extra_args=None,
            slash_commands=False,
            sandbox=False,
            coding=False,
        )
        cmd = invocation["cmd"]
        self.assertIn("--model", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "claude-sonnet-5-5")
        self.assertIn("--effort", cmd)
        self.assertEqual(cmd[cmd.index("--effort") + 1], "high")

    def test_legacy_agy_model_alias_resolution(self):
        """구버전 AGY 모델명(claude-sonnet-4-6 등)이 최신 모델명으로 자동 매핑된다."""
        agent_engine.set_engine("agy")
        invocation = agent_engine._build_invocation(
            "테스트 질문",
            None,
            workdir=_REPO_ROOT,
            model="claude-sonnet-4-6",
            effort="medium",
            extra_args=None,
            slash_commands=False,
            sandbox=False,
            coding=False,
        )
        cmd = invocation["cmd"]
        self.assertIn("--model", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "claude-sonnet-5-5")

    def test_set_model_validates_against_engine_candidates(self):
        """현재 엔진에서 지원하지 않는 엉뚱한 모델은 set_model에서 무시된다."""
        agent_engine.set_engine("agy")
        agent_engine.set_model("gemini-3.8-flash")
        # 지원되지 않는 임의의 모델명 입력
        agent_engine.set_model("nonexistent-invalid-model-xyz")
        # 여전히 기존 모델 유지
        self.assertEqual(agent_engine.get_model(), "gemini-3.8-flash")

    def test_execute_chat_turn_passes_model_and_effort_to_spawn_agent(self):
        """web_server.execute_chat_turn 실행 시 spawn_agent에 model과 effort가 주입된다."""
        agent_engine.set_engine("agy")
        agent_engine.set_model("gemini-4-argon")
        agent_engine.set_effort("high")

        mock_proc = MagicMock()
        mock_proc.pid = 99999

        captured_kwargs = {}

        def fake_spawn_agent(prompt, session_id, **kwargs):
            captured_kwargs.update(kwargs)
            return mock_proc

        async def run_turn():
            with patch("bot.web.server.spawn_agent", side_effect=fake_spawn_agent), \
                 patch("bot.web.server.watch_agent", new_callable=AsyncMock) as mock_watch, \
                 patch("bot.web.server.chat_db.add_message", return_value=123), \
                 patch("bot.web.server.chat_db.update_message"):
                mock_watch.return_value = {"result": "응답 완료", "session_id": "sess-new"}
                await web_server.execute_chat_turn(
                    "안녕",
                    False,
                    room_id="test-room-1",
                    channel="web",
                )

        asyncio.run(run_turn())

        self.assertIn("model", captured_kwargs)
        self.assertEqual(captured_kwargs["model"], "gemini-4-argon")
        self.assertIn("effort", captured_kwargs)
        self.assertEqual(captured_kwargs["effort"], "high")

    def test_room_level_model_and_effort_precedence(self):
        """방에 설정된 model과 effort가 전역 기본값보다 우선하여 spawn_agent에 주입된다."""
        agent_engine.set_engine("agy")
        agent_engine.set_model("gemini-3.8-flash")
        agent_engine.set_effort("medium")

        mock_proc = MagicMock()
        mock_proc.pid = 88888

        captured_kwargs = {}

        def fake_spawn_agent(prompt, session_id, **kwargs):
            captured_kwargs.update(kwargs)
            return mock_proc

        async def run_turn():
            fake_room = {
                "id": "room-custom-cfg",
                "title": "커스텀 방",
                "model": "claude-sonnet-5-5",
                "effort": "max",
            }
            with patch("bot.web.server.spawn_agent", side_effect=fake_spawn_agent), \
                 patch("bot.web.server.watch_agent", new_callable=AsyncMock) as mock_watch, \
                 patch("bot.web.server.chat_db.get_room", return_value=fake_room), \
                 patch("bot.web.server.chat_db.add_message", return_value=124), \
                 patch("bot.web.server.chat_db.update_message") as mock_update_msg:
                mock_watch.return_value = {"result": "응답 완료", "session_id": "sess-custom"}
                await web_server.execute_chat_turn(
                    "심층 분석해줘",
                    False,
                    room_id="room-custom-cfg",
                    channel="web",
                )
                # update_message 에도 방의 active_model 및 active_effort 가 보존되는지 검증
                mock_update_msg.assert_called()
                _, update_kwargs = mock_update_msg.call_args
                self.assertEqual(update_kwargs.get("model"), "claude-sonnet-5-5")
                self.assertEqual(update_kwargs.get("effort"), "max")

        asyncio.run(run_turn())

        self.assertEqual(captured_kwargs.get("model"), "claude-sonnet-5-5")
        self.assertEqual(captured_kwargs.get("effort"), "max")

    def test_handle_api_room_settings(self):
        """POST /api/chat/room/settings 엔드포인트가 방의 모델과 effort를 DB에 갱신한다."""
        from aiohttp import web as aio_web

        async def run_test():
            mock_request = MagicMock()
            mock_request.json = AsyncMock(return_value={
                "roomId": "room-target",
                "model": "gemini-4-argon",
                "effort": "high",
            })

            with patch("bot.web.server.chat_db.update_room") as mock_update_room, \
                 patch("bot.web.server.broadcast_ws_json", new_callable=AsyncMock) as mock_broadcast:
                resp = await web_server.handle_api_room_settings(mock_request)
                self.assertEqual(resp.status, 200)
                mock_update_room.assert_called_once_with(
                    room_id="room-target",
                    session_id=None,
                    model="gemini-4-argon",
                    effort="high",
                )
                mock_broadcast.assert_called()
                broadcast_data = mock_broadcast.call_args[0][0]
                self.assertEqual(broadcast_data.get("type"), "room_settings_updated")
                self.assertEqual(broadcast_data.get("roomId"), "room-target")
                self.assertEqual(broadcast_data.get("model"), "gemini-4-argon")
                self.assertEqual(broadcast_data.get("effort"), "high")

        asyncio.run(run_test())

    def test_handle_api_room_settings_with_workdir(self):
        """POST /api/chat/room/settings 엔드포인트가 방의 workdir를 DB에 갱신하고 브로드캐스트한다."""
        async def run_test():
            mock_request = MagicMock()
            mock_request.json = AsyncMock(return_value={
                "roomId": "room-target-wd",
                "workdir": "/custom/path/target",
            })

            with patch("bot.web.server.chat_db.update_room") as mock_update_room, \
                 patch("bot.web.server.broadcast_ws_json", new_callable=AsyncMock) as mock_broadcast:
                resp = await web_server.handle_api_room_settings(mock_request)
                self.assertEqual(resp.status, 200)
                mock_update_room.assert_called_once_with(
                    room_id="room-target-wd",
                    session_id=None,
                    model=None,
                    effort=None,
                    workdir="/custom/path/target",
                )
                mock_broadcast.assert_called()
                broadcast_data = mock_broadcast.call_args[0][0]
                self.assertEqual(broadcast_data.get("type"), "room_settings_updated")
                self.assertEqual(broadcast_data.get("roomId"), "room-target-wd")
                self.assertEqual(broadcast_data.get("workdir"), "/custom/path/target")

        asyncio.run(run_test())

    def test_orchestrator_dispatch_with_dynamic_effort(self):
        """마스터 에이전트의 dispatch 요청 시 effort 및 model이 대상 방 및 턴에 주입된다."""
        async def run_test():
            mock_request = MagicMock()
            mock_request.json = AsyncMock(return_value={
                "action": "send",
                "roomId": "sub-room-1",
                "text": "복잡한 알고리즘을 분석해줘",
                "model": "claude-opus-5-5",
                "effort": "high",
                "wait": False,
            })

            with patch("bot.web.server._resolve_orchestrator_target_room", return_value={"id": "sub-room-1", "title": "서브 룸"}), \
                 patch("bot.master.master_tasks.create_task", return_value={"id": "mt-mock-effort"}) as mock_create_task, \
                 patch("bot.web.server.chat_db.update_room") as mock_update_room, \
                 patch("bot.web.server.enqueue_room_turn", new_callable=AsyncMock) as mock_enqueue, \
                 patch("bot.web.server.broadcast_ws_json", new_callable=AsyncMock):
                mock_enqueue.return_value = {"already_running": False, "queue_size": 0}
                resp = await web_server.handle_orchestrator_dispatch(mock_request)
                self.assertEqual(resp.status, 200)
                mock_create_task.assert_called_once()

                # 방 모델/effort DB 갱신 확인
                mock_update_room.assert_called_with(
                    "sub-room-1",
                    model="claude-opus-5-5",
                    effort="high",
                )

                # 턴 큐에 model과 effort 주입 확인
                mock_enqueue.assert_called_once()
                _, enqueue_kwargs = mock_enqueue.call_args
                self.assertEqual(enqueue_kwargs.get("model"), "claude-opus-5-5")
                self.assertEqual(enqueue_kwargs.get("effort"), "high")

        asyncio.run(run_test())

    def test_is_quota_exhausted_error(self):
        """모델 쿼터 소진 오류 판별 검증."""
        self.assertTrue(
            agent_engine.is_quota_exhausted_error(
                "⚠️ Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 123h54m59s."
            )
        )
        self.assertTrue(agent_engine.is_quota_exhausted_error("RESOURCE_EXHAUSTED: quota exceeded"))
        self.assertFalse(agent_engine.is_quota_exhausted_error("정상 답변입니다."))
        self.assertFalse(agent_engine.is_quota_exhausted_error(""))
        self.assertFalse(agent_engine.is_quota_exhausted_error(None))

    def test_execute_chat_turn_quota_fallback_to_default_model(self):
        """쿼터 소진 시 기본 AGY 모델로 전환하고 기존 세션을 유지한 채 재실행한다."""
        agent_engine.set_engine("agy")
        agent_engine.set_model("claude-sonnet-5-5")
        agent_engine.set_effort("medium")

        spawn_calls = []

        def fake_spawn_agent(prompt, session_id, **kwargs):
            spawn_calls.append({"session_id": session_id, "model": kwargs.get("model")})
            proc = MagicMock()
            proc.pid = 70000 + len(spawn_calls)
            return proc

        async def run_turn():
            fake_room = {
                "id": "room-quota-test",
                "title": "쿼터 폴백 방",
                "model": "claude-sonnet-5-5",
                "effort": "medium",
            }
            with patch("bot.web.server.spawn_agent", side_effect=fake_spawn_agent), \
                 patch("bot.web.server.watch_agent", new_callable=AsyncMock) as mock_watch, \
                 patch("bot.web.server.chat_db.get_room", return_value=fake_room), \
                 patch("bot.web.server.chat_db.update_room") as mock_update_room, \
                 patch("bot.web.server.chat_db.add_message", return_value=500), \
                 patch("bot.web.server.chat_db.update_message") as mock_update_msg, \
                 patch("bot.web.server.broadcast_ws_json", new_callable=AsyncMock):
                mock_watch.side_effect = [
                    {
                        "result": "⚠️ Individual quota reached. Resets in 123h.",
                        "session_id": "sess-keep-1",
                        "model": "claude-sonnet-5-5",
                    },
                    {
                        "result": "기본 모델로 정상 응답했습니다.",
                        "session_id": "sess-keep-1",
                        "model": agent_engine.AGY_DEFAULT_MODEL,
                    },
                ]
                res = await web_server.execute_chat_turn(
                    "안녕",
                    False,
                    client_session_id="sess-keep-1",
                    room_id="room-quota-test",
                    channel="web",
                )
                self.assertEqual(res["reply"], "기본 모델로 정상 응답했습니다.")
                mock_update_room.assert_any_call(
                    "room-quota-test", model=agent_engine.AGY_DEFAULT_MODEL
                )
                _, update_kwargs = mock_update_msg.call_args
                self.assertEqual(update_kwargs.get("text"), "기본 모델로 정상 응답했습니다.")
                self.assertEqual(update_kwargs.get("model"), agent_engine.AGY_DEFAULT_MODEL)

        asyncio.run(run_turn())

        self.assertEqual(
            spawn_calls,
            [
                {"session_id": "sess-keep-1", "model": "claude-sonnet-5-5"},
                {"session_id": "sess-keep-1", "model": agent_engine.AGY_DEFAULT_MODEL},
            ],
        )
        self.assertEqual(agent_engine.get_model(), agent_engine.AGY_DEFAULT_MODEL)

    def test_dispatch_spawn_passes_parent_and_skill_flags(self):
        """spawn 시 parent='__core__'와 rawPrompt/slashCommands/orchestrator 옵션이 전달되는지 검증."""
        async def run_test():
            mock_request = MagicMock()
            mock_request.json = AsyncMock(return_value={
                "action": "spawn",
                "parent": "__core__",
                "title": "메모리 리밸런싱",
                "prompt": "/memory-rebalance",
                "model": "gemini-3.8-flash",
                "effort": "low",
                "forceNew": True,
                "rawPrompt": True,
                "slashCommands": True,
                "orchestrator": True,
                "wait": False,
            })

            with patch("bot.web.server.chat_db.get_or_create_room") as mock_create_room, \
                 patch("bot.web.server.chat_db.add_message", return_value=701), \
                 patch("bot.master.master_tasks.create_task", return_value={"id": "mt-rebalance"}), \
                 patch("bot.web.server.enqueue_room_turn", new_callable=AsyncMock) as mock_enqueue, \
                 patch("bot.web.server.broadcast_ws_json", new_callable=AsyncMock) as mock_broadcast:
                mock_enqueue.return_value = {"already_running": False, "queue_size": 0}
                resp = await web_server.handle_orchestrator_dispatch(mock_request)
                self.assertEqual(resp.status, 200)

                self.assertGreaterEqual(mock_create_room.call_count, 1)
                _, room_kwargs = mock_create_room.call_args_list[0]
                self.assertEqual(room_kwargs.get("parent_agent_id"), "__core__")

                mock_enqueue.assert_called_once()
                _, enqueue_kwargs = mock_enqueue.call_args
                self.assertEqual(enqueue_kwargs.get("channel"), "orchestrator")
                self.assertTrue(enqueue_kwargs.get("raw_prompt"))
                self.assertTrue(enqueue_kwargs.get("slash_commands"))
                self.assertTrue(enqueue_kwargs.get("orchestrator"))
                self.assertEqual(enqueue_kwargs.get("model"), "gemini-3.8-flash")
                self.assertEqual(enqueue_kwargs.get("effort"), "low")

                broadcast_types = [c.args[0].get("type") for c in mock_broadcast.call_args_list]
                self.assertIn("room_created", broadcast_types)
                self.assertIn("orchestrator_dispatch", broadcast_types)
                for call in mock_broadcast.call_args_list:
                    evt = call.args[0]
                    if evt.get("type") == "room_created":
                        self.assertEqual(evt["room"].get("parentAgentId"), "__core__")
                    elif evt.get("type") == "orchestrator_dispatch":
                        self.assertEqual(evt.get("parentAgentId"), "__core__")

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
