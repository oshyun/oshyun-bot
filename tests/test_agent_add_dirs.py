"""에이전트/마스터별 add-dir 사전 정의 및 UI/DB 설정 연동 단위 테스트."""

import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import bot.core.agent_configs as agent_configs
import bot.core.chat_db as chat_db
from bot.engine.engine_drivers import AGYEngine


class TestAgentAddDirsConfig(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_db_fd, self.temp_db_path = tempfile.mkstemp(suffix=".db")
        chat_db.init_db(self.temp_db_path)

    def tearDown(self):
        try:
            os.close(self.temp_db_fd)
            if os.path.exists(self.temp_db_path):
                os.remove(self.temp_db_path)
        except OSError:
            pass

    def test_builtin_defaults_add_dirs(self):
        """기본 내장 에이전트들의 addDirs 가 올바르게 사전 정의되어 있는지 검증."""
        core = agent_configs.get_agent_config(agent_configs.CORE_ORCHESTRATOR_ROOM_ID)
        vault = agent_configs.get_agent_config(agent_configs.EXPERT_VAULT_ROOM_ID)
        coding = agent_configs.get_agent_config(agent_configs.EXPERT_CODING_ROOM_ID)

        self.assertIsNotNone(core)
        self.assertIn("addDirs", core)
        # 마스터는 볼트 전체가 아닌 scripts 등 필요한 경로만 포함해야 함
        self.assertNotIn(agent_configs.WORKDIR, core["addDirs"])

        self.assertIsNotNone(vault)
        self.assertIn("addDirs", vault)
        # 세컨브레인은 볼트 경로가 포함되어야 함
        self.assertIn(agent_configs.WORKDIR, vault["addDirs"])

        self.assertIsNotNone(coding)
        self.assertIn("addDirs", coding)

    def test_normalize_agent_record_add_dirs(self):
        """_normalize_agent_record 가 다양한 포맷의 addDirs 를 정규화하는지 검증."""
        defaults_map = agent_configs._builtin_defaults()
        with tempfile.TemporaryDirectory() as tmpdir1, tempfile.TemporaryDirectory() as tmpdir2:
            # 1. 줄바꿈 문자열 포맷 + 주석/공백 포함
            raw_str = f"  {tmpdir1}  \n# 주석 라인\n\n{tmpdir2}\n"
            norm = agent_configs._normalize_agent_record(
                "__custom_test__",
                {"name": "테스트", "addDirs": raw_str},
                defaults_map,
            )
            self.assertIn(os.path.abspath(tmpdir1), norm["addDirs"])
            self.assertIn(os.path.abspath(tmpdir2), norm["addDirs"])
            self.assertEqual(len(norm["addDirs"]), 2)

            # 2. 리스트 포맷
            norm_list = agent_configs._normalize_agent_record(
                "__custom_test2__",
                {"name": "테스트2", "add_dirs": [tmpdir1, tmpdir2, tmpdir1]},
                defaults_map,
            )
            # 중복 제거 검증
            self.assertEqual(len(norm_list["addDirs"]), 2)

    def test_sanitize_add_dirs(self):
        """sanitize_add_dirs 가 존재하는 디렉토리만 필터링하는지 검증."""
        with tempfile.TemporaryDirectory() as exist_dir:
            non_exist = os.path.join(exist_dir, "non_existent_subdir_xyz")
            sanitized = agent_configs.sanitize_add_dirs([exist_dir, non_exist, "", None])
            self.assertIn(os.path.abspath(exist_dir), sanitized)
            self.assertNotIn(non_exist, sanitized)
            self.assertEqual(len(sanitized), 1)

    def test_chat_db_room_add_dirs_persistence(self):
        """chat_db 에서 rooms 테이블에 add_dirs 가 저장되고 get_room/get_rooms 로 정상 복원되는지 검증."""
        room_id = "test-room-dirs"
        sample_dirs = ["/tmp/test_dir_1", "/tmp/test_dir_2"]

        # 1. get_or_create_room 시 add_dirs 저장
        chat_db.get_or_create_room(
            room_id=room_id,
            title="디렉토리 테스트 방",
            add_dirs=sample_dirs,
            db_path=self.temp_db_path,
        )

        room = chat_db.get_room(room_id, db_path=self.temp_db_path)
        self.assertIsNotNone(room)
        self.assertEqual(room.get("addDirs"), sample_dirs)

        # 2. update_room 으로 갱신
        new_dirs = ["/tmp/test_dir_updated"]
        chat_db.update_room(room_id, add_dirs=new_dirs, db_path=self.temp_db_path)

        updated = chat_db.get_room(room_id, db_path=self.temp_db_path)
        self.assertEqual(updated.get("addDirs"), new_dirs)

    def test_build_cmd_injects_add_dirs(self):
        """AGYEngine.build_cmd 에 add_dirs 목록 전달 시 --add-dir 플래그로 올바르게 주입되는지 검증."""
        driver = AGYEngine()
        with tempfile.TemporaryDirectory() as dir1, tempfile.TemporaryDirectory() as dir2:
            res = driver.build_cmd(
                prompt="테스트",
                session_id=None,
                workdir=dir1,
                model="gemini-3.8-flash",
                effort="medium",
                extra_args=None,
                slash_commands=False,
                sandbox=False,
                coding=False,
                orchestrator=True,
                add_dirs=[dir1, dir2],
            )
            cmd = res["cmd"]
            add_dirs_in_cmd = [
                cmd[i + 1]
                for i, arg in enumerate(cmd)
                if arg == "--add-dir" and i + 1 < len(cmd)
            ]
            self.assertIn(os.path.realpath(dir1), [os.path.realpath(d) for d in add_dirs_in_cmd])
            self.assertIn(os.path.realpath(dir2), [os.path.realpath(d) for d in add_dirs_in_cmd])

    @patch("bot.web.turn_execution._active_agent_pids", {})
    @patch("bot.web.turn_execution._save_web_job")
    @patch("bot.web.turn_execution.spawn_agent")
    @patch("bot.web.turn_execution.watch_agent")
    @patch("bot.web.turn_execution.broadcast_ws_json")
    @patch("bot.web.turn_execution.chat_db")
    async def test_execute_chat_turn_passes_add_dirs_to_spawn_agent(
        self,
        mock_chat_db,
        mock_broadcast,
        mock_watch,
        mock_spawn,
        mock_save_job,
    ):
        """execute_chat_turn 실행 시 방/에이전트의 addDirs 가 spawn_agent 에 kwargs 로 주입되는지 검증."""
        from bot.web.turn_execution import execute_chat_turn

        sample_add_dirs = ["/tmp/custom_agent_dir"]
        mock_chat_db.get_room.return_value = {
            "id": "room-with-dirs",
            "model": "gemini-3.8-flash",
            "effort": "medium",
            "addDirs": sample_add_dirs,
        }
        mock_chat_db.add_message.return_value = 101
        mock_proc = MagicMock()
        mock_proc.pid = 9876
        mock_spawn.return_value = mock_proc
        mock_watch.return_value = {"result": "응답 완료", "status": "done"}

        res = await execute_chat_turn(
            user_text="작업 실행해줘",
            is_voice_mode=False,
            room_id="room-with-dirs",
        )

        self.assertTrue(res.get("ok"))
        self.assertTrue(mock_spawn.called)
        kwargs = mock_spawn.call_args.kwargs
        self.assertIn("add_dirs", kwargs)
        self.assertEqual(kwargs["add_dirs"], sample_add_dirs)


if __name__ == "__main__":
    unittest.main()
