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

import bot.engine.usage_pricing as usage_pricing
import bot.web.server as web_server


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
        res = usage_pricing.parse_quota_data_to_usage(
            sample_data,
            timestamp=1000.0,
            cached_at="2026-11-18 21:30:00",
            source="agy CLI (/usage 캐시)",
        )
        self.assertIsNotNone(res)
        self.assertEqual(res["badge_text"], "5h 75% · W 47%")
        self.assertEqual(res["five_hour"]["pct"], 75.1)
        self.assertEqual(res["five_hour"]["reset_time"], "2026-11-18T15:00:06Z")
        self.assertEqual(res["weekly"]["pct"], 47.0)
        self.assertEqual(res["weekly"]["reset_time"], "2026-11-25T00:00:06Z")
        self.assertEqual(res["timestamp"], 1000.0)
        self.assertEqual(res["cached_at"], "2026-11-18 21:30:00")
        self.assertEqual(res["source"], "agy CLI (/usage 캐시)")

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

    def test_parse_quota_data_all_model_groups(self):
        sample_data = {
            "Gemini Models": {
                "Weekly Limit Remaining": {"pct": 4.3, "window": "weekly", "reset_time": "2026-11-25T00:00:06Z"},
                "Five Hour Limit Remaining": {"pct": 99.2, "window": "5h", "reset_time": "2026-11-20T08:00:06Z"},
            },
            "Claude and GPT models": {
                "Weekly Limit Remaining": {"pct": 82.2, "window": "weekly", "reset_time": "2026-11-25T14:33:45Z"},
                "Five Hour Limit Remaining": {"pct": 100, "window": "5h", "reset_time": "2026-11-20T09:56:20Z"},
            },
            "Gemini 4 Argon": {
                "Gemini 4 Argon Weekly Limit": {"pct": 99.8, "window": "weekly", "reset_time": "2026-11-25T02:41:21Z"},
            },
        }
        res = usage_pricing.parse_quota_data_to_usage(sample_data)
        self.assertIsNotNone(res)
        self.assertEqual(res["badge_text"], "5h 99% · W 4%")
        groups = res.get("groups")
        self.assertIsInstance(groups, list)
        self.assertEqual(len(groups), 3)
        by_name = {g["name"]: g for g in groups}
        self.assertEqual(by_name["Gemini Models"]["weekly"]["pct"], 4.3)
        self.assertEqual(by_name["Gemini Models"]["five_hour"]["pct"], 99.2)
        self.assertEqual(by_name["Claude and GPT models"]["weekly"]["pct"], 82.2)
        self.assertEqual(by_name["Claude and GPT models"]["five_hour"]["pct"], 100.0)
        self.assertEqual(by_name["Gemini 4 Argon"]["weekly"]["pct"], 99.8)
        self.assertIsNone(by_name["Gemini 4 Argon"]["five_hour"])

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
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._patchers = [
            patch("bot.core.chat_db.CHAT_DB_FILE", os.path.join(self._tmp_dir.name, "chat.db")),
            patch("bot.master.master_tasks.MASTER_TASKS_FILE", os.path.join(self._tmp_dir.name, "bot.master.master_tasks.json")),
            patch.object(web_server, "WEB_JOBS_FILE", os.path.join(self._tmp_dir.name, "web_jobs.json")),
        ]
        for p in self._patchers:
            p.start()
        self.orig_auth = getattr(web_server, "WEB_AUTH_REQUIRED", True)
        web_server.WEB_AUTH_REQUIRED = False
        return web_server.create_web_app()

    async def tearDownAsync(self):
        web_server.WEB_AUTH_REQUIRED = getattr(self, "orig_auth", True)
        if hasattr(self, "client") and self.client:
            await self.client.close()
        for p in reversed(getattr(self, "_patchers", [])):
            p.stop()
        if hasattr(self, "_tmp_dir"):
            self._tmp_dir.cleanup()

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

            with patch("bot.engine.usage_pricing.DEFAULT_AGY_QUOTA_CACHE_FILE", cache_file):
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

            with patch("bot.engine.usage_pricing.DEFAULT_AGY_QUOTA_CACHE_FILE", cache_file):
                payload = web_server.build_status_payload()
                self.assertIn("usage", payload)
                self.assertIsNotNone(payload["usage"])
                self.assertEqual(payload["usage"]["badge_text"], "5h 90%")


class TestUsageHtmlUi(unittest.TestCase):
    """index.html 내 배지 엘리먼트 및 클라이언트 스크립트 존재 검증."""

    def test_html_ui_markers(self):
        static_dir = os.path.join(_SRC_DIR, "static")
        html_path = os.path.join(static_dir, "index.html")
        self.assertTrue(os.path.isfile(html_path))
        parts = []
        with open(html_path, "r", encoding="utf-8") as f:
            parts.append(f.read())
        for sub in ("css", "js"):
            sub_dir = os.path.join(static_dir, sub)
            if os.path.isdir(sub_dir):
                for fname in sorted(os.listdir(sub_dir)):
                    if fname.endswith((".css", ".js")):
                        with open(os.path.join(sub_dir, fname), "r", encoding="utf-8") as f:
                            parts.append(f.read())
        content = "\n".join(parts)

        for marker in (
            'id="avatar-usage-badge"',
            "avatar-usage-badge",
            ".avatar-usage-badge.safe",
            ".avatar-usage-badge.warning",
            ".avatar-usage-badge.danger",
            "function updateAvatarUsageBadge(",
            "function buildAgyUsageTooltip(",
            "function formatAgyRelativeTime(",
            "function fetchAgyUsage(",
            "function initUsagePolling(",
            "initUsagePolling();",
            "/api/usage",
            "mouseenter",
            'id="settings-group-usage"',
            'id="settings-usage-list"',
            'id="modal-all-usage"',
            'id="modal-usage-list"',
            "function openAllUsageModal(",
            "function renderAllModelsUsageHtml(",
            "function renderAllModelsUsageTargets(",
            "function refreshAllModelsUsage(",
            'id="topbar-usage-badge"',
            "document.querySelectorAll('.avatar-usage-badge')",
        ):
            self.assertIn(marker, content, f"HTML에 필수 마커 '{marker}'가 누락되었습니다.")

    def test_mobile_topbar_badge_visible_outside_drawer(self):
        """회귀: 모바일에선 사이드바(드로어) 안 배지가 안 보이므로 상단바에도 배지가 있어야 한다."""
        with open(os.path.join(_SRC_DIR, "static", "index.html"), encoding="utf-8") as f:
            html = f.read()
        sidebar_end = html.index("</aside>") if "</aside>" in html else html.index('<header class="topbar"')
        topbar_pos = html.index('id="topbar-usage-badge"')
        self.assertGreater(topbar_pos, sidebar_end, "topbar 배지는 사이드바 밖에 있어야 함")
        with open(os.path.join(_SRC_DIR, "static", "css", "responsive.css"), encoding="utf-8") as f:
            self.assertIn("topbar-usage-badge", f.read())

    def test_pwa_standalone_cache_busting_and_lifecycle(self):
        """안드로이드 PWA(standalone) 환경에서 구버전 캐시 고착을 막고 즉시 갱신되도록 보장하는지 검증."""
        static_dir = os.path.join(_SRC_DIR, "static")
        with open(os.path.join(static_dir, "index.html"), encoding="utf-8") as f:
            html = f.read()
        with open(os.path.join(static_dir, "sw.js"), encoding="utf-8") as f:
            sw_js = f.read()
        with open(os.path.join(static_dir, "js", "rooms-sync.js"), encoding="utf-8") as f:
            rooms_sync_js = f.read()
        with open(os.path.join(static_dir, "js", "settings.js"), encoding="utf-8") as f:
            settings_js = f.read()
        with open(os.path.join(static_dir, "css", "responsive.css"), encoding="utf-8") as f:
            responsive_css = f.read()

        # 1. Service Worker 캐시 이름이 업그레이드되었는지 (구버전 캐시 자동 퍼지)
        self.assertRegex(sw_js, r"oshyun-hub-v(?:[8-9]|\d{2,})", "Service Worker 캐시 버전이 v8 이상으로 업그레이드되어야 함")
        self.assertNotIn("oshyun-hub-v7", sw_js, "구버전 캐시 v7이 남아있지 않아야 함")

        # 2. index.html의 캐시 버스팅 파라미터가 갱신되었는지
        self.assertRegex(html, r"rooms-sync\.js\?v=(?:[3-9]|\d{2,})", "rooms-sync.js의 캐시 버스팅 파라미터가 갱신되어야 함")
        self.assertRegex(html, r"responsive\.css\?v=(?:[2-9]|\d{2,})", "responsive.css의 캐시 버스팅 파라미터가 갱신되어야 함")

        # 3. PWA 라이프사이클 (pageshow, focus, visibilitychange) 복귀 시 갱신 리스너 검증
        self.assertIn("addEventListener('pageshow'", rooms_sync_js)
        self.assertIn("addEventListener('focus'", rooms_sync_js)
        self.assertIn("fetchAgyUsage(true)", rooms_sync_js, "최초 실행 시 강제 갱신으로 document.hidden 여부와 무관하게 즉시 조회해야 함")

        # 4. SW 등록 시 백그라운드 갱신 검사(update) 및 제어권 교체(controllerchange)
        self.assertIn("reg.update()", settings_js)
        self.assertIn("controllerchange", settings_js)

        # 5. PWA standalone 미디어 쿼리 지원
        self.assertIn("display-mode: standalone", responsive_css)
        self.assertIn("topbar-usage-badge", responsive_css)


if __name__ == "__main__":
    unittest.main()
