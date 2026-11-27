"""단순 인사 턴 지연 방지 및 오케스트레이터 볼트 마운트 최적화 단위 테스트."""

import unittest
from unittest.mock import MagicMock, patch

from bot.engine.engine_drivers import AGYEngine
from bot.web.turn_execution import is_simple_greeting_fast_path


class TestGreetingLatencyOptimization(unittest.IsolatedAsyncioTestCase):
    def test_is_simple_greeting_fast_path(self):
        # 1. 단순 인사 / 안부 / 단답 (Fast-Path 대상: True)
        valid_greetings = [
            "안녕",
            "안녕?",
            "안녕하세요",
            "안녕하세요!",
            "안뇽~",
            "반가워",
            "반갑습니다!",
            "하이",
            "하이요",
            "hi",
            "hello",
            "좋은 아침",
            "좋은아침입니다",
            "고마워",
            "감사합니다",
            "수고해",
            "누구야",
            "너 누구야?",
            "ㅎㅇ",
        ]
        for greeting in valid_greetings:
            with self.subTest(greeting=greeting):
                self.assertTrue(
                    is_simple_greeting_fast_path(greeting),
                    f"'{greeting}'은 단순 인사로 판별되어야 함",
                )

        # 2. 질문, 명령, 긴 문장 (Fast-Path 제외: False)
        non_greetings = [
            "안녕 오늘 일정 어때?",
            "안녕하세요 코드 작성해줘",
            "안녕 파일 검색해줘",
            "왜 안 되는 거야?",
            "오늘 날씨 알려줘",
            "회의록 요약해봐",
            "안녕" * 15,  # 너무 긴 문장
            "",
            "   ",
        ]
        for text in non_greetings:
            with self.subTest(text=text):
                self.assertFalse(
                    is_simple_greeting_fast_path(text),
                    f"'{text}'은 단순 인사에서 제외되어야 함",
                )

    def test_orchestrator_skips_vault_add_dir(self):
        """마스터(orchestrator=True)는 거대 볼트 디렉토리를 --add-dir 하지 않아야 함."""
        driver = AGYEngine()
        vault_workdir = "/Users/user/Obsidian/Vault"

        # 1. 마스터(orchestrator=True)일 때
        cmd_info_master = driver.build_cmd(
            prompt="안녕",
            session_id=None,
            workdir=vault_workdir,
            model="gemini-3.8-flash",
            effort="medium",
            extra_args=None,
            slash_commands=False,
            sandbox=False,
            coding=False,
            orchestrator=True,
        )
        cmd_master = cmd_info_master["cmd"]
        # --add-dir 인자 쌍들을 수집
        add_dirs_master = [
            cmd_master[i + 1]
            for i, arg in enumerate(cmd_master)
            if arg == "--add-dir" and i + 1 < len(cmd_master)
        ]
        self.assertNotIn(
            vault_workdir,
            add_dirs_master,
            "마스터 실행 시 거대 볼트 경로는 --add-dir에 포함되지 않아야 함",
        )

        # 2. 일반/전문가(orchestrator=False)일 때
        cmd_info_worker = driver.build_cmd(
            prompt="노트 읽어줘",
            session_id=None,
            workdir=vault_workdir,
            model="gemini-3.8-flash",
            effort="medium",
            extra_args=None,
            slash_commands=False,
            sandbox=False,
            coding=False,
            orchestrator=False,
            expert_vault=True,
        )
        cmd_worker = cmd_info_worker["cmd"]
        add_dirs_worker = [
            cmd_worker[i + 1]
            for i, arg in enumerate(cmd_worker)
            if arg == "--add-dir" and i + 1 < len(cmd_worker)
        ]
        self.assertIn(
            vault_workdir,
            add_dirs_worker,
            "전문가(볼트) 실행 시 볼트 경로는 --add-dir에 정상 포함되어야 함",
        )

    @patch("bot.web.turn_execution._active_agent_pids", {})
    @patch("bot.web.turn_execution._save_web_job")
    @patch("bot.web.turn_execution.spawn_agent")
    @patch("bot.web.turn_execution.watch_agent")
    @patch("bot.web.turn_execution.broadcast_ws_json")
    @patch("bot.web.turn_execution.chat_db")
    async def test_execute_chat_turn_fast_path_effort(
        self,
        mock_chat_db,
        mock_broadcast,
        mock_watch,
        mock_spawn,
        mock_save_job,
    ):
        """단순 인사 발화 시 execute_chat_turn이 active_effort를 low로 완화하는지 검증."""
        from bot.web.turn_execution import execute_chat_turn

        mock_chat_db.get_room.return_value = {"id": "__core__", "effort": "medium"}
        mock_chat_db.add_message.return_value = 999
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_spawn.return_value = mock_proc
        mock_watch.return_value = {"result": "안녕하세요! 반갑습니다.", "status": "done"}

        res = await execute_chat_turn(
            user_text="안녕?",
            is_voice_mode=False,
            room_id="__core__",
            effort="medium",
        )

        self.assertTrue(res.get("ok"))
        # spawn_agent 호출 시 넘겨진 effort kwargs 검증
        self.assertTrue(mock_spawn.called)
        call_kwargs = mock_spawn.call_args.kwargs
        self.assertEqual(
            call_kwargs.get("effort"),
            "low",
            "단순 인사 발화 시 spawn_agent의 effort는 'low'로 완화되어야 함",
        )


if __name__ == "__main__":
    unittest.main()
