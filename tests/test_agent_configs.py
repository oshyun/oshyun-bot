import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import bot.core.agent_configs as agent_configs
import bot.core.chat_db as chat_db
import bot.core.paths as paths
import bot.web.orchestrator as web_orchestrator


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
        self.assertIn("__expert_general__", ids)
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
        with mock.patch("bot.web.execution.broadcast_ws_json", new=mock.AsyncMock()):
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
            return {"already_running": False, "queue_size": 0}

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

        patches = [
            mock.patch("bot.web.execution.broadcast_ws_json", new=mock.AsyncMock()),
            mock.patch("bot.web.execution.enqueue_room_turn", side_effect=fake_enqueue_room_turn),
        ]

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
        with mock.patch("bot.web.execution.broadcast_ws_json", new=mock.AsyncMock()):
            resp_del = await web_orchestrator.handle_orchestrator_agent_delete(req_del)
        data_del = json.loads(resp_del.text)
        self.assertTrue(data_del["ok"])
        self.assertNotIn(custom_id, data_del["orchestratorRoomIds"])

    def test_nested_agent_hierarchy_and_parent_reassignment(self):
        """세컨브레인 산하 에이전트 및 중첩 하위 에이전트 생성, 부모 삭제 시 마스터 재배치 검증."""
        # 1. 세컨브레인 산하 화장품 에이전트 생성
        cosmetics = agent_configs.upsert_agent_config(
            {
                "name": "화장품 분석가",
                "emoji": "💄",
                "parentAgentId": "__expert_vault__",
                "roleDescription": "화장품 성분 및 재고 관리",
            }
        )
        cosmetics_id = cosmetics["id"]
        self.assertEqual(cosmetics["parentAgentId"], "__expert_vault__")

        # 2. 화장품 에이전트 산하 스킨케어 에이전트 생성 (N-Level 중첩 계층)
        skincare = agent_configs.upsert_agent_config(
            {
                "name": "스킨케어 루틴",
                "emoji": "🧴",
                "parentAgentId": cosmetics_id,
                "roleDescription": "스킨케어 루틴 최적화",
            }
        )
        skincare_id = skincare["id"]
        self.assertEqual(skincare["parentAgentId"], cosmetics_id)

        # 3. 중간 부모(화장품 에이전트) 삭제 시 하위(스킨케어)가 __core__로 안전하게 재배치되는지 확인
        deleted = agent_configs.delete_custom_agent_config(cosmetics_id)
        self.assertTrue(deleted)
        reloaded_skincare = agent_configs.get_agent_config(skincare_id)
        self.assertIsNotNone(reloaded_skincare)
        self.assertEqual(reloaded_skincare["parentAgentId"], "__core__")

    def test_cycle_prevention_in_agent_hierarchy(self):
        """에이전트 계층 설정 시 순환 참조(A->B->A, 자기자신 등) 방지 및 마스터 폴백 검증."""
        # 1. 에이전트 A 생성 (부모: 마스터)
        agent_a = agent_configs.upsert_agent_config(
            {"name": "에이전트A", "parentAgentId": "__core__"}
        )
        id_a = agent_a["id"]

        # 2. 에이전트 B 생성 (부모: A)
        agent_b = agent_configs.upsert_agent_config(
            {"name": "에이전트B", "parentAgentId": id_a}
        )
        id_b = agent_b["id"]
        self.assertEqual(agent_b["parentAgentId"], id_a)

        # 3. A의 부모를 B로 변경 시도 (A -> B -> A 순환)
        updated_a = agent_configs.upsert_agent_config(
            {"id": id_a, "parentAgentId": id_b}
        )
        # 순환 감지되어 마스터(__core__)로 안전하게 유지/폴백됨
        self.assertEqual(updated_a["parentAgentId"], "__core__")

        # 4. 자기 자신을 부모로 지정 시도
        self_parent = agent_configs.upsert_agent_config(
            {"id": id_b, "parentAgentId": id_b}
        )
        self.assertEqual(self_parent["parentAgentId"], "__core__")

    def test_custom_agent_subchat_spawn_instruction_enforcement(self):
        """커스텀 전문가 에이전트가 systemPrompt 유무와 무관하게 하위 채팅 스폰(session-ctl spawn) 프로토콜을 탑재하는지 검증."""
        import bot.engine.engine_drivers as engine_drivers
        from bot.engine.engine_drivers import get_system_prompt

        # 1. systemPrompt가 비어 있는 화장품 전문가 에이전트 생성
        cosmetics = agent_configs.upsert_agent_config(
            {
                "name": "화장품",
                "emoji": "💄",
                "parentAgentId": "__expert_vault__",
                "roleDescription": "화장품 성분 분석 및 루틴 추천",
                "systemPrompt": "",
                "childModel": "gemini-3.8-flash",
                "childEffort": "medium",
            }
        )
        cosmetics_id = cosmetics["id"]

        eff_prompt = agent_configs.get_effective_agent_system_prompt(cosmetics_id)
        self.assertIn("session-ctl spawn", eff_prompt)
        self.assertIn(f"--parent {cosmetics_id}", eff_prompt)
        self.assertIn("~시켜", eff_prompt)
        self.assertIn("사전 탐색 제로", eff_prompt)
        self.assertIn("--wait", eff_prompt)

        # get_agent_instruction_content 호환성 검증
        content = agent_configs.get_agent_instruction_content(cosmetics_id)
        self.assertIsNotNone(content)
        self.assertIn("session-ctl spawn", content)

        # 런타임 AGENTS.md 파일 동기화 내용 검증
        rt_dir = agent_configs.get_runtime_instruction_dir(cosmetics_id)
        self.assertIsNotNone(rt_dir)
        agents_md_text = (Path(rt_dir) / "AGENTS.md").read_text(encoding="utf-8")
        self.assertEqual(agents_md_text.strip(), eff_prompt.strip())

        # engine_drivers._resolve_system_prompt 및 get_system_prompt 호출 시 실효 프롬프트 반영 검증
        engine_sys = get_system_prompt(agent_id=cosmetics_id)
        self.assertIn("session-ctl spawn", engine_sys)
        self.assertIn(f"--parent {cosmetics_id}", engine_sys)
        resolved_sys = engine_drivers._resolve_system_prompt(agent_id=cosmetics_id)
        self.assertEqual(engine_sys, resolved_sys)

        # 2. 사용자가 도메인 특화 systemPrompt를 입력한 경우 (프로토콜 + 도메인 지침 결합)
        updated = agent_configs.upsert_agent_config(
            {
                "id": cosmetics_id,
                "systemPrompt": "# 화장품 전문가 도메인 수칙\n- 레티날과 비타민C는 동시 사용을 금지한다.",
            }
        )
        eff_prompt_domain = agent_configs.get_effective_agent_system_prompt(cosmetics_id)
        self.assertIn("화장품 전문가 도메인 수칙", eff_prompt_domain)
        self.assertIn("레티날과 비타민C", eff_prompt_domain)
        self.assertIn("session-ctl spawn", eff_prompt_domain)
        self.assertIn("~시켜", eff_prompt_domain)
        self.assertIn(f"--parent {cosmetics_id}", eff_prompt_domain)

    async def test_room_settings_default_inheritance_and_reset(self):
        """대화방 설정에서 모델/Effort 복원 시 에이전트 계층의 기본값이 올바르게 계산·적용되는지 검증."""
        import bot.web.rooms as web_rooms

        # 1. 커스텀 코딩 하위 에이전트 생성 (상위: __expert_coding__)
        custom_coder = agent_configs.upsert_agent_config({
            "name": "고난도코더",
            "parentAgentId": "__expert_coding__",
            "model": "gemini-4-argon",
            "effort": "high",
            "childModel": "gemini-4-hydrogen",
            "childEffort": "xhigh",
        })
        coder_id = custom_coder["id"]

        # 2. chat.db에 방 생성 (오케스트레이터 방 & 자식 방)
        chat_db.get_or_create_room(coder_id, title="고난도코더 에이전트")
        chat_db.get_or_create_room(
            "room-sub-task",
            title="고난도 작업 서브챗",
            parent_agent_id=coder_id,
            model="gemini-3.8-flash",
            effort="low",
        )

        # 3. _load_chat_rooms_index() 호출 시 defaultModel/defaultEffort 확인
        rooms = web_rooms._load_chat_rooms_index()
        coder_room = next(r for r in rooms if r["id"] == coder_id)
        sub_room = next(r for r in rooms if r["id"] == "room-sub-task")

        # 에이전트 본체 방의 기본값은 해당 에이전트의 model/effort
        self.assertEqual(coder_room["defaultModel"], "gemini-4-argon")
        self.assertEqual(coder_room["defaultEffort"], "high")

        # 자식 방의 기본값은 부모 에이전트의 childModel/childEffort
        self.assertEqual(sub_room["defaultModel"], "gemini-4-hydrogen")
        self.assertEqual(sub_room["defaultEffort"], "xhigh")

        # 4. 자식 방 설정 초기화(복원) API 호출 시 (model="", effort="")
        req = DummyRequest(body={"roomId": "room-sub-task", "model": "", "effort": ""})
        resp = await web_rooms.handle_api_room_settings(req)
        data = json.loads(resp.text)
        self.assertTrue(data["ok"])
        self.assertIsNone(data["model"])
        self.assertIsNone(data["effort"])
        self.assertEqual(data["defaultModel"], "gemini-4-hydrogen")
        self.assertEqual(data["defaultEffort"], "xhigh")

        # 5. DB에서 모델 오버라이드가 제거되었는지 확인
        sub_db = chat_db.get_room("room-sub-task")
        self.assertIsNone(sub_db.get("model"))
        self.assertIsNone(sub_db.get("effort"))


if __name__ == "__main__":
    unittest.main()
