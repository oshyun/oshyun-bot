"""시스템 프롬프트 및 인스트럭션 투명 조회/수정/복원 통합 관리 체계 단위 테스트."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

import bot.core.agent_configs as agent_configs
import bot.core.paths as paths
from bot.web.orchestrator import (
    handle_prompt_detail,
    handle_prompt_reset,
    handle_prompt_save,
    handle_prompts_list,
)


class TestTransparentSystemPrompts(AioHTTPTestCase):
    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp(prefix="test_prompts_")
        self.state_dir = os.path.join(self.test_dir, "state")
        self.custom_dir = os.path.join(self.state_dir, "custom_instructions")
        self.runtime_dir = os.path.join(self.state_dir, "agent_instructions")
        self.configs_file = os.path.join(self.state_dir, "agent_configs.json")
        os.makedirs(self.custom_dir, exist_ok=True)
        os.makedirs(self.runtime_dir, exist_ok=True)

        self._patchers = [
            patch("bot.core.paths.STATE_DIR", self.state_dir),
            patch("bot.core.paths.CUSTOM_INSTRUCTIONS_DIR", self.custom_dir),
            patch("bot.core.paths.RUNTIME_INSTRUCTIONS_DIR", self.runtime_dir),
            patch("bot.core.paths.AGENT_CONFIGS_FILE", self.configs_file),
            patch("bot.core.agent_configs.CUSTOM_INSTRUCTIONS_DIR", self.custom_dir),
            patch("bot.core.agent_configs.RUNTIME_INSTRUCTIONS_DIR", self.runtime_dir),
            patch("bot.core.agent_configs.AGENT_CONFIGS_FILE", self.configs_file),
        ]
        for p in self._patchers:
            p.start()

        super().setUp()

    def tearDown(self) -> None:
        super().tearDown()
        for p in reversed(self._patchers):
            p.stop()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    async def get_application(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/api/prompts", handle_prompts_list)
        app.router.add_get("/api/prompts/detail", handle_prompt_detail)
        app.router.add_post("/api/prompts/save", handle_prompt_save)
        app.router.add_post("/api/prompts/reset", handle_prompt_reset)
        return app

    async def test_list_all_prompt_layers(self) -> None:
        """전체 프롬프트 레이어(공통/채널/에이전트) 목록 조회가 정상 동작하는지 검증."""
        resp = await self.client.get("/api/prompts")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertTrue(data.get("ok"))
        prompts = data.get("prompts", [])
        self.assertGreaterEqual(len(prompts), 4)

        ids = [p["id"] for p in prompts]
        self.assertIn("common", ids)
        self.assertIn("web", ids)
        self.assertIn("coding", ids)
        self.assertIn("__core__", ids)
        self.assertIn("__expert_vault__", ids)
        self.assertIn("__expert_coding__", ids)

    async def test_get_prompt_layer_detail_and_assembly(self) -> None:
        """특정 프롬프트(common 및 __expert_vault__)의 상세 조회 및 결합 프롬프트 분해 검증."""
        resp = await self.client.get("/api/prompts/detail?id=common")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertTrue(data["ok"])
        detail = data["detail"]
        self.assertEqual(detail["id"], "common")
        self.assertIn("assembledSystemPrompt", detail)
        self.assertGreater(detail["assembledCharCount"], 0)

        # 에이전트 상세 조회
        resp_ag = await self.client.get("/api/prompts/detail?id=__expert_vault__&sampleTask=식재료점검")
        self.assertEqual(resp_ag.status, 200)
        data_ag = await resp_ag.json()
        self.assertTrue(data_ag["ok"])
        detail_ag = data_ag["detail"]
        self.assertEqual(detail_ag["id"], "__expert_vault__")
        self.assertIn("layers", detail_ag)
        self.assertGreaterEqual(len(detail_ag["layers"]), 2)
        self.assertIn("childPreview", detail_ag)
        self.assertIn("식재료점검", detail_ag["childPreview"]["assembledChildUserPrompt"])

    async def test_custom_save_and_reset_global_instruction(self) -> None:
        """공통 지침(common) 커스텀 저장 시 런타임 파일 생성 및 기본값 복원 검증."""
        custom_content = "# 사용자 정의 공통 시스템 지침\n1. 핵심 요약부터 보고하라."
        save_resp = await self.client.post(
            "/api/prompts/save",
            json={"id": "common", "content": custom_content},
        )
        self.assertEqual(save_resp.status, 200)
        save_data = await save_resp.json()
        self.assertTrue(save_data["ok"])
        self.assertTrue(save_data["detail"]["isCustomized"])
        self.assertEqual(save_data["detail"]["currentContent"], custom_content)

        # 커스텀 파일이 실제로 기록되었는지 확인
        custom_file = Path(self.custom_dir) / "common" / "AGENTS.md"
        self.assertTrue(custom_file.is_file())
        self.assertEqual(custom_file.read_text(encoding="utf-8").strip(), custom_content)

        # 기본값으로 복원
        reset_resp = await self.client.post("/api/prompts/reset", json={"id": "common"})
        self.assertEqual(reset_resp.status, 200)
        reset_data = await reset_resp.json()
        self.assertTrue(reset_data["ok"])
        self.assertFalse(reset_data["detail"]["isCustomized"])
        self.assertFalse(custom_file.is_file())

    async def test_custom_save_and_reset_agent_instruction(self) -> None:
        """에이전트 지침(__expert_coding__) 수정 저장 및 초기화 검증."""
        custom_agent_sys = "# 봇개발 전용 커스텀 지침\n- 워크트리 규칙을 철저히 준수하라."
        custom_child_tpl = "[하위 워커 지침]\n1. 테스트 전수 통과 확인"
        save_resp = await self.client.post(
            "/api/prompts/save",
            json={
                "id": "__expert_coding__",
                "content": custom_agent_sys,
                "childPromptTemplate": custom_child_tpl,
            },
        )
        self.assertEqual(save_resp.status, 200)
        save_data = await save_resp.json()
        self.assertTrue(save_data["ok"])
        detail = save_data["detail"]
        self.assertEqual(detail["currentContent"], custom_agent_sys)
        self.assertEqual(detail["childPreview"]["childPromptTemplate"], custom_child_tpl)

        # 복원
        reset_resp = await self.client.post("/api/prompts/reset", json={"id": "__expert_coding__"})
        self.assertEqual(reset_resp.status, 200)
        reset_data = await reset_resp.json()
        self.assertTrue(reset_data["ok"])
        self.assertFalse(reset_data["detail"]["isCustomized"])

    def test_engine_drivers_uses_custom_instruction(self) -> None:
        """커스텀 지침 파일 존재 시 engine_drivers.get_instruction_dir가 커스텀 경로를 반환하는지 검증."""
        from bot.engine.engine_drivers import get_instruction_dir

        # 커스텀 없을 때는 리포지토리 기본 경로
        default_dir = get_instruction_dir("common")
        self.assertIn("src/bot/instructions/common", default_dir)

        # 커스텀 파일 생성
        custom_target = Path(self.custom_dir) / "common"
        custom_target.mkdir(parents=True, exist_ok=True)
        (custom_target / "AGENTS.md").write_text("# Custom common", encoding="utf-8")

        custom_dir = get_instruction_dir("common")
        self.assertEqual(custom_dir, str(custom_target))

    def test_index_html_has_prompts_center_elements(self) -> None:
        """index.html에 시스템 프롬프트 & 인스트럭션 센터 관련 마크업 및 리소스가 포함되어 있는지 검증."""
        index_path = Path(__file__).parent.parent / "src/bot/web/static/index.html"
        self.assertTrue(index_path.is_file())
        content = index_path.read_text(encoding="utf-8")

        self.assertIn("modal-prompts-center", content)
        self.assertIn("settings-prompts-banner-card", content)
        self.assertIn("prompts-center.css", content)
        self.assertIn("prompts-center.js", content)
        self.assertIn("openPromptsCenterModal", content)
