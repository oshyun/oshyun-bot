import os
import tempfile
import unittest
from unittest import mock

from bot.core import chat_db
from bot.web import turn_title


class TestTurnTitle(unittest.TestCase):
    def test_set_and_load_turn_title_in_chat_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "chat.db")
            chat_db.init_db(db_path)
            chat_db.get_or_create_room("room-t1", title="테스트 방", db_path=db_path)
            chat_db.add_message(
                "room-t1", "user", "질문입니다", group_id="grp-abc123", db_path=db_path
            )
            chat_db.set_turn_title("grp-abc123", "room-t1", "테스트 주제 요약", db_path=db_path)

            msgs = chat_db.get_messages("room-t1", db_path=db_path)
            self.assertEqual(len(msgs), 1)
            self.assertEqual(msgs[0].get("turnTitle"), "테스트 주제 요약")

    def test_clean_title_and_summarize_mocked(self):
        self.assertEqual(turn_title._clean_title('  "Gmail 권한 설정 및 재인증."  '), "Gmail 권한 설정 및 재인증")

        fake_resp = mock.MagicMock()
        fake_resp.read.return_value = b'{"candidates":[{"content":{"parts":[{"text":"Gmail \\uad8c\\ud55c \\ucd94\\uac00 \\ubc0f \\uc7ac\\uc778\\uc99d"}]}}]}'
        fake_resp.__enter__.return_value = fake_resp

        with mock.patch("urllib.request.urlopen", return_value=fake_resp) as mock_open:
            title = turn_title.summarize_turn_title_sync(
                [{"role": "user", "text": "메일 삭제 권한 추가해줘"}],
                api_key="AIzaFakeTestKey12345678901234567890123",
            )
        self.assertEqual(title, "Gmail 권한 추가 및 재인증")
        req = mock_open.call_args[0][0]
        self.assertIn("gemini-3.8-flash:generateContent", req.full_url)
