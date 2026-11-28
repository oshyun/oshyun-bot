import os
import shutil
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC_DIR = os.path.join(_REPO_ROOT, "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

import bot.core.chat_db as chat_db


class TestChatDb(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="chat_db_test_")
        self.db_path = os.path.join(self.test_dir, "test_chat.db")
        self.orig_chats_dir = chat_db.CHATS_DIR
        self.orig_rooms_file = chat_db.CHAT_ROOMS_FILE
        chat_db.CHATS_DIR = os.path.join(self.test_dir, "empty_chats")
        chat_db.CHAT_ROOMS_FILE = os.path.join(self.test_dir, "empty_rooms.json")
        chat_db.init_db(self.db_path, migrate_legacy=False)

    def tearDown(self):
        chat_db.CHATS_DIR = self.orig_chats_dir
        chat_db.CHAT_ROOMS_FILE = self.orig_rooms_file
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_init_and_empty_rooms(self):
        rooms = chat_db.get_rooms(db_path=self.db_path)
        self.assertEqual(rooms, [])

    def test_create_room_and_add_message(self):
        # 1. 방 자동 생성 및 메시지 추가 (User)
        msg_id1 = chat_db.add_message(
            room_id="room-1",
            role="user",
            text="오늘 오사카 날씨 어때?",
            channel="web",
            session_id="sess-osaka-1",
            db_path=self.db_path,
        )
        self.assertGreater(msg_id1, 0)

        # 방 메타데이터 검증
        room = chat_db.get_room("room-1", db_path=self.db_path)
        self.assertIsNotNone(room)
        self.assertEqual(room["title"], "오늘 오사카 날씨 어때?")
        self.assertEqual(room["sessionId"], "sess-osaka-1")
        self.assertEqual(room["lastChannel"], "web")
        self.assertEqual(room["messageCount"], 1)

        # 2. 어시스턴트 답변 추가
        msg_id2 = chat_db.add_message(
            room_id="room-1",
            role="assistant",
            text="오사카는 현재 맑고 기온은 18도입니다.",
            channel="web",
            steps=[{"type": "tool", "name": "weather"}],
            model="gemini-flash",
            cost=0.0002,
            session_id="sess-osaka-1",
            db_path=self.db_path,
        )
        self.assertGreater(msg_id2, msg_id1)

        # 갱신된 방 메타데이터 확인
        room = chat_db.get_room("sess-osaka-1", db_path=self.db_path)  # sessionId 로도 조회 가능
        self.assertIsNotNone(room)
        self.assertEqual(room["preview"], "오사카는 현재 맑고 기온은 18도입니다.")
        self.assertEqual(room["messageCount"], 2)

        # 메시지 목록 조회 검증
        msgs = chat_db.get_messages("room-1", db_path=self.db_path)
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["role"], "user")
        self.assertEqual(msgs[0]["channel"], "web")
        self.assertEqual(msgs[1]["role"], "assistant")
        self.assertEqual(len(msgs[1]["steps"]), 1)
        self.assertEqual(msgs[1]["steps"][0]["name"], "weather")

    def test_delete_room(self):
        chat_db.add_message("room-del", "user", "삭제 테스트", db_path=self.db_path)
        rooms_before = chat_db.get_rooms(db_path=self.db_path)
        self.assertEqual(len(rooms_before), 1)

        # 소프트 삭제
        ok = chat_db.delete_room("room-del", hard_delete=False, db_path=self.db_path)
        self.assertTrue(ok)
        rooms_after = chat_db.get_rooms(include_hidden=False, db_path=self.db_path)
        self.assertEqual(len(rooms_after), 0)

        # include_hidden=True 시 조회됨
        all_rooms = chat_db.get_rooms(include_hidden=True, db_path=self.db_path)
        self.assertEqual(len(all_rooms), 1)
        self.assertEqual(all_rooms[0]["isHidden"], 1)

        # 하드 삭제
        ok2 = chat_db.delete_room("room-del", hard_delete=True, db_path=self.db_path)
        self.assertTrue(ok2)
        all_rooms2 = chat_db.get_rooms(include_hidden=True, db_path=self.db_path)
        self.assertEqual(len(all_rooms2), 0)
        # 외래키로 메시지도 삭제되었는지 확인
        msgs = chat_db.get_messages("room-del", db_path=self.db_path)
        self.assertEqual(msgs, [])

    def test_parse_timestamp_and_iso_format(self):
        # ISO 문자열 타임스탬프로 메시지 저장 검증
        msg_id = chat_db.add_message(
            room_id="room-iso",
            role="user",
            text="ISO 날짜 테스트",
            created_at="2026-11-04T03:37:29Z",
            db_path=self.db_path,
        )
        self.assertGreater(msg_id, 0)
        msgs = chat_db.get_messages("room-iso", db_path=self.db_path)
        self.assertEqual(len(msgs), 1)
        self.assertIsInstance(msgs[0]["createdAt"], float)
        self.assertGreater(msgs[0]["createdAt"], 1700000000)

    def test_cleanup_duplicate_rooms(self):
        # 강제로 동일한 session_id 를 갖는 두 개의 방을 DB에 직접 삽입
        with chat_db.get_connection(self.db_path) as conn:
            conn.execute("INSERT INTO rooms (id, session_id, title, created_at, updated_at) VALUES ('room-client-temp', 'shared-sid-999', '333333333', 100, 100);")
            conn.execute("INSERT INTO rooms (id, session_id, title, created_at, updated_at) VALUES ('shared-sid-999', 'shared-sid-999', '333333333', 200, 200);")
            conn.commit()

        chat_db.add_message("room-client-temp", "user", "333333333", session_id="shared-sid-999", db_path=self.db_path)
        chat_db.add_message("shared-sid-999", "assistant", "답변입니다", session_id="shared-sid-999", db_path=self.db_path)

        # 중복 정리 함수 실행
        merged = chat_db.cleanup_duplicate_rooms(db_path=self.db_path)
        self.assertGreaterEqual(merged, 1)

        # 목록 조회 시 오직 1개의 방만 나와야 함
        rooms = chat_db.get_rooms(db_path=self.db_path)
        matching = [r for r in rooms if r["sessionId"] == "shared-sid-999"]
        self.assertEqual(len(matching), 1)
        # 메시지는 2개 모두 보존되어야 함
        msgs = chat_db.get_messages("shared-sid-999", db_path=self.db_path)
        self.assertEqual(len(msgs), 2)

    def test_cleanup_duplicate_rooms_preserves_empty_placeholder_messages(self):
        """빈 텍스트(text='') 플레이스홀더나 도구 실행 중인 서로 다른 턴의 어시스턴트 메시지가
        중복 제거 로직에 의해 오삭제되지 않고 온전히 보존되는지 검증한다."""
        room_id = "room-test-placeholders"
        # 1. 이전 턴: 도구 실행 기록은 있으나 결과 텍스트 없이 종료된 어시스턴트 메시지 (text="")
        msg1_id = chat_db.add_message(
            room_id,
            "assistant",
            "",
            steps=[{"kind": "tool_call", "tool": "read_file", "status": "done"}],
            db_path=self.db_path,
        )
        # 2. 다음 턴: 새로운 턴 시작 시 막 생성된 플레이스홀더 어시스턴트 메시지 (text="", steps=[])
        msg2_id = chat_db.add_message(
            room_id,
            "assistant",
            "",
            steps=[],
            db_path=self.db_path,
        )
        # 3. 공백만 있는 메시지 (text="   ")
        msg3_id = chat_db.add_message(
            room_id,
            "assistant",
            "   ",
            steps=[],
            db_path=self.db_path,
        )
        # 4. 실제 중복인 정상 텍스트 메시지 2건 (하나는 steps 보유)
        msg4_id = chat_db.add_message(
            room_id,
            "assistant",
            "완료된 답변입니다.",
            steps=[],
            db_path=self.db_path,
        )
        msg5_id = chat_db.add_message(
            room_id,
            "assistant",
            "완료된 답변입니다.",
            steps=[{"kind": "tool_call", "tool": "run_command", "status": "done"}],
            db_path=self.db_path,
        )

        # 중복 정리 실행
        chat_db.cleanup_duplicate_rooms(db_path=self.db_path)

        msgs = chat_db.get_messages(room_id, db_path=self.db_path)
        msg_ids = {m["id"] for m in msgs}

        # 빈 텍스트 메시지(msg1, msg2, msg3)는 턴 간 독립 레코드이므로 모두 생존해야 함
        self.assertIn(msg1_id, msg_ids, "1턴 빈 본문 어시스턴트 메시지가 삭제되지 않아야 함")
        self.assertIn(msg2_id, msg_ids, "2턴 플레이스홀더 어시스턴트 메시지가 삭제되지 않아야 함")
        self.assertIn(msg3_id, msg_ids, "공백 어시스턴트 메시지가 삭제되지 않아야 함")

        # 내용이 있는 중복 메시지는 steps_json 이 있는 msg5 가 남고 msg4 는 정리되어야 함
        self.assertIn(msg5_id, msg_ids, "steps 보유 중복 메시지는 보존되어야 함")
        self.assertNotIn(msg4_id, msg_ids, "steps 미보유 단순 복제 텍스트 메시지는 중복 제거되어야 함")


    def test_get_rooms_ordered_by_recency(self):
        # 방 3개를 다른 시간(초 및 밀리초 단위)으로 생성
        chat_db.add_message("room-old", "user", "오래된 질문", created_at=1700000000.0, db_path=self.db_path)
        chat_db.add_message("room-mid", "user", "중간 질문", created_at=1710000000.0, db_path=self.db_path)
        # 밀리초 타임스탬프 (1720000000000 ms -> 1720000000 초로 자동 정규화되어야 함)
        chat_db.add_message("room-new", "user", "최신 질문", created_at=1720000000000.0, db_path=self.db_path)

        rooms = chat_db.get_rooms(db_path=self.db_path)
        self.assertEqual(len(rooms), 3)
        self.assertEqual(rooms[0]["id"], "room-new")
        self.assertEqual(rooms[1]["id"], "room-mid")
        self.assertEqual(rooms[2]["id"], "room-old")

        # 오래된 방에 새 메시지 추가 시 최상단으로 재정렬
        chat_db.add_message("room-old", "user", "새 메시지로 방 갱신", created_at=1730000000.0, db_path=self.db_path)
        rooms_updated = chat_db.get_rooms(db_path=self.db_path)
        self.assertEqual(rooms_updated[0]["id"], "room-old")
        self.assertEqual(rooms_updated[1]["id"], "room-new")
        self.assertEqual(rooms_updated[2]["id"], "room-mid")

    def test_update_message(self):
        # 플레이스홀더 메시지 생성
        msg_id = chat_db.add_message(
            "room-update",
            "assistant",
            "",
            steps=[],
            model="test-model",
            db_path=self.db_path,
        )
        self.assertGreater(msg_id, 0)

        # 1. 도구 실행 단계 증분 업데이트
        steps_step1 = [{"kind": "tool_call", "tool": "read_file", "title": "파일 읽기"}]
        ok1 = chat_db.update_message(
            msg_id,
            steps=steps_step1,
            update_preview=False,
            db_path=self.db_path,
        )
        self.assertTrue(ok1)
        msg1 = chat_db.get_message(msg_id, db_path=self.db_path)
        self.assertEqual(msg1["steps"], steps_step1)
        self.assertEqual(msg1["text"], "")

        # 2. 스트리밍 텍스트 및 완료 정보 업데이트
        steps_step2 = [
            {"kind": "tool_call", "tool": "read_file", "title": "파일 읽기", "status": "done"}
        ]
        ok2 = chat_db.update_message(
            msg_id,
            text="최종 답변입니다.",
            steps=steps_step2,
            effort="high",
            cost=0.0015,
            session_id="new-session-123",
            update_preview=True,
            db_path=self.db_path,
        )
        self.assertTrue(ok2)
        msg2 = chat_db.get_message(msg_id, db_path=self.db_path)
        self.assertEqual(msg2["text"], "최종 답변입니다.")
        self.assertEqual(msg2["steps"], steps_step2)
        self.assertEqual(msg2["effort"], "high")
        self.assertEqual(msg2["cost"], 0.0015)
        self.assertEqual(msg2["sessionId"], "new-session-123")

        # 방 preview 업데이트 확인
        room = chat_db.get_room("room-update", db_path=self.db_path)
        self.assertEqual(room["preview"], "최종 답변입니다.")

    def test_archive_room(self):
        """대화방 아카이브(보관) 및 보관 해제, 필터링 조회 검증."""
        chat_db.get_or_create_room("room-archive-1", title="보관 테스트 1", db_path=self.db_path)
        chat_db.get_or_create_room("room-archive-2", title="보관 테스트 2", db_path=self.db_path)
        chat_db.add_message("room-archive-1", "user", "질문 1", db_path=self.db_path)
        chat_db.add_message("room-archive-2", "user", "질문 2", db_path=self.db_path)

        # 초기 상태: isArchived == False
        r1 = chat_db.get_room("room-archive-1", db_path=self.db_path)
        self.assertFalse(r1["isArchived"])

        # 아카이브 처리
        self.assertTrue(chat_db.archive_room("room-archive-1", is_archived=True, db_path=self.db_path))
        r1_archived = chat_db.get_room("room-archive-1", db_path=self.db_path)
        self.assertTrue(r1_archived["isArchived"])

        # include_archived=False 조회 시 제외됨
        active_rooms = chat_db.get_rooms(include_archived=False, db_path=self.db_path)
        active_ids = [r["roomId"] for r in active_rooms]
        self.assertNotIn("room-archive-1", active_ids)
        self.assertIn("room-archive-2", active_ids)

        # archived_only=True 조회 시 포함됨
        archived_rooms = chat_db.get_rooms(archived_only=True, db_path=self.db_path)
        archived_ids = [r["roomId"] for r in archived_rooms]
        self.assertIn("room-archive-1", archived_ids)
        self.assertNotIn("room-archive-2", archived_ids)

        # 아카이브 해제 (복원)
        self.assertTrue(chat_db.archive_room("room-archive-1", is_archived=False, db_path=self.db_path))
        r1_restored = chat_db.get_room("room-archive-1", db_path=self.db_path)
        self.assertFalse(r1_restored["isArchived"])


class TestSessionContinuityAndSync(unittest.TestCase):
    """웹 연속 대화방 유지, 비DB 세션 복구 및 다중 프로세스 설정 동기화 회귀 테스트."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="session_cont_test_")
        self.db_path = os.path.join(self.tmp_dir, "test_chat.db")
        chat_db.init_db(self.db_path, migrate_legacy=False)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_get_or_create_room_preserves_room_when_session_id_changes(self):
        """기존 방 ID로 새 session_id가 들어와도 방이 분할되지 않고 session_id만 갱신된다."""
        chat_db.get_or_create_room(
            "tg-sess-1",
            session_id="tg-sess-1",
            title="오늘은 주말이라 주식 못 사",
            channel="web",
            db_path=self.db_path,
        )
        chat_db.add_message(
            "tg-sess-1",
            "user",
            "오늘은 주말이라 주식 못 사",
            channel="web",
            session_id="tg-sess-1",
            db_path=self.db_path,
        )
        chat_db.add_message(
            "tg-sess-1",
            "assistant",
            "네, 주말에는 휴장입니다.",
            channel="web",
            session_id="tg-sess-1",
            db_path=self.db_path,
        )

        # 백엔드 세션 ID가 tg-sess-2로 바뀌었더라도 기존 room_id("tg-sess-1")를 지정하면 한 방에 이어진다
        merged_room = chat_db.get_or_create_room(
            "tg-sess-1",
            session_id="tg-sess-2",
            title="근데 네가 50만원 구매하라고 했잖아",
            channel="web",
            db_path=self.db_path,
        )
        self.assertEqual(merged_room["id"], "tg-sess-1")
        self.assertEqual(merged_room["session_id"], "tg-sess-2")
        self.assertEqual(merged_room["title"], "오늘은 주말이라 주식 못 사")

        chat_db.add_message(
            "tg-sess-1",
            "user",
            "근데 네가 50만원 구매하라고 했잖아",
            channel="web",
            session_id="tg-sess-2",
            db_path=self.db_path,
        )
        rooms = chat_db.get_rooms(db_path=self.db_path)
        self.assertEqual(len(rooms), 1)
        resolved = chat_db.get_room("tg-sess-2", db_path=self.db_path)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["id"], "tg-sess-1")
        msgs = chat_db.get_messages("tg-sess-1", db_path=self.db_path)
        self.assertEqual(len(msgs), 3)

    def test_engine_settings_multiprocess_sync_preserves_external_session_and_core_keys(self):
        """외부 프로세스가 bot_settings.json에 기록한 세션이 다른 프로세스의 set_session으로 덮이지 않는다."""
        import copy
        import json
        from unittest.mock import patch
        import bot.engine.engine_settings as engine_settings

        settings_file = os.path.join(self.tmp_dir, "bot_settings.json")
        orig_settings = copy.deepcopy(engine_settings._settings)
        with patch("bot.engine.engine_settings._SETTINGS_FILE", settings_file):
            try:
                with open(settings_file, "w", encoding="utf-8") as f:
                    json.dump({"engine": "agy", "agy_session_id": "tg-old"}, f)
                engine_settings._sync_from_disk()
                self.assertEqual(engine_settings.get_session(""), "tg-old")

                # 다른 웹 프로세스가 디스크의 agy_session_id를 tg-live로 갱신했다고 가정
                with open(settings_file, "w", encoding="utf-8") as f:
                    json.dump({"engine": "agy", "agy_session_id": "tg-live"}, f)

                # 현재 프로세스(web_server)가 구식 메모리 상태를 가진 채 core_ 세션을 갱신해도 tg-live가 보존되어야 함
                engine_settings.set_session("core-sess-1", ns="core_")

                self.assertEqual(engine_settings.get_session(""), "tg-live")
                self.assertEqual(engine_settings.get_session("core_"), "core-sess-1")
                with open(settings_file, "r", encoding="utf-8") as f:
                    on_disk = json.load(f)
                self.assertEqual(on_disk["agy_session_id"], "tg-live")
                self.assertEqual(on_disk["agy_core_session_id"], "core-sess-1")
                self.assertIn("core_session_id", on_disk)
                self.assertIsNone(on_disk["core_session_id"])
            finally:
                engine_settings._settings.clear()
                engine_settings._settings.update(orig_settings)
                if os.path.exists(engine_settings._SETTINGS_FILE):
                    engine_settings._sync_from_disk()

    def test_room_workdir_lifecycle(self):
        # 1. 방 생성 시 workdir 지정
        room = chat_db.get_or_create_room(
            room_id="room-custom-workdir",
            title="커스텀 경로 방",
            workdir="/custom/path/project",
            db_path=self.db_path,
        )
        self.assertEqual(room["workdir"], "/custom/path/project")

        # 2. get_room 조회 검증
        fetched = chat_db.get_room("room-custom-workdir", db_path=self.db_path)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched["workdir"], "/custom/path/project")

        # 3. get_rooms 목록 검증
        chat_db.add_message(
            room_id="room-custom-workdir",
            role="user",
            text="hello",
            db_path=self.db_path,
        )
        rooms = chat_db.get_rooms(db_path=self.db_path)
        target = next((r for r in rooms if r["id"] == "room-custom-workdir"), None)
        self.assertIsNotNone(target)
        self.assertEqual(target["workdir"], "/custom/path/project")

        # 4. update_room 으로 workdir 변경
        chat_db.update_room(
            room_id="room-custom-workdir",
            workdir="/updated/path/project",
            db_path=self.db_path,
        )
        updated = chat_db.get_room("room-custom-workdir", db_path=self.db_path)
        self.assertEqual(updated["workdir"], "/updated/path/project")

    def test_update_message_created_at_timestamp(self):
        """메시지 완료 시 update_message의 created_at이 정확히 갱신되고 get_messages로 조회되는지 검증한다."""
        t_start = 1700000000.0
        t_done = 1700000045.5
        msg_id = chat_db.add_message(
            room_id="room-ts-test",
            role="assistant",
            text="",
            created_at=t_start,
            db_path=self.db_path,
        )
        self.assertGreater(msg_id, 0)

        ok = chat_db.update_message(
            msg_id,
            text="완료된 답변",
            created_at=t_done,
            db_path=self.db_path,
        )
        self.assertTrue(ok)

        msgs = chat_db.get_messages("room-ts-test", db_path=self.db_path)
        self.assertEqual(len(msgs), 1)
        self.assertAlmostEqual(msgs[0]["createdAt"], t_done, places=3)

    def test_search_rooms_by_title_and_message_content(self):
        """search_rooms가 제목뿐만 아니라 과거 메시지 본문(대소문자 무시)까지 검색하고 스니펫을 추출하는지 검증한다."""
        chat_db.get_or_create_room("room-a", title="일반 대화", db_path=self.db_path)
        chat_db.add_message("room-a", "user", "오늘 점심 메뉴 추천해줘", db_path=self.db_path)
        chat_db.add_message("room-a", "assistant", "김치찌개와 돈까스를 추천합니다.", db_path=self.db_path)

        chat_db.get_or_create_room("room-b", title="프로젝트 회의", db_path=self.db_path)
        chat_db.add_message("room-b", "user", "SQLite FTS 말고 INSTR로 본문 검색 구현하기", db_path=self.db_path)
        chat_db.add_message("room-b", "assistant", "네, 완료했습니다.", db_path=self.db_path)

        # 1. preview에 없는 과거 메시지 본문 검색 ("김치찌개"는 preview가 아닌 직전 메시지가 아니라 room-a 마지막 메시지에 있음;
        #    room-b의 경우 마지막 메시지가 "네, 완료했습니다."이므로 preview에 "INSTR"이 없음)
        res_instr = chat_db.search_rooms("instr", db_path=self.db_path)
        self.assertEqual([r["id"] for r in res_instr], ["room-b"])
        self.assertIn("INSTR", res_instr[0]["matchSnippet"])

        # 2. 제목 검색
        res_title = chat_db.search_rooms("회의", db_path=self.db_path)
        self.assertEqual([r["id"] for r in res_title], ["room-b"])

        # 3. 한글 본문 검색
        res_kr = chat_db.search_rooms("돈까스", db_path=self.db_path)
        self.assertEqual([r["id"] for r in res_kr], ["room-a"])
        self.assertIn("돈까스", res_kr[0]["matchSnippet"])

    def test_room_status_lifecycle_and_cleanup(self):
        """대화방의 실행 상태(idle -> running -> completed/failed) 전이와 좀비 클린업을 검증한다."""
        chat_db.get_or_create_room("room-status-1", title="상태 테스트", db_path=self.db_path)
        r = chat_db.get_room("room-status-1", db_path=self.db_path)
        self.assertEqual(r["status"], "idle")
        self.assertIsNone(r["activeJobId"])

        # 1. 턴 시작 -> running
        ok = chat_db.update_room_status("room-status-1", "running", active_job_id="job-101", db_path=self.db_path)
        self.assertTrue(ok)
        r = chat_db.get_room("room-status-1", db_path=self.db_path)
        self.assertEqual(r["status"], "running")
        self.assertEqual(r["activeJobId"], "job-101")
        self.assertIsNotNone(r["runUpdatedAt"])

        # 2. 턴 완료 -> completed
        chat_db.update_room_status("room-status-1", "completed", active_job_id=None, db_path=self.db_path)
        r = chat_db.get_room("room-status-1", db_path=self.db_path)
        self.assertEqual(r["status"], "completed")
        self.assertIsNone(r["activeJobId"])

        # 3. 좀비 클린업: running 상태로 남아있는 고아 방들 정리
        chat_db.get_or_create_room("room-zombie-1", title="좀비1", db_path=self.db_path)
        chat_db.get_or_create_room("room-zombie-2", title="좀비2", db_path=self.db_path)
        chat_db.update_room_status("room-zombie-1", "running", active_job_id="dead-job-1", db_path=self.db_path)
        chat_db.update_room_status("room-zombie-2", "running", active_job_id="live-job-2", db_path=self.db_path)

        cleaned = chat_db.cleanup_stale_running_rooms(active_job_ids={"live-job-2"}, db_path=self.db_path)
        self.assertEqual(cleaned, 1)

        r1 = chat_db.get_room("room-zombie-1", db_path=self.db_path)
        self.assertEqual(r1["status"], "failed")
        self.assertIsNone(r1["activeJobId"])

        r2 = chat_db.get_room("room-zombie-2", db_path=self.db_path)
        self.assertEqual(r2["status"], "running")
        self.assertEqual(r2["activeJobId"], "live-job-2")


if __name__ == "__main__":
    unittest.main()
