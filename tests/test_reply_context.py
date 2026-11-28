import os
import tempfile
import unittest

from bot.core import chat_db
from bot.web.turn_execution import _build_reply_group_context


class TestReplyContext(unittest.TestCase):
    def test_build_reply_group_context_by_group_id_and_legacy_turn_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "chat.db")
            chat_db.init_db(db_path)
            chat_db.get_or_create_room("room-r1", title="테스트", db_path=db_path)

            chat_db.add_message(
                "room-r1", "user", "그러면 지금 수정권한 요청하는 스코프 띄우면되잖아",
                group_id="grp-gmail1", db_path=db_path,
            )
            chat_db.add_message(
                "room-r1", "assistant", "네, gmail.modify 스코프를 추가하고 재인증 링크를 준비하겠습니다.",
                group_id="grp-gmail1", db_path=db_path,
            )

            ctx = _build_reply_group_context(
                type("DB", (), {"get_messages": staticmethod(lambda rid: chat_db.get_messages(rid, db_path=db_path))}),
                "room-r1",
                "grp-gmail1",
            )
            self.assertIn("[이어서 답장하는 이전 대화 맥락]", ctx)
            self.assertIn("수정권한 요청하는 스코프", ctx)
            self.assertIn("gmail.modify 스코프", ctx)

            legacy_uid = chat_db.add_message("room-r1", "user", "레거시 질문", db_path=db_path)
            chat_db.add_message("room-r1", "assistant", "레거시 답변", db_path=db_path)
            legacy_ctx = _build_reply_group_context(
                type("DB", (), {"get_messages": staticmethod(lambda rid: chat_db.get_messages(rid, db_path=db_path))}),
                "room-r1",
                f"turn_u_{legacy_uid}",
            )
            self.assertIn("레거시 질문", legacy_ctx)
            self.assertIn("레거시 답변", legacy_ctx)
