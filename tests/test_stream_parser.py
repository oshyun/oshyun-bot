"""test_stream_parser — stream-json 파서 및 AGY 트랜스크립트 복구 단위 테스트."""

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import stream_parser


class TestStreamParser(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.patch_session_log = patch(
            "stream_parser._SESSION_LOG_FILE",
            os.path.join(self.temp_dir, "session_log.jsonl"),
        )
        self.patch_session_log.start()

    def tearDown(self):
        self.patch_session_log.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_extract_agy_transcript_final_response(self):
        """AGY 세션 transcript.jsonl 에서 최종 PLANNER_RESPONSE 의 텍스트가 정상 추출되는지 검증."""
        conv_id = "test-conv-1234"
        brain_dir = os.path.join(self.temp_dir, "brain", conv_id, ".system_generated", "logs")
        os.makedirs(brain_dir, exist_ok=True)
        transcript_path = os.path.join(brain_dir, "transcript.jsonl")

        with open(transcript_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"step_index": 0, "type": "USER_INPUT", "content": "질문"}) + "\n")
            f.write(json.dumps({"step_index": 1, "type": "PLANNER_RESPONSE", "content": "", "tool_calls": [{"name": "read"}]}) + "\n")
            f.write(json.dumps({"step_index": 2, "type": "GENERIC", "content": "도구 결과"}) + "\n")
            f.write(json.dumps({"step_index": 3, "type": "PLANNER_RESPONSE", "content": "최종 완성된 답변입니다."}) + "\n")

        with patch("os.path.expanduser", return_value=transcript_path):
            res = stream_parser.extract_agy_transcript_final_response(conv_id)
            self.assertEqual(res, "최종 완성된 답변입니다.")

    def test_extract_agy_transcript_ignores_system_messages_and_generic(self):
        """시스템 메시지(SYSTEM_MESSAGE)나 도구 결과(GENERIC)가 최종 답변으로 추출되지 않고 모델 응답만 추출되는지 검증."""
        conv_id = "test-conv-system-filter"
        brain_dir = os.path.join(self.temp_dir, "brain", conv_id, ".system_generated", "logs")
        os.makedirs(brain_dir, exist_ok=True)
        transcript_path = os.path.join(brain_dir, "transcript.jsonl")

        with open(transcript_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "질문"}) + "\n")
            f.write(json.dumps({"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "content": "진짜 모델 응답입니다."}) + "\n")
            f.write(json.dumps({"step_index": 2, "source": "MODEL", "type": "GENERIC", "content": "Created At: 2026-11-13 ... Command output:"}) + "\n")
            f.write(json.dumps({"step_index": 3, "source": "SYSTEM", "type": "SYSTEM_MESSAGE", "content": "<SYSTEM_MESSAGE>All your subagents have been stopped</SYSTEM_MESSAGE>"}) + "\n")
            f.write(json.dumps({"step_index": 4, "source": "SYSTEM", "type": "PLANNER_RESPONSE", "content": "<SYSTEM_MESSAGE>위장된 시스템 메시지</SYSTEM_MESSAGE>"}) + "\n")

        with patch("os.path.expanduser", return_value=transcript_path):
            res = stream_parser.extract_agy_transcript_final_response(conv_id)
            self.assertEqual(res, "진짜 모델 응답입니다.")

    def test_agy_stream_with_text_deltas_fallback(self):
        """result.response 가 비어있을 때 스트리밍 text_delta 누적본으로 폴백 복구되는지 검증."""
        lines = [
            json.dumps({"event": "init", "conversation_id": "conv-test"}),
            json.dumps({"event": "step_update", "step_update": {"step_type": "agent_response", "text_delta": "안녕"}}),
            json.dumps({"event": "step_update", "step_update": {"step_type": "agent_response", "text_delta": "하세요!"}}),
            json.dumps({"event": "result", "result": {"conversation_id": "conv-test", "status": "SUCCESS", "response": ""}}),
        ]
        out = "\n".join(lines)
        parsed = stream_parser._parse_stream(out)
        self.assertEqual(parsed["result"], "안녕하세요!")
        self.assertEqual(parsed["session_id"], "conv-test")

    def test_agy_stream_interrupted_with_transcript_fallback(self):
        """result 이벤트 없이 스트림이 끊겼을 때 transcript.jsonl 로부터 최종 답변이 복구되는지 검증."""
        conv_id = "conv-interrupted"
        transcript_path = os.path.join(self.temp_dir, "transcript.jsonl")
        with open(transcript_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"step_index": 0, "type": "USER_INPUT", "content": "질문"}) + "\n")
            f.write(json.dumps({"step_index": 1, "type": "PLANNER_RESPONSE", "content": "복원된 온전한 답변입니다."}) + "\n")

        lines = [
            json.dumps({"event": "init", "conversation_id": conv_id}),
            json.dumps({"event": "step_update", "step_update": {"step_type": "tool", "state": "ACTIVE", "tool_name": "cmd"}}),
        ]
        out = "\n".join(lines)
        with patch.object(stream_parser, "extract_agy_transcript_final_response", return_value="복원된 온전한 답변입니다."):
            parsed = stream_parser._parse_stream(out, fallback_session_id=conv_id)
            self.assertEqual(parsed["result"], "복원된 온전한 답변입니다.")
            self.assertEqual(parsed["session_id"], conv_id)

    def test_agy_stale_trajectory_error_ignored_when_turn_succeeds(self):
        """과거 턴 에러로 result.status=='ERROR' 여도 현재 턴이 정상 응답하면 에러로 덮어쓰지 않는다."""
        conv_id = "conv-stale-err"
        lines = [
            json.dumps({"event": "init", "conversation_id": conv_id, "init": {"model": "gemini-3.8-flash"}}),
            json.dumps({"event": "step_update", "step_update": {"step_index": 236, "step_type": "user_input", "state": "DONE"}}),
            json.dumps({"event": "step_update", "step_update": {"step_index": 237, "step_type": "agent_response", "state": "ACTIVE", "text_delta": "정상 답변입니다."}}),
            json.dumps({"event": "step_update", "step_update": {"step_index": 237, "step_type": "agent_response", "state": "DONE"}}),
            json.dumps({
                "event": "result",
                "result": {
                    "conversation_id": conv_id,
                    "status": "ERROR",
                    "response": "정상 답변입니다.",
                    "error": "Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 123h54m59s.",
                },
            }),
        ]
        parsed = stream_parser._parse_stream("\n".join(lines))
        self.assertEqual(parsed["result"], "정상 답변입니다.")
        self.assertEqual(parsed["session_id"], conv_id)

    def test_agy_current_turn_error_step_reported_as_error(self):
        """현재 턴에 error_message 스텝이 발생하고 result.status=='ERROR' 이면 에러 문구를 반환한다."""
        conv_id = "conv-cur-err"
        err_msg = "Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 123h54m59s."
        lines = [
            json.dumps({"event": "init", "conversation_id": conv_id, "init": {"model": "claude-sonnet-5-5"}}),
            json.dumps({"event": "step_update", "step_update": {"step_index": 228, "step_type": "user_input", "state": "DONE"}}),
            json.dumps({"event": "step_update", "step_update": {"step_index": 232, "step_type": "error_message", "state": "DONE"}}),
            json.dumps({
                "event": "result",
                "result": {
                    "conversation_id": conv_id,
                    "status": "ERROR",
                    "response": "",
                    "error": err_msg,
                },
            }),
        ]
        parsed = stream_parser._parse_stream("\n".join(lines))
        self.assertEqual(parsed["result"], f"⚠️ {err_msg}")
        self.assertEqual(parsed["session_id"], conv_id)

    def test_agy_empty_response_with_error_status_reported_as_error(self):
        """error_message 스텝 없이 즉시 result.status=='ERROR' 및 빈 response 가 오면 에러를 반환한다."""
        err_msg = "Individual quota reached. Resets in 10h."
        lines = [
            json.dumps({"event": "init", "conversation_id": "conv-imm-err"}),
            json.dumps({
                "event": "result",
                "result": {
                    "conversation_id": "conv-imm-err",
                    "status": "ERROR",
                    "response": "",
                    "error": err_msg,
                },
            }),
        ]
        parsed = stream_parser._parse_stream("\n".join(lines))
        self.assertEqual(parsed["result"], f"⚠️ {err_msg}")


if __name__ == "__main__":
    unittest.main()
