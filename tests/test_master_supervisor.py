"""test_master_supervisor.py — 마스터 에이전트 작업 원장(master_tasks) 및 감독 루프(master_supervisor) 테스트."""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import master_memory
import master_supervisor
import master_tasks


class TestMasterTasksLedger(unittest.TestCase):
    """작업 원장 영속화, 상태 전이, 검토 완료 마킹 검증."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.tasks_file = os.path.join(self.tmp_dir, "master_tasks.json")
        self.memory_file = os.path.join(self.tmp_dir, "master_memory.json")
        self.patcher = patch("master_tasks.MASTER_TASKS_FILE", self.tasks_file)
        self.memory_patcher = patch("master_memory.MASTER_MEMORY_FILE", self.memory_file)
        self.patcher.start()
        self.memory_patcher.start()

    def tearDown(self):
        self.memory_patcher.stop()
        self.patcher.stop()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_create_and_get_task(self):
        task = master_tasks.create_task(
            room_id="room-alpha-1",
            instruction="포트폴리오 요약해줘",
            session_id="sess-alpha",
            title="주식 분석",
            origin="dispatch",
            wait=True,
        )
        self.assertTrue(task["id"].startswith("mt-"))
        self.assertEqual(task["roomId"], "room-alpha-1")
        self.assertEqual(task["status"], "queued")
        self.assertFalse(task["reviewed"])

        fetched = master_tasks.get_task(task["id"])
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched["instruction"], "포트폴리오 요약해줘")

    def test_update_task_and_reviewed_reset(self):
        task = master_tasks.create_task(
            room_id="room-beta",
            instruction="문서 수정",
        )
        tid = task["id"]

        # 1. 실행 중으로 변경
        upd = master_tasks.update_task(tid, status="running", feed="파일 읽는 중…")
        self.assertEqual(upd["status"], "running")
        self.assertEqual(upd["feed"], "파일 읽는 중…")

        # 2. 완료 처리 시 needs_attention 은 True
        upd_done = master_tasks.update_task(tid, status="done", resultPreview="완료되었습니다.")
        self.assertTrue(master_tasks.needs_attention(upd_done))

        # 3. 검토 완료 마킹
        marked = master_tasks.mark_reviewed([tid])
        self.assertEqual(marked, 1)
        reviewed_task = master_tasks.get_task(tid)
        self.assertTrue(reviewed_task["reviewed"])
        self.assertFalse(master_tasks.needs_attention(reviewed_task))

        # 4. 상태가 다시 실패로 변경되면 reviewed 플래그가 자동 리셋되어 다시 검토 대상이 됨
        reopened = master_tasks.update_task(tid, status="failed", error="에러 발생")
        self.assertFalse(reopened["reviewed"])
        self.assertTrue(master_tasks.needs_attention(reopened))

    def test_mark_reviewed_with_expected_status_guard(self):
        task = master_tasks.create_task(room_id="room-gamma", instruction="분석")
        tid = task["id"]
        master_tasks.update_task(tid, status="done")

        # 기대 상태가 running 인데 현재 상태가 done 이면 마킹되지 않음
        marked = master_tasks.mark_reviewed([tid], expected_status={tid: "running"})
        self.assertEqual(marked, 0)
        self.assertFalse(master_tasks.get_task(tid)["reviewed"])

        # 기대 상태가 일치하면 정상 마킹
        marked2 = master_tasks.mark_reviewed([tid], expected_status={tid: "done"})
        self.assertEqual(marked2, 1)
        self.assertTrue(master_tasks.get_task(tid)["reviewed"])

    def test_patrol_state_persistence(self):
        st = master_tasks.get_patrol_state()
        self.assertEqual(st, {})

        master_tasks.update_patrol_state(status="running", lastReason="정기 점검", runs=1)
        updated = master_tasks.get_patrol_state()
        self.assertEqual(updated["status"], "running")
        self.assertEqual(updated["runs"], 1)

    def test_room_summaries_lifecycle_backfill_and_cap(self):
        """create_task/update_task 자동 요약 생성·누적, 500자 하드 캡, 레거시 backfill 및 삭제 정리를 검증."""
        # 1. __core__ 작업은 하위 채팅 요약본에 등록되지 않음
        master_tasks.create_task(room_id="__core__", instruction="정기 점검")
        self.assertIsNone(master_tasks.get_room_summary("__core__"))

        # 2. 하위 방 작업 생성 및 완료/메모 누적 시 room_summaries 자동 생성·갱신
        t1 = master_tasks.create_task(
            room_id="room-ins-sum",
            instruction="바른세상병원 족부 진료 실손보험 청구 계산해줘",
            title="실손보험 청구",
        )
        rsum = master_tasks.get_room_summary("room-ins-sum")
        self.assertIsNotNone(rsum)
        self.assertEqual(rsum["taskCount"], 1)
        self.assertEqual(rsum["queries"], ["바른세상병원 족부 진료 실손보험 청구 계산해줘"])
        self.assertIn("실손보험 청구", rsum["summary"])

        master_tasks.update_task(
            t1["id"],
            status="done",
            sessionId="sess-ins-99",
            resultPreview="농협 실손보험 예상환급액은 자기부담금 제외 42,500원입니다.",
            note="환급액 산출 확인 완료",
        )
        rsum2 = master_tasks.get_room_summary("room-ins-sum")
        self.assertEqual(rsum2["sessionId"], "sess-ins-99")
        self.assertEqual(rsum2["lastStatus"], "done")
        self.assertIn("42,500원", rsum2["lastResult"])
        self.assertEqual(rsum2["lastNote"], "환급액 산출 확인 완료")
        self.assertIn("[결과]", rsum2["summary"])

        # 3. 500자 하드 캡(Token Firewall) 검증
        long_text = "매우긴질문내용_" * 80
        capped = master_tasks.upsert_room_summary("room-ins-sum", summary=long_text)
        self.assertLessEqual(len(capped["summary"]), 500)
        self.assertEqual(capped["customSummary"], capped["summary"])

        # 4. customSummary 를 비우면 자동 합성 요약으로 복원되고 역시 500자 이내 유지
        t2 = master_tasks.create_task(
            room_id="room-ins-sum",
            instruction="추가 질문_" * 60,
            title="실손보험 청구",
        )
        master_tasks.update_task(t2["id"], status="done", resultPreview="추가결과_" * 60)
        restored = master_tasks.upsert_room_summary("room-ins-sum", summary="")
        self.assertEqual(restored["customSummary"], "")
        self.assertIn("[주제]", restored["summary"])
        self.assertLessEqual(len(restored["summary"]), 500)
        self.assertEqual(restored["taskCount"], 2)

        # 5. 레거시 master_tasks.json (room_summaries 키 없음) 에서 자동 backfill 검증
        import json as _json
        with open(self.tasks_file, "w", encoding="utf-8") as f:
            _json.dump({
                "tasks": {
                    "mt-legacy-1": {
                        "id": "mt-legacy-1",
                        "roomId": "room-legacy",
                        "sessionId": "sess-legacy",
                        "title": "레거시 작업방",
                        "instruction": "지난해 연말정산 의료비 내역 정리",
                        "status": "done",
                        "resultPreview": "연말정산 의료비 공제 120만원 확인",
                        "createdAt": 100.0,
                        "updatedAt": 200.0,
                        "notes": [{"at": 210.0, "text": "확인 완료"}],
                    }
                },
                "patrol": {},
            }, f, ensure_ascii=False)
        backfilled = master_tasks.get_room_summary("room-legacy")
        self.assertIsNotNone(backfilled)
        self.assertEqual(backfilled["sessionId"], "sess-legacy")
        self.assertIn("지난해 연말정산 의료비 내역 정리", backfilled["queries"])
        self.assertIn("120만원", backfilled["lastResult"])
        self.assertEqual(backfilled["lastNote"], "확인 완료")

        # 6. handle_rooms_deleted 호출 시 room_summaries 에서도 함께 제거됨
        master_tasks.handle_rooms_deleted(["room-legacy"])
        self.assertIsNone(master_tasks.get_room_summary("room-legacy"))


class TestMasterSupervisorLogic(unittest.TestCase):
    """reconcile_tasks, build_graph, build_patrol_prompt 순수 로직 검증."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.tasks_file = os.path.join(self.tmp_dir, "master_tasks.json")
        self.memory_file = os.path.join(self.tmp_dir, "master_memory.json")
        self.patcher = patch("master_tasks.MASTER_TASKS_FILE", self.tasks_file)
        self.memory_patcher = patch("master_memory.MASTER_MEMORY_FILE", self.memory_file)
        self.patcher.start()
        self.memory_patcher.start()

    def tearDown(self):
        self.memory_patcher.stop()
        self.patcher.stop()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_reconcile_tasks_running_and_stalled(self):
        now = 1000.0
        tasks = [
            {
                "id": "mt-1",
                "roomId": "room-1",
                "sessionId": "s-1",
                "status": "queued",
                "feed": "",
                "stepCount": 0,
                "lastActivityAt": 950.0,
                "updatedAt": 950.0,
            },
            {
                "id": "mt-2",
                "roomId": "room-2",
                "sessionId": "s-2",
                "status": "running",
                "feed": "실행 중",
                "stepCount": 2,
                "lastActivityAt": 800.0,  # 200초 동안 진행 없음
                "updatedAt": 800.0,
            },
        ]
        live_runs = [
            {
                "roomId": "room-1",
                "feed": "자료 검색 중…",
                "steps": [{"tool": "read_file"}],
            },
            {
                "roomId": "room-2",
                "feed": "실행 중",
                "steps": [{}, {}],  # 스텝 수 변화 없음
            },
        ]

        # stall_seconds = 100
        changes = master_supervisor.reconcile_tasks(
            tasks,
            live_runs,
            now=now,
            stall_seconds=100,
        )
        change_dict = dict(changes)

        # mt-1 은 피드/스텝 변화로 lastActivityAt 갱신됨
        self.assertIn("mt-1", change_dict)
        self.assertEqual(change_dict["mt-1"]["feed"], "자료 검색 중…")
        self.assertEqual(change_dict["mt-1"]["stepCount"], 1)

        # mt-2 는 100초 이상 정체되어 stalled 로 변경됨
        self.assertIn("mt-2", change_dict)
        self.assertEqual(change_dict["mt-2"]["status"], "stalled")

    def test_reconcile_tasks_disappeared_done_or_lost(self):
        now = 1000.0
        tasks = [
            {
                "id": "mt-done",
                "roomId": "room-done",
                "status": "running",
                "updatedAt": 900.0,  # 유예 30초 초과
                "createdAt": 850.0,
            },
            {
                "id": "mt-lost",
                "roomId": "room-lost",
                "status": "running",
                "updatedAt": 900.0,
                "createdAt": 850.0,
            },
        ]

        def fake_latest_reply(room_id, since_ts):
            if room_id == "room-done":
                return "답변이 완료되었습니다."
            return None

        changes = master_supervisor.reconcile_tasks(
            tasks,
            live_runs=[],
            now=now,
            latest_reply=fake_latest_reply,
        )
        cd = dict(changes)
        self.assertEqual(cd["mt-done"]["status"], "done")
        self.assertIn("완료되었습니다", cd["mt-done"]["resultPreview"])
        self.assertEqual(cd["mt-lost"]["status"], "lost")
        self.assertIn("추적이 끊겼고", cd["mt-lost"]["error"])

    def test_build_graph_structure(self):
        rooms = [
            {"id": "room-1", "title": "작업방 1", "updatedAt": 990.0},
            {"id": "room-2", "title": "작업방 2", "updatedAt": 980.0},
        ]
        live_runs = [
            {"roomId": "room-1", "isRunning": True, "feed": "분석 중…", "steps": [1]},
            {"roomId": master_supervisor.CORE_ROOM_ID, "feed": "대기 중", "channel": "master_patrol"},
        ]
        tasks = [
            {
                "id": "mt-1",
                "roomId": "room-1",
                "title": "작업방 1",
                "instruction": "코드 분석",
                "status": "running",
                "reviewed": False,
                "createdAt": 950.0,
                "updatedAt": 990.0,
            }
        ]
        patrol = {"enabled": True, "lastRunAt": 900.0, "status": "idle"}

        graph = master_supervisor.build_graph(
            rooms=rooms,
            live_runs=live_runs,
            tasks=tasks,
            patrol=patrol,
            now=1000.0,
        )

        self.assertEqual(graph["master"]["id"], master_supervisor.CORE_ROOM_ID)
        self.assertEqual(graph["master"]["status"], "patrolling")
        self.assertTrue(len(graph["nodes"]) >= 1)

        # room-1 노드 확인
        node1 = next(n for n in graph["nodes"] if n["id"] == "room-1")
        self.assertEqual(node1["status"], "running")
        self.assertEqual(node1["feed"], "분석 중…")
        self.assertEqual(len(node1["tasks"]), 1)

        # 연결선(edges) 확인
        edge1 = next(e for e in graph["edges"] if e["to"] == "room-1")
        self.assertEqual(edge1["from"], master_supervisor.CORE_ROOM_ID)
        self.assertEqual(edge1["type"], "dispatch")
        self.assertTrue(edge1["active"])

    def test_build_graph_excludes_deleted_rooms(self):
        """삭제된 대화방(rooms 에 없고 실행 중도 아님)은 관계도 노드에서 제외된다."""
        rooms = [
            {"id": "room-alive", "title": "살아있는 방", "updatedAt": 990.0},
        ]
        live_runs = []
        tasks = [
            {
                "id": "mt-deleted",
                "roomId": "sub-room-deleted",
                "title": "삭제된 방",
                "instruction": "과거 지시",
                "status": "closed",
                "reviewed": True,
                "createdAt": 800.0,
                "updatedAt": 950.0,
            },
            {
                "id": "mt-alive",
                "roomId": "room-alive",
                "title": "살아있는 방",
                "instruction": "활성 지시",
                "status": "done",
                "reviewed": False,
                "createdAt": 900.0,
                "updatedAt": 990.0,
            },
        ]
        patrol = {"enabled": True, "lastRunAt": 900.0, "status": "idle"}

        graph = master_supervisor.build_graph(
            rooms=rooms,
            live_runs=live_runs,
            tasks=tasks,
            patrol=patrol,
            now=1000.0,
        )

        node_ids = [n["id"] for n in graph["nodes"]]
        self.assertIn("room-alive", node_ids)
        self.assertNotIn("sub-room-deleted", node_ids)

    def test_handle_rooms_deleted_closes_tasks(self):
        """방 삭제 시 master_tasks.handle_rooms_deleted 가 해당 방의 활성 작업을 종결 처리한다."""
        task = master_tasks.create_task(
            room_id="room-to-delete",
            instruction="삭제될 방의 작업",
        )
        tid = task["id"]
        self.assertEqual(task["status"], "queued")

        updated = master_tasks.handle_rooms_deleted(["room-to-delete"])
        self.assertEqual(updated, 1)

        closed_task = master_tasks.get_task(tid)
        self.assertEqual(closed_task["status"], "closed")
        self.assertTrue(closed_task["reviewed"])
        self.assertIn("대화방 삭제", closed_task["notes"][-1]["text"])
        self.assertIsNone(master_tasks.get_room_summary("room-to-delete"))

    def test_build_graph_includes_room_summary_and_queries(self):
        """build_graph 가 하위 채팅 노드에 관리 중인 요약본(summary)과 질문 이력(queries)을 포함하는지 검증."""
        task = master_tasks.create_task(
            room_id="room-sum-graph",
            instruction="크레스토정 복약 주기 및 콜레스테롤 수치 비교해줘",
            title="고지혈증 약 복약 정리",
        )
        master_tasks.update_task(
            task["id"],
            status="done",
            resultPreview="LDL 수치가 160에서 105로 개선되었습니다.",
        )
        graph = master_supervisor.build_graph(
            rooms=[{"id": "room-sum-graph", "title": "고지혈증 약 복약 정리", "updatedAt": 1000.0}],
            live_runs=[],
            tasks=[master_tasks.get_task(task["id"])],
            patrol={},
            now=1005.0,
        )
        node = next(n for n in graph["nodes"] if n["id"] == "room-sum-graph")
        self.assertIn("크레스토정", node["summary"])
        self.assertIn("LDL 수치가 160에서 105로", node["summary"])
        self.assertEqual(len(node["queries"]), 1)

    def test_build_patrol_prompt(self):
        targets = [
            {
                "id": "mt-10",
                "title": "테스트 방",
                "roomId": "room-10",
                "status": "done",
                "instruction": "기능 점검",
                "resultPreview": "점검 완료",
            }
        ]
        prompt = master_supervisor.build_patrol_prompt(targets, live_runs=[], now=1000.0, reason="정기 점검")
        self.assertIn("정기 점검 · 자동", prompt)
        self.assertIn("검토가 필요한 위임 작업 1건", prompt)
        self.assertIn("mt-10", prompt)
        self.assertIn("session-ctl task", prompt)


class TestMasterSupervisorAsync(unittest.IsolatedAsyncioTestCase):
    """MasterSupervisor 인스턴스의 틱, 스케줄링 및 사용자 선점(Preemption) 비동기 테스트."""

    async def asyncSetUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.tasks_file = os.path.join(self.tmp_dir, "master_tasks.json")
        self.memory_file = os.path.join(self.tmp_dir, "master_memory.json")
        self.patcher = patch("master_tasks.MASTER_TASKS_FILE", self.tasks_file)
        self.memory_patcher = patch("master_memory.MASTER_MEMORY_FILE", self.memory_file)
        self.patcher.start()
        self.memory_patcher.start()

    async def asyncTearDown(self):
        self.memory_patcher.stop()
        self.patcher.stop()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    async def test_supervisor_patrol_scheduling_and_completion(self):
        host = MagicMock()
        host.snapshot.return_value = {
            "rooms": [{"id": "room-sub", "title": "하위방"}],
            "liveRuns": [],
        }
        host.core_busy.return_value = False
        host.latest_reply.return_value = None
        host.broadcast = AsyncMock()

        # 점검 턴 결과를 시뮬레이션할 Future
        patrol_fut = asyncio.get_running_loop().create_future()
        host.enqueue_patrol = AsyncMock(return_value=patrol_fut)

        # 검토가 필요한 done 작업 1개 생성
        task = master_tasks.create_task(room_id="room-sub", instruction="문서 정리")
        master_tasks.update_task(task["id"], status="done", resultPreview="정리 완료")

        sup = master_supervisor.MasterSupervisor(
            host,
            tick_seconds=1,
            interval_seconds=10,
            stall_seconds=60,
        )

        # 틱 실행 -> 점검 실행 트리거
        await sup.tick()
        self.assertTrue(sup.patrol_running)
        host.enqueue_patrol.assert_awaited_once()

        # 점검 결과 회신
        patrol_fut.set_result({"ok": True, "reply": "모든 하위 작업이 정상 종료되었습니다."})
        await asyncio.sleep(0.01)

        # 작업이 검토 완료로 마킹되었는지 확인
        updated_t = master_tasks.get_task(task["id"])
        self.assertTrue(updated_t["reviewed"])
        self.assertFalse(sup.patrol_running)

    async def test_supervisor_preemption_on_cancel(self):
        host = MagicMock()
        host.snapshot.return_value = {"rooms": [], "liveRuns": []}
        host.core_busy.return_value = False
        host.latest_reply.return_value = None
        host.broadcast = AsyncMock()

        patrol_fut = asyncio.get_running_loop().create_future()
        host.enqueue_patrol = AsyncMock(return_value=patrol_fut)

        task = master_tasks.create_task(room_id="room-sub", instruction="점검 대상")
        master_tasks.update_task(task["id"], status="failed", error="연결 끊김")

        sup = master_supervisor.MasterSupervisor(
            host,
            tick_seconds=1,
            interval_seconds=10,
            stall_seconds=60,
        )

        await sup.tick()
        self.assertTrue(sup.patrol_running)

        # 사용자 요청이 와서 점검 Future가 취소됨 (양보)
        patrol_fut.cancel()
        await asyncio.sleep(0.01)

        self.assertFalse(sup.patrol_running)
        st = master_tasks.get_patrol_state()
        self.assertEqual(st.get("status"), "preempted")
        self.assertEqual(st.get("preempted"), 1)
        # 실패 작업은 아직 검토되지 않은 상태로 유지되어야 함
        self.assertFalse(master_tasks.get_task(task["id"])["reviewed"])

    async def test_supervisor_periodic_patrol_disabled_by_default(self):
        """기본값(interval_seconds=0)에서 검토 대상이 있어도 주기적 자동 점검 턴이 생성되지 않는다."""
        host = MagicMock()
        host.snapshot.return_value = {
            "rooms": [{"id": "room-sub", "title": "하위방"}],
            "liveRuns": [],
        }
        host.core_busy.return_value = False
        host.latest_reply.return_value = None
        host.broadcast = AsyncMock()
        host.enqueue_patrol = AsyncMock()

        task = master_tasks.create_task(room_id="room-sub", instruction="점검 대상")
        master_tasks.update_task(task["id"], status="done", resultPreview="완료")

        # 기본 interval_seconds (기본값 0)로 인스턴스화
        sup = master_supervisor.MasterSupervisor(host)
        self.assertEqual(sup.interval_seconds, 0)

        # 틱 실행 -> 자동 점검이 트리거되지 않아야 함
        await sup.tick()
        self.assertFalse(sup.patrol_running)
        host.enqueue_patrol.assert_not_called()

        # 수동 점검 요청 시에는 정상적으로 동작해야 함
        patrol_fut = asyncio.get_running_loop().create_future()
        host.enqueue_patrol.return_value = patrol_fut
        res = await sup.start_patrol(reason="수동 점검", force=True)
        self.assertTrue(res["ok"])
        self.assertTrue(res["llmTriggered"])
        self.assertTrue(sup.patrol_running)
        host.enqueue_patrol.assert_awaited_once()

    async def test_start_patrol_clean_skips_llm_when_all_healthy(self):
        """모든 작업이 정상 진행 중이거나 보고 완료된 done 상태이면 LLM 점검 턴을 띄우지 않는다."""
        host = MagicMock()
        host.snapshot.return_value = {
            "rooms": [{"id": "room-run", "title": "실행중"}, {"id": "room-done", "title": "완료됨"}],
            "liveRuns": [{"roomId": "room-run", "isRunning": True, "feed": "분석 중", "steps": [1]}],
        }
        host.core_busy.return_value = False
        host.latest_reply.return_value = None
        host.broadcast = AsyncMock()
        host.enqueue_patrol = AsyncMock()

        t_run = master_tasks.create_task(room_id="room-run", instruction="실행 작업")
        master_tasks.update_task(t_run["id"], status="running", feed="분석 중", stepCount=1)

        t_done_reported = master_tasks.create_task(room_id="room-done", instruction="보고 완료 작업")
        master_tasks.update_task(t_done_reported["id"], status="done", resultPreview="끝")
        self.assertTrue(master_tasks.claim_report(t_done_reported["id"]))
        self.assertFalse(master_tasks.needs_attention(master_tasks.get_task(t_done_reported["id"])))

        t_done_wait = master_tasks.create_task(room_id="room-wait", instruction="동기 완료 작업", wait=True)
        master_tasks.update_task(t_done_wait["id"], status="done", resultPreview="동기 끝")
        self.assertFalse(master_tasks.needs_attention(master_tasks.get_task(t_done_wait["id"])))

        sup = master_supervisor.MasterSupervisor(host, interval_seconds=0, stall_seconds=60)
        res = await sup.start_patrol(reason="사용자 수동 점검", force=True)

        self.assertTrue(res["ok"])
        self.assertEqual(res["status"], "clean")
        self.assertFalse(res["llmTriggered"])
        self.assertEqual(res["targets"], 0)
        self.assertEqual(res["activeCount"], 1)
        host.enqueue_patrol.assert_not_called()
        host.broadcast.assert_awaited()

    async def test_start_patrol_triggers_llm_only_when_abnormal_or_stalled(self):
        """백엔드 대조 시 정체(stalled)·실패·유실 또는 미보고 작업이 발견되면 그때만 LLM 복구 점검을 트리거한다."""
        now = time.time()
        host = MagicMock()
        host.snapshot.return_value = {
            "rooms": [{"id": "room-stall", "title": "정체방"}],
            "liveRuns": [{"roomId": "room-stall", "isRunning": True, "feed": "대기", "steps": []}],
        }
        host.core_busy.return_value = False
        host.latest_reply.return_value = None
        host.broadcast = AsyncMock()
        patrol_fut = asyncio.get_running_loop().create_future()
        host.enqueue_patrol = AsyncMock(return_value=patrol_fut)

        t_stall = master_tasks.create_task(room_id="room-stall", instruction="오래 멈춘 작업")
        master_tasks.update_task(t_stall["id"], status="running", feed="대기", stepCount=0, lastActivityAt=now - 300)

        sup = master_supervisor.MasterSupervisor(host, interval_seconds=0, stall_seconds=60)
        res = await sup.start_patrol(reason="사용자 수동 점검", force=True)

        self.assertTrue(res["ok"])
        self.assertEqual(res["status"], "started")
        self.assertTrue(res["llmTriggered"])
        self.assertEqual(res["targets"], 1)
        self.assertEqual(master_tasks.get_task(t_stall["id"])["status"], "stalled")
        host.enqueue_patrol.assert_awaited_once()
        prompt_arg = host.enqueue_patrol.call_args.args[0]
        self.assertIn(t_stall["id"], prompt_arg)
        self.assertNotIn("지금 실행/대기 중인 하위 채팅", prompt_arg)


class TestMasterNotifyEndpoint(unittest.IsolatedAsyncioTestCase):
    """POST /api/orchestrator/notify 엔드포인트 및 마스터 턴 트리거 검증."""

    async def asyncSetUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.memory_patcher = patch(
            "master_memory.MASTER_MEMORY_FILE",
            os.path.join(self.tmp_dir, "master_memory.json"),
        )
        self.memory_patcher.start()

    async def asyncTearDown(self):
        self.memory_patcher.stop()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    async def test_handle_orchestrator_notify_enqueues_core_turn(self):
        import web_orchestrator
        from aiohttp.test_utils import make_mocked_request

        fake_enqueue = AsyncMock(return_value=(False, 0))
        fake_broadcast = AsyncMock()

        payload = {
            "message": "[하위 작업 완료 보고] room-test 분석 완료",
            "taskId": "mt-test-123",
            "roomId": "room-test",
        }

        req = make_mocked_request("POST", "/api/orchestrator/notify")
        req.json = AsyncMock(return_value=payload)

        with patch("web_orchestrator._get_mod_attr") as mock_attr:
            def side_effect(name, fallback):
                if name == "enqueue_room_turn":
                    return fake_enqueue
                if name == "broadcast_ws_json":
                    return fake_broadcast
                return fallback
            mock_attr.side_effect = side_effect

            resp = await web_orchestrator.handle_orchestrator_notify(req)
            self.assertEqual(resp.status, 200)

            # 마스터 에이전트(__core__)로 턴이 적재되었는지 확인
            fake_enqueue.assert_awaited_once()
            call_kwargs = fake_enqueue.call_args[1]
            self.assertEqual(call_kwargs["room_id"], "__core__")
            self.assertEqual(call_kwargs["channel"], "task_report")
            self.assertEqual(call_kwargs["task_id"], "mt-test-123")
            self.assertIn("room-test 분석 완료", call_kwargs["user_text"])

            # 브로드캐스트 전송 확인
            fake_broadcast.assert_awaited_once()


class TestAsyncReportSafetyNet(unittest.IsolatedAsyncioTestCase):
    """비동기 위임 작업 완료 보고 누락 방지(claim 중복 차단 + 감독 루프 안전망)."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.patcher = patch("master_tasks.MASTER_TASKS_FILE", os.path.join(self.tmp_dir, "master_tasks.json"))
        self.memory_patcher = patch("master_memory.MASTER_MEMORY_FILE", os.path.join(self.tmp_dir, "master_memory.json"))
        self.patcher.start()
        self.memory_patcher.start()

    def tearDown(self):
        self.memory_patcher.stop()
        self.patcher.stop()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_claim_report_is_once_and_releasable(self):
        t = master_tasks.create_task(room_id="r1", instruction="x")
        master_tasks.update_task(t["id"], status="done", resultPreview="ok")
        self.assertTrue(master_tasks.needs_attention(master_tasks.get_task(t["id"])))

        self.assertTrue(master_tasks.claim_report(t["id"]))
        claimed = master_tasks.get_task(t["id"])
        self.assertTrue(claimed["reviewed"])
        self.assertFalse(master_tasks.needs_attention(claimed))
        self.assertFalse(master_tasks.claim_report(t["id"]))

        master_tasks.release_report(t["id"])
        released = master_tasks.get_task(t["id"])
        self.assertFalse(released["reviewed"])
        self.assertTrue(master_tasks.needs_attention(released))
        self.assertTrue(master_tasks.claim_report(t["id"]))

    def test_unreported_async_tasks_filters(self):
        now = time.time()
        a = master_tasks.create_task(room_id="r1", instruction="a")
        b = master_tasks.create_task(room_id="r2", instruction="b", wait=True)
        c = master_tasks.create_task(room_id="r3", instruction="c")
        for t in (a, b, c):
            master_tasks.update_task(t["id"], status="done", finishedAt=now - 60)
        master_tasks.claim_report(c["id"])
        ids = [t["id"] for t in master_tasks.unreported_async_tasks(now=now, max_age=3600)]
        self.assertEqual(ids, [a["id"]])

    async def test_tick_reports_missed_task(self):
        now = time.time()
        a = master_tasks.create_task(room_id="r1", instruction="a")
        master_tasks.update_task(a["id"], status="done", finishedAt=now - 60)
        host = MagicMock()
        host.snapshot.return_value = {"rooms": [], "liveRuns": []}
        host.broadcast = AsyncMock()
        host.notify_task = AsyncMock()
        sup = master_supervisor.MasterSupervisor(host, interval_seconds=0)
        await sup.tick()
        host.notify_task.assert_awaited_once()


class TestMasterMemoryTiers(unittest.TestCase):
    """마스터 에이전트 4계층 메모리(hot/warm/cold/freeze) 연쇄 하위 이동 및 관리 검증."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.tasks_patcher = patch(
            "master_tasks.MASTER_TASKS_FILE",
            os.path.join(self.tmp_dir, "master_tasks.json"),
        )
        self.memory_patcher = patch(
            "master_memory.MASTER_MEMORY_FILE",
            os.path.join(self.tmp_dir, "master_memory.json"),
        )
        self.tasks_patcher.start()
        self.memory_patcher.start()

    def tearDown(self):
        self.memory_patcher.stop()
        self.tasks_patcher.stop()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_cascading_overflow_demotion_across_four_tiers(self):
        """각 계층 길이/개수 한도를 넘길 때마다 hot → warm → cold → freeze 순으로 연쇄 이동한다."""
        limits = {
            "hot": {"maxChars": 24, "maxItems": 2, "itemMaxChars": 24},
            "warm": {"maxChars": 24, "maxItems": 2, "itemMaxChars": 24},
            "cold": {"maxChars": 24, "maxItems": 2, "itemMaxChars": 24},
            "freeze": {"maxChars": 24, "maxItems": 2, "itemMaxChars": 24},
        }
        master_memory.update_tier_limits(limits)

        # 10글자짜리 9건 순차 추가 (각 계층 최대 2건·24자 → 8건이 전 계층을 채우고 9건째에 1건 방출)
        for idx in range(1, 10):
            snap = master_memory.add_memory(f"mem-item-{idx}", title=f"T{idx}")

        self.assertEqual([it["title"] for it in snap["tiers"]["hot"]["items"]], ["T9", "T8"])
        self.assertEqual([it["title"] for it in snap["tiers"]["warm"]["items"]], ["T7", "T6"])
        self.assertEqual([it["title"] for it in snap["tiers"]["cold"]["items"]], ["T5", "T4"])
        self.assertEqual([it["title"] for it in snap["tiers"]["freeze"]["items"]], ["T3", "T2"])
        self.assertEqual(snap["totalItems"], 8)
        self.assertEqual(snap["stats"]["droppedCount"], 1)
        self.assertGreaterEqual(snap["stats"]["cascadeCount"], 7)

        # 하위 이동된 항목에는 직전 계층 및 이동 시각 기록이 남아야 함
        warm_latest = snap["tiers"]["warm"]["items"][0]
        self.assertEqual(warm_latest["fromTier"], "hot")
        self.assertIsNotNone(warm_latest["movedAt"])
        self.assertGreaterEqual(warm_latest["demoteCount"], 1)

        # charCount 기반 연쇄 이동 검증: hot 에 20자 1건 후 15자 1건 추가 -> 35 > 24 이므로 첫 항목이 warm 이동
        master_memory.clear_memory("all")
        master_memory.add_memory("A" * 20, title="LONG1")
        snap2 = master_memory.add_memory("B" * 15, title="LONG2")
        self.assertEqual(snap2["tiers"]["hot"]["itemCount"], 1)
        self.assertEqual(snap2["tiers"]["hot"]["items"][0]["title"], "LONG2")
        self.assertEqual(snap2["tiers"]["warm"]["itemCount"], 1)
        self.assertEqual(snap2["tiers"]["warm"]["items"][0]["title"], "LONG1")

    def test_demote_clear_prompt_context_and_graph_snapshot(self):
        """수동 하위 이동(demote_tier), 비우기(clear_memory), 프롬프트 컨텍스트 및 build_graph 스냅샷 통합 검증."""
        master_memory.add_memory("최근 주식 포트폴리오 점검 완료", title="주식점검")
        master_memory.add_memory("방화벽 예외 승인 상태 확인", title="보안승인")

        ctx1 = master_memory.build_memory_prompt_context()
        self.assertIn("HOT (즉각 활성 컨텍스트)", ctx1)
        self.assertIn("주식점검", ctx1)

        demoted = master_memory.demote_tier("hot")
        self.assertEqual(demoted["tiers"]["hot"]["itemCount"], 0)
        self.assertEqual(demoted["tiers"]["warm"]["itemCount"], 2)

        ctx2 = master_memory.build_memory_prompt_context()
        self.assertIn("WARM (최근 하위 이동 요약)", ctx2)
        self.assertIn("방화벽 예외 승인 상태 확인", ctx2)

        graph = master_supervisor.build_graph(
            rooms=[],
            live_runs=[],
            tasks=[],
            patrol={},
            now=time.time(),
        )
        self.assertIn("memory", graph)
        self.assertEqual(graph["memory"]["tiers"]["warm"]["itemCount"], 2)

        cleared_warm = master_memory.clear_memory("warm")
        self.assertEqual(cleared_warm["tiers"]["warm"]["itemCount"], 0)

        master_memory.add_memory("냉각 테스트", tier="cold")
        master_memory.add_memory("동결 테스트", tier="freeze")
        cleared_all = master_memory.clear_memory("all")
        self.assertEqual(cleared_all["totalItems"], 0)
        self.assertEqual(master_memory.build_memory_prompt_context(), "")


if __name__ == "__main__":
    unittest.main()
