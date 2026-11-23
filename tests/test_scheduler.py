"""test_scheduler — 스케줄러 실행 및 _fire 로직 단위 테스트."""

import asyncio
from datetime import datetime
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

_SRC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "bot"
)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from bot.core.cron import Cron
import bot.scheduler.scheduler as scheduler


class TestScheduler(unittest.IsolatedAsyncioTestCase):
    def test_cron_match(self):
        c = Cron("* * * * *")
        now = datetime.now().astimezone()
        self.assertTrue(c.matches(now))

    async def test_fire_normal_agent_run(self):
        bot = MagicMock()
        entry = {
            "id": "sched1",
            "chat_id": 12345,
            "description": "일일 브리핑",
            "prompt": "오늘의 주요 뉴스 요약",
            "model": "haiku",
        }
        send_to_chat = AsyncMock()
        vault_log = MagicMock()

        mock_run_result = {"result": "요약 결과입니다.", "cost": 0.005}

        with patch("bot.scheduler.scheduler.run_agent", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = mock_run_result
            await scheduler._fire(
                bot,
                entry,
                workdir="/mock/vault",
                extra_args=[],
                timeout=60,
                send_to_chat=send_to_chat,
                vault_log=vault_log,
            )

            mock_run.assert_awaited_once_with(
                "오늘의 주요 뉴스 요약",
                None,
                workdir="/mock/vault",
                model="haiku",
                extra_args=[],
                timeout=60,
            )
            vault_log.assert_called_once()
            send_to_chat.assert_awaited_once()
            args = send_to_chat.call_args[0]
            self.assertEqual(args[0], bot)
            self.assertEqual(args[1], 12345)
            self.assertIn("일일 브리핑", args[2])
            self.assertIn("요약 결과입니다.", args[2])

    async def test_fire_usage_report(self):
        bot = MagicMock()
        entry = {
            "id": "sched_usage",
            "chat_id": 54321,
            "description": "사용량 확인",
            "prompt": "/usage",
        }
        send_to_chat = AsyncMock()
        vault_log = MagicMock()

        with patch("bot.scheduler.scheduler.get_agy_usage_report", new_callable=AsyncMock) as mock_report, \
             patch("bot.scheduler.scheduler.run_agent", new_callable=AsyncMock) as mock_run:
            mock_report.return_value = "현재 주간 사용량: 45%"

            await scheduler._fire(
                bot,
                entry,
                workdir="/mock/vault",
                extra_args=[],
                timeout=60,
                send_to_chat=send_to_chat,
                vault_log=vault_log,
            )

            mock_report.assert_awaited_once()
            mock_run.assert_not_called()
            send_to_chat.assert_awaited_once()
            sent_text = send_to_chat.call_args[0][2]
            self.assertIn("현재 주간 사용량: 45%", sent_text)

    async def test_fire_exception_handling(self):
        bot = MagicMock()
        entry = {
            "id": "sched_err",
            "chat_id": 999,
            "description": "에러 작업",
            "prompt": "오류 발생 유도",
        }
        send_to_chat = AsyncMock()
        vault_log = MagicMock()

        with patch("bot.scheduler.scheduler.run_agent", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = RuntimeError("Claude CLI 프로세스 비정상 종료")

            await scheduler._fire(
                bot,
                entry,
                workdir="/mock/vault",
                extra_args=[],
                timeout=60,
                send_to_chat=send_to_chat,
                vault_log=vault_log,
            )

            # 예외가 발생해도 삼켜지고 send_to_chat 으로 오류 안내가 전송되어야 함
            send_to_chat.assert_awaited_once()
            sent_text = send_to_chat.call_args[0][2]
            self.assertIn("⚠️ 정기 작업 실행 중 오류가 발생했습니다", sent_text)
            self.assertIn("Claude CLI 프로세스 비정상 종료", sent_text)

    async def test_fire_burn_schedule_uses_normal_prompt_pipeline(self):
        bot = MagicMock()
        entry = {
            "id": "burn-auto",
            "chat_id": 0,
            "cron": "0 * * * *",
            "description": "🔥 자동 토큰 버닝 (매시 정각 조건 점검)",
            "prompt": "토큰 버닝 점검 및 실행",
        }
        send_to_chat = AsyncMock()
        vault_log = MagicMock()

        with patch("bot.scheduler.scheduler.run_agent", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = {"result": "조건 미달로 패스", "cost": 0.0}
            await scheduler._fire(
                bot,
                entry,
                workdir="/mock/vault",
                extra_args=[],
                timeout=60,
                send_to_chat=send_to_chat,
                vault_log=vault_log,
            )
            mock_run.assert_awaited_once_with(
                "토큰 버닝 점검 및 실행",
                None,
                workdir="/mock/vault",
                model=scheduler.get_model(),
                extra_args=[],
                timeout=60,
            )
            send_to_chat.assert_awaited_once()
            self.assertIn("조건 미달로 패스", send_to_chat.call_args[0][2])

    async def test_fire_triggers_master_without_direct_run_agent(self):
        bot = MagicMock()
        entry = {
            "id": "sched_master_1",
            "chat_id": 0,
            "cron": "0 8 * * *",
            "description": "아침 브리핑",
            "prompt": "오늘 일정과 주요 할일 요약해줘",
            "model": "gemini-3.8-flash",
        }
        send_to_chat = AsyncMock()
        vault_log = MagicMock()
        trigger_master = AsyncMock()

        with patch("bot.scheduler.scheduler.run_agent", new_callable=AsyncMock) as mock_run:
            await scheduler._fire(
                bot,
                entry,
                workdir="/mock/vault",
                extra_args=[],
                timeout=60,
                send_to_chat=send_to_chat,
                vault_log=vault_log,
                trigger_master=trigger_master,
                trigger_source="cron",
            )

            mock_run.assert_not_called()
            send_to_chat.assert_not_called()
            trigger_master.assert_awaited_once()
            called_entry, master_prompt = trigger_master.call_args[0]
            self.assertEqual(called_entry["id"], "sched_master_1")
            self.assertIn("[정기 스케줄 실행 요청]", master_prompt)
            self.assertIn("아침 브리핑", master_prompt)
            self.assertIn("0 8 * * *", master_prompt)
            self.assertIn("오늘 일정과 주요 할일 요약해줘", master_prompt)
            self.assertIn("gemini-3.8-flash", master_prompt)
            self.assertIn("정기 도래(cron)", master_prompt)
            vault_log.assert_called_once_with("🔔 스케줄 트리거", "아침 브리핑 → 마스터(__core__)")

    async def test_fire_triggers_master_with_usage_report(self):
        entry = {
            "id": "sched_master_usage",
            "chat_id": 0,
            "cron": "0 * * * *",
            "description": "사용량 체크",
            "prompt": "/usage",
        }
        send_to_chat = AsyncMock()
        trigger_master = AsyncMock()

        with patch("bot.scheduler.scheduler.get_agy_usage_report", new_callable=AsyncMock) as mock_report, \
             patch("bot.scheduler.scheduler.run_agent", new_callable=AsyncMock) as mock_run:
            mock_report.return_value = "주간 잔여 72%"
            await scheduler._fire(
                None,
                entry,
                workdir="/mock/vault",
                extra_args=[],
                timeout=60,
                send_to_chat=send_to_chat,
                vault_log=None,
                trigger_master=trigger_master,
                trigger_source="manual",
            )

            mock_report.assert_awaited_once()
            mock_run.assert_not_called()
            send_to_chat.assert_not_called()
            trigger_master.assert_awaited_once()
            _, master_prompt = trigger_master.call_args[0]
            self.assertIn("수동 즉시 실행", master_prompt)
            self.assertIn("[사전 조회된 AGY 사용량 리포트]", master_prompt)
            self.assertIn("주간 잔여 72%", master_prompt)

    async def test_fire_trigger_master_exception_notifies_fallback(self):
        entry = {
            "id": "sched_master_err",
            "chat_id": 0,
            "cron": "0 9 * * *",
            "description": "오류 테스트",
            "prompt": "테스트",
        }
        send_to_chat = AsyncMock()
        trigger_master = AsyncMock(side_effect=RuntimeError("마스터 큐 적재 실패"))

        await scheduler._fire(
            None,
            entry,
            workdir="/mock/vault",
            extra_args=[],
            timeout=60,
            send_to_chat=send_to_chat,
            vault_log=None,
            trigger_master=trigger_master,
        )

        send_to_chat.assert_awaited_once()
        sent_text = send_to_chat.call_args[0][2]
        self.assertIn("마스터 트리거 중 오류가 발생했습니다", sent_text)
        self.assertIn("마스터 큐 적재 실패", sent_text)


if __name__ == "__main__":
    unittest.main()
