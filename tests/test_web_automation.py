"""test_web_automation — 정기 작업 및 토큰 버닝 REST API 단위 테스트."""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import AioHTTPTestCase

_SRC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "bot"
)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

import bot.core.burn_todo as burn_todo
import bot.core.schedules as schedules
import bot.web.server as web_server


class TestWebAutomationEndpoints(AioHTTPTestCase):
    async def get_application(self):
        self.test_dir = tempfile.mkdtemp()
        self._patchers = [
            patch("bot.core.chat_db.CHAT_DB_FILE", os.path.join(self.test_dir, "chat.db")),
            patch("bot.master.master_tasks.MASTER_TASKS_FILE", os.path.join(self.test_dir, "bot.master.master_tasks.json")),
            patch.object(web_server, "WEB_JOBS_FILE", os.path.join(self.test_dir, "web_jobs.json")),
        ]
        for p in self._patchers:
            p.start()
        self.orig_sched_file = schedules.SCHEDULES_FILE
        self.orig_burn_file = burn_todo.BURN_TODO_FILE
        self.orig_sched_enabled = os.environ.get("WEB_SCHEDULER_ENABLED")
        self.orig_auth_required = web_server.WEB_AUTH_REQUIRED

        schedules.SCHEDULES_FILE = os.path.join(self.test_dir, "schedules.json")
        burn_todo.BURN_TODO_FILE = os.path.join(self.test_dir, "burn_todo.json")
        os.environ["WEB_SCHEDULER_ENABLED"] = "0"
        web_server.WEB_AUTH_REQUIRED = False

        return web_server.create_web_app(static_dir=self.test_dir)

    async def tearDownAsync(self):
        await super().tearDownAsync()
        for p in reversed(getattr(self, "_patchers", [])):
            p.stop()
        schedules.SCHEDULES_FILE = self.orig_sched_file
        burn_todo.BURN_TODO_FILE = self.orig_burn_file
        web_server.WEB_AUTH_REQUIRED = self.orig_auth_required
        if self.orig_sched_enabled is None:
            os.environ.pop("WEB_SCHEDULER_ENABLED", None)
        else:
            os.environ["WEB_SCHEDULER_ENABLED"] = self.orig_sched_enabled
        shutil.rmtree(self.test_dir, ignore_errors=True)

    async def test_schedules_crud_and_nl_creation(self):
        # 1. 초기 빈 목록
        resp = await self.client.get("/api/schedules")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["items"], [])

        # 2. 자연어 등록
        with patch("bot.web.automation.run_agent", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = {
                "result": json.dumps({
                    "cron": "0 8 * * *",
                    "prompt": "오늘 일정 요약해줘",
                    "description": "매일 아침 8시 일정 요약",
                })
            }
            resp = await self.client.post(
                "/api/schedules",
                json={"naturalText": "매일 아침 8시에 오늘 일정 요약해줘"},
            )
            self.assertEqual(resp.status, 200)
            created = (await resp.json())["item"]
            self.assertEqual(created["cron"], "0 8 * * *")
            self.assertEqual(created["statusLabel"], "활성")

        sched_id = created["id"]

        # 3. 비활성화 토글 (PATCH)
        resp = await self.client.patch(f"/api/schedules/{sched_id}", json={"enabled": False})
        self.assertEqual(resp.status, 200)
        toggled = (await resp.json())["item"]
        self.assertFalse(toggled["enabled"])
        self.assertEqual(toggled["statusLabel"], "일시중지")

        # 4. 수동 즉시 실행 (POST /api/schedules/{id}/run) -> 마스터(__core__) 트리거
        with patch("bot.web.automation.scheduler._fire", new_callable=AsyncMock) as mock_fire:
            resp = await self.client.post(f"/api/schedules/{sched_id}/run")
            self.assertEqual(resp.status, 200)
            run_data = await resp.json()
            self.assertEqual(run_data["status"], "ok")
            self.assertIn("__core__", run_data["message"])
            await asyncio.sleep(0)
            mock_fire.assert_awaited_once()
            kwargs = mock_fire.call_args.kwargs
            self.assertEqual(kwargs.get("trigger_source"), "manual")
            self.assertIs(kwargs.get("trigger_master"), web_server._trigger_schedule_in_master)

        # 5. 삭제 (DELETE)
        resp = await self.client.delete(f"/api/schedules/{sched_id}")
        self.assertEqual(resp.status, 200)
        resp = await self.client.get("/api/schedules")
        self.assertEqual((await resp.json())["items"], [])

    async def test_schedule_full_update_patch_put_and_validation(self):
        # 직접 등록 (model, effort 포함)
        resp = await self.client.post(
            "/api/schedules",
            json={
                "cron": "0 8 * * *",
                "prompt": "초기 프롬프트",
                "description": "초기 설명",
                "model": "gemini-3.8-flash",
                "effort": "medium",
            },
        )
        self.assertEqual(resp.status, 200)
        created = (await resp.json())["item"]
        sched_id = created["id"]
        self.assertEqual(created["model"], "gemini-3.8-flash")
        self.assertEqual(created["effort"], "medium")

        # PATCH로 cron, prompt, description, model, effort, enabled 수정
        resp = await self.client.patch(
            f"/api/schedules/{sched_id}",
            json={
                "cron": "30 9 * * 1-5",
                "prompt": "수정된 프롬프트",
                "description": "평일 오전 9시 30분 요약",
                "model": "claude-sonnet-4-6",
                "effort": "high",
                "enabled": False,
            },
        )
        self.assertEqual(resp.status, 200)
        updated = (await resp.json())["item"]
        self.assertEqual(updated["cron"], "30 9 * * 1-5")
        self.assertEqual(updated["prompt"], "수정된 프롬프트")
        self.assertEqual(updated["description"], "평일 오전 9시 30분 요약")
        self.assertEqual(updated["model"], "claude-sonnet-4-6")
        self.assertEqual(updated["effort"], "high")
        self.assertFalse(updated["enabled"])
        self.assertTrue(updated["updated"])

        # PUT으로 model/effort 초기화 및 재활성화
        resp = await self.client.put(
            f"/api/schedules/{sched_id}",
            json={
                "cron": "0 * * * *",
                "prompt": "매시 정각 점검",
                "description": "매시 점검",
                "model": "",
                "effort": "",
                "enabled": True,
            },
        )
        self.assertEqual(resp.status, 200)
        put_item = (await resp.json())["item"]
        self.assertEqual(put_item["cron"], "0 * * * *")
        self.assertEqual(put_item["model"], "")
        self.assertEqual(put_item["effort"], "")
        self.assertTrue(put_item["enabled"])

        # 영속화 확인 (GET)
        resp = await self.client.get("/api/schedules")
        items = (await resp.json())["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["prompt"], "매시 정각 점검")

        # 유효성 검사 실패 케이스
        bad_cron = await self.client.patch(f"/api/schedules/{sched_id}", json={"bot.core.cron": "invalid cron"})
        self.assertEqual(bad_cron.status, 400)

        empty_prompt = await self.client.patch(f"/api/schedules/{sched_id}", json={"prompt": "   "})
        self.assertEqual(empty_prompt.status, 400)

        no_fields = await self.client.patch(f"/api/schedules/{sched_id}", json={})
        self.assertEqual(no_fields.status, 400)

        not_found = await self.client.patch("/api/schedules/nonexistent", json={"bot.core.cron": "0 8 * * *"})
        self.assertEqual(not_found.status, 404)

    async def test_burn_status_and_todo_crud(self):
        fake_usage = {"groups": [{"name": "Test Group", "buckets": []}]}
        with patch(
            "web_automation.burn_runner.get_agy_usage_data",
            new_callable=AsyncMock,
            return_value=(fake_usage, ""),
        ), patch(
            "web_automation.burn_runner.evaluate_burn_condition",
            return_value=(
                False,
                "리셋까지 10.0시간 남아있습니다.",
                {"group_name": "Test Group", "weekly_pct": 85, "hours_left": 10.0},
            ),
        ):
            resp = await self.client.get("/api/burn/status")
            self.assertEqual(resp.status, 200)
            data = await resp.json()
            self.assertEqual(data["weeklyPct"], 85)
            self.assertEqual(data["hoursLeftLabel"], "10.0시간 남음")
            self.assertTrue(data["canRun"])
            self.assertGreater(len(data["items"]), 0)

        # 새 TODO 추가 및 삭제
        resp = await self.client.post(
            "/api/burn/todo",
            json={"title": "테스트 과제", "prompt": "코드 점검", "workdirType": "repo"},
        )
        self.assertEqual(resp.status, 200)
        item = (await resp.json())["item"]
        self.assertEqual(item["workdirLabel"], "코드 워크트리")
        self.assertEqual(item["statusLabel"], "대기")

        resp = await self.client.delete(f"/api/burn/todo/{item['id']}")
        self.assertEqual(resp.status, 200)


if __name__ == "__main__":
    unittest.main()
