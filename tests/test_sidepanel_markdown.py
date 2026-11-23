"""
사이드패널 마크다운 통일 및 일관된 스타일링 검증 테스트.
스레드 사이드바, 마스터 메모리 카드, 볼트 뷰어에서 본문의 formatMarkdown 및 .md 스타일이 일원화되어 적용되는지 확인합니다.
"""
import os
import unittest


class TestSidepanelMarkdownUnification(unittest.TestCase):
    def setUp(self):
        self.static_dir = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "src", "bot", "web", "static")
        )

    def test_chat_css_has_md_compact(self):
        """chat.css 에 사이드패널 및 카드 공용 .md.md-compact 스타일이 정의되어 있는지 검증."""
        path = os.path.join(self.static_dir, "css", "chat.css")
        with open(path, "r", encoding="utf-8") as f:
            css = f.read()
        self.assertIn(".md.md-compact", css)
        self.assertIn(".md.md-compact p", css)
        self.assertIn(".md.md-compact pre", css)
        self.assertIn(".md.md-compact .table-wrap", css)

    def test_master_inspector_css_markdown_unification(self):
        """master-inspector.css 가 독자적인 레거시 p, pre 오버라이드를 지양하고 .md 통일 클래스를 지원하는지 검증."""
        path = os.path.join(self.static_dir, "css", "master-inspector.css")
        with open(path, "r", encoding="utf-8") as f:
            css = f.read()
        self.assertIn(".thread-prompt-body.md", css)
        self.assertIn(".thread-msg-bubble.md", css)
        self.assertIn(".msb-mem-card-text.md", css)

    def test_rooms_ui_thread_uses_unified_markdown(self):
        """rooms-ui.js 스레드 패널에서 md md-compact 클래스를 적용하는지 검증."""
        path = os.path.join(self.static_dir, "js", "rooms-ui.js")
        with open(path, "r", encoding="utf-8") as f:
            js = f.read()
        self.assertIn('class="thread-prompt-body md md-compact"', js)
        self.assertIn("thread-msg-bubble ${role === 'assistant' ? 'md md-compact' : ''}", js)

    def test_master_memory_uses_format_markdown(self):
        """master-memory.js 카드 텍스트에서 formatMarkdown 및 md md-compact 클래스를 적용하는지 검증."""
        path = os.path.join(self.static_dir, "js", "master-memory.js")
        with open(path, "r", encoding="utf-8") as f:
            js = f.read()
        self.assertIn("formatMarkdown(rawText)", js)
        self.assertIn('class="msb-mem-card-text md md-compact"', js)

    def test_vault_viewer_markdown_preview(self):
        """vault.js 및 index.html 에서 볼트 파일 마크다운 뷰어/에디터 모드 및 프리뷰가 구비되어 있는지 검증."""
        html_path = os.path.join(self.static_dir, "index.html")
        with open(html_path, "r", encoding="utf-8") as f:
            html = f.read()
        self.assertIn('id="vault-mode-switch"', html)
        self.assertIn('id="vault-preview"', html)

        js_path = os.path.join(self.static_dir, "js", "vault.js")
        with open(js_path, "r", encoding="utf-8") as f:
            js = f.read()
        self.assertIn("switchVaultMode", js)
        self.assertIn("formatMarkdown(rawText)", js)


if __name__ == "__main__":
    unittest.main()
