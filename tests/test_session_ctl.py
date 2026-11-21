"""test_session_ctl.py — session-ctl CLI 도구의 서브커맨드(spawn, send, watch, notify) 검증."""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# scripts/session-ctl 모듈 동적 로드
from importlib.machinery import SourceFileLoader
SCRIPT_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "../scripts/session-ctl"))
loader = SourceFileLoader("session_ctl", SCRIPT_PATH)
spec = importlib.util.spec_from_loader("session_ctl", loader)
session_ctl = importlib.util.module_from_spec(spec)
sys.modules["session_ctl"] = session_ctl
loader.exec_module(session_ctl)


class TestSessionCtlCli(unittest.TestCase):
    """session-ctl 서브커맨드 및 플래그 동작 테스트."""

    @patch("session_ctl._api_request")
    def test_cmd_notify(self, mock_api):
        mock_api.return_value = {"ok": True, "status": "queued", "queuePosition": 1}
        args = argparse.Namespace(
            message="[하위 작업 완료] 분석 끝남",
            task_id="mt-999",
            room_id="room-sub",
            json=True,
        )
        ret = session_ctl.cmd_notify(args)
        self.assertEqual(ret, 0)
        mock_api.assert_called_once_with(
            "POST",
            "/api/orchestrator/notify",
            payload={
                "message": "[하위 작업 완료] 분석 끝남",
                "taskId": "mt-999",
                "roomId": "room-sub",
            },
            timeout=30,
        )

    @patch("session_ctl._api_request")
    @patch("subprocess.Popen")
    def test_cmd_spawn_with_notify_flag(self, mock_popen, mock_api):
        mock_api.return_value = {
            "ok": True,
            "status": "started",
            "reused": False,
            "roomId": "room-new-123",
            "taskId": "mt-new-456",
            "title": "테스트 세션",
            "serverOnComplete": True,
        }
        args = argparse.Namespace(
            title="테스트 세션",
            message="분석해줘",
            effort=None,
            model=None,
            force_new=False,
            async_mode=True,
            notify=True,
            json=False,
        )
        ret = session_ctl.cmd_spawn(args)
        self.assertEqual(ret, 0)
        # 외부 Watcher 프로세스가 실행되지 않고 서버 내장 훅 플래그가 전달되는지 검증
        mock_popen.assert_not_called()
        self.assertTrue(mock_api.call_args[1]["payload"].get("notify"))

    @patch("session_ctl._api_request")
    @patch("subprocess.Popen")
    def test_cmd_spawn_reused_and_force_new(self, mock_popen, mock_api):
        import io
        from contextlib import redirect_stdout

        mock_api.return_value = {
            "ok": True,
            "status": "started",
            "reused": True,
            "matchedSimilarity": 0.812,
            "roomId": "room-existing-card",
            "taskId": "mt-reused-111",
            "title": "11월 카드 혜택 정리",
            "serverOnComplete": True,
        }
        args = argparse.Namespace(
            title="카드 혜택 추가 확인",
            message="이번 달 실적 확인해줘",
            effort=None,
            model=None,
            force_new=False,
            async_mode=True,
            notify=True,
            json=False,
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            ret = session_ctl.cmd_spawn(args)
        self.assertEqual(ret, 0)
        self.assertIn("유사한 기존 채팅방", buf.getvalue())
        self.assertIn("0.812", buf.getvalue())
        self.assertNotIn("forceNew", mock_api.call_args[1]["payload"])
        mock_popen.assert_not_called()

        # --force-new 전달 시 payload에 forceNew=True 포함 검증
        args_forced = argparse.Namespace(
            title="카드 혜택 추가 확인",
            message="이번 달 실적 확인해줘",
            effort=None,
            model=None,
            force_new=True,
            async_mode=True,
            notify=False,
            json=False,
        )
        mock_api.return_value = {
            "ok": True,
            "status": "started",
            "reused": False,
            "roomId": "room-new-999",
            "taskId": "mt-new-999",
            "title": "카드 혜택 추가 확인",
        }
        ret2 = session_ctl.cmd_spawn(args_forced)
        self.assertEqual(ret2, 0)
        self.assertTrue(mock_api.call_args[1]["payload"].get("forceNew"))

    @patch("session_ctl._api_request")
    def test_cmd_memory_rebalance_single_call(self, mock_api):
        import io
        from contextlib import redirect_stdout

        mock_api.return_value = {
            "ok": True,
            "totalItems": 2,
            "totalChars": 80,
            "tierOrder": ["hot", "warm", "cold", "freeze"],
            "lastOperation": {
                "action": "rebalance",
                "purged": 3,
                "summarized": 1,
                "promoted": 1,
                "demoted": 0,
                "deduped": 1,
                "cleaned": 3,
                "cascaded": 0,
                "dropped": 0,
            },
            "tiers": {
                "hot": {"label": "HOT · 활성", "itemCount": 1, "maxItems": 8, "charCount": 40, "maxChars": 1500, "usagePct": 3, "items": [{"id": "mm-1", "title": "가계부", "text": "11월 정산 완료"}]},
                "warm": {"label": "WARM · 최근", "itemCount": 1, "maxItems": 16, "charCount": 40, "maxChars": 3000, "usagePct": 1, "items": [{"id": "mm-2", "title": "차량", "text": "주유 기록 완료"}]},
                "cold": {"label": "COLD · 압축", "itemCount": 0, "maxItems": 32, "charCount": 0, "maxChars": 6000, "usagePct": 0, "items": []},
                "freeze": {"label": "FREEZE · 동결", "itemCount": 0, "maxItems": 100, "charCount": 0, "maxChars": 15000, "usagePct": 0, "items": []},
            },
        }
        args = argparse.Namespace(
            add=None, batch=None, update=None, rebalance=True, move=None,
            delete=None, promote=None, demote=None, clear=None,
            tier="all", limit=50, json=False,
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            ret = session_ctl.cmd_memory(args)
        self.assertEqual(ret, 0)
        mock_api.assert_called_once_with("POST", "/api/orchestrator/memory", {"action": "rebalance"}, timeout=60)
        out = buf.getvalue()
        self.assertIn("잡음삭제: 3건", out)
        self.assertIn("요약: 1건", out)
        self.assertIn("승격: 1건", out)

    @patch("session_ctl._api_request")
    @patch("subprocess.Popen")
    def test_cmd_send_with_notify_flag(self, mock_popen, mock_api):
        mock_api.return_value = {
            "ok": True,
            "status": "started",
            "roomId": "room-existing",
            "taskId": "mt-task-789",
            "title": "기존 세션",
            "serverOnComplete": True,
        }
        args = argparse.Namespace(
            target="room-existing",
            message="다음 단계 진행해",
            effort=None,
            model=None,
            async_mode=True,
            notify=True,
            json=False,
        )
        ret = session_ctl.cmd_send(args)
        self.assertEqual(ret, 0)
        mock_popen.assert_not_called()
        self.assertTrue(mock_api.call_args[1]["payload"].get("notify"))

    @patch("session_ctl._api_request")
    def test_cmd_watch_and_notify_master(self, mock_api):
        # 1회차: read (isRunning: True)
        # 2회차: read (isRunning: False, 새 메시지 완료)
        # 3회차: tasks 조회 (상태: done)
        # 4회차: notify 전송
        mock_api.side_effect = [
            {"ok": True, "session": {"roomId": "room-sub", "title": "하위작업", "isRunning": True, "messageCount": 1}},
            {
                "ok": True,
                "session": {"roomId": "room-sub", "title": "하위작업", "isRunning": False, "messageCount": 2},
                "messages": [{"role": "assistant", "text": "하위 작업 결과입니다."}],
            },
            {"ok": True, "tasks": [{"id": "mt-123", "status": "done"}]},
            {"ok": True, "status": "started"},
        ]

        args = argparse.Namespace(
            target="room-sub",
            task_id="mt-123",
            notify_master=True,
            timeout=10,
            interval=0.01,
            json=False,
        )
        ret = session_ctl.cmd_watch(args)
        self.assertEqual(ret, 0)

        # 마지막 호출이 notify인지 확인
        last_call = mock_api.call_args_list[-1]
        self.assertEqual(last_call[0][0], "POST")
        self.assertEqual(last_call[0][1], "/api/orchestrator/notify")
        payload = last_call[1]["payload"]
        self.assertEqual(payload["taskId"], "mt-123")
        self.assertIn("하위 작업 결과입니다.", payload["message"])

    @patch("session_ctl._api_request")
    def test_cmd_summaries_and_summary(self, mock_api):
        """session-ctl summaries 및 session-ctl summary 커맨드 동작 검증."""
        import io
        from contextlib import redirect_stdout

        mock_api.return_value = {
            "ok": True,
            "total": 1,
            "summaries": [
                {
                    "roomId": "room-tax",
                    "sessionId": "sess-tax",
                    "title": "연말정산 정리",
                    "busy": False,
                    "lastStatus": "done",
                    "taskCount": 2,
                    "summary": "[주제] 연말정산 정리 | [결과] 의료비 공제 확인 완료",
                    "queries": ["의료비 공제 내역 확인"],
                    "lastResult": "의료비 공제 확인 완료",
                    "updatedAtFormatted": "2026-11-15 02:00",
                }
            ],
        }
        buf = io.StringIO()
        with redirect_stdout(buf):
            ret = session_ctl.cmd_summaries(
                argparse.Namespace(limit=20, include_archived=False, json=False)
            )
        self.assertEqual(ret, 0)
        mock_api.assert_called_with("GET", "/api/orchestrator/summaries?limit=20&includeArchived=0", timeout=15)
        out = buf.getvalue()
        self.assertIn("연말정산 정리", out)
        self.assertIn("의료비 공제 확인 완료", out)

        mock_api.return_value = {
            "ok": True,
            "roomId": "room-tax",
            "summary": "연말정산 의료비 및 카드 소득공제 요약본 확정",
        }
        buf2 = io.StringIO()
        with redirect_stdout(buf2):
            ret2 = session_ctl.cmd_summary(
                argparse.Namespace(
                    target="room-tax",
                    summary="연말정산 의료비 및 카드 소득공제 요약본 확정",
                    note="검토 완료",
                    query=None,
                    result=None,
                    json=False,
                )
            )
        self.assertEqual(ret2, 0)
        mock_api.assert_called_with(
            "POST",
            "/api/orchestrator/session/summary",
            payload={
                "target": "room-tax",
                "summary": "연말정산 의료비 및 카드 소득공제 요약본 확정",
                "note": "검토 완료",
            },
            timeout=15,
        )
        self.assertIn("요약본이 갱신되었습니다", buf2.getvalue())

    @patch.object(session_ctl, "_api_request")
    def test_cmd_memory_and_reset(self, mock_api):
        """session-ctl memory / reset 명령이 올바른 API 엔드포인트와 페이로드를 호출하는지 검증."""
        import io
        from contextlib import redirect_stdout

        mock_api.return_value = {
            "ok": True,
            "tierOrder": ["hot", "warm", "cold", "freeze"],
            "totalItems": 1,
            "totalChars": 10,
            "tiers": {
                "hot": {
                    "label": "HOT (활성 컨텍스트)",
                    "itemCount": 1,
                    "maxItems": 8,
                    "charCount": 10,
                    "maxChars": 2400,
                    "usagePct": 1,
                    "items": [{"id": "mm-1", "title": "테스트", "text": "메모리 항목"}],
                }
            },
        }
        buf = io.StringIO()
        with redirect_stdout(buf):
            ret = session_ctl.cmd_memory(
                argparse.Namespace(tier="hot", add="메모리 항목", title="테스트", kind="note", room_id="", demote=None, clear=None, limit=20, json=False)
            )
        self.assertEqual(ret, 0)
        mock_api.assert_called_with(
            "POST",
            "/api/orchestrator/memory",
            {"action": "add", "text": "메모리 항목", "title": "테스트", "tier": "hot", "kind": "note", "roomId": ""},
            timeout=15,
        )

        mock_api.reset_mock()
        mock_api.return_value = {"ok": True, "tiers": {}, "totalItems": 0, "totalChars": 0}
        with redirect_stdout(io.StringIO()):
            ret_get = session_ctl.cmd_memory(
                argparse.Namespace(tier="cold", add=None, title="", kind="note", room_id="", demote=None, clear=None, limit=15, json=False)
            )
        self.assertEqual(ret_get, 0)
        mock_api.assert_called_with("GET", "/api/orchestrator/memory?limit=15&tier=cold", timeout=15)

        mock_api.reset_mock()
        mock_api.return_value = {"ok": True, "roomId": "__core__", "memoryMode": "keep", "clearedTasks": 0, "memory": {"totalItems": 3}}
        buf_rst_default = io.StringIO()
        with redirect_stdout(buf_rst_default):
            ret_rst_default = session_ctl.cmd_reset(
                argparse.Namespace(memory_mode="keep", clear_memory=False, clear_done_tasks=False, json=False)
            )
        self.assertEqual(ret_rst_default, 0)
        mock_api.assert_called_with(
            "POST",
            "/api/orchestrator/reset",
            {"memoryMode": "keep", "clearDoneTasks": False},
            timeout=30,
        )

        mock_api.reset_mock()
        mock_api.return_value = {"ok": True, "roomId": "__core__", "memoryMode": "clear", "clearedTasks": 2, "memory": {"totalItems": 0}}
        buf_rst = io.StringIO()
        with redirect_stdout(buf_rst):
            ret_rst = session_ctl.cmd_reset(
                argparse.Namespace(memory_mode="cascade", clear_memory=True, clear_done_tasks=True, json=False)
            )
        self.assertEqual(ret_rst, 0)
        mock_api.assert_called_with(
            "POST",
            "/api/orchestrator/reset",
            {"memoryMode": "clear", "clearDoneTasks": True},
            timeout=30,
        )
        self.assertIn("초기화 완료", buf_rst.getvalue())

    def test_send_and_spawn_default_to_async(self):
        """session-ctl send/spawn CLI 파서가 기본값으로 비동기(async_mode=True)를 사용하고 --wait 시 동기 전환하는지 검증."""
        parser = session_ctl.build_parser()

        args_send_default = parser.parse_args(["send", "room-1", "작업해줘"])
        self.assertTrue(args_send_default.async_mode)

        args_send_wait = parser.parse_args(["send", "room-1", "확인해줘", "--wait"])
        self.assertFalse(args_send_wait.async_mode)

        args_spawn_default = parser.parse_args(["spawn", "새 작업방", "분석해줘"])
        self.assertTrue(args_spawn_default.async_mode)

        args_spawn_wait = parser.parse_args(["spawn", "새 작업방", "확인해줘", "--wait"])
        self.assertFalse(args_spawn_wait.async_mode)

    @patch("session_ctl._api_request")
    def test_cmd_delete_requires_yes_or_dry_run(self, mock_api):
        """--yes 또는 --dry-run 없이 delete 호출 시 API 호출 없이 거부되는지 검증."""
        import io
        from contextlib import redirect_stderr

        args = argparse.Namespace(targets=["room-1"], dry_run=False, yes=False, json=False)
        buf = io.StringIO()
        with redirect_stderr(buf):
            ret = session_ctl.cmd_delete(args)
        self.assertEqual(ret, 1)
        self.assertIn("--yes", buf.getvalue())
        mock_api.assert_not_called()

    @patch("session_ctl._api_request")
    def test_cmd_delete_refuses_orchestrator_and_running(self, mock_api):
        """오케스트레이터 방(__core__ 등) 및 실행 중인 방은 삭제를 거부하는지 검증."""
        import io
        from contextlib import redirect_stderr

        mock_api.return_value = {
            "ok": True,
            "sessions": [
                {"id": "__expert_coding__", "title": "봇개발 에이전트", "isRunning": False},
                {"id": "room-running", "title": "실행중 방", "isRunning": True, "queueSize": 0},
                {"id": "room-idle", "title": "대기 방", "isRunning": False, "queueSize": 0},
            ],
        }
        for target in ("__core__", "__expert_coding__", "room-running"):
            buf = io.StringIO()
            with redirect_stderr(buf):
                ret = session_ctl.cmd_delete(
                    argparse.Namespace(targets=[target], dry_run=False, yes=True, json=False)
                )
            self.assertEqual(ret, 1)
            self.assertIn("삭제 거부", buf.getvalue())
        # 목록 조회(GET)만 호출되고 삭제(POST)는 호출되지 않아야 함
        self.assertTrue(all(c[0][0] == "GET" for c in mock_api.call_args_list))

    @patch("session_ctl._api_request")
    def test_cmd_delete_dry_run_and_execute(self, mock_api):
        """--dry-run 미리보기 및 --yes 실제 일괄 삭제 API 호출 검증."""
        import io
        from contextlib import redirect_stdout

        sessions_payload = {
            "ok": True,
            "sessions": [
                {"id": "room-1", "sessionId": "sess-1", "title": "완료된 작업 1", "isRunning": False},
                {"id": "room-2", "sessionId": "sess-2", "title": "완료된 작업 2", "isRunning": False},
            ],
        }
        # 1. --dry-run
        mock_api.return_value = sessions_payload
        buf_dry = io.StringIO()
        with redirect_stdout(buf_dry):
            ret_dry = session_ctl.cmd_delete(
                argparse.Namespace(targets=["room-1", "완료된 작업 2"], dry_run=True, yes=False, json=False)
            )
        self.assertEqual(ret_dry, 0)
        self.assertIn("DRY-RUN", buf_dry.getvalue())
        mock_api.assert_called_once_with("GET", "/api/orchestrator/sessions?limit=500", timeout=20)

        # 2. --yes 실제 삭제
        mock_api.reset_mock()
        mock_api.side_effect = [
            sessions_payload,
            {"ok": True, "roomIds": ["room-1", "room-2"]},
        ]
        buf_yes = io.StringIO()
        with redirect_stdout(buf_yes):
            ret_yes = session_ctl.cmd_delete(
                argparse.Namespace(targets=["room-1", "완료된 작업 2"], dry_run=False, yes=True, json=False)
            )
        self.assertEqual(ret_yes, 0)
        self.assertIn("2개 영구 삭제 완료", buf_yes.getvalue())
        self.assertEqual(mock_api.call_args_list[1][0][:2], ("POST", "/api/session/batch-delete"))
        self.assertEqual(mock_api.call_args_list[1][1]["payload"], {"roomIds": ["room-1", "room-2"]})


if __name__ == "__main__":
    unittest.main()
