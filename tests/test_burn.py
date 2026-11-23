"""test_burn — burn_todo 및 burn_runner 핵심 로직 단위 테스트."""

import datetime
import os
import shutil
import sys
import tempfile
import unittest

# src/bot 경로 추가
_SRC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "bot"
)
sys.path.insert(0, _SRC_DIR)

import bot.core.burn_todo as burn_todo
import bot.core.paths as paths
from bot.scheduler.burn_runner import evaluate_burn_condition


class TestBurnTodo(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.orig_burn_file = paths.BURN_TODO_FILE
        paths.BURN_TODO_FILE = os.path.join(self.test_dir, "test_burn_todo.json")
        # reset module internal if needed
        burn_todo.BURN_TODO_FILE = paths.BURN_TODO_FILE

    def tearDown(self):
        paths.BURN_TODO_FILE = self.orig_burn_file
        burn_todo.BURN_TODO_FILE = self.orig_burn_file
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_init_defaults(self):
        tasks = burn_todo.list_all()
        self.assertGreaterEqual(len(tasks), 5)
        first = tasks[0]
        self.assertEqual(first["id"], "burn-test-suite")
        self.assertEqual(first["status"], "pending")

    def test_add_and_remove(self):
        added = burn_todo.add("새 작업 테스트", "테스트 프롬프트", priority=1)
        self.assertIn("id", added)
        self.assertEqual(added["title"], "새 작업 테스트")

        # get_next_pending should return this priority 1 item
        pending = burn_todo.get_next_pending()
        self.assertIsNotNone(pending)
        self.assertEqual(pending["id"], added["id"])

        # remove
        ok = burn_todo.remove(added["id"])
        self.assertTrue(ok)
        self.assertIsNone(burn_todo.get_task(added["id"]))

    def test_update_and_reset(self):
        tasks = burn_todo.list_all()
        target_id = tasks[0]["id"]

        burn_todo.update_task(target_id, status="completed", result_summary="성공")
        task = burn_todo.get_task(target_id)
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["result_summary"], "성공")

        cnt = burn_todo.reset_all()
        self.assertGreaterEqual(cnt, 1)
        task_reset = burn_todo.get_task(target_id)
        self.assertEqual(task_reset["status"], "pending")


class TestBurnCondition(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.orig_burn_file = paths.BURN_TODO_FILE
        paths.BURN_TODO_FILE = os.path.join(self.test_dir, "test_burn_todo.json")
        burn_todo.BURN_TODO_FILE = paths.BURN_TODO_FILE

    def tearDown(self):
        paths.BURN_TODO_FILE = self.orig_burn_file
        burn_todo.BURN_TODO_FILE = self.orig_burn_file
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_force_execution(self):
        should_burn, reason, _ = evaluate_burn_condition(None, force=True)
        self.assertTrue(should_burn)
        self.assertIn("수동 강제", reason)

    def test_no_data(self):
        should_burn, reason, _ = evaluate_burn_condition(None, force=False)
        self.assertFalse(should_burn)

    def test_burn_condition_matching(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        # 3시간 뒤 리셋 (6시간 미만)
        reset_time = (now + datetime.timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")

        mock_usage = {
            "command": {
                "name": "usage",
                "data": {
                    "groups": [
                        {
                            "name": "Gemini Models",
                            "buckets": [
                                {
                                    "id": "gemini-weekly",
                                    "name": "Weekly Limit Remaining",
                                    "window": "weekly",
                                    "remaining_fraction": 0.50,  # 50% 남음
                                    "reset_time": reset_time,
                                },
                                {
                                    "id": "gemini-5h",
                                    "name": "Five Hour Limit Remaining",
                                    "window": "5h",
                                    "remaining_fraction": 0.80,  # 80% 남음
                                },
                            ],
                        }
                    ]
                },
            }
        }

        should_burn, reason, meta = evaluate_burn_condition(mock_usage, force=False)
        self.assertTrue(should_burn)
        self.assertEqual(meta["weekly_pct"], 50)
        self.assertAlmostEqual(meta["hours_left"], 3.0, delta=0.1)

    def test_burn_condition_not_matching_when_time_too_far(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        # 12시간 뒤 리셋 (> 6시간)
        reset_time = (now + datetime.timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ")

        mock_usage = {
            "command": {
                "name": "usage",
                "data": {
                    "groups": [
                        {
                            "name": "Gemini Models",
                            "buckets": [
                                {
                                    "id": "gemini-weekly",
                                    "name": "Weekly Limit Remaining",
                                    "window": "weekly",
                                    "remaining_fraction": 0.60,
                                    "reset_time": reset_time,
                                }
                            ],
                        }
                    ]
                },
            }
        }

        should_burn, reason, _ = evaluate_burn_condition(mock_usage, force=False)
        self.assertFalse(should_burn)
        self.assertIn("6시간 이하", reason)

    def test_burn_condition_not_matching_when_quota_exhausted(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        # 2시간 뒤 리셋이지만 잔여 쿼터 10% (< 30%)
        reset_time = (now + datetime.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")

        mock_usage = {
            "command": {
                "name": "usage",
                "data": {
                    "groups": [
                        {
                            "name": "Gemini Models",
                            "buckets": [
                                {
                                    "id": "gemini-weekly",
                                    "name": "Weekly Limit Remaining",
                                    "window": "weekly",
                                    "remaining_fraction": 0.10,
                                    "reset_time": reset_time,
                                }
                            ],
                        }
                    ]
                },
            }
        }

        should_burn, reason, _ = evaluate_burn_condition(mock_usage, force=False)
        self.assertFalse(should_burn)
        self.assertIn("부족", reason)

    def test_recover_stuck_tasks(self):
        import asyncio
        from unittest.mock import AsyncMock, patch
        from bot.scheduler.burn_runner import recover_stuck_burn_tasks

        # 가상 태스크 추가 후 running 상태로 설정
        added = burn_todo.add("중단된 작업", "프롬프트", priority=1)
        burn_todo.update_task(added["id"], status="running")

        sent_messages = []
        async def mock_send_to_chat(bot, chat_id, text):
            sent_messages.append((chat_id, text))

        with patch("bot.scheduler.burn_runner.run_auto_burn_pipeline", new_callable=AsyncMock) as mock_pipeline:
            loop = asyncio.new_event_loop()
            count = loop.run_until_complete(
                recover_stuck_burn_tasks(
                    bot=object(),
                    chat_id=12345,
                    send_to_chat=mock_send_to_chat,
                )
            )
            pending = asyncio.all_tasks(loop)
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()
            mock_pipeline.assert_awaited_once()

        self.assertEqual(count, 1)
        task = burn_todo.get_task(added["id"])
        self.assertEqual(task["status"], "pending")
        self.assertEqual(len(sent_messages), 1)
        self.assertIn("재트리거", sent_messages[0][1])

    def test_format_token_usage(self):
        from bot.engine.agent_engine import format_token_usage

        self.assertEqual(format_token_usage(None), "토큰 정보 없음")

        usage = {
            "input_tokens": 12500,
            "output_tokens": 3400,
            "thinking_tokens": 8200,
            "total_tokens": 24100,
        }
        res = format_token_usage(usage)
        self.assertIn("총 24,100 tokens", res)
        self.assertIn("입력 12,500", res)
        self.assertIn("출력 3,400", res)
        self.assertIn("사고 8,200", res)


if __name__ == "__main__":
    unittest.main()
