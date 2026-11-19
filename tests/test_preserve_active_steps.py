import os
import sys
import unittest
from unittest.mock import patch

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_BOT = os.path.join(REPO_ROOT, "src", "bot")
if SRC_BOT not in sys.path:
    sys.path.insert(0, SRC_BOT)

import web_execution
import web_rooms


class TestPreserveActiveSteps(unittest.TestCase):
    """서버 동기화 시 실행/스트리밍 중인 응답의 도구 호출 단계 및 진행 상태 보존 테스트."""

    def setUp(self):
        web_execution._room_live_snapshots.clear()
        web_execution._active_watch_tasks.clear()
        web_execution._active_agent_pids.clear()
        web_execution._room_queues.clear()
        web_execution._room_session_ids.clear()
        web_execution._room_live_feeds.clear()

    def tearDown(self):
        web_execution._room_live_snapshots.clear()
        web_execution._active_watch_tasks.clear()
        web_execution._active_agent_pids.clear()
        web_execution._room_queues.clear()
        web_execution._room_session_ids.clear()
        web_execution._room_live_feeds.clear()

    def test_serialize_live_run_entry_by_room_and_session_id(self):
        """snap이 roomId로 저장되어 있어도 sessionId로 조회 시 정상 직렬화 및 steps 보존."""
        room_id = "room-test-123"
        session_id = "session-test-abc"
        steps = [
            {"kind": "tool_call", "tool": "read_file", "title": "read_file: note.md", "status": "done"},
            {"kind": "thinking", "title": "thinking", "detail": "계획 수립 중", "status": "done"},
        ]
        web_execution._room_live_snapshots[room_id] = {
            "jobId": "job-1",
            "roomId": room_id,
            "sessionId": session_id,
            "userText": "테스트 요청",
            "steps": steps,
            "streamText": "답변 스트리밍 중...",
            "startedAt": 1000000,
        }
        web_execution._room_session_ids[room_id] = session_id

        # 1. room_id 로 직접 조회
        entry_by_room = web_execution._serialize_live_run_entry(room_id)
        self.assertIsNotNone(entry_by_room)
        self.assertEqual(len(entry_by_room["steps"]), 2)
        self.assertEqual(entry_by_room["streamText"], "답변 스트리밍 중...")

        # 2. session_id 로 alias 조회
        entry_by_session = web_execution._serialize_live_run_entry(session_id)
        self.assertIsNotNone(entry_by_session)
        self.assertEqual(len(entry_by_session["steps"]), 2)
        self.assertEqual(entry_by_session["streamText"], "답변 스트리밍 중...")

        # 3. get_live_run_for_room 함수로도 조회 가능
        live_run = web_execution.get_live_run_for_room(session_id)
        self.assertIsNotNone(live_run)
        self.assertEqual(live_run["jobId"], "job-1")
        self.assertEqual(len(live_run["steps"]), 2)

    def test_build_active_runs_payload_deduplication_and_steps(self):
        """build_active_runs_payload가 roomId와 sessionId 둘 다 candidate에 있어도 중복 없이 steps 보존."""
        room_id = "room-test-456"
        session_id = "session-test-def"
        steps = [{"kind": "tool_call", "tool": "grep_search", "title": "grep_search: keyword", "status": "running"}]
        web_execution._room_live_snapshots[room_id] = {
            "jobId": "job-456",
            "roomId": room_id,
            "sessionId": session_id,
            "userText": "검색 요청",
            "steps": steps,
            "streamText": "",
            "startedAt": 2000000,
        }
        web_execution._room_session_ids[room_id] = session_id
        # candidate_keys 에 session_id 도 포함되도록 설정
        web_execution._room_session_ids[session_id] = room_id

        runs = web_execution.build_active_runs_payload()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["jobId"], "job-456")
        self.assertEqual(len(runs[0]["steps"]), 1)
        self.assertEqual(runs[0]["steps"][0]["tool"], "grep_search")

    def test_frontend_index_html_contains_merge_steps_and_preservation(self):
        """index.html 및 js 모듈에 도구 호출 단계 보존을 위한 mergeToolSteps 함수와 스냅샷 보호 로직이 존재하는지 검증."""
        static_dir = os.path.join(SRC_BOT, "static")
        index_path = os.path.join(static_dir, "index.html")
        self.assertTrue(os.path.exists(index_path))
        parts = []
        with open(index_path, "r", encoding="utf-8") as f:
            parts.append(f.read())
        js_dir = os.path.join(static_dir, "js")
        if os.path.isdir(js_dir):
            for fname in sorted(os.listdir(js_dir)):
                if fname.endswith(".js"):
                    with open(os.path.join(js_dir, fname), "r", encoding="utf-8") as f:
                        parts.append(f.read())
        content = "\n".join(parts)

        self.assertIn("function mergeToolSteps", content)
        self.assertIn("liveState.steps = mergeToolSteps(liveState.steps, serverSteps);", content)
        self.assertIn("serverStream.length >= localStream.length", content)
        self.assertIn("hasLiveContent", content)
        self.assertIn("wasStepsOpen", content)
        self.assertIn("openStepIndices", content)
        self.assertIn("liveBackup", content)
        self.assertIn("isWsActive", content)


if __name__ == "__main__":
    unittest.main()
