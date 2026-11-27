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

    async def test_transcript_planner_response_skipped_if_deltas_streamed(self):
        """실시간 text_delta 스트리밍이 이미 이뤄졌다면 transcript 의 PLANNER_RESPONSE 중복 브로드캐스트를 스킵하는지 검증."""
        with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False) as f:
            temp_out_path = f.name

        t_dir = tempfile.mkdtemp()
        t_file = os.path.join(t_dir, "transcript.jsonl")

        try:
            tail_stop_event = asyncio.Event()
            broadcast_mock = AsyncMock()
            chat_db_mock = MagicMock()
            collected_steps = []
            live_snapshot = {"streamText": "", "feed": ""}

            # mock expanduser so transcript points to t_file
            import unittest.mock as mock
            with mock.patch("os.path.expanduser", return_value=t_file):
                loop_task = asyncio.create_task(
                    _run_stream_tail_loop(
                        out_file_path=temp_out_path,
                        tail_stop_event=tail_stop_event,
                        room_key="room_test",
                        room_id="room_test",
                        job_id="job_test",
                        asst_msg_id=None,
                        start_timestamp=0.0,
                        collected_steps=collected_steps,
                        live_snapshot=live_snapshot,
                        agy_conv_id="conv-123",
                        current_broadcast=broadcast_mock,
                        current_chat_db=chat_db_mock,
                    )
                )

                # out_file 에 델타 출력
                delta_line = json.dumps({
                    "event": "step_update",
                    "step_update": {
                        "step_type": "agent_response",
                        "state": "ACTIVE",
                        "text_delta": "실시간 델타 텍스트입니다.",
                    },
                }) + "\n"
                with open(temp_out_path, "a", encoding="utf-8") as f:
                    f.write(delta_line)
                    f.flush()

                await asyncio.sleep(0.08)

                # transcript 에 PLANNER_RESPONSE 기록
                t_line = json.dumps({
                    "type": "PLANNER_RESPONSE",
                    "source": "MODEL",
                    "content": "실시간 델타 텍스트입니다. 최종본",
                }) + "\n"
                with open(t_file, "a", encoding="utf-8") as f:
                    f.write(t_line)
                    f.flush()

                await asyncio.sleep(0.08)
                tail_stop_event.set()
                await loop_task

                # stream_chunk 가 중복 호출되지 않았는지 검증
                chunk_calls = [call.args[0] for call in broadcast_mock.call_args_list if call.args[0].get("type") == "stream_chunk"]
                self.assertEqual(len(chunk_calls), 0)

        finally:
            if os.path.exists(temp_out_path):
                os.remove(temp_out_path)
            if os.path.exists(t_file):
                os.remove(t_file)
            if os.path.exists(t_dir):
                os.rmdir(t_dir)


if __name__ == "__main__":
    unittest.main()
