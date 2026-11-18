"""test_web_jobs — web_jobs 스레드 안전 영속화, 고아 파일 정리, 단계 추출 단위 테스트."""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest

_SRC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "bot"
)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

import web_execution
import web_jobs


class TestWebJobs(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.jobs_file = os.path.join(self.test_dir, "web_jobs.json")
        self.jobs_dir = os.path.join(self.test_dir, "jobs")
        os.makedirs(self.jobs_dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_save_load_remove(self):
        self.assertEqual(web_jobs.load_web_jobs(self.jobs_file), {})
        web_jobs.save_web_job(self.jobs_file, "web-1", {"pid": 123, "room_id": "r1"})
        loaded = web_jobs.load_web_jobs(self.jobs_file)
        self.assertEqual(loaded["web-1"]["pid"], 123)

        web_jobs.remove_web_job(self.jobs_file, "web-1")
        self.assertEqual(web_jobs.load_web_jobs(self.jobs_file), {})

    def test_sweep_orphan_job_files_respects_active_and_age(self):
        active_path = os.path.join(self.jobs_dir, "web-active.jsonl")
        recent_orphan = os.path.join(self.jobs_dir, "web-recent.jsonl")
        old_orphan = os.path.join(self.jobs_dir, "web-old.jsonl")
        old_err = os.path.join(self.jobs_dir, "web-old.err")

        for p in (active_path, recent_orphan, old_orphan, old_err):
            with open(p, "w", encoding="utf-8") as f:
                f.write("data\n")

        old_ts = time.time() - 7200
        os.utime(old_orphan, (old_ts, old_ts))
        os.utime(old_err, (old_ts, old_ts))

        removed = web_jobs.sweep_orphan_job_files(
            self.jobs_dir,
            {"web-active"},
            max_age_seconds=3600,
        )
        self.assertEqual(removed, 2)
        self.assertTrue(os.path.exists(active_path))
        self.assertTrue(os.path.exists(recent_orphan))
        self.assertFalse(os.path.exists(old_orphan))
        self.assertFalse(os.path.exists(old_err))

    def test_extract_steps_from_jsonl(self):
        out_file = os.path.join(self.jobs_dir, "steps.jsonl")
        events = [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "read_file", "input": {"Path": "/tmp/a.md"}}
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "content": "ok"}
                    ]
                },
            },
        ]
        with open(out_file, "w", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")

        steps, offset = web_jobs.extract_steps_from_jsonl(
            out_file, web_execution.extract_stream_events_from_line
        )
        self.assertGreater(offset, 0)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["status"], "done")

    def test_compute_remaining_timeout(self):
        self.assertEqual(web_jobs.compute_remaining_timeout(time.time() - 10, 0), 0)
        rem = web_jobs.compute_remaining_timeout(time.time() - 10, 100)
        self.assertTrue(88 <= rem <= 91)
        self.assertEqual(web_jobs.compute_remaining_timeout(time.time() - 200, 100), 1)


if __name__ == "__main__":
    unittest.main()
