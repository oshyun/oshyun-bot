import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent_configs
import chat_db
import paths
import web_orchestrator


class DummyRequest:
    def __init__(self, body=None, query=None, match_info=None, method="POST"):
        self._body = body or {}
        self.query = query or {}
        self.match_info = match_info or {}
        self.method = method

    async def json(self):
        return self._body


class TestAgentConfigsSystem(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp_dir.name)
        self.vault_dir = self.tmp_path / "vault"
        self.code_dir = self.tmp_path / "code"
        self.state_dir = self.tmp_path / "state"
        self.vault_dir.mkdir(parents=True, exist_ok=True)
        self.code_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)

        self.cfg_file = self.state_dir / "agent_configs.json"
        self.runtime_instr_dir = self.state_dir / "agent_instructions"
        self.db_file = self.state_dir / "chat.db"

        self.patchers = [
            mock.patch.object(paths, "WORKDIR", str(self.vault_dir)),
            mock.patch.object(paths, "CODEDIR", str(self.code_dir)),
            mock.patch.object(paths, "STATE_DIR", str(self.state_dir)),
            mock.patch.object(paths, "AGENT_CONFIGS_FILE", str(self.cfg_file)),
            mock.patch.object(paths, "RUNTIME_INSTRUCTIONS_DIR", str(self.runtime_instr_dir)),
            mock.patch.object(paths, "CHAT_DB_FILE", str(self.db_file)),
            mock.patch.object(agent_configs, "WORKDIR", str(self.vault_dir)),
            mock.patch.object(agent_configs, "CODEDIR", str(self.code_dir)),
            mock.patch.object(agent_configs, "AGENT_CONFIGS_FILE", str(self.cfg_file)),
            mock.patch.object(agent_configs, "RUNTIME_INSTRUCTIONS_DIR", str(self.runtime_instr_dir)),
            mock.patch.object(chat_db, "CHAT_DB_FILE", str(self.db_file)),
        ]
        for p in self.patchers:
            p.start()
        chat_db.init_db()

    def tearDown(self):
        for p in reversed(self.patchers):
            p.stop()
        self.tmp_dir.cleanup()

    def test_builtin_agents_loaded_and_customizable(self):
        configs = agent_configs.list_agent_configs()
        ids = [c["id"] for c in configs]
        self.assertIn("__core__", ids)
        self.assertIn("__expert_vault__", ids)
        self.assertIn("__expert_coding__", ids)

        # 세컨브레인 하위 프롬프트 템플릿 및 모델 수정
        updated = agent_configs.upsert_agent_config(
            {
                "id": "__expert_vault__",
                "name": "세컨브레인 커스텀",
                "roleDescription": "볼트 및 가계부 심층 관리",
                "childModel": "gemini-3.8-flash",
                "childEffort": "high",
                "childPromptTemplate": "[세컨브레인 워커 특별 규칙]\n- 반드시 표로 요약할 것",
                "injectChildTemplate": True,
            }
        )
        self.assertEqual(updated["name"], "세컨브레인 커스텀")
        self.assertEqual(updated["childEffort"], "high")
        self.assertIn("표로 요약할 것", updated["childPromptTemplate"])

        # 하위 프롬프트 조립 확인
        assembled = agent_configs.compose_child_dispatch_prompt(
            "__expert_vault__", "오늘 식재료 재고 정리해줘"
        )
        self.assertIn("[세컨브레인 워커 특별 규칙]", assembled)
        self.assertIn("[작업 지시]\n오늘 식재료 재고 정리해줘", assembled)

        # 주입 비활성화(raw_prompt=True) 시 원문만 전달
        raw_only = agent_configs.compose_child_dispatch_prompt(
            "__expert_vault__", "오늘 식재료 재고 정리해줘", raw_prompt=True
        )
        self.assertEqual(raw_only, "오늘 식재료 재고 정리해줘")

        # 초기 기본값 복원 확인
        reset_cfg = agent_configs.reset_builtin_agent_config("__expert_vault__")
        self.assertEqual(reset_cfg["name"], "세컨브레인")
        self.assertEqual(reset_cfg["childEffort"], "medium")

    def test_custom_agent_lifecycle_and_workdir_resolution(self):
        # 볼트 하위 상대 경로로 신규 커스텀 에이전트 생성
        created = agent_configs.upsert_agent_config(
            {
                "name": "가계부 분석가",
                "emoji": "💰",
                "parentAgentId": "__expert_vault__",
                "roleDescription": "월간 지출 및 예산 분석 전담",
                "workdir": "금융/가계부",
                "model": "gemini-3.8-flash",
                "effort": "medium",
                "childModel": "gemini-3.8-flash",
                "childEffort": "low",
                "childPromptTemplate": "[가계부 워커 규칙]\n1. 금액 단위를 명확히 표기한다.",
                "systemPrompt": "# 가계부 분석가 지침\n항상 예산 대비 지출 비율을 계산한다.",
            }
        )
        custom_id = created["id"]
        self.assertTrue(custom_id.startswith("__custom_"))
        self.assertTrue(custom_id.endswith("__"))
        self.assertFalse(created["builtin"])
        expected_workdir = os.path.abspath(str(self.vault_dir / "금융/가계부"))
        self.assertEqual(created["workdir"], expected_workdir)

        # 런타임 AGENTS.md 동기화 확인
        runtime_dir = agent_configs.get_runtime_instruction_dir(custom_id)
        self.assertIsNotNone(runtime_dir)
        agents_md = Path(runtime_dir) / "AGENTS.md"
        self.assertTrue(agents_md.exists())
        self.assertIn("가계부 분석가 지침", agents_md.read_text(encoding="utf-8"))

        # child_default_workdir 연동 확인
        self.assertEqual(
            paths.child_default_workdir(custom_id),
            expected_workdir,
        )

        # 프롬프트 투명성 미리보기 검증
        preview = agent_configs.build_agent_prompt_preview(
            custom_id, sample_task="이번 달 카드값 합계 계산해줘"
        )
        self.assertEqual(preview["agentId"], custom_id)
        self.assertTrue(preview["injectChildTemplate"])
        self.assertIn("[가계부 워커 규칙]", preview["assembledChildUserPrompt"])
        self.assertIn("이번 달 카드값 합계 계산해줘", preview["assembledChildUserPrompt"])
        self.assertIn(f"--parent {custom_id}", preview["spawnCliExample"])

        # 기본 에이전트는 삭제 불가
        with self.assertRaises(ValueError):
            agent_configs.delete_custom_agent_config("__core__")

        # 커스텀 에이전트 삭제 확인
        deleted = agent_configs.delete_custom_agent_config(custom_id)
        self.assertTrue(deleted)
        self.assertIsNone(agent_configs.get_agent_config(custom_id))

    async def test_api_handlers_and_dispatch_prompt_injection(self):
        # 1. POST /api/orchestrator/agents (신규 에이전트 생성)
        req_create = DummyRequest(
            body={
                "action": "upsert",
                "name": "독서 비서",
                "emoji": "📚",
                "parentAgentId": "__core__",
                "roleDescription": "독서 노트 요약 및 인사이트 정리",
                "childModel": "gemini-3.8-flash",
                "childEffort": "low",
                "childPromptTemplate": "[독서 비서 워커 규칙]\n- 핵심 문장 3개를 인용할 것",
                "injectChildTemplate": True,
            }
        )
        with mock.patch("web_execution.broadcast_ws_json", new=mock.AsyncMock()):
            resp_create = await web_orchestrator.handle_orchestrator_agent_upsert(req_create)
        data_create = json.loads(resp_create.text)
        self.assertTrue(data_create["ok"])
        custom_agent = data_create["agent"]
        custom_id = custom_agent["id"]
        self.assertIn(custom_id, data_create["orchestratorRoomIds"])

        # 2. POST /api/orchestrator/agents/preview
        req_prev = DummyRequest(
            body={
                "agentId": custom_id,
                "sampleTask": "최근 읽은 책 요약해줘",
            }
        )
        resp_prev = await web_orchestrator.handle_orchestrator_agent_preview(req_prev)
        data_prev = json.loads(resp_prev.text)
        self.assertTrue(data_prev["ok"])
        self.assertIn("[독서 비서 워커 규칙]", data_prev["preview"]["assembledChildUserPrompt"])

        # 3. POST /api/orchestrator/dispatch — 커스텀 에이전트를 부모로 스폰 시 childPromptTemplate 자동 주입 및 childEffort 적용 검증
        captured_enqueue = {}

        async def fake_enqueue_room_turn(**kwargs):
            captured_enqueue.update(kwargs)
            return (False, 0)

        req_dispatch = DummyRequest(
            body={
                "action": "spawn",
                "parentAgentId": custom_id,
                "title": "독서 요약 워커",
                "prompt": "사피엔스 노트 요약해줘",
                "wait": False,
            }
        )
        import contextlib
        import sys

        patches = [
            mock.patch("web_execution.broadcast_ws_json", new=mock.AsyncMock()),
            mock.patch("web_execution.enqueue_room_turn", side_effect=fake_enqueue_room_turn),
        ]
        if "web_server" in sys.modules:
            ws_mod = sys.modules["web_server"]
            if hasattr(ws_mod, "broadcast_ws_json"):
                patches.append(mock.patch.object(ws_mod, "broadcast_ws_json", new=mock.AsyncMock()))
            if hasattr(ws_mod, "enqueue_room_turn"):
                patches.append(mock.patch.object(ws_mod, "enqueue_room_turn", side_effect=fake_enqueue_room_turn))

        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            resp_disp = await web_orchestrator.handle_orchestrator_dispatch(req_dispatch)
        data_disp = json.loads(resp_disp.text)
        self.assertTrue(data_disp["ok"])
        self.assertEqual(data_disp["parentAgentId"], custom_id)
        self.assertIn("[독서 비서 워커 규칙]", captured_enqueue["user_text"])
        self.assertIn("사피엔스 노트 요약해줘", captured_enqueue["user_text"])
        self.assertEqual(captured_enqueue["effort"], "low")

        # 4. POST /api/orchestrator/agents/delete
        req_del = DummyRequest(body={"agentId": custom_id, "deleteRoom": True})
        with mock.patch("web_execution.broadcast_ws_json", new=mock.AsyncMock()):
            resp_del = await web_orchestrator.handle_orchestrator_agent_delete(req_del)
        data_del = json.loads(resp_del.text)
        self.assertTrue(data_del["ok"])
        self.assertNotIn(custom_id, data_del["orchestratorRoomIds"])


if __name__ == "__main__":
    unittest.main()
