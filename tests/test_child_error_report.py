"""test_child_error_report.py — 하위 채팅 오류의 부모 보고(멱등·루프 가드·모델 정규화) 검증."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import child_error_report as cer
import web_execution
import web_server
from contextlib import ExitStack

MODELS = {"claude-sonnet-5-5": "Sonnet 5.5", "gemini-3.8-flash": "Flash 3.8"}


class TestPure(unittest.TestCase):
    def test_normalize_display_name(self):
        self.assertEqual(
            cer.normalize_model("Claude Sonnet 5.5 (Medium)", MODELS), ("claude-sonnet-5-5", "medium")
        )
        self.assertEqual(cer.normalize_model("claude-sonnet-5-5", MODELS), ("claude-sonnet-5-5", None))
        self.assertEqual(cer.normalize_model("claude-sonnet-5.5", MODELS)[0], "claude-sonnet-5-5")
        self.assertIsNone(cer.normalize_model("nope-9", MODELS)[0])

    def test_error_detection(self):
        self.assertTrue(cer.looks_like_error_reply('invalid model selection (--model "x")'))
        self.assertTrue(cer.looks_like_error_reply("⚠️ 작업이 중단되었습니다."))
        self.assertFalse(cer.looks_like_error_reply("정상 결과입니다"))

    def test_report_includes_models_and_original(self):
        msg = cer.build_error_report(
            room_title="방", room_id="r1", error_text='invalid model selection (--model "x")',
            model="x", effort="medium", allowed_models=MODELS,
        )
        self.assertTrue(msg.startswith("[하위 작업 중단/오류 보고]"))
        self.assertIn("[하위 작업 오류 보고]", msg)
        self.assertIn("claude-sonnet-5-5", msg)
        self.assertIn("effort: medium", msg)
        self.assertIn("invalid model selection", msg)

    def test_claim_idempotent(self):
        cer._reported.clear()
        self.assertTrue(cer.claim_error_report("r", "e"))
        self.assertFalse(cer.claim_error_report("r", "e"))
        cer.release_error_report("r", "e")
        self.assertTrue(cer.claim_error_report("r", "e"))


class TestRouting(unittest.IsolatedAsyncioTestCase):
    def _db(self, parent):
        db = MagicMock()
        db.get_room.return_value = {"id": "c1", "title": "자식", "parentAgentId": parent}
        return db

    async def _fin(self, *, channel="web", is_orchestrator=False, ok=False, parent="__expert_coding__"):
        notify = AsyncMock()
        with ExitStack() as st:
            for mod in (web_execution, web_server):
                st.enter_context(patch.object(mod, "chat_db", self._db(parent)))
            st.enter_context(patch.object(web_execution, "_notify_master_agent", notify))
            st.enter_context(patch.object(web_execution, "master_tasks", MagicMock(get_task=lambda _t: {})))
            await web_execution._finalize_orchestrator_completion(
                room_id="c1", task_id=None, channel=channel, is_orchestrator=is_orchestrator,
                is_success=ok, result_text="boom", status="failed" if not ok else "done",
                model="m", effort="low",
            )
        return notify

    async def test_ui_child_failure_reported(self):
        n = await self._fin()
        n.assert_awaited_once()
        self.assertEqual(n.call_args.kwargs["model"], "m")

    async def test_no_parent_ui_room_not_reported(self):
        n = await self._fin(parent=None)
        n.assert_not_awaited()

    async def test_loop_guards(self):
        (await self._fin(channel="task_report")).assert_not_awaited()
        (await self._fin(is_orchestrator=True)).assert_not_awaited()

    async def test_success_ui_child_not_reported(self):
        (await self._fin(ok=True)).assert_not_awaited()

    async def test_notify_enqueues_to_parent_once(self):
        cer._reported.clear()
        enq = AsyncMock()
        with ExitStack() as st:
            for mod in (web_execution, web_server):
                st.enter_context(patch.object(mod, "chat_db", self._db("__expert_vault__")))
                st.enter_context(patch.object(mod, "enqueue_room_turn", enq))
            st.enter_context(patch.object(web_execution, "master_memory", MagicMock()))
            for _ in range(2):
                await web_execution._notify_master_agent("c1", None, "invalid model selection x", is_success=False)
        enq.assert_awaited_once()
        kw = enq.call_args.kwargs
        self.assertEqual(kw["room_id"], "__expert_vault__")
        self.assertIn("[하위 작업 오류 보고]", kw["user_text"])


if __name__ == "__main__":
    unittest.main()
