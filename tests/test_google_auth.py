"""test_google_auth — Google OAuth 2.0 통합 인증 매니저 단위 테스트."""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

_SRC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "bot"
)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from bot.google.google_auth import DEFAULT_GOOGLE_SCOPES, GoogleAuthManager


class TestGoogleAuthManager(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cred_file = os.path.join(self.temp_dir.name, "google_credentials.json")
        self.token_file = os.path.join(self.temp_dir.name, "google_token.json")
        self.mgr = GoogleAuthManager(
            credentials_file=self.cred_file,
            token_file=self.token_file,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_initial_state_and_scopes(self):
        self.assertFalse(self.mgr.has_credentials)
        self.assertFalse(self.mgr.has_token)
        self.assertFalse(self.mgr.is_authenticated())
        self.assertIn("https://www.googleapis.com/auth/gmail.readonly", self.mgr.scopes)
        self.assertIn("https://www.googleapis.com/auth/calendar", self.mgr.scopes)

    def test_save_credentials_file_dict_and_string(self):
        # 1. 딕셔너리로 저장
        valid_dict = {
            "installed": {
                "client_id": "test_client_id",
                "client_secret": "test_client_secret",
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        }
        res = self.mgr.save_credentials_file(valid_dict)
        self.assertTrue(res)
        self.assertTrue(self.mgr.has_credentials)

        with open(self.cred_file, "r", encoding="utf-8") as f:
            saved = json.load(f)
            self.assertEqual(saved["installed"]["client_id"], "test_client_id")

        # 2. JSON 문자열로 저장
        valid_str = json.dumps({
            "web": {
                "client_id": "web_client_123",
                "client_secret": "web_secret_456",
            }
        })
        self.mgr.save_credentials_file(valid_str)
        with open(self.cred_file, "r", encoding="utf-8") as f:
            saved = json.load(f)
            self.assertEqual(saved["web"]["client_id"], "web_client_123")

    def test_save_credentials_file_invalid(self):
        # 유효하지 않은 JSON 문자열
        with self.assertRaises(ValueError):
            self.mgr.save_credentials_file("{not_json")

        # installed/web 키 누락
        with self.assertRaises(ValueError):
            self.mgr.save_credentials_file({"other_key": 123})

    def test_get_auth_url_without_credentials_raises(self):
        with self.assertRaises(FileNotFoundError):
            self.mgr.get_auth_url("http://localhost:8765/callback")

    @patch("google_auth_oauthlib.flow.Flow.from_client_secrets_file")
    def test_get_auth_url_success(self, mock_flow_from_file):
        with open(self.cred_file, "w") as f:
            f.write(json.dumps({"installed": {"client_id": "123"}}))

        mock_flow = MagicMock()
        mock_flow.authorization_url.return_value = ("https://accounts.google.com/auth?test=1", "state123")
        mock_flow_from_file.return_value = mock_flow

        url = self.mgr.get_auth_url("http://localhost:8765/callback", state="mystate")
        self.assertEqual(url, "https://accounts.google.com/auth?test=1")
        mock_flow.authorization_url.assert_called_once_with(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
            state="mystate",
        )

    @patch("google_auth_oauthlib.flow.Flow.from_client_secrets_file")
    def test_exchange_code_success(self, mock_flow_from_file):
        with open(self.cred_file, "w") as f:
            f.write(json.dumps({"installed": {"client_id": "123"}}))

        mock_flow = MagicMock()
        mock_creds = MagicMock()
        mock_creds.valid = True
        mock_creds.to_json.return_value = json.dumps({"token": "ya29.test_token"})
        mock_flow.credentials = mock_creds
        mock_flow_from_file.return_value = mock_flow

        success = self.mgr.exchange_code("auth_code_123", "http://localhost:8765/callback")
        self.assertTrue(success)
        self.assertTrue(self.mgr.has_token)

        with open(self.token_file, "r") as f:
            saved = json.load(f)
            self.assertEqual(saved["token"], "ya29.test_token")

    def test_disconnect(self):
        with open(self.token_file, "w") as f:
            f.write("{}")
        self.assertTrue(self.mgr.has_token)

        res = self.mgr.disconnect()
        self.assertTrue(res)
        self.assertFalse(self.mgr.has_token)
        self.assertFalse(os.path.exists(self.token_file))

        # 이미 없는 경우 disconnect 호출 시 False 반환
        self.assertFalse(self.mgr.disconnect())

    def test_get_account_info(self):
        # 1. 미인증 상태
        info = self.mgr.get_account_info()
        self.assertFalse(info["isAuthenticated"])
        self.assertIsNone(info["email"])

        # 2. 토큰 파일 내 email 존재 상태
        with open(self.token_file, "w") as f:
            f.write(json.dumps({"token": "123", "email": "user@example.com"}))

        mock_creds = MagicMock()
        mock_creds.valid = True
        with patch.object(self.mgr, "get_credentials", return_value=mock_creds):
            info = self.mgr.get_account_info()
            self.assertTrue(info["isAuthenticated"])
            self.assertEqual(info["email"], "user@example.com")


if __name__ == "__main__":
    unittest.main()
