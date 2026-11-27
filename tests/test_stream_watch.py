import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock

from bot.web.stream_watch import _run_stream_tail_loop


class TestStreamWatchTailLoop(unittest.IsolatedAsyncioTestCase):
    async def test_agent_response_done_state_emits_delta(self):
        """agent_response 의 DONE 상태에 포함된 마지막 text_delta 도 누락 없이 브로드캐스트되는지 검증."""
        with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False) as f:
            temp_path = f.name

        try:
            tail_stop_event = asyncio.Event()
            broadcast_mock = AsyncMock()
            chat_db_mock = MagicMock()
            collected_steps = []
            live_snapshot = {"streamText": "", "feed": ""}

            loop_task = asyncio.create_task(
                _run_stream_tail_loop(
                    out_file_path=temp_path,
                    tail_stop_event=tail_stop_event,
                    room_key="room_test",
                    room_id="room_test",
                    job_id="job_test",
                    asst_msg_id=None,
                    start_timestamp=0.0,
                    collected_steps=collected_steps,
                    live_snapshot=live_snapshot,
                    agy_conv_id=None,
                    current_broadcast=broadcast_mock,
                    current_chat_db=chat_db_mock,
                )
            )

            # 1. ACTIVE 상태의 text_delta 기록
            line1 = json.dumps({
                "event": "step_update",
                "step_update": {
                    "step_type": "agent_response",
                    "state": "ACTIVE",
                    "text_delta": "안녕하세요. ",
                },
            }) + "\n"
            # 2. DONE 상태의 마지막 text_delta 기록
            line2 = json.dumps({
                "event": "step_update",
                "step_update": {
                    "step_type": "agent_response",
                    "state": "DONE",
                    "text_delta": "반갑습니다!\n",
                },
            }) + "\n"

            with open(temp_path, "a", encoding="utf-8") as f:
                f.write(line1)
                f.flush()

            await asyncio.sleep(0.08)

            with open(temp_path, "a", encoding="utf-8") as f:
                f.write(line2)
                f.flush()

            await asyncio.sleep(0.08)
            tail_stop_event.set()
            await loop_task

            # streamText 에 두 델타가 모두 누적되었는지 검증
            self.assertEqual(live_snapshot["streamText"], "안녕하세요. 반갑습니다!\n")

            # 브로드캐스트가 stream_delta 타입으로 2회 호출되었는지 검증
            calls = [call.args[0] for call in broadcast_mock.call_args_list if call.args[0].get("type") == "stream_delta"]
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0]["delta"], "안녕하세요. ")
            self.assertEqual(calls[1]["delta"], "반갑습니다!\n")

        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)


if __name__ == "__main__":
    unittest.main()
