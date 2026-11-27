import unittest
from pathlib import Path

class TestTurnAccordionContainer(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent.parent
        self.static_dir = self.root / "src" / "bot" / "web" / "static"

    def test_messages_js_has_turn_accordion_logic(self):
        messages_js = (self.static_dir / "js" / "messages.js").read_text(encoding="utf-8")
        self.assertIn("function groupEntriesIntoTurns", messages_js)
        self.assertIn("function toggleTurnAccordion", messages_js)
        self.assertIn("function toggleAllTurnsInCurrentRoom", messages_js)
        self.assertIn("chat-turn-container", messages_js)
        self.assertIn("turn-header-bar", messages_js)
        self.assertIn("turn-collapsed-summary", messages_js)
        self.assertIn("turn-body", messages_js)

    def test_chat_css_has_clean_light_accordion_styles(self):
        chat_css = (self.static_dir / "css" / "chat.css").read_text(encoding="utf-8")
        self.assertIn(".chat-turn-container", chat_css)
        self.assertIn(".turn-header-bar", chat_css)
        self.assertIn(".turn-collapsed-summary", chat_css)
        self.assertIn(".turn-body", chat_css)
        self.assertIn(".turns-global-bar", chat_css)
        self.assertIn(".btn-turn-accordion-toggle", chat_css)

        # 다크 테마 하드코딩 금지 검증
        turn_css_section = chat_css[chat_css.find("/* ─── 대화 세트(턴) 단일 컨테이너 및 아코디언"):]
        forbidden_dark_colors = ["#18181b", "#09090b", "#121215", "#161B26"]
        for dark_color in forbidden_dark_colors:
            self.assertNotIn(dark_color.lower(), turn_css_section.lower(), f"Forbidden dark color {dark_color} found in turn css!")

    def test_index_html_has_updated_cache_burst_versions(self):
        index_html = (self.static_dir / "index.html").read_text(encoding="utf-8")
        self.assertIn('/css/chat.css?v=25', index_html)
        self.assertIn('/js/messages.js?v=26', index_html)
        self.assertIn('/js/core.js?v=26', index_html)

if __name__ == "__main__":
    unittest.main()
