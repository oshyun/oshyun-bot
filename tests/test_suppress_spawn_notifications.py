"""test_suppress_spawn_notifications — 상위 에이전트 스폰 안내 알림 억제 및 알림 레벨 시스템 구분 단위 테스트.

1. 상위 에이전트가 하위 작업을 스폰/위임하고 남긴 중간 안내 -> delegation_ack, notifyLevel: silent, silentNotification: True
2. 스폰 없는 일반 직접 응답 -> direct_answer, notifyLevel: alert, silentNotification: False
3. 내부 오케스트레이션 보고(task_report, master_patrol 등) -> internal_report, notifyLevel: silent, silentNotification: True
4. 하위 작업 최종 완료 시 부모 방 바이패스 보고 -> delegation_result, notifyLevel: silent, silentNotification: True (내부 오케스트레이션 알림 억제)
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from bot.web.agent_reporting import _notify_master_agent


class TestSuppressSpawnNotifications(unittest.IsolatedAsyncioTestCase):
    """오케스트레이터 알림 시스템 구분 및 불필요 알림 억제 검증."""

    async def test_delegation_ack_marked_silent_when_steps_contain_spawn(self):
        """하위 세션 스폰 도구 호출이 포함된 상위 에이전트 턴은 delegation_ack / silent 처리되어 알림이 억제된다."""
        from bot.web.turn_execution import execute_chat_turn

        broadcast_mock = AsyncMock()

        mock_db = MagicMock()
        mock_db.get_room.return_value = {
            "id": "__core__",
            "title": "마스터 에이전트",
            "parentAgentId": None,
            "isOrchestratorChild": False,
        }
        mock_db.get_rooms.return_value = [
            {"id": "room-12345", "title": "화장품 전문가 하위 채팅 버그 분석"}
        ]
        mock_db.add_message.return_value = 101

        with patch("bot.web.runtime_state.broadcast_ws_json", broadcast_mock), \
             patch("bot.web.turn_execution.broadcast_ws_json", broadcast_mock), \
             patch("bot.web.turn_execution.chat_db", mock_db), \
             patch("bot.web.rooms.chat_db", mock_db), \
             patch("bot.web.rooms._append_room_message", return_value=101), \
             patch("bot.web.turn_execution.spawn_agent") as mock_spawn, \
             patch("bot.web.turn_execution.watch_agent") as mock_watch, \
             patch("bot.web.turn_execution._run_stream_tail_loop", new_callable=AsyncMock), \
             patch("bot.web.turn_execution._finalize_orchestrator_completion", new_callable=AsyncMock), \
             patch("bot.web.orchestrator.extract_delegated_subagents") as mock_extract:

            mock_proc = MagicMock()
            mock_proc.pid = 99999
            mock_spawn.return_value = mock_proc
            mock_watch.return_value = {
                "text": "봇개발 전문가에게 작업을 위임했습니다. 완료되는 대로 보고드리겠습니다.",
                "model": "gemini-3.8-flash",
                "effort": "medium",
                "cost": 0.001,
            }
            mock_extract.return_value = [{
                "roomId": "room-12345",
                "title": "화장품 전문가 하위 채팅 버그 분석",
                "status": "running",
                "failed": False,
            }]

            res = await execute_chat_turn(
                user_text="화장품 전문가 버그 고쳐줘",
                is_voice_mode=False,
                room_id="__core__",
                client_msg_id="client-test-1",
            )
            self.assertTrue(res["ok"])

            # 브로드캐스트된 result 이벤트 검증
            broadcast_calls = [call.args[0] for call in broadcast_mock.call_args_list if call.args]
            result_evts = [e for e in broadcast_calls if isinstance(e, dict) and e.get("type") == "result"]
            self.assertTrue(len(result_evts) >= 1)
            final_res = result_evts[-1]

            self.assertEqual(final_res.get("turnType"), "delegation_ack")
            self.assertEqual(final_res.get("notifyLevel"), "silent")
            self.assertTrue(final_res.get("silentNotification"))
            self.assertIn("delegatedRooms", final_res)

    async def test_direct_answer_marked_alert(self):
        """하위 작업 위임이 없는 일반 직접 답변은 direct_answer / alert 레벨로 정상 발송된다."""
        from bot.web.turn_execution import execute_chat_turn

        broadcast_mock = AsyncMock()

        mock_db = MagicMock()
        mock_db.get_room.return_value = {"id": "__core__", "title": "마스터 에이전트"}
        mock_db.get_rooms.return_value = []
        mock_db.add_message.return_value = 102

        with patch("bot.web.runtime_state.broadcast_ws_json", broadcast_mock), \
             patch("bot.web.turn_execution.broadcast_ws_json", broadcast_mock), \
             patch("bot.web.turn_execution.chat_db", mock_db), \
             patch("bot.web.rooms.chat_db", mock_db), \
             patch("bot.web.rooms._append_room_message", return_value=102), \
             patch("bot.web.turn_execution.spawn_agent") as mock_spawn, \
             patch("bot.web.turn_execution.watch_agent") as mock_watch, \
             patch("bot.web.turn_execution._run_stream_tail_loop", new_callable=AsyncMock), \
             patch("bot.web.turn_execution._finalize_orchestrator_completion", new_callable=AsyncMock):

            mock_proc = MagicMock()
            mock_proc.pid = 99998
            mock_spawn.return_value = mock_proc
            mock_watch.return_value = {
                "text": "오늘 서울 날씨는 맑음이며 기온은 15도입니다.",
                "model": "gemini-3.8-flash",
                "effort": "medium",
                "cost": 0.0005,
            }

            res = await execute_chat_turn(
                user_text="오늘 날씨 어때?",
                is_voice_mode=False,
                room_id="__core__",
                client_msg_id="client-test-2",
            )
            self.assertTrue(res["ok"])

            broadcast_calls = [call.args[0] for call in broadcast_mock.call_args_list if call.args]
            result_evts = [e for e in broadcast_calls if isinstance(e, dict) and e.get("type") == "result"]
            self.assertTrue(len(result_evts) >= 1)
            final_res = result_evts[-1]

            self.assertEqual(final_res.get("turnType"), "direct_answer")
            self.assertEqual(final_res.get("notifyLevel"), "alert")
            self.assertFalse(final_res.get("silentNotification"))

    async def test_internal_report_channel_marked_silent(self):
        """channel이 task_report 또는 master_patrol인 내부 턴은 internal_report / silent 처리된다."""
        from bot.web.turn_execution import execute_chat_turn

        broadcast_mock = AsyncMock()

        mock_db = MagicMock()
        mock_db.get_room.return_value = {"id": "__core__", "title": "마스터 에이전트"}
        mock_db.get_rooms.return_value = []
        mock_db.add_message.return_value = 103

        with patch("bot.web.runtime_state.broadcast_ws_json", broadcast_mock), \
             patch("bot.web.turn_execution.broadcast_ws_json", broadcast_mock), \
             patch("bot.web.turn_execution.chat_db", mock_db), \
             patch("bot.web.rooms.chat_db", mock_db), \
             patch("bot.web.rooms._append_room_message", return_value=102), \
             patch("bot.web.turn_execution.spawn_agent") as mock_spawn, \
             patch("bot.web.turn_execution.watch_agent") as mock_watch, \
             patch("bot.web.turn_execution._run_stream_tail_loop", new_callable=AsyncMock), \
             patch("bot.web.turn_execution._finalize_orchestrator_completion", new_callable=AsyncMock):

            mock_proc = MagicMock()
            mock_proc.pid = 99997
            mock_spawn.return_value = mock_proc
            mock_watch.return_value = {
                "text": "내부 상태 점검 완료",
                "model": "gemini-3.8-flash",
            }

            res = await execute_chat_turn(
                user_text="[자동 보고] 상태 보고",
                is_voice_mode=False,
                room_id="__core__",
                client_msg_id="client-test-3",
                channel="task_report",
            )
            self.assertTrue(res["ok"])

            broadcast_calls = [call.args[0] for call in broadcast_mock.call_args_list if call.args]
            result_evts = [e for e in broadcast_calls if isinstance(e, dict) and e.get("type") == "result"]
            self.assertTrue(len(result_evts) >= 1)
            final_res = result_evts[-1]

            self.assertEqual(final_res.get("turnType"), "internal_report")
            self.assertEqual(final_res.get("notifyLevel"), "silent")
            self.assertTrue(final_res.get("silentNotification"))

    async def test_notify_master_agent_bypass_marked_delegation_result_silent(self):
        """하위 작업이 완료되어 부모 방으로 바이패스 보고될 때는 delegation_result / silent 레벨로 중복/내부 알림이 억제된다."""
        broadcast_mock = AsyncMock()

        mock_db = MagicMock()
        mock_db.get_room.return_value = {
            "id": "room-child-1",
            "title": "화장품 버그 분석",
            "parentAgentId": "__core__",
            "isOrchestratorChild": True,
        }
        mock_db.add_message.return_value = 201

        with patch("bot.web.runtime_state.broadcast_ws_json", broadcast_mock), \
             patch("bot.web.agent_reporting.broadcast_ws_json", broadcast_mock), \
             patch("bot.web.agent_reporting.chat_db", mock_db), \
             patch("bot.master.master_tasks.claim_report", return_value=True):

            await _notify_master_agent(
                room_id="room-child-1",
                task_id="task-123",
                result_text="버그 분석 완료: 정적 규칙 복사 로직을 수정했습니다.",
                is_success=True,
            )

            broadcast_calls = [call.args[0] for call in broadcast_mock.call_args_list if call.args]
            result_evts = [e for e in broadcast_calls if isinstance(e, dict) and e.get("type") == "result"]
            self.assertEqual(len(result_evts), 1)
            bypass_res = result_evts[0]

            self.assertEqual(bypass_res.get("roomId"), "__core__")
            self.assertEqual(bypass_res.get("bypassedFromRoomId"), "room-child-1")
            self.assertEqual(bypass_res.get("turnType"), "delegation_result")
            self.assertEqual(bypass_res.get("notifyLevel"), "silent")
            self.assertTrue(bypass_res.get("silentNotification"))
            self.assertEqual(bypass_res.get("subagentTitle"), "화장품 버그 분석")
            self.assertTrue(bypass_res.get("isSuccess"))

    async def test_notify_master_agent_routes_to_origin_room_id(self):
        """parentAgentId가 __expert_coding__이더라도 originRoomId가 __core__이면 완료 보고가 __core__로 전달된다."""
        broadcast_mock = AsyncMock()

        mock_db = MagicMock()
        mock_db.get_room.return_value = {
            "id": "room-coding-sub-1",
            "title": "스크롤 락 구현",
            "parentAgentId": "__expert_coding__",
            "isOrchestratorChild": True,
        }
        mock_db.add_message.return_value = 202

        mock_task = {
            "id": "task-coding-1",
            "originRoomId": "__core__",
            "parentAgentId": "__expert_coding__",
            "status": "queued",
        }

        with patch("bot.web.runtime_state.broadcast_ws_json", broadcast_mock), \
             patch("bot.web.agent_reporting.broadcast_ws_json", broadcast_mock), \
             patch("bot.web.agent_reporting.chat_db", mock_db), \
             patch("bot.master.master_tasks.get_task", return_value=mock_task), \
             patch("bot.master.master_tasks.claim_report", return_value=True):

            await _notify_master_agent(
                room_id="room-coding-sub-1",
                task_id="task-coding-1",
                result_text="스크롤 락 구현 완료",
                is_success=True,
            )

            broadcast_calls = [call.args[0] for call in broadcast_mock.call_args_list if call.args]
            result_evts = [e for e in broadcast_calls if isinstance(e, dict) and e.get("type") == "result"]
            self.assertEqual(len(result_evts), 1)
            bypass_res = result_evts[0]

            self.assertEqual(bypass_res.get("roomId"), "__core__")
            self.assertEqual(bypass_res.get("bypassedFromRoomId"), "room-coding-sub-1")
            self.assertEqual(bypass_res.get("turnType"), "delegation_result")


if __name__ == "__main__":
    unittest.main()
