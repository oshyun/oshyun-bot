"""test_gmail_schedule — Gmail 스케줄 등록 및 스케줄러 연동 테스트."""

import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

_SRC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"
)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

import bot.scheduler.scheduler as scheduler
import bot.core.schedules as schedules


class TestGmailScheduleIntegration(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.orig_schedules_file = schedules.SCHEDULES_FILE
        schedules.SCHEDULES_FILE = os.path.join(self.temp_dir.name, "schedules.json")

    def tearDown(self):
        schedules.SCHEDULES_FILE = self.orig_schedules_file
        self.temp_dir.cleanup()

    def test_schedules_add_gmail_kind(self):
        entry = schedules.add(
            chat_id=0,
            cron="0 8 * * *",
            prompt="최신 메일 요약",
            description="아침 메일 브리핑",
            kind="gmail",
        )
        self.assertEqual(entry["kind"], "gmail")
        self.assertEqual(entry["cron"], "0 8 * * *")

        all_items = schedules.list_all()
        self.assertEqual(len(all_items), 1)
        self.assertEqual(all_items[0]["kind"], "gmail")

    def test_ensure_default_gmail_schedule(self):
        # 첫 호출: 생성됨
        created = schedules.ensure_default_gmail_schedule("0 9 * * *")
        self.assertIsNotNone(created)
        self.assertEqual(created["id"], "gmail-auto")
        self.assertEqual(created["kind"], "gmail")
        self.assertEqual(created["cron"], "0 9 * * *")

        # 두 번째 호출: 중복 생성 안 됨
        second = schedules.ensure_default_gmail_schedule()
        self.assertIsNone(second)

    def test_is_gmail_schedule_detection(self):
        # 1. kind="gmail"
        self.assertTrue(scheduler._is_gmail_schedule({"kind": "gmail"}, "아무 프롬프트"))

        # 2. prompt/description 기반 자연어 매칭
        self.assertTrue(
            scheduler._is_gmail_schedule(
                {"description": "정기 메일 수신"}, "메일 읽고 요약해줘"
            )
        )
        self.assertTrue(
            scheduler._is_gmail_schedule(
                {"description": "일반 작업"}, "gmail 최신 동기화"
            )
        )

        # 3. 무관한 작업
        self.assertFalse(
            scheduler._is_gmail_schedule(
                {"description": "일정 정리"}, "오늘 캘린더 일정 알려줘"
            )
        )

    def test_build_master_schedule_prompt_with_mail_report(self):
        entry = {
            "id": "sched_mail_1",
            "description": "메일 요약",
            "cron": "0 8 * * *",
            "prompt": "메일을 읽고 요약해줘",
            "kind": "gmail",
        }
        mail_report = "📬 메일 3건 수신 완료 (미열람 1건)"
        prompt = scheduler.build_master_schedule_prompt(
            entry,
            trigger_source="cron",
            mail_report=mail_report,
        )
        self.assertIn("스케줄 ID: sched_mail_1", prompt)
        self.assertIn("[사전 수신된 메일 동기화 현황]", prompt)
        self.assertIn("📬 메일 3건 수신 완료", prompt)

    def test_scheduler_fire_executes_gmail_sync(self):
        entry = {
            "id": "gmail_test",
            "description": "메일 동기화",
            "cron": "0 8 * * *",
            "prompt": "메일 읽기",
            "kind": "gmail",
        }

        mock_trigger_master = AsyncMock()
        mock_gmail_mgr = MagicMock()
        mock_gmail_mgr.sync_to_vault.return_value = {
            "ok": True,
            "totalCount": 4,
            "unreadCount": 2,
            "vaultFile": "/tmp/메일.md",
        }

        with patch("bot.google.gmail_manager.default_gmail_manager", mock_gmail_mgr):
            asyncio.run(
                scheduler._fire(
                    bot=None,
                    schedule_entry=entry,
                    workdir="/tmp",
                    extra_args=[],
                    timeout=30,
                    send_to_chat=None,
                    trigger_master=mock_trigger_master,
                )
            )

            # sync_to_vault가 실행되었는지 확인
            mock_gmail_mgr.sync_to_vault.assert_called_once()
            # trigger_master에 넘겨진 prompt에 사전 동기화 결과가 주입되었는지 확인
            mock_trigger_master.assert_called_once()
            passed_entry, passed_prompt = mock_trigger_master.call_args[0]
            self.assertEqual(passed_entry["id"], "gmail_test")
            self.assertIn("4건 수신 완료", passed_prompt)
            self.assertIn("미열람 2건", passed_prompt)


if __name__ == "__main__":
    unittest.main()
