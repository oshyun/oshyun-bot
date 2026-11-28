import tempfile
import unittest
from pathlib import Path
from bot.core import chat_db

class TestTurnAccordionContainer(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent.parent
        self.static_dir = self.root / "src" / "bot" / "web" / "static"

    def test_messages_js_has_turn_accordion_logic(self):
        messages_js = (self.static_dir / "js" / "messages.js").read_text(encoding="utf-8")
        self.assertIn("function groupEntriesIntoTurns", messages_js)
        self.assertIn("function shouldStartNewTurnGroup", messages_js)
        self.assertIn("function toggleTurnAccordion", messages_js)
        self.assertIn("function toggleAllTurnsInCurrentRoom", messages_js)
        self.assertIn("chat-turn-container", messages_js)
        self.assertIn("turn-header-bar", messages_js)
        self.assertIn("turn-collapsed-summary", messages_js)
        self.assertIn("turn-body", messages_js)
        self.assertIn("turn-query-count-badge", messages_js)
        self.assertIn("function mergeTurnsByChildRoom", messages_js)

    def test_chat_css_has_clean_light_accordion_styles(self):
        chat_css = (self.static_dir / "css" / "chat.css").read_text(encoding="utf-8")
        self.assertIn(".chat-turn-container", chat_css)
        self.assertIn(".turn-header-bar", chat_css)
        self.assertIn(".turn-collapsed-summary", chat_css)
        self.assertIn(".turn-body", chat_css)
        self.assertIn(".turns-global-bar", chat_css)
        self.assertIn(".btn-turn-accordion-toggle", chat_css)
        self.assertIn(".turn-query-count-badge", chat_css)

        # 다크 테마 하드코딩 금지 검증
        turn_css_section = chat_css[chat_css.find("/* ─── 대화 세트(턴) 단일 컨테이너 및 아코디언"):]
        forbidden_dark_colors = ["#18181b", "#09090b", "#121215", "#161B26"]
        for dark_color in forbidden_dark_colors:
            self.assertNotIn(dark_color.lower(), turn_css_section.lower(), f"Forbidden dark color {dark_color} found in turn css!")

    def test_index_html_has_updated_cache_burst_versions(self):
        index_html = (self.static_dir / "index.html").read_text(encoding="utf-8")
        self.assertIn('/css/chat.css?v=26', index_html)
        self.assertIn('/js/messages.js?v=29', index_html)
        self.assertIn('/js/composer.js?v=26', index_html)
        self.assertIn('/js/ws.js?v=26', index_html)
        self.assertIn('/js/core.js?v=26', index_html)

    def test_chat_db_group_id_support(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_file = str(Path(tmp_dir) / "test_chat.db")
            chat_db.init_db(db_path=db_file, migrate_legacy=False)

            # 1. group_id 포함 메시지 추가
            msg_id_1 = chat_db.add_message(
                room_id="test_room",
                role="user",
                text="메일삭제권한 부여하고싶어 안내해줘",
                session_id="sess-123",
                group_id="grp-test-001",
                db_path=db_file,
            )
            self.assertGreater(msg_id_1, 0)

            msg_id_2 = chat_db.add_message(
                room_id="test_room",
                role="assistant",
                text="OAuth 스코프 수정이 필요합니다.",
                session_id="sess-123",
                group_id="grp-test-001",
                db_path=db_file,
            )
            self.assertGreater(msg_id_2, 0)

            # 2. 후속 질문도 동일 group_id 로 추가
            msg_id_3 = chat_db.add_message(
                room_id="test_room",
                role="user",
                text="수정할수 있는 링크알려줘야지",
                session_id="sess-123",
                group_id="grp-test-001",
                db_path=db_file,
            )
            self.assertGreater(msg_id_3, 0)

            # 3. get_messages 조회 시 groupId 정상 반환 검증
            messages = chat_db.get_messages("test_room", db_path=db_file)
            self.assertEqual(len(messages), 3)
            self.assertEqual(messages[0]["groupId"], "grp-test-001")
            self.assertEqual(messages[1]["groupId"], "grp-test-001")
            self.assertEqual(messages[2]["groupId"], "grp-test-001")

            # 4. update_message 로 group_id 갱신 검증
            ok = chat_db.update_message(
                msg_id_3,
                group_id="grp-test-updated",
                db_path=db_file,
            )
            self.assertTrue(ok)
            updated_msgs = chat_db.get_messages("test_room", db_path=db_file)
            self.assertEqual(updated_msgs[2]["groupId"], "grp-test-updated")

if __name__ == "__main__":
    unittest.main()
