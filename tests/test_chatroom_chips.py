"""tests/test_chatroom_chips.py — 대화방 참조 칩(Chip/Badge) 및 링크 이동 렌더링 단위 테스트."""

import os
import re
import unittest

STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "src", "bot", "static")


def read_static_bundle(static_dir: str) -> str:
    parts = []
    index_file = os.path.join(static_dir, "index.html")
    if os.path.isfile(index_file):
        with open(index_file, "r", encoding="utf-8") as f:
            parts.append(f.read())
    for sub in ("css", "js"):
        dir_path = os.path.join(static_dir, sub)
        if os.path.isdir(dir_path):
            for fname in sorted(os.listdir(dir_path)):
                if fname.endswith((".css", ".js")):
                    with open(os.path.join(dir_path, fname), "r", encoding="utf-8") as f:
                        parts.append(f.read())
    return "\n".join(parts)


class TestChatroomChips(unittest.TestCase):
    def setUp(self):
        self.html = read_static_bundle(STATIC_DIR)

    def test_chatroom_chip_css_styles_exist(self):
        """대화방 칩 스타일(.md-link-chip.is-room) 및 역할별 테마가 존재하는지 검증."""
        self.assertIn(".md .md-link-chip.is-room", self.html)
        self.assertIn(".md .md-link-chip.is-room:hover", self.html)
        self.assertIn(".md .md-link-chip.is-room.is-core", self.html)
        self.assertIn(".md .md-link-chip.is-room.is-expert-vault", self.html)
        self.assertIn(".md .md-link-chip.is-room.is-expert-coding", self.html)

    def test_js_functions_exist(self):
        """대화방 검색 및 네비게이션, 칩 생성 JS 함수가 존재하는지 검증."""
        self.assertIn("function findChatRoom(", self.html)
        self.assertIn("function navigateToChatRoom(", self.html)
        self.assertIn("function buildChatRoomChipHtml(", self.html)
        self.assertIn("ICONS.chat", self.html)

    def test_markdown_inline_parsing_chatroom_chips(self):
        """renderInline 에서 room: 및 [방이름] 대괄호 칩 변환 로직이 포함되어 있는지 검증."""
        self.assertIn("room|chat", self.html)
        self.assertIn("is-room", self.html)
        self.assertIn("navigateToChatRoom", self.html)
        self.assertIn("findChatRoom", self.html)

    def test_bracket_regex_matches_worker_title_in_image(self):
        """첨부 이미지의 실제 문구 '[하위 일꾼 메시지 마크다운 포매팅 렌더링 누락 버그 수정] 하위 작업 완료 검수 보고입니다.' 대괄호 매칭 시뮬레이션."""
        text = "[하위 일꾼 메시지 마크다운 포매팅 렌더링 누락 버그 수정] 하위 작업 완료 검수 보고입니다."

        # index.html 의 정규식과 동일한 패턴
        pattern = re.compile(r"(?<!\[)\[([^\[\]\r\n]{2,100})\](?!\])")
        matches = pattern.findall(text)

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0], "하위 일꾼 메시지 마크다운 포매팅 렌더링 누락 버그 수정")

        # 시스템 제외 키워드 필터링 테스트
        ignored_samples = [
            "[ ]", "[x]", "[1]", "[TODO]", "[DONE]", "[PASS]", "[FAIL]",
            "[하위 작업 완료 보고]", "[하위 작업 중단/오류 보고]", "[System Event] 서브 태스크"
        ]
        filter_regex = re.compile(r"^(?:[ xX\^_]|\d+|!?(?:NOTE|TIP|IMPORTANT|WARNING|WARN|CAUTION|TODO|DONE|PASS|FAIL|ERROR|INFO)|하위 작업 완료 보고|하위 작업 중단/오류 보고|System Event.*)$", re.IGNORECASE)

        for sample in ignored_samples:
            inner = sample.strip("[]")
            self.assertTrue(filter_regex.match(inner), f"Should be filtered: {sample}")

        # 정상적인 방 제목은 필터에 걸리지 않아야 함
        self.assertIsNone(filter_regex.match(matches[0]))


if __name__ == "__main__":
    unittest.main()
