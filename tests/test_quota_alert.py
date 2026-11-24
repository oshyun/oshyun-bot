import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

BOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if BOT_DIR not in sys.path:
    sys.path.insert(0, BOT_DIR)

import bot.engine.agent_engine as agent_engine
import bot.engine.engine_drivers as engine_drivers


class TestQuotaAlertInjection(unittest.TestCase):
    def _write_cache(self, path: str, weekly_pct: float, five_pct: float) -> None:
        payload = {
            "timestamp": 9999999999.0,
            "cached_at": "2026-11-19 02:20:00",
            "ttl": 300,
            "data": {
                "Gemini Models": {
                    "Weekly Limit Remaining": {
                        "pct": weekly_pct,
                        "window": "weekly",
                        "reset_time": "2026-11-25T00:00:06Z",
                    },
                    "Five Hour Limit Remaining": {
                        "pct": five_pct,
                        "window": "5h",
                        "reset_time": "2026-11-18T20:00:06Z",
                    },
                }
            },
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)

    def test_get_quota_alert_prompt_below_and_above_threshold(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = os.path.join(tmpdir, "agy_quota_cache.json")

            # 캐시 없음 -> 잔여 쿼터 수치 없이 상시 지침 반환
            alert_no_cache = engine_drivers.get_quota_alert_prompt(cache_file=cache_path)
            self.assertIn("[GEMINI CONSULT 상시 적극 오프로드 지침]", alert_no_cache)
            self.assertIn("gemini-consult", alert_no_cache)
            self.assertIn("~/.local/bin/gemini-consult", alert_no_cache)

            # 50% 이상 -> 잔여량 조건 없이 상시 반환하며 쿼터 정보 표기
            self._write_cache(cache_path, weekly_pct=62.0, five_pct=80.0)
            alert_high = engine_drivers.get_quota_alert_prompt(cache_file=cache_path)
            self.assertIn("[GEMINI CONSULT 상시 적극 오프로드 지침", alert_high)
            self.assertIn("62.0%", alert_high)
            self.assertIn("gemini-consult", alert_high)

            # 50% 미만 -> 상시 반환하며 쿼터 정보 표기
            self._write_cache(cache_path, weekly_pct=18.3, five_pct=73.5)
            alert_low = engine_drivers.get_quota_alert_prompt(cache_file=cache_path)
            self.assertIn("[GEMINI CONSULT 상시 적극 오프로드 지침", alert_low)
            self.assertIn("18.3%", alert_low)
            self.assertIn("gemini-consult", alert_low)
            self.assertIn("~/.local/bin/gemini-consult", alert_low)

    def test_claude_engine_injects_quota_alert_into_system_prompt(self):
        engine = engine_drivers.ClaudeEngine()
        fake_alert = "[QUOTA ALERT] 테스트 알림"
        with patch.object(engine_drivers, "get_quota_alert_prompt", return_value=fake_alert):
            inv_web = engine.build_cmd(
                "리팩토링 설계해줘",
                session_id=None,
                workdir="/tmp",
                model="sonnet",
                effort=None,
                extra_args=None,
                slash_commands=False,
                sandbox=False,
                coding=False,
                web=True,
            )
            sp_idx = inv_web["cmd"].index("--append-system-prompt")
            self.assertIn(fake_alert, inv_web["cmd"][sp_idx + 1])

            # 오케스트레이터에는 주입하지 않음
            inv_orch = engine.build_cmd(
                "전체 상태 요약",
                session_id=None,
                workdir="/tmp",
                model="sonnet",
                effort=None,
                extra_args=None,
                slash_commands=False,
                sandbox=False,
                coding=False,
                orchestrator=True,
            )
            sp_orch_idx = inv_orch["cmd"].index("--append-system-prompt")
            self.assertNotIn(fake_alert, inv_orch["cmd"][sp_orch_idx + 1])

    def test_agy_inject_quota_alert_for_workers_only(self):
        fake_alert = "[QUOTA ALERT] 테스트 알림"
        agy = engine_drivers.AGYEngine()
        with (
            patch.object(agent_engine, "get_engine_instance", return_value=agy),
            patch.object(agent_engine, "get_quota_alert_prompt", return_value=fake_alert),
        ):
            # 일반 워커 프롬프트 -> 주입됨
            injected = agent_engine._inject_quota_alert_for_agy("코드 원인 분석해줘")
            self.assertTrue(injected.startswith(fake_alert))
            self.assertTrue(injected.endswith("코드 원인 분석해줘"))

            # 슬래시 명령어 -> 미주입
            self.assertEqual(agent_engine._inject_quota_alert_for_agy("/usage"), "/usage")
            self.assertEqual(
                agent_engine._inject_quota_alert_for_agy("일반", slash_commands=True),
                "일반",
            )

            # 마스터(오케스트레이터) -> 미주입
            self.assertEqual(
                agent_engine._inject_quota_alert_for_agy("위임", orchestrator=True),
                "위임",
            )
            # 전문가/실무 세션 -> 상시 주입됨
            injected_coding = agent_engine._inject_quota_alert_for_agy("위임", expert_coding=True)
            self.assertTrue(injected_coding.startswith(fake_alert))
            injected_vault = agent_engine._inject_quota_alert_for_agy("위임", expert_vault=True)
            self.assertTrue(injected_vault.startswith(fake_alert))


if __name__ == "__main__":
    unittest.main()
