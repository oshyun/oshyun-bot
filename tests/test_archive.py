"""test_archive.py — 채팅방 보관(아카이브) 및 일괄 보관 chat_db 검증."""

from __future__ import annotations

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


class TestChatDbArchive(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="archive_test_")
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

    def test_archive_normal_room(self):
        """일반 대화방 아카이브 및 복원 테스트."""
        chat_db.get_or_create_room("room-worker-1", title="워커 1", db_path=self.db_path)
        
        # 기본: 미보관 상태
        room = chat_db.get_room("room-worker-1", db_path=self.db_path)
        self.assertFalse(bool(room.get("isArchived")))

        # 아카이브 처리
        ok = chat_db.archive_room("room-worker-1", is_archived=True, db_path=self.db_path)
        self.assertTrue(ok)

        room_archived = chat_db.get_room("room-worker-1", db_path=self.db_path)
        self.assertTrue(bool(room_archived.get("isArchived")))

        # include_archived=False 시 제외 확인
        active_rooms = chat_db.get_rooms(include_archived=False, db_path=self.db_path)
        self.assertEqual(len(active_rooms), 0)

        # archived_only=True 시 조회 확인
        archived_rooms = chat_db.get_rooms(archived_only=True, db_path=self.db_path)
        self.assertEqual(len(archived_rooms), 1)

        # 복원 처리
        ok_restore = chat_db.archive_room("room-worker-1", is_archived=False, db_path=self.db_path)
        self.assertTrue(ok_restore)
        room_restored = chat_db.get_room("room-worker-1", db_path=self.db_path)
        self.assertFalse(bool(room_restored.get("isArchived")))

    def test_protected_room_cannot_be_archived(self):
        """마스터 및 전문가 코어 세션은 보관 처리 거부 확인."""
        chat_db.get_or_create_room("__core__", title="마스터 에이전트", db_path=self.db_path)
        chat_db.get_or_create_room("__expert_vault__", title="세컨브레인", db_path=self.db_path)
        chat_db.get_or_create_room("__expert_coding__", title="봇개발", db_path=self.db_path)

        for core_id in ("__core__", "__expert_vault__", "__expert_coding__"):
            ok = chat_db.archive_room(core_id, is_archived=True, db_path=self.db_path)
            self.assertFalse(ok, f"{core_id}는 보관 처리가 거부되어야 함")
            r = chat_db.get_room(core_id, db_path=self.db_path)
            self.assertFalse(bool(r.get("isArchived")))

    def test_archive_rooms_bulk(self):
        """일괄 아카이브 및 코어 세션 보호 테스트."""
        chat_db.get_or_create_room("room-w1", title="워커 1", db_path=self.db_path)
        chat_db.get_or_create_room("room-w2", title="워커 2", db_path=self.db_path)
        chat_db.get_or_create_room("__core__", title="마스터", db_path=self.db_path)

        # 코어가 포함된 리스트로 일괄 아카이브 시도
        count = chat_db.archive_rooms_bulk(["room-w1", "room-w2", "__core__"], is_archived=True, db_path=self.db_path)
        self.assertEqual(count, 2)  # __core__는 제외되고 2개만 보관됨

        # 상태 검증
        self.assertTrue(bool(chat_db.get_room("room-w1", db_path=self.db_path).get("isArchived")))
        self.assertTrue(bool(chat_db.get_room("room-w2", db_path=self.db_path).get("isArchived")))
        self.assertFalse(bool(chat_db.get_room("__core__", db_path=self.db_path).get("isArchived")))

        # 일괄 복원
        restore_count = chat_db.archive_rooms_bulk(["room-w1", "room-w2"], is_archived=False, db_path=self.db_path)
        self.assertEqual(restore_count, 2)
        self.assertFalse(bool(chat_db.get_room("room-w1", db_path=self.db_path).get("isArchived")))


if __name__ == "__main__":
    unittest.main()

