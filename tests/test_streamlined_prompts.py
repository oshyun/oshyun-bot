"""test_streamlined_prompts — 지침 문서 슬림화 및 런타임 동적 메타데이터 주입 검증 테스트."""

from __future__ import annotations

import contextlib
import os
import unittest
from unittest.mock import MagicMock, patch

from bot.engine.engine_drivers import _FALLBACK_EXPERT_CODING_PROMPT, _FALLBACK_EXPERT_VAULT_PROMPT, _FALLBACK_ORCHESTRATOR_PROMPT, _load_instruction_file
import bot.web.execution as web_execution
try:
    import bot.web.server as web_server
except ImportError:
    web_server = None


class TestStreamlinedInstructions(unittest.TestCase):
    """지침 문서 및 Fallback 프롬프트의 정적 규칙 복사 제거 및 슬림화 검증."""

    def test_fallback_expert_coding_prompt_streamlined(self) -> None:
        """전문가 코딩 Fallback 프롬프트에서 장황한 5대 원칙 나열이 제거되고 핵심 요구사항 위주로 정리되었는지 확인."""
        self.assertNotIn("① 워크트리 격리 필수", _FALLBACK_EXPERT_CODING_PROMPT)
        self.assertNotIn("② 단위 테스트 전수 검증", _FALLBACK_EXPERT_CODING_PROMPT)
        self.assertIn("핵심 과업 목표 및 요구사항", _FALLBACK_EXPERT_CODING_PROMPT)
        self.assertIn("정적 규칙 복사 금지", _FALLBACK_EXPERT_CODING_PROMPT)

    def test_fallback_expert_vault_prompt_streamlined(self) -> None:
        """전문가 볼트 Fallback 프롬프트에서 볼트 정적 규칙 중복 나열이 제거되었는지 확인."""
        self.assertNotIn("<지시 + 볼트 규칙>", _FALLBACK_EXPERT_VAULT_PROMPT)
        self.assertIn("핵심 과업 목표 및 요구사항", _FALLBACK_EXPERT_VAULT_PROMPT)
        self.assertIn("AGENTS.md 및 rules/를 자동 로드", _FALLBACK_EXPERT_VAULT_PROMPT)

    def test_fallback_orchestrator_prompt_streamlined(self) -> None:
        """마스터 오케스트레이터 Fallback 프롬프트에서 긴 지침 복사 패턴이 제거되었는지 확인."""
        self.assertNotIn("워크트리·테스트·/simplify·main merge·restart·push 필수", _FALLBACK_ORCHESTRATOR_PROMPT)
        self.assertIn("핵심 요구사항", _FALLBACK_ORCHESTRATOR_PROMPT)
        self.assertIn("AGENTS.md 및 rules/를 자동 로드", _FALLBACK_ORCHESTRATOR_PROMPT)

    def test_instruction_files_content(self) -> None:
        """각 지침 문서에 정적 규칙 복사 금지 및 간결한 동적 컨텍스트 전달 원칙이 반영되었는지 확인."""
        orch = _load_instruction_file("orchestrator")
        self.assertIn("정적 규칙 복사 금지", orch)
        self.assertIn("필수 동적 컨텍스트만 간결하게 전달", orch)

        coding = _load_instruction_file("expert_coding")
        self.assertIn("정적 규칙 복사 금지", coding)
        self.assertIn("핵심 과업 목표 및 요구사항", coding)

        vault = _load_instruction_file("expert_vault")
        self.assertIn("정적 규칙 복사 금지", vault)
        self.assertIn("핵심 과업 목표 및 요구사항", vault)

        common = _load_instruction_file("common")
        self.assertIn("프롬프트 위임 원칙", common)


class TestRuntimeMetadataInjection(unittest.IsolatedAsyncioTestCase):
    """런타임 하위 세션 실행 시 필수 동적 메타데이터 주입 검증."""

    async def test_subagent_runtime_metadata_injected(self) -> None:
        """하위 작업 세션(parentAgentId 및 taskId 보유) 실행 시 필수 동적 메타데이터가 주입되는지 검증."""
        captured_prompts: list[str] = []
        captured_kwargs: list[dict] = []

        def mock_spawn(prompt, session_id, **kwargs):
            captured_prompts.append(prompt)
            captured_kwargs.append(kwargs)
            fake_proc = MagicMock()
            fake_proc.pid = 99999
            fake_proc.poll.return_value = 0
            return fake_proc

        mock_db = MagicMock()
        mock_db.get_room.return_value = {
            "id": "child-room-1",
            "parentAgentId": "__expert_coding__",
            "title": "기능 구현 작업",
            "sessionId": "sess-child-1",
        }
        mock_db.add_message.return_value = 101
        mock_db.get_message.return_value = None

        mods = [web_execution]
        if web_server is not None:
            mods.append(web_server)

        with contextlib.ExitStack() as stack:
            for mod in mods:
                stack.enter_context(patch.object(mod, "spawn_agent", side_effect=mock_spawn))
                stack.enter_context(patch.object(mod, "chat_db", mock_db))
                stack.enter_context(patch.object(mod, "watch_agent", return_value={"result": "성공", "session_id": "sess-child-1"}))
                stack.enter_context(patch.object(mod, "broadcast_ws_json", return_value=None))

            res = await web_execution.execute_chat_turn(
                user_text="버그를 수정해줘.",
                is_voice_mode=False,
                room_id="child-room-1",
                channel="orchestrator",
                task_id="task-test-777",
            )

        self.assertTrue(len(captured_prompts) > 0)
        injected_prompt = captured_prompts[0]
        self.assertIn("[런타임 작업 메타데이터]", injected_prompt)
        self.assertIn("• 부모 에이전트(보고 대상): __expert_coding__", injected_prompt)
        self.assertIn("• 작업 ID: task-test-777", injected_prompt)
        self.assertIn("버그를 수정해줘.", injected_prompt)

        # 환경변수 검증
        self.assertTrue(len(captured_kwargs) > 0)
        extra_env = captured_kwargs[0].get("extra_env", {})
        self.assertEqual(extra_env.get("OSHYUN_PARENT_AGENT_ID"), "__expert_coding__")
        self.assertEqual(extra_env.get("OSHYUN_TASK_ID"), "task-test-777")
        self.assertEqual(extra_env.get("OSHYUN_AGENT_ROOM_ID"), "child-room-1")


if __name__ == "__main__":
    unittest.main()
