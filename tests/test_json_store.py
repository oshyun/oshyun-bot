"""test_json_store — JSON 읽기 및 원자적 쓰기 헬퍼 단위 테스트."""

import json
import os
import shutil
import sys
import tempfile
import unittest

_SRC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "bot"
)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from bot.core.json_store import read_json, write_json_atomic


class TestJsonStore(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_read_json_non_existent(self):
        path = os.path.join(self.test_dir, "non_existent.json")
        self.assertEqual(read_json(path, default=[]), [])
        self.assertEqual(read_json(path, default={"a": 1}), {"a": 1})

    def test_read_json_corrupted(self):
        path = os.path.join(self.test_dir, "corrupted.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{invalid json format: missing close brace")
        self.assertEqual(read_json(path, default={}), {})

    def test_read_json_valid(self):
        path = os.path.join(self.test_dir, "valid.json")
        data = {"key": "값", "numbers": [1, 2, 3], "flag": True}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        self.assertEqual(read_json(path, default={}), data)

    def test_read_json_expect_type(self):
        path = os.path.join(self.test_dir, "type_test.json")
        # 리스트 데이터 저장
        with open(path, "w", encoding="utf-8") as f:
            json.dump([1, 2, 3], f)

        # expect_type=list 이면 정상 반환
        self.assertEqual(read_json(path, default=[], expect_type=list), [1, 2, 3])
        # expect_type=dict 이면 타입 불일치로 default 반환
        self.assertEqual(read_json(path, default={}, expect_type=dict), {})

    def test_write_json_atomic_basic(self):
        path = os.path.join(self.test_dir, "atomic.json")
        data = {"hello": "world", "count": 42}
        write_json_atomic(path, data)

        self.assertTrue(os.path.exists(path))
        # tmp 파일은 os.replace 로 인해 남아있지 않아야 함
        self.assertFalse(os.path.exists(path + ".tmp"))

        loaded = read_json(path, default=None)
        self.assertEqual(loaded, data)

    def test_write_json_atomic_creates_parent_dirs(self):
        # 깊은 경로의 부모 디렉토리가 없는 경우
        deep_path = os.path.join(self.test_dir, "sub", "deep", "nested", "test.json")
        data = {"nested": True}
        write_json_atomic(deep_path, data)

        self.assertTrue(os.path.exists(deep_path))
        self.assertEqual(read_json(deep_path, default=None), data)

    def test_write_json_atomic_unicode_and_indent(self):
        path = os.path.join(self.test_dir, "unicode.json")
        data = {"메시지": "안녕하세요 🚀", "목록": ["사과", "바나나"]}
        write_json_atomic(path, data, indent=2)

        with open(path, "r", encoding="utf-8") as f:
            content = f.read()

        # ensure_ascii=False 이므로 escape 되지 않은 원본 한글이 포함되어야 함
        self.assertIn("안녕하세요", content)
        self.assertIn("🚀", content)
        # indent=2 가 적용되어 들여쓰기 줄바꿈이 있어야 함
        self.assertIn("\n  ", content)

        # read_json 으로 역직렬화 시에도 동일해야 함
        self.assertEqual(read_json(path, default=None), data)


if __name__ == "__main__":
    unittest.main()
