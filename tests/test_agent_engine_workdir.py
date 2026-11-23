"""test_agent_engine_workdir — agent_engine workdir 안전 폴백 단위 테스트."""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

_SRC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"
)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from bot.core.paths import ROOT_DIR, WORKDIR
import bot.engine.agent_engine as agent_engine


class TestAgentEngineWorkdirFallback(unittest.TestCase):
    def test_build_invocation_workdir_fallback(self):
        """존재하지 않는 workdir 로 호출 시 ROOT_DIR 또는 WORKDIR 로 자동 폴백되는지 검증."""
        fake_engine = MagicMock()
        fake_engine.build_cmd.return_value = {"cmd": ["echo"], "workdir": "dummy", "env": {}}

        with patch("bot.engine.agent_engine.get_engine_instance", return_value=fake_engine):
            # 1. coding=True 일 때 무효 경로 -> ROOT_DIR 로 폴백
            agent_engine._build_invocation(
                "test", None, workdir="/nonexistent/path/xyz", model="test-model", coding=True
            )
            fake_engine.build_cmd.assert_called_once()
            call_kwargs = fake_engine.build_cmd.call_args[1]
            self.assertEqual(call_kwargs["workdir"], os.path.realpath(ROOT_DIR))

            # 2. expert_coding=True 일 때 무효 경로 -> ROOT_DIR 로 폴백
            fake_engine.reset_mock()
            agent_engine._build_invocation(
                "test", None, workdir="/nonexistent/path/xyz", model="test-model", expert_coding=True
            )
            fake_engine.build_cmd.assert_called_once()
            call_kwargs = fake_engine.build_cmd.call_args[1]
            self.assertEqual(call_kwargs["workdir"], os.path.realpath(ROOT_DIR))


if __name__ == "__main__":
    unittest.main()
