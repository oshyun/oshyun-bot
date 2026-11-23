"""test_paths — paths 모듈 경로 해석 및 환경변수 fallback 단위 테스트."""

import os
import sys
import unittest
from unittest.mock import patch

_SRC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"
)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

import bot.core.paths as paths


class TestPaths(unittest.TestCase):
    def test_paths_structure(self):
        """기본 경로 상수들이 서로 올바른 부모-자식 관계를 맺고 있는지 검증."""
        self.assertTrue(os.path.isabs(paths.ROOT_DIR))
        self.assertTrue(os.path.isabs(paths.DATA_DIR))

        # LOGS_DIR, STATE_DIR, JOBS_DIR, AGENT_HOME_DIR 은 DATA_DIR 하위여야 함
        self.assertEqual(paths.LOGS_DIR, os.path.join(paths.DATA_DIR, "logs"))
        self.assertEqual(paths.STATE_DIR, os.path.join(paths.DATA_DIR, "state"))
        self.assertEqual(paths.JOBS_DIR, os.path.join(paths.DATA_DIR, "jobs"))
        self.assertEqual(paths.ATTACHED_FILES_DIR, os.path.join(paths.DATA_DIR, "attached_files"))
        self.assertEqual(paths.AGENT_HOME_DIR, os.path.join(paths.DATA_DIR, "claude-home"))
        self.assertEqual(paths.AGENT_TMP_DIR, os.path.join(paths.AGENT_HOME_DIR, "tmp"))

        # 로그 파일들은 LOGS_DIR 하위
        self.assertEqual(paths.SESSION_LOG_FILE, os.path.join(paths.LOGS_DIR, "session_log.jsonl"))
        self.assertEqual(paths.WEB_LOG_FILE, os.path.join(paths.LOGS_DIR, "web-server.log"))

        # 상태 파일들은 STATE_DIR 하위
        self.assertEqual(paths.BOT_SETTINGS_FILE, os.path.join(paths.STATE_DIR, "bot_settings.json"))
        self.assertEqual(paths.SCHEDULES_FILE, os.path.join(paths.STATE_DIR, "schedules.json"))
        self.assertEqual(paths.WEB_JOBS_FILE, os.path.join(paths.STATE_DIR, "web_jobs.json"))
        self.assertEqual(paths.BURN_TODO_FILE, os.path.join(paths.STATE_DIR, "burn_todo.json"))
        self.assertEqual(paths.BURN_STATE_FILE, os.path.join(paths.STATE_DIR, "burn_state.json"))
        self.assertEqual(paths.GOOGLE_CREDENTIALS_FILE, os.path.join(paths.STATE_DIR, "google_credentials.json"))
        self.assertEqual(paths.GOOGLE_TOKEN_FILE, os.path.join(paths.STATE_DIR, "google_token.json"))

    def test_resolve_workdir_precedence(self):
        """_resolve_workdir: AGENT_WORKDIR -> VAULT_DIR -> CLAUDE_WORKDIR -> 기본값 ~"""
        # 1. AGENT_WORKDIR 우선
        with patch.dict(
            os.environ,
            {
                "AGENT_WORKDIR": "/custom/agent/vault",
                "VAULT_DIR": "/custom/vault",
                "CLAUDE_WORKDIR": "/custom/claude",
            },
            clear=False,
        ):
            self.assertEqual(paths._resolve_workdir(), "/custom/agent/vault")

        # 2. AGENT_WORKDIR 가 없을 때 VAULT_DIR
        env_without_agent = os.environ.copy()
        env_without_agent.pop("AGENT_WORKDIR", None)
        env_without_agent.pop("AGENT__WORKDIR", None)
        env_without_agent["VAULT_DIR"] = "/custom/vault"
        env_without_agent["CLAUDE_WORKDIR"] = "/custom/claude"
        with patch.dict(os.environ, env_without_agent, clear=True):
            self.assertEqual(paths._resolve_workdir(), "/custom/vault")

        # 3. AGENT_WORKDIR, VAULT_DIR 모두 없을 때 CLAUDE_WORKDIR
        env_claude_only = {
            "CLAUDE_WORKDIR": "/custom/claude",
        }
        with patch.dict(os.environ, env_claude_only, clear=True):
            self.assertEqual(paths._resolve_workdir(), "/custom/claude")

        # 4. 아무것도 없을 때 기본값 ~ (홈 디렉토리)
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(paths._resolve_workdir(), os.path.expanduser("~"))

        # 5. ~ 가 지정되어 있어도 홈 디렉토리로 확장
        with patch.dict(os.environ, {"AGENT_WORKDIR": "~"}, clear=True):
            self.assertEqual(paths._resolve_workdir(), os.path.expanduser("~"))

        # 6. 도메인 키 AGENT__WORKDIR 지원
        with patch.dict(os.environ, {"AGENT__WORKDIR": "/domain/agent/vault"}, clear=True):
            self.assertEqual(paths._resolve_workdir(), "/domain/agent/vault")

    def test_resolve_codedir_precedence(self):
        """_resolve_codedir: AGENT_CODEDIR -> CODE_DIR -> CLAUDE_CODEDIR -> None"""
        # 1. AGENT_CODEDIR 우선
        with patch.dict(
            os.environ,
            {
                "AGENT_CODEDIR": "/code/agent",
                "CODE_DIR": "/code/dir",
                "CLAUDE_CODEDIR": "/code/claude",
            },
            clear=False,
        ):
            self.assertEqual(paths._resolve_codedir(), "/code/agent")

        # 2. 도메인 키 AGENT__CODEDIR 지원
        with patch.dict(os.environ, {"AGENT__CODEDIR": "/domain/code/agent"}, clear=True):
            self.assertEqual(paths._resolve_codedir(), "/domain/code/agent")

        # 3. CODE_DIR
        env_code_dir = {"CODE_DIR": "/code/dir", "CLAUDE_CODEDIR": "/code/claude"}
        with patch.dict(os.environ, env_code_dir, clear=True):
            self.assertEqual(paths._resolve_codedir(), "/code/dir")

        # 3. CLAUDE_CODEDIR
        env_claude_code = {"CLAUDE_CODEDIR": "/code/claude"}
        with patch.dict(os.environ, env_claude_code, clear=True):
            self.assertEqual(paths._resolve_codedir(), "/code/claude")

        # 4. 전부 비어있을 때 None
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(paths._resolve_codedir())

    def test_child_default_workdir_fallback(self):
        """child_default_workdir: __expert_coding__ 일 때 무효 경로면 ROOT_DIR 로 안전 폴백"""
        with patch.object(paths, "CODEDIR", "/nonexistent/invalid/repo/path"), \
             patch.object(paths, "WORKDIR", "/nonexistent/invalid/vault/path"):
            resolved = paths.child_default_workdir("__expert_coding__")
            self.assertEqual(resolved, paths.ROOT_DIR)

        with patch.object(paths, "CODEDIR", paths.ROOT_DIR):
            resolved = paths.child_default_workdir("__expert_coding__")
            self.assertEqual(resolved, paths.ROOT_DIR)


if __name__ == "__main__":
    unittest.main()
