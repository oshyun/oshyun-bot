"""tests/test_delegated_chips.py — 마스터 에이전트 하위 에이전트 위임 칩 추출 단위 테스트."""

import sys
import os
import unittest
from unittest.mock import MagicMock

# src(패키지 루트) 경로 추가
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from bot.web.orchestrator import extract_delegated_subagents


class TestDelegatedSubagentChips(unittest.TestCase):
    def test_extract_from_session_ctl_spawn_with_output(self):
        """session-ctl spawn 호출 및 성공 출력에서 title과 roomId를 정확히 추출하는지 검증."""
        steps = [
            {
                "kind": "tool_call",
                "tool": "Bash",
                "title": "session-ctl spawn: 일정 조사",
                "detail": "$ session-ctl spawn \"일정 조사\" \"오늘 일정 확인해줘\" --async",
            },
            {
                "kind": "tool_result",
                "title": "tool_result (2 lines)",
                "detail": "🚀 새 채팅방(AGY 세션) [일정 조사] (Room ID: room-17412345-abcd)을 생성하고 백그라운드로 작업을 시작했습니다.",
            },
        ]
        res = extract_delegated_subagents(steps)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["roomId"], "room-17412345-abcd")
        self.assertEqual(res[0]["title"], "일정 조사")
        self.assertIn(res[0]["status"], ("done", "running"))

    def test_extract_from_reused_session_spawn(self):
        """기존 유사 채팅방 재사용 출력에서 title과 roomId 추출 검증."""
        steps = [
            {
                "kind": "tool_call",
                "tool": "run_command",
                "title": "session-ctl spawn: 주식 분석",
                "detail": "$ session-ctl spawn '주식 분석' '포트폴리오 분석' --async",
            },
            {
                "kind": "tool_result",
                "title": "tool_result",
                "detail": "♻️ 유사한 기존 채팅방 [주식 분석] (Room ID: room-stock-9999, 유사도: 0.88)을 찾아 이어서 작업을 시작했습니다.",
            },
        ]
        res = extract_delegated_subagents(steps)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["roomId"], "room-stock-9999")
        self.assertEqual(res[0]["title"], "주식 분석")

    def test_extract_from_session_ctl_send(self):
        """session-ctl send 호출 및 출력에서 title과 roomId 추출 검증."""
        steps = [
            {
                "kind": "tool_call",
                "tool": "Bash",
                "title": "session-ctl send: room-task-1234",
                "detail": "$ session-ctl send \"room-task-1234\" \"추가 분석 진행해줘\" --async",
            },
            {
                "kind": "tool_result",
                "title": "tool_result",
                "detail": "✅ [데이터 정리] (ID: room-task-1234) 세션에 작업을 비동기로 지시했습니다. (상태: queued)",
            },
        ]
        res = extract_delegated_subagents(steps)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["roomId"], "room-task-1234")
        self.assertEqual(res[0]["title"], "데이터 정리")

    def test_extract_from_json_mode(self):
        """session-ctl --json 출력에서 title과 roomId 추출 검증."""
        steps = [
            {
                "kind": "tool_call",
                "tool": "Bash",
                "title": "session-ctl spawn: 번역",
                "detail": "$ session-ctl spawn \"영한 번역\" \"translate this\" --async --json",
            },
            {
                "kind": "tool_result",
                "title": "tool_result",
                "detail": '{\n  "ok": true,\n  "roomId": "room-trans-777",\n  "title": "영한 번역",\n  "status": "queued"\n}',
            },
        ]
        res = extract_delegated_subagents(steps)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["roomId"], "room-trans-777")
        self.assertEqual(res[0]["title"], "영한 번역")

    def test_dedup_and_multiple_delegations(self):
        """동일 세션에 여러 번 지시한 경우 중복 제거 및 여러 하위 에이전트 목록 정상 반환 검증."""
        steps = [
            {
                "kind": "tool_call",
                "tool": "Bash",
                "title": "session-ctl spawn: 작업 1",
                "detail": "🚀 새 채팅방(AGY 세션) [작업 1] (Room ID: room-1)을 생성하고 백그라운드로 작업을 시작했습니다.",
            },
            {
                "kind": "tool_call",
                "tool": "Bash",
                "title": "session-ctl send: room-1",
                "detail": "✅ [작업 1] (ID: room-1) 세션에 작업을 비동기로 지시했습니다.",
            },
            {
                "kind": "tool_call",
                "tool": "Bash",
                "title": "session-ctl spawn: 작업 2",
                "detail": "🚀 새 채팅방(AGY 세션) [작업 2] (Room ID: room-2)을 생성하고 백그라운드로 작업을 시작했습니다.",
            },
        ]
        res = extract_delegated_subagents(steps)
        self.assertEqual(len(res), 2)
        room_ids = [item["roomId"] for item in res]
        self.assertEqual(room_ids, ["room-1", "room-2"])

    def test_status_running_when_busy(self):
        """web_execution.is_room_busy 가 True 일 때 status 가 running 으로 설정되는지 검증."""
        mock_exec = MagicMock()
        mock_exec.is_room_busy.return_value = True

        steps = [
            {
                "kind": "tool_call",
                "tool": "Bash",
                "title": "session-ctl spawn: 일정",
                "detail": "🚀 새 채팅방(AGY 세션) [일정] (Room ID: room-busy-1) 생성",
            }
        ]
        res = extract_delegated_subagents(steps, web_exec_module=mock_exec)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["status"], "running")

    def test_extract_empty_or_none(self):
        """steps 가 None 이거나 비어있을 때 안전하게 빈 리스트 반환 검증."""
        self.assertEqual(extract_delegated_subagents(None), [])
        self.assertEqual(extract_delegated_subagents([]), [])

    def test_filter_out_core_itself(self):
        """마스터 에이전트 자신(__core__)은 하위 위임 대상에서 제외되는지 검증."""
        steps = [
            {
                "kind": "tool_call",
                "tool": "Bash",
                "title": "session-ctl send: __core__",
                "detail": "$ session-ctl send \"__core__\" \"자기 자신에게 전달\" --async",
            }
        ]
        res = extract_delegated_subagents(steps)
        self.assertEqual(len(res), 0)

    def test_prompt_containing_error_word_not_falsely_marked_error(self):
        """프롬프트 인자에 '오류'/'실패' 단어가 있어도 실행 결과가 성공이면 error가 아닌 done/running으로 판정되는지 검증."""
        steps = [
            {
                "kind": "tool_call",
                "tool": "run_command",
                "title": "session-ctl spawn: 완료된 채팅방 제목 [done] 일괄 표기",
                "detail": "$ session-ctl spawn \"완료된 채팅방 제목 [done] 일괄 표기\" \"미결/중단/오류 상태가 아닌 채팅방들을 선별하세요\" --async",
            },
            {
                "kind": "tool_result",
                "detail": "Command exited with code 0.\nCommand output:\n🚀 새 채팅방(AGY 세션) [완료된 채팅방 제목 [done] 일괄 표기] (Room ID: room-done-123)을 생성하고 백그라운드로 작업을 시작했습니다.",
            },
        ]
        res = extract_delegated_subagents(steps)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["roomId"], "room-done-123")
        self.assertEqual(res[0]["title"], "완료된 채팅방 제목 [done] 일괄 표기")
        self.assertNotEqual(res[0]["status"], "error")
        self.assertEqual(res[0]["status"], "done")
        self.assertEqual(res[0]["reason"], "정상 완료")

    def test_failed_command_marked_error_with_reason(self):
        """실제 명령어 실행 실패 시 status가 error로 설정되고 구체적인 reason이 담기는지 검증."""
        steps = [
            {
                "kind": "tool_call",
                "tool": "run_command",
                "title": "session-ctl spawn: 실패 테스트",
                "detail": "$ session-ctl spawn \"실패 테스트\" \"...\" --async",
                "status": "error",
                "error": "포트 연결 불가",
            },
        ]
        res = extract_delegated_subagents(steps)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["status"], "error")
        self.assertIn("포트 연결 불가", res[0]["reason"])

    def test_task_status_and_reason_reflection(self):
        """master_tasks 원장에서 작업 상태(failed, cancelled, done 등)와 reason이 반영되는지 검증."""
        mock_tasks = MagicMock()
        mock_tasks.list_tasks.return_value = [
            {
                "roomId": "room-task-failed",
                "status": "failed",
                "error": "세션 타임아웃 180초 초과",
            }
        ]
        steps = [
            {
                "kind": "tool_call",
                "tool": "Bash",
                "title": "session-ctl spawn: 장기 작업",
                "detail": "🚀 새 채팅방(AGY 세션) [장기 작업] (Room ID: room-task-failed) 생성",
            }
        ]
        res = extract_delegated_subagents(steps, master_tasks_module=mock_tasks)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["status"], "error")
        self.assertEqual(res[0]["reason"], "세션 타임아웃 180초 초과")

    def test_load_room_messages_injects_delegated_rooms(self):
        """web_rooms._load_room_messages 에서 __core__ 메시지에 delegatedRooms 필드가 주입되는지 검증."""
        from bot.web.rooms import _load_room_messages

        mock_db = MagicMock()
        mock_db.get_messages.return_value = [
            {
                "id": 1,
                "role": "user",
                "text": "일정 알려줘",
            },
            {
                "id": 2,
                "role": "assistant",
                "text": "하위 에이전트에게 위임했습니다.",
                "steps": [
                    {
                        "kind": "tool_call",
                        "tool": "Bash",
                        "title": "session-ctl spawn: 일정 조사",
                        "detail": "$ session-ctl spawn \"일정 조사\" \"오늘 일정 확인\" --async",
                    },
                    {
                        "kind": "tool_result",
                        "detail": "🚀 새 채팅방(AGY 세션) [일정 조사] (Room ID: room-cal-999) 생성 완료",
                    },
                ],
            },
        ]

        import bot.web.rooms as web_rooms
        orig_chat_db = web_rooms.chat_db
        web_server = sys.modules.get("bot.web.server")
        orig_ws_chat_db = getattr(web_server, "chat_db", None) if web_server else None
        try:
            web_rooms.chat_db = mock_db
            if web_server and hasattr(web_server, "chat_db"):
                web_server.chat_db = mock_db
            for orch_rid in ("__core__", "__expert_vault__", "__expert_coding__"):
                msgs = _load_room_messages(orch_rid)
                self.assertEqual(len(msgs), 2)
                self.assertNotIn("delegatedRooms", msgs[0])  # user 메시지에는 없음
                self.assertIn("delegatedRooms", msgs[1])      # assistant 메시지에 주입됨
                self.assertEqual(len(msgs[1]["delegatedRooms"]), 1)
                self.assertEqual(msgs[1]["delegatedRooms"][0]["roomId"], "room-cal-999")
                self.assertEqual(msgs[1]["delegatedRooms"][0]["title"], "일정 조사")
        finally:
            web_rooms.chat_db = orig_chat_db
            if web_server and orig_ws_chat_db is not None:
                web_server.chat_db = orig_ws_chat_db

    def test_ignore_help_command_no_duplicate_chip(self):
        """session-ctl spawn --help 호출 시 '(23줄):' 같은 가짜 칩이 추출되지 않고 실제 위임 1개만 반환되는지 검증."""
        steps = [
            {
                "kind": "tool_call",
                "tool": "Bash",
                "title": "session-ctl spawn: --help",
                "detail": "$ /Users/user/repos/oshyun-bot/scripts/session-ctl spawn --help\n결과 (23줄): usage: session-ctl spawn ...",
            },
            {
                "kind": "tool_call",
                "tool": "Bash",
                "title": "session-ctl spawn: 웹 UI 폰트 크기 슬라이더 개편",
                "detail": "$ /Users/user/repos/oshyun-bot/scripts/session-ctl spawn \"웹 UI 폰트 크기 슬라이더 개편\" \"작업 지시\"\n🚀 새 채팅방(AGY 세션) [웹 UI 폰트 크기 슬라이더 개편] (Room ID: room-font-1) 생성",
            },
        ]
        res = extract_delegated_subagents(steps)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["roomId"], "room-font-1")
        self.assertEqual(res[0]["title"], "웹 UI 폰트 크기 슬라이더 개편")

    def test_retry_spawn_generates_single_chip_with_actual_room_id(self):
        """동일 과업에 대해 최초 spawn 실패/미완료 스텝과 이후 spawn 성공 스텝이 공존할 때 1개만 반환되고 실제 roomId가 바인딩되는지 검증."""
        steps = [
            {
                "kind": "tool_call",
                "tool": "run_command",
                "title": "session-ctl spawn: 스킨케어 동적 추천 체계 개편 및 스케줄러 연동",
                "detail": "$ session-ctl spawn \"스킨케어 동적 추천 체계 개편 및 스케줄러 연동\" \"과업 지시...\" --parent __expert_vault__",
                "status": "running",
            },
            {
                "kind": "tool_call",
                "tool": "run_command",
                "title": "run_command: ./scripts/status.sh",
                "detail": "$ ./scripts/status.sh",
            },
            {
                "kind": "tool_call",
                "tool": "run_command",
                "title": "session-ctl spawn: 스킨케어 동적 추천 체계 개편 및 스케줄러 연동",
                "detail": "$ WEB_SSL_ENABLED=0 session-ctl spawn \"스킨케어 동적 추천 체계 개편 및 스케줄러 연동\" \"과업 지시...\"\n결과:\n🚀 새 채팅방(AGY 세션) [스킨케어 동적 추천 체계 개편 및 스케줄러 연동] (Room ID: room-1795273462171-d73e)을 생성하고 백그라운드로 작업을 시작했습니다.",
                "status": "done",
            },
        ]
        res = extract_delegated_subagents(steps)
        self.assertEqual(len(res), 1, "중복 칩 없이 단 1개만 반환되어야 함")
        self.assertEqual(res[0]["roomId"], "room-1795273462171-d73e")
        self.assertEqual(res[0]["title"], "스킨케어 동적 추천 체계 개편 및 스케줄러 연동")
        self.assertIn(res[0]["status"], ("done", "running"))

    def test_missing_room_id_never_puts_title_into_room_id(self):
        """실제 room ID가 파싱되지 않고 DB에도 없는 경우 roomId 필드에 절대 제목 문자열이 주입되지 않는지 검증."""
        steps = [
            {
                "kind": "tool_call",
                "tool": "Bash",
                "title": "session-ctl spawn: 아직 생성 전인 미지의 작업",
                "detail": "$ session-ctl spawn \"아직 생성 전인 미지의 작업\" \"지시문...\"",
                "status": "running",
            }
        ]
        res = extract_delegated_subagents(steps)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["roomId"], "", "유효한 room ID가 없으면 빈 문자열이어야 함")
        self.assertEqual(res[0]["title"], "아직 생성 전인 미지의 작업")


if __name__ == "__main__":
    unittest.main()
