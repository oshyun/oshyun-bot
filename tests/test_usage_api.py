"""tests/test_usage_api.py — AGY 쿼터 캐시 서빙, GET /api/usage 엔드포인트 및 프론트엔드 연동 테스트."""

import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC_DIR = os.path.join(_REPO_ROOT, "src", "bot")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

import usage_pricing
import web_server


class TestUsagePricingHelpers(unittest.TestCase):
    """usage_pricing 모듈의 쿼터 캐시 파싱 및 헬퍼 함수 동작 검증."""

    def test_parse_quota_data_both_buckets(self):
        sample_data = {
            "Gemini Models": {
                "Weekly Limit Remaining": {
                    "pct": 47.0,
                    "window": "weekly",
                    "reset_time": "2026-11-25T00:00:06Z",
                },
                "Five Hour Limit Remaining": {
                    "pct": 75.1,
                    "window": "5h",
                    "reset_time": "2026-11-18T15:00:06Z",
                },
            }
        }
        res = usage_pricing.parse_quota_data_to_usage(sample_data, timestamp=1000.0)
        self.assertIsNotNone(res)
        self.assertEqual(res["badge_text"], "5h 75% · W 47%")
        self.assertEqual(res["five_hour"]["pct"], 75.1)
        self.assertEqual(res["five_hour"]["reset_time"], "2026-11-18T15:00:06Z")
        self.assertEqual(res["weekly"]["pct"], 47.0)
        self.assertEqual(res["weekly"]["reset_time"], "2026-11-25T00:00:06Z")
        self.assertEqual(res["timestamp"], 1000.0)

    def test_parse_quota_data_single_bucket(self):
        sample_data = {
            "Gemini Models": {
                "Five Hour Limit Remaining": {
                    "pct": 82.4,
                    "window": "5h",
                    "reset_time": "2026-11-18T15:00:06Z",
                }
            }
        }
        res = usage_pricing.parse_quota_data_to_usage(sample_data)
        self.assertIsNotNone(res)
        self.assertEqual(res["badge_text"], "5h 82%")
        self.assertIsNone(res["weekly"])
        self.assertEqual(res["five_hour"]["pct"], 82.4)

    def test_parse_quota_data_invalid(self):
        self.assertIsNone(usage_pricing.parse_quota_data_to_usage(None))
        self.assertIsNone(usage_pricing.parse_quota_data_to_usage({}))
        self.assertIsNone(usage_pricing.parse_quota_data_to_usage({"other": "invalid"}))

    def test_cache_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = os.path.join(tmpdir, "agy_cache.json")
            data = {
                "Gemini Models": {
                    "Weekly Limit Remaining": {"pct": 50.0, "window": "weekly"},
                    "Five Hour Limit Remaining": {"pct": 80.0, "window": "5h"},
                }
            }
            usage_pricing._save_cached_quota(data, cache_file=cache_file)
            loaded = usage_pricing._load_cached_quota_file(cache_file=cache_file)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.get("ttl"), 300)
            self.assertEqual(loaded.get("data"), data)

            usage = usage_pricing.get_cached_agy_usage(allow_refresh=False, cache_file=cache_file)
            self.assertIsNotNone(usage)
            self.assertEqual(usage["badge_text"], "5h 80% · W 50%")


class TestUsageEndpoints(AioHTTPTestCase):
    """GET /api/usage 및 build_status_payload 검증."""

    async def get_application(self):
        self.orig_auth = getattr(web_server, "WEB_AUTH_REQUIRED", True)
        web_server.WEB_AUTH_REQUIRED = False
        return web_server.create_web_app()

    async def tearDownAsync(self):
        web_server.WEB_AUTH_REQUIRED = getattr(self, "orig_auth", True)
        if hasattr(self, "client") and self.client:
            await self.client.close()

    async def test_api_usage_endpoint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = os.path.join(tmpdir, "agy_cache.json")
            data = {
                "Gemini Models": {
                    "Weekly Limit Remaining": {"pct": 47.0, "window": "weekly", "reset_time": "2026-11-25T00:00:06Z"},
                    "Five Hour Limit Remaining": {"pct": 75.1, "window": "5h", "reset_time": "2026-11-18T15:00:06Z"},
                }
            }
            usage_pricing._save_cached_quota(data, cache_file=cache_file)

            with patch("usage_pricing.DEFAULT_AGY_QUOTA_CACHE_FILE", cache_file):
                resp = await self.client.request("GET", "/api/usage")
                self.assertEqual(resp.status, 200)
                body = await resp.json()
                self.assertEqual(body.get("status"), "ok")
                usage = body.get("usage")
                self.assertIsNotNone(usage)
                self.assertEqual(usage.get("badge_text"), "5h 75% · W 47%")
                self.assertEqual(usage.get("five_hour", {}).get("pct"), 75.1)
                self.assertEqual(usage.get("weekly", {}).get("pct"), 47.0)

    async def test_build_status_payload_includes_usage(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = os.path.join(tmpdir, "agy_cache.json")
            data = {
                "Gemini Models": {
                    "Five Hour Limit Remaining": {"pct": 90.0, "window": "5h"},
                }
            }
            usage_pricing._save_cached_quota(data, cache_file=cache_file)

            with patch("usage_pricing.DEFAULT_AGY_QUOTA_CACHE_FILE", cache_file):
                payload = web_server.build_status_payload()
                self.assertIn("usage", payload)
                self.assertIsNotNone(payload["usage"])
                self.assertEqual(payload["usage"]["badge_text"], "5h 90%")


class TestUsageHtmlUi(unittest.TestCase):
    """index.html 내 배지 엘리먼트 및 클라이언트 스크립트 존재 검증."""

    def test_html_ui_markers(self):
        html_path = os.path.join(_SRC_DIR, "static", "index.html")
        self.assertTrue(os.path.isfile(html_path))
        with open(html_path, "r", encoding="utf-8") as f:
            content = f.read()

        for marker in (
            'id="avatar-usage-badge"',
            "avatar-usage-badge",
            ".avatar-usage-badge.safe",
            ".avatar-usage-badge.warning",
            ".avatar-usage-badge.danger",
            "function updateAvatarUsageBadge(",
            "function fetchAgyUsage(",
            "function initUsagePolling(",
            "initUsagePolling();",
            "/api/usage",
        ):
            self.assertIn(marker, content, f"HTML에 필수 마커 '{marker}'가 누락되었습니다.")


if __name__ == "__main__":
    unittest.main()
