"""test_cancel_queue_recognition.py — 대기열 취소 및 즉시 반영 검증 단위 테스트."""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from aiohttp import WSMsgType

from bot.master import master_tasks
from bot.web import room_queue, ws, agent_reporting
from bot.web.runtime_state import (
    _active_agent_pids,
    _active_watch_tasks,
    _room_live_snapshots,
    _room_queues,
    _user_cancelled_rooms,
)


class DummyWsMsg:
    def __init__(self, data):
        self.type = WSMsgType.TEXT
        self.data = json.dumps(data)


class DummyWebSocket:
    def __init__(self, incoming_messages):
        self.closed = False
        self.messages = [DummyWsMsg(m) for m in incoming_messages]
        self.sent = []
        self.close_code = None

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.messages:
            raise StopAsyncIteration
        return self.messages.pop(0)

    async def prepare(self, request):
        pass

    async def send_str(self, s):
        self.sent.append(json.loads(s))


class TestCancelQueueRecognition(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.room_id = "test-cancel-queue-room"
        _room_queues.pop(self.room_id, None)
        _room_live_snapshots.pop(self.room_id, None)
        _active_agent_pids.pop(self.room_id, None)
        _active_watch_tasks.pop(self.room_id, None)
        _user_cancelled_rooms.discard(self.room_id)

    def tearDown(self):
        _room_queues.pop(self.room_id, None)
        _room_live_snapshots.pop(self.room_id, None)
        _active_agent_pids.pop(self.room_id, None)
        _active_watch_tasks.pop(self.room_id, None)
        _user_cancelled_rooms.discard(self.room_id)

    async def test_cancel_queued_by_client_msg_id_in_ws(self):
        """clientMsgId 단일 식별자로 큐 항목이 정확히 취소되고 broadcast_fn에 반영된다."""
        q = asyncio.Queue()
        fut1 = asyncio.Future()
        fut2 = asyncio.Future()
        q.put_nowait({
            "client_msg_id": "c-111",
            "user_text": "첫 번째 질문",
            "result_future": fut1,
        })
        q.put_nowait({
            "client_msg_id": "c-222",
            "user_text": "두 번째 질문",
            "result_future": fut2,
        })
        _room_queues[self.room_id] = q

        dummy_ws = DummyWebSocket([
            {
                "type": "cancel_queued",
                "roomId": self.room_id,
                "clientMsgId": "c-111",
            }
        ])

        mock_broadcast = AsyncMock()
        mock_cancel_run = MagicMock()
        mock_request = MagicMock()

        with patch("aiohttp.web.WebSocketResponse", return_value=dummy_ws), \
             patch("bot.web.runtime_state.broadcast_ws_json", mock_broadcast), \
             patch("bot.web.room_queue.cancel_active_run", mock_cancel_run):
            await ws.handle_websocket(mock_request)

        self.assertTrue(fut1.cancelled() or fut1.done())
        self.assertFalse(fut2.done())
        self.assertEqual(q.qsize(), 1)
        remaining = q.get_nowait()
        self.assertEqual(remaining["client_msg_id"], "c-222")

        # broadcast_fn 확인
        mock_broadcast.assert_awaited_once()
        b_data = mock_broadcast.await_args[0][0]
        self.assertEqual(b_data["type"], "queued_cancelled")
        self.assertEqual(b_data["removedCount"], 1)
        self.assertEqual(b_data["cancelledClientMsgIds"], ["c-111"])

    async def test_cancel_queued_fallback_to_active_run_when_already_popped(self):
        """이미 큐에서 pop되어 실행 중인 경우 cancel_active_run으로 자동 Fallback 취소된다."""
        q = asyncio.Queue()
        _room_queues[self.room_id] = q  # 큐는 비어있음

        # 활성 스냅샷 존재
        _room_live_snapshots[self.room_id] = {
            "roomId": self.room_id,
            "jobId": "job-test-fallback",
            "clientMsgId": "c-active-run",
            "userText": "실행 중인 질문",
        }

        dummy_ws = DummyWebSocket([
            {
                "type": "cancel_queued",
                "roomId": self.room_id,
                "clientMsgId": "c-active-run",
            }
        ])

        mock_broadcast = AsyncMock()
        mock_cancel_run = MagicMock()
        mock_request = MagicMock()

        with patch("aiohttp.web.WebSocketResponse", return_value=dummy_ws), \
             patch("bot.web.runtime_state.broadcast_ws_json", mock_broadcast), \
             patch("bot.web.room_queue.cancel_active_run", mock_cancel_run):
            await ws.handle_websocket(mock_request)

        # cancel_active_run이 실행되었는지 검증
        mock_cancel_run.assert_called_once_with(self.room_id)
        # broadcast_fn이 cancelled와 queued_cancelled를 차례로 호출했는지 확인
        self.assertEqual(mock_broadcast.await_count, 2)
        call1 = mock_broadcast.await_args_list[0][0][0]
        call2 = mock_broadcast.await_args_list[1][0][0]
        self.assertEqual(call1["type"], "cancelled")
        self.assertEqual(call2["type"], "queued_cancelled")
        self.assertTrue(call2["fallbackActiveCancelled"])

    def test_cancel_slot_message_for_task_updates_db_and_task(self):
        """하위 태스크 취소 시 사전 예약 슬롯(slotMsgId) 메시지와 태스크 레코드가 즉시 취소 상태로 영속화된다."""
        task = master_tasks.create_task(
            room_id=self.room_id,
            instruction="하위 작업 1",
            slot_msg_id=999,
        )
        task_id = task["id"]

        mock_db = MagicMock()
        with patch.object(agent_reporting, "chat_db", mock_db):
            agent_reporting.cancel_slot_message_for_task(task_id, self.room_id, reason="사용자 취소")

            # 슬롯 메시지 업데이트 검증
            mock_db.update_message.assert_called_once()
            args, kwargs = mock_db.update_message.call_args
            self.assertEqual(args[0], 999)
            self.assertIn("취소되었습니다", kwargs["text"])
            self.assertEqual(kwargs["steps"][0]["status"], "cancelled")

        # 태스크 상태 검증
        updated_task = master_tasks.get_task(task_id)
        self.assertEqual(updated_task["status"], "cancelled")
        self.assertTrue(updated_task["user_cancelled"])
        self.assertTrue(updated_task["reviewed"])
