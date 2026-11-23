"""test_agy_wrapper.py — Phase 1 `agy -p` 슈퍼바이저 래퍼, Token Firewall 및 콜백 웹훅 통합 테스트."""

from __future__ import annotations

import argparse
import asyncio
import copy
import importlib.util
from importlib.machinery import SourceFileLoader
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import urllib.error

from aiohttp.test_utils import AioHTTPTestCase

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC_DIR = os.path.join(_REPO_ROOT, "src", "bot")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

import agy_wrapper
import bot_logger
import chat_db
import engine_settings
import master_tasks
import stream_parser
import web_execution
import web_orchestrator
import web_server


def _load_session_ctl():
    script_path = os.path.join(_REPO_ROOT, "scripts", "session-ctl")
    loader = SourceFileLoader("session_ctl_sup", script_path)
    spec = importlib.util.spec_from_loader("session_ctl_sup", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class TestRunAgyCliWatchdog(unittest.IsolatedAsyncioTestCase):
    """run_agy_cli() 정상/오류/타임아웃 강제 종료 및 클린 환경 검증."""

    async def test_run_agy_cli_normal_and_error(self):
        proc_ok = MagicMock()
        proc_ok.returncode = 0
        proc_ok.communicate = AsyncMock(return_value=(b"  done output  ", b""))

        with patch.dict(os.environ, {"ANTIGRAVITY_AGENT": "1", "ANTIGRAVITY_CONVERSATION_ID": "c1"}), patch(
            "asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc_ok
        ) as mock_exec:
            res = await agy_wrapper.run_agy_cli("hello", timeout_sec=10)

        self.assertEqual(res.exit_status, "NORMAL")
        self.assertEqual(res.return_code, 0)
        self.assertEqual(res.stdout, "done output")
        passed_env = mock_exec.call_args.kwargs["env"]
        self.assertNotIn("ANTIGRAVITY_AGENT", passed_env)
        self.assertNotIn("ANTIGRAVITY_CONVERSATION_ID", passed_env)

        proc_err = MagicMock()
        proc_err.returncode = 2
        proc_err.communicate = AsyncMock(return_value=(b"partial", b"boom"))
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc_err):
            res_err = await agy_wrapper.run_agy_cli("bad", timeout_sec=10)
        self.assertEqual(res_err.exit_status, "ERROR")
        self.assertEqual(res_err.return_code, 2)
        self.assertEqual(res_err.stderr, "boom")

    async def test_run_agy_cli_timeout_kills_and_collects_partial_stdout(self):
        proc = MagicMock()
        proc.kill = MagicMock()
        proc.communicate = AsyncMock(
            side_effect=[
                asyncio.TimeoutError(),
                (b"partial stdout before timeout", b"partial err"),
            ]
        )
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc):
            res = await agy_wrapper.run_agy_cli("slow", timeout_sec=1)

        proc.kill.assert_called_once()
        self.assertEqual(res.exit_status, "TIMEOUT_KILLED")
        self.assertEqual(res.return_code, -9)
        self.assertEqual(res.stdout, "partial stdout before timeout")
        self.assertEqual(res.stderr, "partial err")


class TestTokenFirewallAndInspector(unittest.IsolatedAsyncioTestCase):
    """Token Firewall 4대 규칙(오프로딩·입력 절삭·500자 하드 캡·300자 Fallback) 및 Inspector 파싱 검증."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.ws_root = os.path.join(self.tmp_dir, ".agy_workspace")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_prepare_sub_agy_prompt_enforces_artifact_offloading(self):
        prompt = agy_wrapper.prepare_sub_agy_prompt(
            "task_001",
            "에러 로그를 분석하라",
            [],
            workspace_root=self.ws_root,
        )
        expected_artifact = os.path.join(self.ws_root, "task_001", "output.md")
        self.assertIn(expected_artifact, prompt)
        self.assertIn("표준 출력(stdout)에 직접 출력하지 마십시오", prompt)
        self.assertTrue(os.path.isdir(os.path.join(self.ws_root, "task_001")))

    async def test_inspector_truncates_stdout_to_last_1500_and_dumps_full_log(self):
        prefix_marker = "START_IGNORE_MARKER_" * 200  # 4,000자
        tail_marker = "TAIL_KEEP_MARKER_" * 60       # 1,020자
        long_stdout = prefix_marker + tail_marker    # 총 5,020자

        art_dir = os.path.join(self.ws_root, "task_trunc")
        os.makedirs(art_dir, exist_ok=True)
        with open(os.path.join(art_dir, "output.md"), "w", encoding="utf-8") as f:
            f.write("## 분석 산출물\n원인: token_expired 라인 42")

        sub_res = agy_wrapper.SubAgyRunResult(
            exit_status="NORMAL",
            return_code=0,
            stdout=long_stdout,
            stderr="",
        )
        captured_prompts: list[str] = []

        async def fake_run_cli(prompt: str, **_kwargs):
            captured_prompts.append(prompt)
            return agy_wrapper.SubAgyRunResult(
                exit_status="NORMAL",
                return_code=0,
                stdout=(
                    "<VERDICT>PASS</VERDICT>\n"
                    "<SUMMARY>원인 라인 42 확인 완료</SUMMARY>\n"
                    "<MISSING_REASON>NONE</MISSING_REASON>\n"
                    "<DELTA_TASK_FOR_NEXT_AGENT>NONE</DELTA_TASK_FOR_NEXT_AGENT>"
                ),
                stderr="",
            )

        with patch("agy_wrapper.run_agy_cli", side_effect=fake_run_cli):
            insp = await agy_wrapper.run_inspector_agy(
                objective="에러 분석",
                criteria="원인 라인 특정",
                accumulated_context=[],
                sub_result=sub_res,
                task_id="task_trunc",
                workspace_root=self.ws_root,
            )

        self.assertEqual(insp.verdict, "PASS")
        # 1. full.log 에 5,020자 전문이 덤프되었는지 확인
        full_log_path = os.path.join(art_dir, "full.log")
        self.assertTrue(os.path.isfile(full_log_path))
        with open(full_log_path, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), long_stdout)

        # 2. Inspector 프롬프트에는 마지막 1,500자 + output.md 내용만 들어갔는지 확인
        insp_prompt = captured_prompts[0]
        self.assertIn(long_stdout[-1500:], insp_prompt)
        self.assertIn("원인: token_expired 라인 42", insp_prompt)
        self.assertNotIn(long_stdout[:2000], insp_prompt)

    def test_extract_and_cap_summary_regex_hard_cap_and_fallback(self):
        # 1. 태그 외부 사족 폐기
        chatter = (
            "긴 내부 추론 사족입니다. 절대 마스터에게 넘기면 안됩니다.\n"
            "<SUMMARY>핵심 결과만 추출됨</SUMMARY>\n"
            "뒤에 붙은 불필요한 인사말입니다."
        )
        self.assertEqual(agy_wrapper.extract_and_cap_summary(chatter), "핵심 결과만 추출됨")

        # 2. 800자 -> 정확히 500자 절삭 + 접미사
        long_summary_body = "A" * 800
        raw_long = f"사족<SUMMARY>{long_summary_body}</SUMMARY>꼬리말"
        capped = agy_wrapper.extract_and_cap_summary(raw_long)
        expected = ("A" * 500) + agy_wrapper.SUMMARY_TRUNCATE_SUFFIX
        self.assertEqual(capped, expected)

        # 3. <SUMMARY> 태그 누락 시 맨 마지막 300자만 Fallback
        no_tag = ("X" * 400) + ("Y" * 300)
        fallback = agy_wrapper.extract_and_cap_summary(no_tag)
        self.assertEqual(len(fallback), 300)
        self.assertEqual(fallback, "Y" * 300)
        self.assertNotIn("X", fallback)

    async def test_inspector_missing_summary_tag_falls_back_to_last_300_chars(self):
        sub_res = agy_wrapper.SubAgyRunResult("NORMAL", 0, "완료", "")
        raw_insp = ("A" * 500) + "<VERDICT>PASS</VERDICT>\n" + ("Z" * 300)
        with patch(
            "agy_wrapper.run_agy_cli",
            new_callable=AsyncMock,
            return_value=agy_wrapper.SubAgyRunResult("NORMAL", 0, raw_insp, ""),
        ) as mock_cli:
            insp = await agy_wrapper.run_inspector_agy(
                objective="목표",
                criteria="기준",
                accumulated_context=[],
                sub_result=sub_res,
                task_id="task_nosum",
                workspace_root=self.ws_root,
            )
        mock_cli.assert_awaited_once()
        self.assertEqual(insp.verdict, "PASS")
        self.assertEqual(len(insp.summary_for_master), 300)
        self.assertNotIn("AAAA", insp.summary_for_master)

    async def test_inspector_format_repair_and_timeout_forces_partial(self):
        sub_timeout = agy_wrapper.SubAgyRunResult(
            exit_status="TIMEOUT_KILLED",
            return_code=-9,
            stdout="작업 중단됨",
            stderr="",
        )
        # 1차 응답: 태그 깨짐 -> 2차 교정 응답: PASS 로 잘못 판정해도 TIMEOUT_KILLED 이므로 PARTIAL 강제
        responses = [
            agy_wrapper.SubAgyRunResult("NORMAL", 0, "죄송합니다 태그를 잊었습니다", ""),
            agy_wrapper.SubAgyRunResult(
                "NORMAL",
                0,
                "<VERDICT>PASS</VERDICT><SUMMARY>중간 요약</SUMMARY>"
                "<MISSING_REASON>NONE</MISSING_REASON><DELTA_TASK_FOR_NEXT_AGENT>나머지 수행</DELTA_TASK_FOR_NEXT_AGENT>",
                "",
            ),
        ]
        with patch("agy_wrapper.run_agy_cli", side_effect=responses) as mock_cli:
            insp = await agy_wrapper.run_inspector_agy(
                objective="목표",
                criteria="기준",
                accumulated_context=[],
                sub_result=sub_timeout,
                task_id="task_timeout",
                workspace_root=self.ws_root,
            )

        self.assertEqual(mock_cli.call_count, 2)
        self.assertEqual(insp.verdict, "PARTIAL")
        self.assertEqual(insp.summary_for_master, "중간 요약")


class TestSuperviseTaskIntegration(unittest.IsolatedAsyncioTestCase):
    """supervise_task() PARTIAL -> INTERIM_REPORT_AND_RETRY -> 보강 재스폰 -> PASS COMPLETED 시나리오 검증."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.ws_root = os.path.join(self.tmp_dir, ".agy_workspace")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    async def test_partial_then_retry_then_pass_reports_with_pointers(self):
        cli_calls: list[str] = []
        reports: list[dict] = []

        async def fake_run_cli(prompt: str, **_kwargs):
            cli_calls.append(prompt)
            idx = len(cli_calls)
            if idx == 1:
                # 1차 Sub-Agy: 불완전 결과
                return agy_wrapper.SubAgyRunResult("NORMAL", 0, "1차: 발생 시각 14:10 확인", "")
            if idx == 2:
                # 1차 Inspector: PARTIAL 판정 + DELTA_TASK
                return agy_wrapper.SubAgyRunResult(
                    "NORMAL",
                    0,
                    "<VERDICT>PARTIAL</VERDICT>\n"
                    "<SUMMARY>발생 시각 14:10 확인했으나 원인 코드 라인 미특정</SUMMARY>\n"
                    "<MISSING_REASON>원인 코드 라인 및 해결 방안 누락</MISSING_REASON>\n"
                    "<DELTA_TASK_FOR_NEXT_AGENT>auth.py 의 원인 코드 라인과 해결 방안만 추가 확인하라</DELTA_TASK_FOR_NEXT_AGENT>",
                    "",
                )
            if idx == 3:
                # 2차 보강 Sub-Agy: output.md 에 최종 결과 기록
                art_path = os.path.join(self.ws_root, "task_98a7f6", "output.md")
                with open(art_path, "w", encoding="utf-8") as f:
                    f.write("시각: 14:10 / 코드: auth.py:42 / 해결: 토큰 갱신 락 추가")
                return agy_wrapper.SubAgyRunResult("NORMAL", 0, "2차 보강 완료", "")
            # 2차 Inspector: PASS 판정
            return agy_wrapper.SubAgyRunResult(
                "NORMAL",
                0,
                "<VERDICT>PASS</VERDICT>\n"
                "<SUMMARY>14:10 에러, auth.py:42 원인 및 토큰 락 해결 방안 모두 완료</SUMMARY>\n"
                "<MISSING_REASON>NONE</MISSING_REASON>\n"
                "<DELTA_TASK_FOR_NEXT_AGENT>NONE</DELTA_TASK_FOR_NEXT_AGENT>",
                "",
            )

        def capture_report(url: str, payload: dict, **_kwargs):
            reports.append({"url": url, "payload": payload})

        with patch("agy_wrapper.run_agy_cli", side_effect=fake_run_cli), patch(
            "agy_wrapper.report_to_master", side_effect=capture_report
        ):
            final = await agy_wrapper.supervise_task(
                task_id="task_98a7f6",
                callback_url="http://127.0.0.1:8765/v1/callbacks/task-events",
                objective="에러 로그 분석 및 원인 파악",
                criteria="1. 시각 확인 | 2. 원인 라인 특정 | 3. 해결 방안",
                max_retries=2,
                workspace_root=self.ws_root,
            )

        self.assertEqual(len(cli_calls), 4)
        # 2차 Sub-Agy 프롬프트에 1차 요약 및 delta_task 주입 확인
        self.assertIn("발생 시각 14:10 확인했으나 원인 코드 라인 미특정", cli_calls[2])
        self.assertIn("auth.py 의 원인 코드 라인과 해결 방안만 추가 확인하라", cli_calls[2])

        # 마스터 보고 순서: 1) INTERIM_REPORT_AND_RETRY -> 2) COMPLETED
        self.assertEqual(len(reports), 2)
        interim = reports[0]["payload"]
        completed = reports[1]["payload"]

        self.assertEqual(interim["event_type"], "INTERIM_REPORT_AND_RETRY")
        self.assertEqual(interim["verdict"], "PARTIAL")
        self.assertEqual(interim["attempt"], 1)
        self.assertFalse(interim["requires_master_intervention"])

        self.assertEqual(completed["event_type"], "COMPLETED")
        self.assertEqual(completed["verdict"], "PASS")
        self.assertEqual(completed["attempt"], 2)
        self.assertEqual(
            completed["pointers"],
            {
                "artifact_path": os.path.join(self.ws_root, "task_98a7f6", "output.md"),
                "raw_log_path": os.path.join(self.ws_root, "task_98a7f6", "full.log"),
            },
        )
        self.assertEqual(final, completed)

    async def test_max_retries_exhausted_reports_partial_success_and_fail_stops(self):
        async def fake_partial(_prompt: str, **_kwargs):
            return agy_wrapper.SubAgyRunResult(
                "NORMAL",
                0,
                "<VERDICT>PARTIAL</VERDICT><SUMMARY>부분 결과</SUMMARY>"
                "<MISSING_REASON>여전히 부족</MISSING_REASON><DELTA_TASK_FOR_NEXT_AGENT>추가</DELTA_TASK_FOR_NEXT_AGENT>",
                "",
            )

        sent_partial: list[dict] = []
        with patch("agy_wrapper.run_agy_cli", side_effect=fake_partial), patch(
            "agy_wrapper.report_to_master", side_effect=lambda _u, p, **_kw: sent_partial.append(p)
        ):
            res = await agy_wrapper.supervise_task(
                task_id="task_max",
                callback_url="http://127.0.0.1:8765/v1/callbacks/task-events",
                objective="목표",
                criteria="기준",
                max_retries=1,
                workspace_root=self.ws_root,
            )

        self.assertEqual([r["event_type"] for r in sent_partial], ["INTERIM_REPORT_AND_RETRY", "PARTIAL_SUCCESS"])
        self.assertTrue(res["requires_master_intervention"])

        async def fake_fail(_prompt: str, **_kwargs):
            return agy_wrapper.SubAgyRunResult(
                "NORMAL",
                0,
                "<VERDICT>FAIL</VERDICT><SUMMARY>권한 오류</SUMMARY>"
                "<MISSING_REASON>접근 불가</MISSING_REASON><DELTA_TASK_FOR_NEXT_AGENT>NONE</DELTA_TASK_FOR_NEXT_AGENT>",
                "",
            )

        sent_fail: list[dict] = []
        with patch("agy_wrapper.run_agy_cli", side_effect=fake_fail), patch(
            "agy_wrapper.report_to_master", side_effect=lambda _u, p, **_kw: sent_fail.append(p)
        ):
            res_fail = await agy_wrapper.supervise_task(
                task_id="task_fail",
                callback_url="http://127.0.0.1:8765/v1/callbacks/task-events",
                objective="목표",
                criteria="기준",
                max_retries=2,
                workspace_root=self.ws_root,
            )

        self.assertEqual(len(sent_fail), 1)
        self.assertEqual(res_fail["event_type"], "FAILED")
        self.assertEqual(res_fail["verdict"], "FAIL")
        self.assertTrue(res_fail["requires_master_intervention"])

    def test_report_to_master_file_inbox_and_http_retry(self):
        inbox_dir = os.path.join(self.tmp_dir, "inbox")
        payload = {"task_id": "task_file_1", "event_type": "COMPLETED", "summary": "ok"}
        agy_wrapper.report_to_master("http://unused", payload, inbox_dir=inbox_dir)

        report_file = os.path.join(inbox_dir, "task_file_1_report.json")
        self.assertTrue(os.path.isfile(report_file))
        with open(report_file, "r", encoding="utf-8") as f:
            self.assertEqual(json.load(f)["event_type"], "COMPLETED")

        # HTTP 1차 일시 실패 -> 2차 성공
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read = MagicMock(return_value=b'{"ok": true}')

        with patch("urllib.request.urlopen", side_effect=[urllib.error.URLError("temp"), mock_resp]) as mock_open:
            agy_wrapper.report_to_master(
                "http://127.0.0.1:8765/v1/callbacks/task-events",
                payload,
                max_retries=2,
                backoff_sec=0,
            )
        self.assertEqual(mock_open.call_count, 2)


class TestTaskEventsCallbackEndpoint(AioHTTPTestCase):
    """POST /v1/callbacks/task-events 웹훅 수신 및 마스터 상태/턴 적재 통합 테스트."""

    async def get_application(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._orig_settings = copy.deepcopy(engine_settings._settings)
        jobs_dir = os.path.join(self._tmp_dir.name, "jobs")
        os.makedirs(jobs_dir, exist_ok=True)
        self._runtime_patchers = [
            patch(
                "engine_settings._SETTINGS_FILE",
                os.path.join(self._tmp_dir.name, "bot_settings.json"),
            ),
            patch(
                "stream_parser._SESSION_LOG_FILE",
                os.path.join(self._tmp_dir.name, "session_log.jsonl"),
            ),
            patch(
                "bot_logger.BOT_LOG_PATH",
                os.path.join(self._tmp_dir.name, "bot-log.md"),
            ),
            patch("bot_logger._run_vault_log", AsyncMock()),
            patch.object(web_execution, "JOBS_DIR", jobs_dir),
        ]
        for p in self._runtime_patchers:
            p.start()
        self.db_path = os.path.join(self._tmp_dir.name, "chat_cb_test.db")
        self._db_patcher = patch("chat_db.CHAT_DB_FILE", self.db_path)
        self._db_patcher.start()
        test_hidden = os.path.join(self._tmp_dir.name, "hidden_rooms.json")
        self._hidden_patcher = patch("web_server.HIDDEN_ROOMS_FILE", test_hidden)
        self._hidden_patcher.start()
        self._chat_db_hidden_patcher = patch("chat_db.HIDDEN_ROOMS_FILE", test_hidden)
        self._chat_db_hidden_patcher.start()
        self._chats_dir_patcher = patch(
            "web_server.CHATS_DIR", os.path.join(self._tmp_dir.name, "chats")
        )
        self._chats_dir_patcher.start()
        self._rooms_file_patcher = patch(
            "web_server.CHAT_ROOMS_FILE",
            os.path.join(self._tmp_dir.name, "chat_rooms.json"),
        )
        self._rooms_file_patcher.start()
        self._rooms_backup_patcher = patch(
            "web_server.CHAT_ROOMS_BACKUP_FILE",
            os.path.join(self._tmp_dir.name, "chat_rooms_backup.json"),
        )
        self._rooms_backup_patcher.start()
        self._migrate_patcher = patch("chat_db.migrate_legacy_files_if_needed")
        self._migrate_patcher.start()
        self._tasks_patcher = patch(
            "master_tasks.MASTER_TASKS_FILE",
            os.path.join(self._tmp_dir.name, "master_tasks.json"),
        )
        self._tasks_patcher.start()
        self._memory_patcher = patch(
            "master_memory.MASTER_MEMORY_FILE",
            os.path.join(self._tmp_dir.name, "master_memory.json"),
        )
        self._memory_patcher.start()
        self.state_file = os.path.join(self._tmp_dir.name, "master_state.json")
        self._state_patcher = patch("web_orchestrator.MASTER_STATE_FILE", self.state_file)
        self._state_patcher.start()
        chat_db.init_db(db_path=self.db_path, migrate_legacy=False)
        web_server.WEB_AUTH_REQUIRED = True
        web_server.WEB_TOKEN = "cb-secret-token"
        return web_server.create_web_app(static_dir=web_server.INTERNAL_STATIC_DIR)

    async def tearDownAsync(self):
        web_server.WEB_AUTH_REQUIRED = True
        web_server._room_session_ids.pop("__core__", None)
        self._state_patcher.stop()
        self._memory_patcher.stop()
        self._tasks_patcher.stop()
        self._migrate_patcher.stop()
        self._rooms_backup_patcher.stop()
        self._rooms_file_patcher.stop()
        self._chats_dir_patcher.stop()
        self._chat_db_hidden_patcher.stop()
        self._hidden_patcher.stop()
        self._db_patcher.stop()
        for p in reversed(self._runtime_patchers):
            p.stop()
        engine_settings._settings.clear()
        engine_settings._settings.update(self._orig_settings)
        if os.path.exists(engine_settings._SETTINGS_FILE):
            engine_settings._sync_from_disk()
        self._tmp_dir.cleanup()
        await super().tearDownAsync()

    async def test_local_loopback_callback_records_state_and_enqueues_master(self):
        long_summary = "S" * 700
        payload = {
            "task_id": "task_98a7f6",
            "event_type": "COMPLETED",
            "verdict": "PASS",
            "attempt": 2,
            "summary": long_summary,
            "pointers": {
                "artifact_path": ".agy_workspace/task_98a7f6/output.md",
                "raw_log_path": ".agy_workspace/task_98a7f6/full.log",
            },
        }
        with patch("web_server.enqueue_room_turn", new_callable=AsyncMock, return_value={"already_running": False, "queue_size": 0}) as mock_enq, patch(
            "web_server.broadcast_ws_json", new_callable=AsyncMock
        ) as mock_bc:
            res = await self.client.post("/v1/callbacks/task-events", json=payload)

        self.assertEqual(res.status, 200)
        body = await res.json()
        self.assertTrue(body.get("ok"))
        self.assertEqual(body.get("task_id"), "task_98a7f6")

        # 1. master_state.json 에 500자 하드 캡 적용 및 포인터 기록 확인
        with open(self.state_file, "r", encoding="utf-8") as f:
            state_data = json.load(f)
        rec = state_data["tasks"]["task_98a7f6"]
        self.assertEqual(rec["verdict"], "PASS")
        self.assertEqual(rec["summary"], ("S" * 500) + agy_wrapper.SUMMARY_TRUNCATE_SUFFIX)
        self.assertEqual(rec["pointers"]["artifact_path"], ".agy_workspace/task_98a7f6/output.md")

        # 2. master_tasks 원장 동기화 및 자동 검토 완료 마킹 확인
        ledger_task = master_tasks.get_task("task_98a7f6")
        self.assertIsNotNone(ledger_task)
        self.assertEqual(ledger_task["status"], "done")
        self.assertTrue(ledger_task["reviewed"])
        self.assertIsNotNone(ledger_task.get("reportedAt"))
        self.assertFalse(master_tasks.needs_attention(ledger_task))

        # 3. __core__ 마스터에 포인터 기반 보고 적재 확인
        mock_enq.assert_awaited_once()
        enq_kwargs = mock_enq.call_args.kwargs
        self.assertEqual(enq_kwargs["room_id"], "__core__")
        self.assertIn(".agy_workspace/task_98a7f6/output.md", enq_kwargs["user_text"])
        self.assertIn(".agy_workspace/task_98a7f6/full.log", enq_kwargs["user_text"])
        mock_bc.assert_awaited()

    async def test_interim_retry_callback_skips_master_enqueue(self):
        payload = {
            "task_id": "task_retry_1",
            "event_type": "INTERIM_REPORT_AND_RETRY",
            "verdict": "PARTIAL",
            "attempt": 1,
            "summary": "1차 부분 결과",
            "requires_master_intervention": False,
        }
        with patch("web_server.enqueue_room_turn", new_callable=AsyncMock, return_value={"already_running": False, "queue_size": 0}) as mock_enq, patch(
            "web_server.broadcast_ws_json", new_callable=AsyncMock
        ) as mock_bc:
            res = await self.client.post("/v1/callbacks/task-events", json=payload)

        self.assertEqual(res.status, 200)
        body = await res.json()
        self.assertEqual(body.get("status"), "recorded")
        mock_enq.assert_not_called()
        mock_bc.assert_awaited()
        ledger_task = master_tasks.get_task("task_retry_1")
        self.assertIsNotNone(ledger_task)
        self.assertEqual(ledger_task["status"], "running")

    async def test_proxied_unauthenticated_callback_rejected(self):
        res = await self.client.post(
            "/v1/callbacks/task-events",
            json={"task_id": "t1", "event_type": "COMPLETED"},
            headers={"X-Forwarded-For": "203.0.113.10"},
        )
        self.assertEqual(res.status, 401)


class TestSessionCtlSupervise(unittest.TestCase):
    """session-ctl supervise 서브커맨드 백그라운드 스폰 검증."""

    def test_cmd_supervise_async(self):
        session_ctl = _load_session_ctl()
        mock_proc = MagicMock(pid=42424)
        args = argparse.Namespace(
            task_id="task_cli_1",
            objective="모듈 점검",
            criteria="에러 0건",
            timeout=90,
            max_retries=2,
            master_callback="http://127.0.0.1:8765/v1/callbacks/task-events",
            async_mode=True,
            json=True,
        )
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            rc = session_ctl.cmd_supervise(args)
        self.assertEqual(rc, 0)
        popen_cmd = mock_popen.call_args.args[0]
        self.assertIn("agy_wrapper.py", popen_cmd[1])
        self.assertIn("task_cli_1", popen_cmd)


if __name__ == "__main__":
    unittest.main()
