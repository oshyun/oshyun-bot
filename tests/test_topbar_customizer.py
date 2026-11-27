"""상단바 가로 스크롤 및 노출 항목 커스텀 드롭다운 정적 에셋 무결성 검증 테스트."""

import os
import unittest


class TestTopbarCustomizer(unittest.TestCase):
    def setUp(self):
        self.base_dir = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "src", "bot", "web", "static")
        )
        self.index_html_path = os.path.join(self.base_dir, "index.html")
        self.layout_css_path = os.path.join(self.base_dir, "css", "layout.css")
        self.rooms_ui_js_path = os.path.join(self.base_dir, "js", "rooms-ui.js")
        self.master_memory_js_path = os.path.join(self.base_dir, "js", "master-memory.js")
        self.gestures_js_path = os.path.join(self.base_dir, "js", "gestures.js")

        with open(self.index_html_path, "r", encoding="utf-8") as f:
            self.html = f.read()
        with open(self.layout_css_path, "r", encoding="utf-8") as f:
            self.css = f.read()
        with open(self.rooms_ui_js_path, "r", encoding="utf-8") as f:
            self.rooms_js = f.read()
        with open(self.master_memory_js_path, "r", encoding="utf-8") as f:
            self.memory_js = f.read()
        with open(self.gestures_js_path, "r", encoding="utf-8") as f:
            self.gestures_js = f.read()
        self.js = self.rooms_js

    def test_index_html_has_scroll_container_and_customizer(self):
        """index.html에 가로 스크롤 컨테이너 및 최우측 커스텀 드롭다운 요소가 존재하는지 확인."""
        self.assertIn('id="topbar-actions-scroll"', self.html)
        self.assertIn('id="topbar-customizer"', self.html)
        self.assertIn('id="btn-topbar-customizer"', self.html)
        self.assertIn('id="topbar-customizer-popover"', self.html)
        self.assertIn('id="topbar-customizer-list"', self.html)

    def test_index_html_all_action_items_tagged(self):
        """상단바 대화방 제목 및 7대 액션 버튼에 data-action-id 속성이 올바르게 부여되었는지 확인."""
        required_actions = [
            'data-action-id="title"',
            'data-action-id="usage"',
            'data-action-id="workdir"',
            'data-action-id="model"',
            'data-action-id="agent-config"',
            'data-action-id="shortcut"',
            'data-action-id="cli"',
            'data-action-id="memory"',
        ]
        for action_attr in required_actions:
            self.assertIn(action_attr, self.html, f"Missing action attribute: {action_attr}")

    def test_settings_screen_has_topbar_items_manager(self):
        """설정 화면에 상단 헤더 항목 순서/표시 관리 UI 섹션이 존재하는지 확인."""
        self.assertIn('id="setting-row-topbar-items"', self.html)
        self.assertIn('id="topbar-items-manager"', self.html)
        self.assertIn('id="btn-reset-topbar-items"', self.html)

    def test_rooms_ui_js_has_order_and_title_support(self):
        """rooms-ui.js에 title 항목 포함 및 순서 이동(moveTopbarItem, reorderTopbarItems) 함수가 정의되어 있는지 확인."""
        self.assertIn("'title'", self.js)
        self.assertIn("moveTopbarItem", self.js)
        self.assertIn("reorderTopbarItems", self.js)
        self.assertIn("getTopbarConfigList", self.js)

    def test_index_html_fouc_prestyle_loader(self):
        """FOUC 방지를 위한 사전 스타일 적용 스크립트가 존재하는지 확인."""
        self.assertIn("oshyun_topbar_visible_items_v1", self.html)
        self.assertIn("topbar-visibility-prestyle", self.html)

    def test_layout_css_has_scroll_and_customizer_rules(self):
        """layout.css에 가로 스크롤, 숨김 처리, 팝오버 스타일이 정의되어 있는지 확인."""
        self.assertIn(".topbar-actions-scroll", self.css)
        self.assertIn("-webkit-overflow-scrolling: touch", self.css)
        self.assertIn("[data-topbar-hidden=\"true\"]", self.css)
        self.assertIn(".topbar-customizer-popover", self.css)
        self.assertIn(".topbar-customizer-btn", self.css)

    def test_rooms_ui_js_has_customizer_logic(self):
        """rooms-ui.js에 설정 저장, 적용, 토글 및 스와이프 함수가 정의되어 있는지 확인."""
        self.assertIn("TOPBAR_STORAGE_KEY", self.js)
        self.assertIn("TOPBAR_ACTION_ITEMS", self.js)
        self.assertIn("getTopbarVisibilityPreferences", self.js)
        self.assertIn("saveTopbarVisibilityPreferences", self.js)
        self.assertIn("applyTopbarVisibility", self.js)
        self.assertIn("toggleTopbarCustomizer", self.js)
        self.assertIn("initTopbarSwipeAndScroll", self.js)

    def test_title_included_inside_scroll_track(self):
        """방 이름(data-action-id='title')이 topbar-actions-scroll 내부 chat-actions 트랙에 포함되어 있는지 확인."""
        scroll_idx = self.html.find('id="topbar-actions-scroll"')
        actions_idx = self.html.find('id="chat-actions"', scroll_idx)
        title_idx = self.html.find('data-action-id="title"', actions_idx)
        self.assertNotEqual(scroll_idx, -1)
        self.assertNotEqual(actions_idx, -1)
        self.assertNotEqual(title_idx, -1)
        self.assertTrue(scroll_idx < actions_idx < title_idx)

    def test_master_memory_title_truncation_logic(self):
        """방 이름 10자 초과 시 말줄임 로직(formatRoomTitleDisplay) 검증."""
        self.assertIn("formatRoomTitleDisplay", self.memory_js)
        self.assertIn("Array.from", self.memory_js)
        self.assertIn("formatRoomTitleDisplay(rawTitle, 10)", self.memory_js)

    def test_gestures_js_blocks_topbar_horizontal_swipe(self):
        """헤더 영역에서 가로 슬라이드 시 좌우 사이드패널 스와이프 제스처 간섭이 차단되는지 확인."""
        self.assertIn(".topbar", self.gestures_js)
        self.assertIn(".topbar-actions-scroll", self.gestures_js)

    def test_layout_css_touch_action_and_overscroll(self):
        """CSS에 touch-action 및 overscroll-behavior-x 가 올바르게 선언되어 있는지 확인."""
        self.assertIn("overscroll-behavior-x: contain", self.css)
        self.assertIn("touch-action: pan-x", self.css)


if __name__ == "__main__":
    unittest.main()
