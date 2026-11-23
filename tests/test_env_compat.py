"""test_env_compat — 도메인별 환경변수 동기화 및 호환성 모듈 테스트."""

import os
import unittest
from unittest.mock import patch

from bot.core.env_compat import DOMAIN_ENV_MAPPINGS, get_domain_env, sync_domain_env


class TestEnvCompat(unittest.TestCase):
    def test_sync_domain_to_legacy(self):
        """DOMAIN__KEY 가 주어지면 대응되는 레거시 키로 자동 전파된다."""
        mock_env = {
            "AGENT__WORKDIR": "/vault/path",
            "AGENT__MODEL": "sonnet",
            "CALENDAR__ICAL_URL": "https://cal.example.com",
            "ANTHROPIC__API_TIMEOUT_MS": "5000",
            "WEB__PORT": "9000",
        }
        sync_domain_env(mock_env)

        self.assertEqual(mock_env.get("AGENT_WORKDIR"), "/vault/path")
        self.assertEqual(mock_env.get("AGENT_MODEL"), "sonnet")
        self.assertEqual(mock_env.get("GOOGLE_ICAL_URL"), "https://cal.example.com")
        self.assertEqual(mock_env.get("API_TIMEOUT_MS"), "5000")
        self.assertEqual(mock_env.get("WEB_PORT"), "9000")

    def test_sync_legacy_to_domain(self):
        """레거시 키만 주어져도 대응되는 DOMAIN__KEY 로 자동 전파된다."""
        mock_env = {
            "AGENT_WORKDIR": "/legacy/vault",
            "AGENT_MODEL": "legacy_model",
            "GOOGLE_ICAL_URL": "https://legacy.cal",
        }
        sync_domain_env(mock_env)

        self.assertEqual(mock_env.get("AGENT__WORKDIR"), "/legacy/vault")
        self.assertEqual(mock_env.get("AGENT__MODEL"), "legacy_model")
        self.assertEqual(mock_env.get("CALENDAR__ICAL_URL"), "https://legacy.cal")

    def test_domain_overrides_legacy_when_both_present(self):
        """둘 다 존재할 경우 새로운 도메인 키의 값이 우선 적용된다."""
        mock_env = {
            "WEB__PORT": "58392",
            "WEB_PORT": "8765",
        }
        sync_domain_env(mock_env)
        self.assertEqual(mock_env["WEB_PORT"], "58392")
        self.assertEqual(mock_env["WEB__PORT"], "58392")

    def test_get_domain_env(self):
        """get_domain_env 는 도메인 키 또는 레거시 키를 fallback 하여 읽는다."""
        mock_env = {
            "AGENT_MODEL": "gemini-flash",
        }
        val = get_domain_env("AGENT__MODEL", env_dict=mock_env)
        self.assertEqual(val, "gemini-flash")

        mock_env2 = {
            "AGENT__MODEL": "gemini-pro",
            "AGENT_MODEL": "gemini-flash",
        }
        val2 = get_domain_env("AGENT__MODEL", env_dict=mock_env2)
        self.assertEqual(val2, "gemini-pro")

        val_default = get_domain_env("NON_EXISTENT", default="def", env_dict=mock_env)
        self.assertEqual(val_default, "def")


if __name__ == "__main__":
    unittest.main()
