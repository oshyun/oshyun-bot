"""tests/test_zombie_reaper.py — 고아 세션 능동 회수(Zombie Session Reaper) 및 시작 시 복구 검증 테스트."""

from __future__ import annotations

import asyncio
import os
import time
import unittest
from unittest.mock import MagicMock, patch

import chat_db
import web_execution


class TestZombieSessionReaper(unittest.IsolatedAsyncioTestCase):
    """비정상 종료 프로세스 및 타임아웃 고아 세션 회수 검증."""

    def setUp(self) -> None:
        # 테스트 전 실행 레지스트리 상태 초기화
        web_execution._room_live_snapshots.clear()
        web_execution._active_agent_pids.clear()
        web_execution._active_watch_tasks.clear()
        web_execution._room_live_feeds.clear()

    def tearDown(self) -> None:
        web_execution._room_live_snapshots.clear()
        web_execution._active_agent_pids.clear()
        web_execution._active_watch_tasks.clear()
        web_execution._room_live_feeds.clear()

    async def test_reap_dead_pid_session(self) -> None:
        """실제 프로세스가 사망(존재하지 않는 PID)한 세션을 능동 감지하여 회수하는지 검증."""
        fake_room_id = "test-room-dead-pid"
        fake_pid = 99999999

        mock_db = MagicMock()
        mock_db.get_messages.return_value = [{"id": 42, "role": "assistant", "text": ""}]

        web_execution._room_live_snapshots[fake_room_id] = {
            "roomId": fake_room_id,
            "asstMsgId": 42,
            "pid": fake_pid,
            "startTimestamp": time.time() - 60.0,
            "startedAt": int((time.time() - 60.0) * 1000),
            "feed": "실행 중…",
            "streamText": "부분 응답 내용",
        }
        web_execution._active_agent_pids[fake_room_id] = fake_pid

        with patch("web_execution._get_mod_attr") as mock_attr:
            def side_effect(name, fallback):
                if name == "chat_db":
                    return mock_db
                if name == "detached_pid_alive":
                    return lambda p: False
                if name == "kill_job":
                    return MagicMock()
                return fallback
            mock_attr.side_effect = side_effect

            with patch("web_execution.detached_pid_alive", return_value=False):
                reaped = await web_execution.reap_zombie_sessions(max_inactive_seconds=900, max_elapsed_seconds=1800)

        self.assertIn(fake_room_id, reaped)
        self.assertNotIn(fake_room_id, web_execution._room_live_snapshots)
        self.assertNotIn(fake_room_id, web_execution._active_agent_pids)

        mock_db.update_message.assert_called_once()
        args, kwargs = mock_db.update_message.call_args
        self.assertEqual(args[0], 42)
        updated_text = kwargs.get("text", "")
        self.assertIn("부분 응답 내용", updated_text)
        self.assertIn("⚠️ 비정상 종료 감지 및 자동 회수되었습니다", updated_text)

    async def test_reap_inactive_timeout_session(self) -> None:
        """PID는 살아있으나 15분 이상 아무런 파일/로그 갱신이 없는 세션을 무응답 고아로 판정하여 회수."""
        fake_room_id = "test-room-inactive"
        fake_pid = 12345
        now = time.time()
        start_ts = now - 1000.0  # 1000초 전 시작

        mock_db = MagicMock()

        web_execution._room_live_snapshots[fake_room_id] = {
            "roomId": fake_room_id,
            "asstMsgId": 99,
            "pid": fake_pid,
            "startTimestamp": start_ts,
            "startedAt": int(start_ts * 1000),
            "feed": "도구 실행 중…",
            "streamText": "",
        }
        web_execution._active_agent_pids[fake_room_id] = fake_pid

        mock_kill = MagicMock()
        with patch("web_execution._get_mod_attr") as mock_attr:
            def side_effect(name, fallback):
                if name == "chat_db":
                    return mock_db
                if name == "detached_pid_alive":
                    return lambda p: True
                if name == "kill_job":
                    return mock_kill
                return fallback
            mock_attr.side_effect = side_effect

            with patch("web_execution.detached_pid_alive", return_value=True):
                # 900초 이상 비활동 세션 회수
                reaped = await web_execution.reap_zombie_sessions(max_inactive_seconds=900, max_elapsed_seconds=3600)

        self.assertIn(fake_room_id, reaped)
        self.assertNotIn(fake_room_id, web_execution._room_live_snapshots)
        mock_kill.assert_called_once_with(fake_pid)

        mock_db.update_message.assert_called_once()
        _, kwargs = mock_db.update_message.call_args
        self.assertIn("15분 무응답 타임아웃", kwargs.get("text", ""))

    async def test_reap_max_elapsed_timeout_session(self) -> None:
        """총 실행 시간이 30분(1800초)을 초과한 세션을 최대 실행 시간 초과로 회수."""
        fake_room_id = "test-room-max-elapsed"
        fake_pid = 54321
        now = time.time()
        start_ts = now - 2000.0  # 2000초 경과

        mock_db = MagicMock()

        web_execution._room_live_snapshots[fake_room_id] = {
            "roomId": fake_room_id,
            "asstMsgId": 77,
            "pid": fake_pid,
            "startTimestamp": start_ts,
            "startedAt": int(start_ts * 1000),
            "feed": "실행 중…",
        }
        web_execution._active_agent_pids[fake_room_id] = fake_pid

        with patch("web_execution._get_mod_attr") as mock_attr:
            def side_effect(name, fallback):
                if name == "chat_db":
                    return mock_db
                if name == "detached_pid_alive":
                    return lambda p: True
                return fallback
            mock_attr.side_effect = side_effect

            with patch("web_execution.detached_pid_alive", return_value=True):
                reaped = await web_execution.reap_zombie_sessions(max_inactive_seconds=3600, max_elapsed_seconds=1800)

        self.assertIn(fake_room_id, reaped)
        self.assertNotIn(fake_room_id, web_execution._room_live_snapshots)

        mock_db.update_message.assert_called_once()
        _, kwargs = mock_db.update_message.call_args
        self.assertIn("최대 실행 시간 초과", kwargs.get("text", ""))

    async def test_recover_startup_placeholders(self) -> None:
        """서버 기동 시 web_jobs에 없지만 DB에 빈 플레이스홀더로 남아있던 고아 메시지가 일괄 복구되는지 검증."""
        mock_db = MagicMock()
        mock_db.get_rooms.return_value = [
            {"id": "room-orphan-1"},
            {"id": "room-normal-2"},
        ]
        # room-orphan-1: 마지막 메시지가 빈 어시스턴트 메시지
        # room-normal-2: 이미 정상 텍스트가 있는 메시지
        mock_db.get_messages.side_effect = lambda rid, limit=5: (
            [{"id": 101, "role": "assistant", "text": ""}] if rid == "room-orphan-1"
            else [{"id": 102, "role": "assistant", "text": "정상 응답입니다."}]
        )

        with patch("web_execution._get_mod_attr") as mock_attr:
            def side_effect(name, fallback):
                if name == "chat_db":
                    return mock_db
                return fallback
            mock_attr.side_effect = side_effect

            fixed = await web_execution._recover_startup_placeholders()

        self.assertEqual(fixed, 1)
        mock_db.update_message.assert_called_once_with(
            101,
            text="⚠️ 서버 재시작으로 답변이 중단되었습니다.",
            update_preview=True,
        )


if __name__ == "__main__":
    unittest.main()
