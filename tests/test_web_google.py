"""test_web_google — Google OAuth2 및 Gmail 연동 REST API 엔드포인트 단위 테스트."""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

_SRC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "bot"
)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from google_auth import GoogleAuthManager
from gmail_manager import GmailManager
import web_google


class TestWebGoogleEndpoints(AioHTTPTestCase):
    async def get_application(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cred_file = os.path.join(self.temp_dir.name, "google_credentials.json")
        self.token_file = os.path.join(self.temp_dir.name, "google_token.json")
        self.vault_file = os.path.join(self.temp_dir.name, "메일.md")
        self.state_file = os.path.join(self.temp_dir.name, "gmail_sync_state.json")

        self.mock_auth = GoogleAuthManager(
            credentials_file=self.cred_file,
            token_file=self.token_file,
        )
        self.mock_gmail = GmailManager(
            auth_manager=self.mock_auth,
            vault_file=self.vault_file,
            state_file=self.state_file,
        )

        # web_google 모듈의 싱글톤 객체 교체
        self.orig_auth = web_google.default_google_auth
        self.orig_gmail = web_google.default_gmail_manager
        web_google.default_google_auth = self.mock_auth
        web_google.default_gmail_manager = self.mock_gmail

        app = web.Application()
        app.router.add_get("/api/google/status", web_google.handle_google_status)
        app.router.add_post("/api/google/credentials", web_google.handle_google_credentials)
        app.router.add_get("/api/google/auth/url", web_google.handle_google_auth_url)
        app.router.add_get("/api/google/auth/callback", web_google.handle_google_auth_callback)
        app.router.add_post("/api/google/auth/code", web_google.handle_google_auth_code)
        app.router.add_post("/api/google/disconnect", web_google.handle_google_disconnect)
        app.router.add_get("/api/gmail/status", web_google.handle_gmail_status)
        app.router.add_post("/api/gmail/sync", web_google.handle_gmail_sync)
        app.router.add_post("/api/schedules/ensure-gmail", web_google.handle_schedules_ensure_gmail)
        return app

    async def asyncTearDown(self):
        web_google.default_google_auth = self.orig_auth
        web_google.default_gmail_manager = self.orig_gmail
        self.temp_dir.cleanup()
        await super().asyncTearDown()

    async def test_google_status_initial(self):
        res = await self.client.get("/api/google/status")
        self.assertEqual(res.status, 200)
        data = await res.json()
        self.assertEqual(data["status"], "ok")
        self.assertFalse(data["account"]["isAuthenticated"])
        self.assertFalse(data["account"]["hasCredentials"])
        self.assertFalse(data["gmail"]["isAuthenticated"])

    async def test_google_credentials_save(self):
        # 1. 빈 요청 시 오류
        res_err = await self.client.post("/api/google/credentials", json={})
        self.assertEqual(res_err.status, 400)

        # 2. 정상 credentials json 저장
        cred_payload = {
            "credentials": {
                "installed": {
                    "client_id": "test_cid",
                    "client_secret": "test_sec",
                }
            }
        }
        res_ok = await self.client.post("/api/google/credentials", json=cred_payload)
        self.assertEqual(res_ok.status, 200)
        data = await res_ok.json()
        self.assertEqual(data["status"], "ok")
        self.assertTrue(self.mock_auth.has_credentials)

    async def test_google_auth_url(self):
        # 자격증명 없을 때 오류
        res = await self.client.get("/api/google/auth/url")
        self.assertEqual(res.status, 400)

        # 자격증명 있을 때 성공
        with open(self.cred_file, "w") as f:
            f.write(json.dumps({"installed": {"client_id": "123"}}))

        with patch.object(self.mock_auth, "get_auth_url", return_value="https://accounts.google.com/o/oauth2/v2/auth?test"):
            res2 = await self.client.get("/api/google/auth/url?redirect_uri=http://localhost:8765/cb")
            self.assertEqual(res2.status, 200)
            data2 = await res2.json()
            self.assertEqual(data2["status"], "ok")
            self.assertIn("https://accounts.google.com", data2["authUrl"])

    async def test_google_auth_callback(self):
        # 에러 파라미터 전달 시 에러 페이지
        res_err = await self.client.get("/api/google/auth/callback?error=access_denied")
        self.assertEqual(res_err.status, 400)
        html_err = await res_err.text()
        self.assertIn("인증 실패", html_err)

        # 정상 code 수신 및 교환 성공
        with patch.object(self.mock_auth, "exchange_code", return_value=True), \
             patch.object(self.mock_auth, "get_account_info", return_value={"email": "tester@gmail.com"}):
            res_ok = await self.client.get("/api/google/auth/callback?code=mock_code")
            self.assertEqual(res_ok.status, 200)
            html_ok = await res_ok.text()
            self.assertIn("구글 계정 연동 완료", html_ok)
            self.assertIn("tester@gmail.com", html_ok)

    async def test_google_auth_code_manual(self):
        with patch.object(self.mock_auth, "exchange_code", return_value=True):
            res = await self.client.post("/api/google/auth/code", json={"code": "4/0A_manual_code"})
            self.assertEqual(res.status, 200)
            data = await res.json()
            self.assertEqual(data["status"], "ok")

    async def test_google_disconnect(self):
        with open(self.token_file, "w") as f:
            f.write("{}")
        self.assertTrue(self.mock_auth.has_token)

        res = await self.client.post("/api/google/disconnect")
        self.assertEqual(res.status, 200)
        self.assertFalse(self.mock_auth.has_token)

    async def test_gmail_sync_endpoint(self):
        # 1. 미인증 시 401
        res_unauth = await self.client.post("/api/gmail/sync", json={})
        self.assertEqual(res_unauth.status, 401)

        # 2. 인증 시 정상 실행
        with patch.object(self.mock_auth, "is_authenticated", return_value=True), \
             patch.object(self.mock_gmail, "sync_to_vault", return_value={"ok": True, "totalCount": 5, "unreadCount": 2}):
            res_sync = await self.client.post("/api/gmail/sync", json={"query": "is:unread"})
            self.assertEqual(res_sync.status, 200)
            data = await res_sync.json()
            self.assertEqual(data["status"], "ok")
            self.assertEqual(data["result"]["totalCount"], 5)

    async def test_schedules_ensure_gmail(self):
        with patch("web_google.ensure_default_gmail_schedule", return_value={"id": "gmail-auto", "cron": "0 8 * * *"}):
            res = await self.client.post("/api/schedules/ensure-gmail", json={"cron": "0 8 * * *"})
            self.assertEqual(res.status, 200)
            data = await res.json()
            self.assertEqual(data["status"], "ok")
            self.assertTrue(data["created"])
            self.assertEqual(data["item"]["id"], "gmail-auto")


if __name__ == "__main__":
    unittest.main()
