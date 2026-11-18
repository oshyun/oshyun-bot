import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "bot"))
import master_memory


class TestMasterMemoryExtensions(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._tmp_file = os.path.join(self._tmp_dir.name, "master_memory.json")
        self._patcher = patch("master_memory.MASTER_MEMORY_FILE", self._tmp_file)
        self._patcher.start()
        master_memory.clear_memory("all")

    def tearDown(self):
        try:
            self._patcher.stop()
        except Exception:
            pass
        self._tmp_dir.cleanup()

    def test_rebalance_and_move_flow(self):
        # 1. 항목 추가
        s1 = master_memory.add_memory("첫번째 메모리 내용입니다.", title="첫번째", tier="hot")
        item1 = s1["addedItem"]
        self.assertIsNotNone(item1)
        self.assertEqual(item1["tier"], "hot")

        s2 = master_memory.add_memory("두번째 메모리 내용입니다.", title="두번째", tier="warm")
        item2 = s2["addedItem"]
        self.assertEqual(item2["tier"], "warm")

        # 2. 개별 항목 자유 이동 (hot -> freeze)
        moved_snap = master_memory.move_memory_item(item1["id"], "freeze")
        self.assertEqual(moved_snap["lastOperation"]["action"], "move")
        self.assertEqual(moved_snap["lastOperation"]["toTier"], "freeze")

        # freeze 계층에 이동되었는지 확인
        freeze_items = moved_snap["tiers"]["freeze"]["items"]
        self.assertTrue(any(it["id"] == item1["id"] for it in freeze_items))

        # 3. 개별 항목 자유 이동 (freeze -> hot 승격)
        promoted_snap = master_memory.move_memory_item(item1["id"], "hot")
        self.assertEqual(promoted_snap["lastOperation"]["toTier"], "hot")
        hot_items = promoted_snap["tiers"]["hot"]["items"]
        self.assertTrue(any(it["id"] == item1["id"] for it in hot_items))

        # 4. 상위 승격 (warm -> hot)
        prom_snap = master_memory.promote_tier("warm")
        self.assertEqual(prom_snap["lastOperation"]["action"], "promote")
        self.assertEqual(prom_snap["lastOperation"]["toTier"], "hot")

        # 5. 리밸런싱 실행
        reb_snap = master_memory.rebalance_memory()
        self.assertEqual(reb_snap["lastOperation"]["action"], "rebalance")

        # 6. 삭제 실행
        del_snap = master_memory.delete_memory_item(item1["id"])
        self.assertEqual(del_snap["lastOperation"]["action"], "delete")
        self.assertFalse(any(it["id"] == item1["id"] for it in del_snap["tiers"]["hot"]["items"]))

    def test_dispatch_summarization_strips_instructions_and_paths(self):
        long_prompt = (
            "현재 날짜(2026년 11월 16일) 기준으로 주요 증권사별 연금저축펀드(연저펀) 신규 입금/순입금 이벤트의 "
            "혜택 금액과 정확한 이벤트 기간을 웹 검색으로 조사해 주세요.\n"
            "[세부 지시]\n"
            "1. 먼저 볼트 내 `/Users/user/obsidian/oshyun/개인/금융/계좌.md` 를 확인하세요.\n"
            "2. 미래에셋, 삼성, 한국투자 증권 이벤트를 조사하세요.\n"
            "[원본 데이터 파일 경로]\n"
            "- /Users/user/repos/oshyun-bot/data/attached_files/1794792794_sample.jpg"
        )
        formatted = master_memory.format_dispatch_memory(
            action="spawn",
            room_title="연저펀 신규금액 이벤트 조사",
            room_id="room-1794798536167-97ca",
            task_id="mt-7d342cf2",
            message=long_prompt,
        )
        snap = master_memory.add_memory(
            formatted,
            title="연저펀 신규금액 이벤트 조사",
            kind="dispatch",
            room_id="room-1794798536167-97ca",
            task_id="mt-7d342cf2",
        )
        added = snap["addedItem"]
        self.assertLessEqual(len(added["text"]), master_memory.AUTO_SUMMARY_MAX_CHARS)
        self.assertIn("room-1794798536167-97ca", added["text"])
        self.assertIn("mt-7d342cf2", added["text"])
        self.assertIn("연금저축펀드", added["text"])
        self.assertNotIn("[세부 지시]", added["text"])
        self.assertNotIn("/Users/user/", added["text"])
        self.assertNotIn("현재 날짜(", added["text"])

    def test_notify_and_task_report_turn_merge_without_duplication(self):
        raw_result = (
            "✅ **2026년 5월 네이버페이 수입·지출 전체 내역(31일, 271건)** 교차 검증 및 "
            "실가계부 정산을 완료하여 볼트에 반영했습니다.\n\n"
            "### 📌 1. 1원 단위 교차 검증 결과\n"
            "- **원본 집계**: 수입 `+60,975,502원` / 지출 `-13,556,718원` / 합계 `+47,418,784원`\n"
            "- 반영 파일: [[개인/금융 💰/가계부/가계부 대시보드|가계부 대시보드.md]]"
        )
        notify_text = master_memory.format_notify_memory(
            status_label="성공",
            room_id="room-1794759775709-8b29",
            task_id="mt-f67f6443",
            result_text=raw_result,
        )
        snap1 = master_memory.add_memory(
            notify_text,
            title="보고: mt-f67f6443",
            kind="notify",
            room_id="room-1794759775709-8b29",
            task_id="mt-f67f6443",
        )
        item1 = snap1["addedItem"]
        self.assertLessEqual(len(item1["text"]), master_memory.AUTO_SUMMARY_MAX_CHARS)
        self.assertIn("mt-f67f6443", item1["text"])
        self.assertIn("+60,975,502원", item1["text"])
        self.assertNotIn("**", item1["text"])
        self.assertNotIn("###", item1["text"])

        # 이어서 _notify_master_agent 가 트리거한 task_report 턴이 들어와도 중복 적재 대신 병합되어야 함
        report_prompt = (
            "[하위 작업 완료 보고] [네이버페이 정산] 세션 작업 완료\n"
            "• 대상 방: 네이버페이 정산 (Room ID: room-1794759775709-8b29)\n"
            "• 작업 ID: mt-f67f6443 (상태: done)\n"
            "• 상태: 정상 완료 (원장 자동 검토 완료)\n"
            "• 하위 에이전트 응답 요약:\n2026년 5월 정산 완료\n\n"
            "※ 마스터 지침: 작업 원장은 자동 완료 처리되었으므로 별도 호출 없이 확인하세요."
        )
        master_reply = (
            "**2026년 5월 네이버페이 가계부 정산**(`mt-f67f6443`) 완료되었습니다.\n"
            "- 순수 생활 지출: `-4,120,000원` 확정 반영"
        )
        snap2 = master_memory.record_orchestrator_turn_memory(
            user_text=report_prompt,
            reply_text=master_reply,
            channel="task_report",
            task_id="mt-f67f6443",
        )
        self.assertIsNotNone(snap2)
        self.assertTrue(snap2["lastOperation"].get("merged"))
        self.assertEqual(snap2["tiers"]["hot"]["itemCount"], 1)
        merged_txt = snap2["tiers"]["hot"]["items"][0]["text"]
        self.assertLessEqual(len(merged_txt), master_memory.AUTO_SUMMARY_MAX_CHARS)
        self.assertIn("mt-f67f6443", merged_txt)
        self.assertNotIn("하위 작업 완료 보고", merged_txt)
        self.assertNotIn("※ 마스터 지침", merged_txt)

    def test_turn_ui_paste_cleanup_and_delegation_merge(self):
        # 1. UI 복붙 노이즈 제거
        ui_query = (
            "15일 포인트 보정해 현재 11월 날짜 선택하기 이전 달 보기다음 달 보기 "
            "전체내역 NPay결제내역만 보기 결제수단 선택 전체 카드 계좌 네이버페이포인트"
        )
        cleaned_q = master_memory.summarize_user_query(ui_query)
        self.assertEqual(cleaned_q, "15일 포인트 보정해 (11월)")

        # 2. 직전 dispatch 와 동일 방 단순 위임 응답 병합
        master_memory.add_memory(
            "[위임:send|room-1794759775709-8b29|mt-93c1530c] 11월 15일 포인트 보정",
            title="네이버페이 정산",
            kind="dispatch",
            room_id="room-1794759775709-8b29",
            task_id="mt-93c1530c",
        )
        delegation_reply = (
            "하위 에이전트(**네이버페이 정산**, `room-1794759775709-8b29`)에게 "
            "**2026년 11월 15일 포인트 보정** 작업을 비동기로 위임했습니다.\n"
            "### 📌 위임 내용\n- 하나머니 상계 반영"
        )
        snap = master_memory.record_orchestrator_turn_memory(
            user_text=ui_query,
            reply_text=delegation_reply,
            channel="web",
        )
        self.assertIsNotNone(snap)
        self.assertTrue(snap["lastOperation"].get("merged"))
        self.assertEqual(snap["tiers"]["hot"]["itemCount"], 1)
        self.assertIn("15일 포인트 보정해", snap["tiers"]["hot"]["items"][0]["text"])

        # 3. 정기 점검의 단순 이상 없음 응답은 노이즈 적재를 생략
        patrol_res = master_memory.record_orchestrator_turn_memory(
            user_text="[마스터 에이전트 정기 점검 — 2026-11-16]\n■ 조치 지침\n1. 확인...",
            reply_text="이상 없음 (모든 작업 정상).",
            channel="master_patrol",
        )
        self.assertIsNone(patrol_res)

    def test_task_event_collapses_repeated_garbage_and_preserves_reason(self):
        ev_text = master_memory.format_task_event_memory(
            event_type="FAILED",
            verdict="FAIL",
            task_id="task_98a7f6",
            summary="S" * 240,
            missing_reason="필수 산출물 누락",
        )
        self.assertLessEqual(len(ev_text), master_memory.AUTO_SUMMARY_MAX_CHARS)
        self.assertIn("task_98a7f6", ev_text)
        self.assertIn("S×240", ev_text)
        self.assertIn("필수 산출물 누락", ev_text)

    def test_v1_legacy_migration_and_compact_memory(self):
        legacy_payload = {
            "version": 1,
            "tiers": {
                "hot": [
                    {
                        "id": "mm-1",
                        "tier": "hot",
                        "kind": "dispatch",
                        "title": "네이버페이 정산",
                        "text": "[디스패치:spawn] 네이버페이 정산 (room-1794759775709-8b29) — "
                                "9월·11월 내역 분석. [세부 지시] 1. /Users/user/repos/oshyun-bot/data/a.txt 읽기 " + ("상세내용 " * 40),
                        "chars": 400,
                        "roomId": "room-1794759775709-8b29",
                        "taskId": "mt-1111aaaa",
                        "createdAt": 1000.0,
                        "updatedAt": 1000.0,
                    },
                    {
                        "id": "mm-2",
                        "tier": "hot",
                        "kind": "notify",
                        "title": "보고: mt-1111aaaa",
                        "text": "[성공 보고 · room-1794759775709-8b29] ✅ **9월 정산(269건)** 완료 · 수입 `+11,275,499원` / 지출 `-4,756,121원` " + ("부가설명 " * 30),
                        "chars": 300,
                        "roomId": "room-1794759775709-8b29",
                        "taskId": "mt-1111aaaa",
                        "createdAt": 1010.0,
                        "updatedAt": 1010.0,
                    },
                    {
                        "id": "mm-3",
                        "tier": "hot",
                        "kind": "turn",
                        "title": "[하위 작업 완료 보고] [네이버페이 정산",
                        "text": "Q: [하위 작업 완료 보고] [네이버페이 정산] 세션 작업 완료 • 대상 방: 네이버페이 정산 (Room ID: room-1794759775709-8b29) • 작업 ID: mt-1111aaaa → A: **9월 정산 완료**(`mt-1111aaaa`) · 실수지 `+6,519,378원` 확정",
                        "chars": 240,
                        "roomId": "__core__",
                        "taskId": "",
                        "createdAt": 1020.0,
                        "updatedAt": 1020.0,
                    },
                ],
                "warm": [],
                "cold": [],
                "freeze": [],
            },
            "limits": {
                "hot": {"maxChars": 1500, "maxItems": 8, "itemMaxChars": 400},
                "warm": {"maxChars": 3000, "maxItems": 16, "itemMaxChars": 300},
                "cold": {"maxChars": 6000, "maxItems": 32, "itemMaxChars": 220},
                "freeze": {"maxChars": 15000, "maxItems": 100, "itemMaxChars": 180},
            },
            "stats": {"totalAdded": 3, "cascadeCount": 0, "droppedCount": 0},
        }
        with open(self._tmp_file, "w", encoding="utf-8") as f:
            json.dump(legacy_payload, f, ensure_ascii=False)

        snap = master_memory.get_memory_snapshot()
        self.assertEqual(snap["version"], 3)
        # 동일 taskId 의 dispatch + notify + task_report turn 3건이 1건으로 통합되고 제목은 dispatch 제목을 상속해야 함
        self.assertEqual(snap["tiers"]["hot"]["itemCount"], 1)
        self.assertLess(snap["tiers"]["hot"]["charCount"], 200)
        self.assertEqual(snap["limits"]["hot"]["itemMaxChars"], 220)
        it = snap["tiers"]["hot"]["items"][0]
        self.assertEqual(it["title"], "네이버페이 정산")
        self.assertLessEqual(len(it["text"]), master_memory.AUTO_SUMMARY_MAX_CHARS)
        self.assertNotIn("[세부 지시]", it["text"])
        self.assertNotIn("하위 작업 완료 보고", it["text"])

        compact_snap = master_memory.compact_memory()
        self.assertEqual(compact_snap["lastOperation"]["action"], "compact")
        self.assertEqual(compact_snap["tiers"]["hot"]["itemCount"], 1)

    def test_v3_table_boilerplate_cleanup_and_dispatch_notify_folding(self):
        # 1. 마크다운 표와 상투적 AGENTS.md 서론 제거 검증
        noisy_report = (
            "oshyun-bot 리포지토리의 AGENTS.md 워크트리 규칙에 따라 작업을 완료하고 웹 서버에 성공적으로 반영했습니다.\n"
            "### 발생 상황 요약\n"
            "| 항목 | 상세 내용 |\n"
            "| :--- | :--- |\n"
            "| 발생 대화방 | 웹 앱 폰트 사이즈 |\n"
            "| 원인 | cleanup_duplicate_rooms() 세션 ID 비교 누락 |\n"
            "- 단위 테스트: 294/294 passed"
        )
        notify_text = master_memory.format_notify_memory(
            status_label="성공",
            room_id="room-1794886018671-5926",
            task_id="mt-3fe68cbb",
            result_text=noisy_report,
        )
        self.assertNotIn(":---", notify_text)
        self.assertNotIn("항목 | 상세 내용", notify_text)
        self.assertNotIn("AGENTS.md 워크트리 규칙에 따라", notify_text)
        self.assertIn("cleanup_duplicate_rooms()", notify_text)

        # 2. dispatch -> notify 도착 시 동일 taskId 의 dispatch 를 흡수·병합하고 제목 상속
        long_dispatch = (
            "방금 턴이 빈 응답으로 종료되었습니다. 다시 한 번 네이버페이 가계부 정산 내역 동기화를 진행해 주세요: "
            "oshyun-bot 리포지토리(oshyun-bot)의 AGENTS.md 워크트리 규칙을 준수하여, "
            "다음 작업을 수행하세요: 1) 볼트 반영 2) 합계 대조"
        )
        disp_text = master_memory.format_dispatch_memory(
            action="send",
            room_title="네이버페이 가계부 정산 동기화",
            room_id="room-1794886018671-5926",
            task_id="mt-b93c5034",
            message=long_dispatch,
        )
        self.assertNotIn("방금 턴이 빈 응답으로 종료되었습니다", disp_text)
        self.assertNotIn("AGENTS.md 워크트리 규칙", disp_text)
        self.assertIn("네이버페이 가계부 정산", disp_text)

        master_memory.add_memory(
            disp_text,
            title="네이버페이 가계부 정산 동기화",
            kind="dispatch",
            room_id="room-1794886018671-5926",
            task_id="mt-b93c5034",
        )
        # inline room_id 없이 위임한 응답도 직전 dispatch 와 병합되어야 함
        turn_snap = master_memory.record_orchestrator_turn_memory(
            user_text="가계부 정산 마저 처리해줘",
            reply_text="네이버페이 가계부 정산 작업을 하위 에이전트에게 위임했습니다.",
            channel="web",
        )
        self.assertIsNotNone(turn_snap)
        self.assertTrue(turn_snap["lastOperation"].get("merged"))
        self.assertEqual(turn_snap["tiers"]["hot"]["itemCount"], 1)

        # 완료 notify 가 도착하면 dispatch 항목을 대체·통합하여 1건으로 유지하고 제목을 상속해야 함
        snap_done = master_memory.add_memory(
            master_memory.format_notify_memory(
                status_label="성공",
                room_id="room-1794886018671-5926",
                task_id="mt-b93c5034",
                result_text="첨부해주신 스크린샷과 로그를 정밀 분석한 결과입니다.\n- 11월 가계부 지출 합계 1,250,000원 볼트 동기화 완료",
            ),
            title="보고: mt-b93c5034",
            kind="notify",
            room_id="room-1794886018671-5926",
            task_id="mt-b93c5034",
        )
        self.assertEqual(snap_done["tiers"]["hot"]["itemCount"], 1)
        folded = snap_done["tiers"]["hot"]["items"][0]
        self.assertEqual(folded["title"], "네이버페이 가계부 정산 동기화")
        self.assertIn("1,250,000원", folded["text"])
        self.assertNotIn("정밀 분석한 결과입니다", folded["text"])

    def test_compact_prompt_context_strips_noise_and_duplicates(self):
        """프롬프트 주입 시 ID 태그 축약, Q/title 중복 제거, 제안형 꼬리 질문 필터링을 검증한다."""
        master_memory.add_memory(
            "[보고:성공|room-1794971854263-5dd5|mt-a85416de] 서버 재시작: ./scripts/restart.sh web 실행 완료 · 299개 단위 테스트 통과 후 main 병합",
            title="웹 UI 폰트 크기 슬라이더 개편 및 실시간 반영",
            kind="notify",
            summarize=False,
        )
        master_memory.add_memory(
            "Q: 토큰버닝탭이 왜필요한지 모르겠어 → A: [스케줄], [토큰 버닝] 탭 전환 UI 삭제 · 현재 잔여 쿼터(%) 뱃지 표시 · 원하시면 불필요한 '토큰 버닝' 서브탭을 즉시 제거해 드릴까요?",
            title="토큰버닝탭이 왜필요한지 모르겠어",
            kind="turn",
            summarize=False,
        )
        turn_snap = master_memory.record_orchestrator_turn_memory(
            user_text="🎨 1. 이번 달 카드 혜택 어떻게 됐어?",
            reply_text="신한카드 이번 달 할인 한도 15,000원 확인 완료되었습니다. 필요하시면 추가 점검도 진행할까요?",
            channel="web",
        )
        self.assertIsNotNone(turn_snap)
        added_turn = turn_snap["addedItem"]
        self.assertNotIn("🎨", added_turn["title"])

        ctx = master_memory.build_memory_prompt_context()
        self.assertIn("[성공]", ctx)
        self.assertNotIn("room-1794971854263-5dd5", ctx)
        self.assertNotIn("mt-a85416de", ctx)
        self.assertNotIn("Q: 토큰버닝탭이", ctx)
        self.assertNotIn("Q: 1. 이번 달 카드 혜택", ctx)
        self.assertNotIn("즉시 제거해 드릴까요", ctx)
        self.assertNotIn("진행할까요", ctx)

    def test_delta_prompt_context_injects_only_changes_per_session(self):
        """동일 세션에서는 변경분이 있을 때만 HOT 변경분만 주입하고 변경이 없으면 빈 문자열을 반환하는지 검증."""
        master_memory.add_memory("초기 HOT 기억 1", title="초기항목1")
        master_memory.add_memory("초기 WARM 기억", title="워밍업", tier="warm")

        # 1. 새 세션은 전체 컨텍스트 주입
        ctx_first = master_memory.build_memory_prompt_context(session_key="sess-alpha")
        self.assertIn("HOT (즉각 활성 컨텍스트)", ctx_first)
        self.assertIn("초기항목1", ctx_first)
        self.assertIn("워밍업", ctx_first)

        # 2. 체크포인트 커밋 후 변경이 없으면 빈 문자열
        master_memory.commit_memory_prompt_checkpoint("sess-alpha")
        ctx_no_change = master_memory.build_memory_prompt_context(session_key="sess-alpha")
        self.assertEqual(ctx_no_change, "")

        # 3. HOT에 새 기억이 추가되면 해당 변경분만 주입 (기존 항목 및 WARM 제외)
        master_memory.add_memory("새로운 하위 작업 완료 보고", title="신규보고")
        ctx_delta = master_memory.build_memory_prompt_context(session_key="sess-alpha")
        self.assertIn("HOT 변경분", ctx_delta)
        self.assertIn("신규보고", ctx_delta)
        self.assertNotIn("초기항목1", ctx_delta)
        self.assertNotIn("워밍업", ctx_delta)

        # 4. 다른 세션은 체크포인트가 없으므로 전체 컨텍스트 주입
        ctx_other = master_memory.build_memory_prompt_context(session_key="sess-beta")
        self.assertIn("HOT (즉각 활성 컨텍스트)", ctx_other)
        self.assertIn("초기항목1", ctx_other)
        self.assertIn("신규보고", ctx_other)

    def test_flash_low_async_llm_refine_updates_memory_item(self):
        """flash low 경량 LLM 백그라운드 요약이 완료되면 해당 항목의 본문이 깔끔한 1문장으로 갱신되는지 검증."""
        import asyncio
        from types import SimpleNamespace

        snap = master_memory.add_memory(
            "[보고:성공|room-1794846261696-f7fa|mt-3b2623e2] 장황한 원문 보고입니다. " + ("세부 내역 " * 12),
            title="택시비 할인카드 확인",
            kind="notify",
            summarize=True,
        )
        item_id = snap["addedItem"]["id"]
        existing_text = snap["addedItem"]["text"]

        async def fake_run_agy_cli(prompt, timeout_sec=20, extra_args=None, cwd=None, env=None):
            self.assertIn("gemini-3.8-flash", extra_args)
            self.assertIn("low", extra_args)
            return SimpleNamespace(
                exit_status="NORMAL",
                return_code=0,
                stdout="택시 10% 할인 카드 2종 보유 확인 및 비교 완료",
                stderr="",
            )

        with patch("agy_wrapper.run_agy_cli", side_effect=fake_run_agy_cli):
            asyncio.run(
                master_memory._refine_memory_item_bg(
                    item_id,
                    "장황한 원문 보고입니다. " + ("세부 내역 " * 12),
                    kind="notify",
                    title="택시비 할인카드 확인",
                    existing_text=existing_text,
                )
            )

        updated_snap = master_memory.get_memory_snapshot(tier="hot")
        updated_item = next(it for it in updated_snap["tiers"]["hot"]["items"] if it["id"] == item_id)
        self.assertTrue(updated_item.get("llmSummarized"))
        self.assertIn("[성공]", updated_item["text"])
        self.assertIn("택시 10% 할인 카드 2종 보유 확인 및 비교 완료", updated_item["text"])

    def test_rebalance_memory_with_llm_summarizes_promotes_and_demotes(self):
        """rebalance_memory_with_llm 호출 시 LLM 요약과 함께 적정 계층 승격/강등이 적용되는지 검증."""
        import asyncio
        import json
        from types import SimpleNamespace

        s_hot_done = master_memory.add_memory(
            "Q: 옛날 날씨 어땠어 → A: 어제는 맑았습니다. " + ("부가설명 " * 10),
            title="옛날 날씨",
            kind="turn",
            tier="hot",
            summarize=False,
        )
        id_hot_done = s_hot_done["addedItem"]["id"]

        s_warm_active = master_memory.add_memory(
            "[보고:성공|room-1794846261696-f7fa|mt-3b2623e2] 현재 진행 중인 미완결 버그 수정 작업 및 후속 검증 필요",
            title="미완결 버그 수정",
            kind="notify",
            tier="warm",
            summarize=False,
        )
        id_warm_active = s_warm_active["addedItem"]["id"]

        async def fake_run_agy_cli(prompt, timeout_sec=30, extra_args=None, cwd=None, env=None):
            self.assertIn("gemini-3.8-flash", extra_args)
            self.assertIn("low", extra_args)
            payload = [
                {
                    "id": id_hot_done,
                    "tier": "cold",
                    "summary": "어제 날씨 맑음 확인(일회성 조회)",
                },
                {
                    "id": id_warm_active,
                    "tier": "hot",
                    "summary": "미완결 버그 수정 진행 중 및 후속 검증 필요",
                },
            ]
            return SimpleNamespace(
                exit_status="NORMAL",
                return_code=0,
                stdout=json.dumps(payload, ensure_ascii=False),
                stderr="",
            )

        with patch.object(master_memory, "LLM_SUMMARY_ENABLED", True), \
             patch("agy_wrapper.run_agy_cli", side_effect=fake_run_agy_cli):
            snap = asyncio.run(master_memory.rebalance_memory_with_llm())

        op = snap["lastOperation"]
        self.assertEqual(op["summarized"], 2)
        self.assertEqual(op["promoted"], 1)
        self.assertEqual(op["demoted"], 1)

        hot_ids = {it["id"]: it for it in snap["tiers"]["hot"]["items"]}
        cold_ids = {it["id"]: it for it in snap["tiers"]["cold"]["items"]}
        self.assertIn(id_warm_active, hot_ids)
        self.assertIn(id_hot_done, cold_ids)
        self.assertIn("[성공]", hot_ids[id_warm_active]["text"])
        self.assertIn("어제 날씨 맑음 확인", cold_ids[id_hot_done]["text"])

    def test_apply_memory_batch_deletes_updates_and_moves(self):
        """apply_memory_batch로 delete, update(요약+이동), move를 일괄 원자적 적용하는지 검증."""
        s1 = master_memory.add_memory("무가치한 일회성 인사", title="인사", tier="hot")
        s2 = master_memory.add_memory("장황한 완료 보고 원문", title="보고", tier="warm")
        s3 = master_memory.add_memory("과거 종결 작업", title="종결", tier="hot")
        id1 = s1["addedItem"]["id"]
        id2 = s2["addedItem"]["id"]
        id3 = s3["addedItem"]["id"]

        snap = master_memory.apply_memory_batch([
            {"action": "delete", "id": id1},
            {"action": "update", "id": id2, "text": "[성공] 핵심 1문장 요약", "tier": "hot"},
            {"action": "move", "id": id3, "tier": "cold"},
        ])

        op = snap["lastOperation"]
        self.assertEqual(op["deleted"], 1)
        self.assertEqual(op["updated"], 1)
        self.assertEqual(op["moved"], 2)

        hot_ids = {it["id"]: it for it in snap["tiers"]["hot"]["items"]}
        cold_ids = {it["id"]: it for it in snap["tiers"]["cold"]["items"]}
        self.assertNotIn(id1, hot_ids)
        self.assertIn(id2, hot_ids)
        self.assertEqual(hot_ids[id2]["text"], "[성공] 핵심 1문장 요약")
        self.assertTrue(hot_ids[id2].get("llmSummarized"))
        self.assertIn(id3, cold_ids)

    def test_internal_dev_noise_filtered_on_ingest_and_rebalance(self):
        """내부 개발/버그픽스/테스트/스케줄 패스 잡음은 자동 적재 시 배제되고 rebalance 시 일괄 삭제되는지 검증."""
        # 1. 자동 적재 시 내부 개발 잡음은 스킵되어야 함
        skip1 = master_memory.add_memory(
            "[성공] index.html renderInline(str) 토큰 보호 단계 추가로 span 태그 렌더링 실패 버그 수정 완료.",
            title="웹 UI span 태그 렌더링 실패 버그 수정",
            kind="notify",
            summarize=True,
        )
        self.assertIsNone(skip1["addedItem"])
        self.assertEqual(skip1["lastOperation"]["action"], "skip")

        skip2 = master_memory.record_orchestrator_turn_memory(
            user_text="[정기 스케줄 실행 요청]\n- 설명: 자동 토큰 버닝",
            reply_text="주간 리셋까지 161시간 남아 발동 기준(6시간 미만) 미달로 자동 토큰 버닝을 패스하고 대기 상태를 유지함.",
            channel="schedule_trigger",
        )
        self.assertIsNone(skip2)

        # 2. 가치 있는 기억(사용자 규칙, 볼트 지식/가계부/차량)은 정상 저장되어야 함
        keep1 = master_memory.add_memory(
            "내부 개발 변경사항 메모리는 영구 삭제하고, 도구 남발을 지양하며 내장 단일 명령어로 간결히 완결하도록 작업 방식을 개선함.",
            title="[규칙] 메모리 관리 원칙",
            kind="turn",
            summarize=True,
        )
        self.assertIsNotNone(keep1["addedItem"])

        keep2 = master_memory.add_memory(
            "[성공] 하나손해보험 자동차보험 상세 계약 정보 및 차량 고정지출 볼트 동기화 완료.",
            title="자동차보험 관리",
            kind="notify",
            summarize=True,
        )
        self.assertIsNotNone(keep2["addedItem"])

        # 3. 이미 디스크에 수동/과거 경로로 들어와 있던 내부 개발 잡음은 rebalance_memory()에서 일괄 삭제되어야 함
        master_memory.add_memory(
            "[성공] 서버 동기화 시 도구 호출 유실 버그 수정 (test_preserve_active_steps 통과).",
            title="버그 수정",
            kind="note",
            tier="warm",
            summarize=False,
        )
        master_memory.add_memory(
            "4계층 메모리 리밸런싱으로 중복·실패 턴 58건을 삭제하고 32건을 재배치 완료함.",
            title="리밸런싱 완료",
            kind="note",
            tier="cold",
            summarize=False,
        )

        reb = master_memory.rebalance_memory()
        self.assertGreaterEqual(reb["lastOperation"]["purged"], 2)
        all_texts = [
            it["text"]
            for t in master_memory.TIERS
            for it in reb["tiers"][t]["items"]
        ]
        self.assertTrue(any("자동차보험" in txt for txt in all_texts))
        self.assertTrue(any("작업 방식" in txt for txt in all_texts))
        self.assertFalse(any("test_preserve_active_steps" in txt for txt in all_texts))
        self.assertFalse(any("58건을 삭제하고" in txt for txt in all_texts))

    def test_rebalance_memory_with_llm_deletes_noise_and_keeps_valuable(self):
        """rebalance_memory_with_llm 호출 시 LLM이 delete로 판정한 잡음을 제거하고 purged 건수를 반환하는지 검증."""
        import asyncio
        from types import SimpleNamespace

        s_noise = master_memory.add_memory(
            "세션 제목 자동 갱신 로직 검증",
            title="제목 로직",
            kind="note",
            tier="hot",
            summarize=False,
        )
        id_noise = s_noise["addedItem"]["id"]

        s_keep = master_memory.add_memory(
            "11월 네이버페이 가계부 지출 합계 1,420,000원 확정",
            title="11월 가계부",
            kind="note",
            tier="warm",
            summarize=False,
        )
        id_keep = s_keep["addedItem"]["id"]

        async def fake_run_agy_cli(prompt, timeout_sec=30, extra_args=None, cwd=None, env=None):
            payload = [
                {"id": id_noise, "tier": "delete", "summary": ""},
                {"id": id_keep, "tier": "hot", "summary": "11월 네이버페이 지출 1,420,000원 확정"},
            ]
            return SimpleNamespace(
                exit_status="NORMAL",
                return_code=0,
                stdout=json.dumps(payload, ensure_ascii=False),
                stderr="",
            )

        with patch.object(master_memory, "LLM_SUMMARY_ENABLED", True), \
             patch("agy_wrapper.run_agy_cli", side_effect=fake_run_agy_cli):
            snap = asyncio.run(master_memory.rebalance_memory_with_llm())

        op = snap["lastOperation"]
        self.assertEqual(op["purged"], 1)
        self.assertEqual(op["promoted"], 1)
        all_ids = {it["id"] for t in master_memory.TIERS for it in snap["tiers"][t]["items"]}
        self.assertNotIn(id_noise, all_ids)
        self.assertIn(id_keep, all_ids)


if __name__ == "__main__":
    unittest.main()
