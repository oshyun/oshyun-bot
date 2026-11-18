"""test_cron — cron 표현식 파싱 및 일치 여부 판정 단위 테스트."""

import datetime
import os
import sys
import unittest

_SRC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "bot"
)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from cron import Cron, _parse_field


class TestCronParseField(unittest.TestCase):
    def test_wildcard(self):
        res = _parse_field("*", 0, 5)
        self.assertEqual(res, {0, 1, 2, 3, 4, 5})

    def test_single_number(self):
        self.assertEqual(_parse_field("3", 0, 10), {3})
        # 범위 밖 숫자는 제외됨
        self.assertEqual(_parse_field("15", 0, 10), set())

    def test_range(self):
        self.assertEqual(_parse_field("2-5", 0, 10), {2, 3, 4, 5})

    def test_step(self):
        self.assertEqual(_parse_field("*/15", 0, 59), {0, 15, 30, 45})
        self.assertEqual(_parse_field("10-30/10", 0, 59), {10, 20, 30})

    def test_comma_list(self):
        self.assertEqual(_parse_field("1,3,7-9", 0, 10), {1, 3, 7, 8, 9})


class TestCronMatches(unittest.TestCase):
    def test_all_wildcards(self):
        c = Cron("* * * * *")
        dt = datetime.datetime(2026, 10, 28, 14, 30)
        self.assertTrue(c.matches(dt))

    def test_exact_match(self):
        c = Cron("30 14 28 10 *")
        self.assertTrue(c.matches(datetime.datetime(2026, 10, 28, 14, 30)))
        # 다른 분
        self.assertFalse(c.matches(datetime.datetime(2026, 10, 28, 14, 31)))
        # 다른 시
        self.assertFalse(c.matches(datetime.datetime(2026, 10, 28, 15, 30)))
        # 다른 일
        self.assertFalse(c.matches(datetime.datetime(2026, 10, 29, 14, 30)))
        # 다른 월
        self.assertFalse(c.matches(datetime.datetime(2026, 11, 28, 14, 30)))

    def test_step_and_list(self):
        c = Cron("*/15 9-18 * * 1-5")
        # 2026-10-28은 수요일(weekday=2, cron_dow=3)
        self.assertTrue(c.matches(datetime.datetime(2026, 10, 28, 9, 0)))
        self.assertTrue(c.matches(datetime.datetime(2026, 10, 28, 10, 45)))
        self.assertFalse(c.matches(datetime.datetime(2026, 10, 28, 8, 45)))  # hour 8
        self.assertFalse(c.matches(datetime.datetime(2026, 10, 28, 10, 10)))  # minute 10

    def test_sunday_normalization_0_and_7(self):
        # 2026-10-25는 일요일 (weekday=6, cron_dow=0)
        sun_dt = datetime.datetime(2026, 10, 25, 12, 0)
        mon_dt = datetime.datetime(2026, 10, 26, 12, 0)

        # 요일 0 = 일요일
        c0 = Cron("0 12 * * 0")
        self.assertTrue(c0.matches(sun_dt))
        self.assertFalse(c0.matches(mon_dt))

        # 요일 7 = 일요일 (0으로 정규화)
        c7 = Cron("0 12 * * 7")
        self.assertTrue(c7.matches(sun_dt))
        self.assertFalse(c7.matches(mon_dt))

    def test_dom_and_dow_restriction_or_rule(self):
        """표준 cron 규칙: 일(dom)과 요일(dow)이 둘 다 제한되면 OR 조건으로 매칭."""
        # 13일이거나 금요일(cron_dow=5)
        c = Cron("0 0 13 * 5")

        # 2026-11-13은 금요일 -> 둘 다 만족
        dt_both = datetime.datetime(2026, 11, 13, 0, 0)
        self.assertTrue(c.matches(dt_both))

        # 2026-10-13은 화요일 (cron_dow=2) -> 일자(13)만 만족
        dt_dom_only = datetime.datetime(2026, 10, 13, 0, 0)
        self.assertTrue(c.matches(dt_dom_only))

        # 2026-10-16은 금요일 (cron_dow=5, 일자는 16) -> 요일(금)만 만족
        dt_dow_only = datetime.datetime(2026, 10, 16, 0, 0)
        self.assertTrue(c.matches(dt_dow_only))

        # 2026-10-14는 수요일 (cron_dow=3, 일자는 14) -> 둘 다 불만족
        dt_neither = datetime.datetime(2026, 10, 14, 0, 0)
        self.assertFalse(c.matches(dt_neither))

    def test_invalid_expressions(self):
        # 필드 개수 부족
        with self.assertRaises(ValueError):
            Cron("* * * *")
        # 필드 개수 초과
        with self.assertRaises(ValueError):
            Cron("* * * * * *")
        # 매칭 불가능한 범위 (예: 유효하지 않은 분)
        with self.assertRaises(ValueError):
            Cron("60-70 * * * *")


if __name__ == "__main__":
    unittest.main()
