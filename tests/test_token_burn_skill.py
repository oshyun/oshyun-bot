"""test_token_burn_skill — token-burn 스킬 O(1) 캐시 조건 검사 및 기본 스케줄 정합성 테스트."""

import datetime
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest

_SRC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"
)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

import bot.core.schedules as schedules

_SKILL_SCRIPT_PATH = os.path.join(
    os.getenv("AGENT_WORKDIR", "/Users/user/obsidian/oshyun"),
    "_agents/skills/token-burn/scripts/burn_check_and_run.py",
)


def _load_skill_module():
    spec = importlib.util.spec_from_file_location("burn_check_and_run", _SKILL_SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


class TestTokenBurnSkill(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.orig_sched_file = schedules.SCHEDULES_FILE
        schedules.SCHEDULES_FILE = os.path.join(self.test_dir, "schedules.json")
        self.skill = _load_skill_module()

    def tearDown(self):
        schedules.SCHEDULES_FILE = self.orig_sched_file
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_ensure_default_burn_schedule_is_normal_prompt(self):
        entry = schedules.ensure_default_burn_schedule()
        self.assertIsNotNone(entry)
        self.assertEqual(entry["id"], "burn-auto")
        self.assertEqual(entry["cron"], "0 * * * *")
        self.assertEqual(entry["prompt"], "토큰 버닝 점검 및 실행")
        self.assertNotIn("kind", entry)
        # 두 번 호출 시 중복 생성 안 됨
        self.assertIsNone(schedules.ensure_default_burn_schedule())

    def test_evaluate_cached_condition_pass_and_match(self):
        now = datetime.datetime(2026, 11, 18, 12, 0, 0, tzinfo=datetime.timezone.utc)
        state_file = os.path.join(self.test_dir, "burn_state.json")

        # 1. 리셋까지 10시간 남음 -> 조건 미달
        quota_far = {
            "Gemini Models": {
                "Weekly Limit Remaining": {
                    "pct": 80.0,
                    "window": "weekly",
                    "reset_time": "2026-11-18T22:00:00Z",
                },
                "Five Hour Limit Remaining": {"pct": 90.0, "window": "5h"},
            }
        }
        ok, reason, _ = self.skill.evaluate_cached_condition(
            quota_far, burn_state_file=state_file, now_utc=now
        )
        self.assertFalse(ok)
        self.assertIn("6시간 미만", reason)

        # 2. 리셋까지 3시간, 주간 잔여 20% -> 조건 미달
        quota_low = {
            "Gemini Models": {
                "Weekly Limit Remaining": {
                    "pct": 20.0,
                    "window": "weekly",
                    "reset_time": "2026-11-18T15:00:00Z",
                },
                "Five Hour Limit Remaining": {"pct": 90.0, "window": "5h"},
            }
        }
        ok, reason, _ = self.skill.evaluate_cached_condition(
            quota_low, burn_state_file=state_file, now_utc=now
        )
        self.assertFalse(ok)
        self.assertIn("부족", reason)

        # 3. 리셋까지 3시간, 주간 잔여 55%, 5시간 90% -> 조건 충족
        quota_ok = {
            "Gemini Models": {
                "Weekly Limit Remaining": {
                    "pct": 55.0,
                    "window": "weekly",
                    "reset_time": "2026-11-18T15:00:00Z",
                },
                "Five Hour Limit Remaining": {"pct": 90.0, "window": "5h"},
            }
        }
        ok, reason, meta = self.skill.evaluate_cached_condition(
            quota_ok, burn_state_file=state_file, now_utc=now
        )
        self.assertTrue(ok)
        self.assertEqual(meta["weekly_pct"], 55.0)

        # 4. 동일 주기 이미 실행됨 -> 중복 차단
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump({"last_burned_cycle": "2026-11-18T15:00:00Z"}, f)
        ok, reason, _ = self.skill.evaluate_cached_condition(
            quota_ok, burn_state_file=state_file, now_utc=now
        )
        self.assertFalse(ok)
        self.assertIn("이미 버닝", reason)

        # 5. --force 시 무조건 통과
        ok, _, _ = self.skill.evaluate_cached_condition(
            None, burn_state_file=state_file, force=True, now_utc=now
        )
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
