"""test_state_modules — schedules 상태 관리 단위 테스트."""

import os
import shutil
import sys
import tempfile
import unittest

_SRC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "bot"
)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

import schedules


class TestSchedules(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.orig_file = schedules.SCHEDULES_FILE
        self.tmp_file = os.path.join(self.test_dir, "schedules.json")
        schedules.SCHEDULES_FILE = self.tmp_file

    def tearDown(self):
        schedules.SCHEDULES_FILE = self.orig_file
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_schedules_crud(self):
        # 1. 초기 상태: 빈 목록
        self.assertEqual(schedules.list_all(), [])

        # 2. 항목 추가
        entry = schedules.add(
            chat_id=12345,
            cron="0 8 * * *",
            prompt="오늘 일정 요약해줘",
            description="아침 다이제스트",
            model="sonnet",
        )
        self.assertIn("id", entry)
        self.assertEqual(entry["chat_id"], 12345)
        self.assertEqual(entry["cron"], "0 8 * * *")
        self.assertEqual(entry["model"], "sonnet")
        self.assertTrue(entry["enabled"])

        # 3. 목록 조회
        all_items = schedules.list_all()
        self.assertEqual(len(all_items), 1)
        self.assertEqual(all_items[0]["id"], entry["id"])

        # 4. 삭제 (성공 및 실패 케이스)
        self.assertFalse(schedules.remove("non-existent-id"))
        self.assertTrue(schedules.remove(entry["id"]))
        self.assertEqual(schedules.list_all(), [])


if __name__ == "__main__":
    unittest.main()
