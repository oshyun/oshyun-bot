"""test_gmail_manager — GmailManager 및 볼트 동기화 기능 단위 테스트."""

import base64
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

_SRC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"
)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from bot.google.gmail_manager import GmailManager, _decode_base64_payload, _extract_body_text, _format_header_date
from bot.google.google_auth import GoogleAuthManager


class TestGmailManager(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cred_file = os.path.join(self.temp_dir.name, "credentials.json")
        self.token_file = os.path.join(self.temp_dir.name, "token.json")
        self.vault_file = os.path.join(self.temp_dir.name, "_bot", "메일.md")
        self.state_file = os.path.join(self.temp_dir.name, "gmail_sync_state.json")

        self.auth_mgr = GoogleAuthManager(
            credentials_file=self.cred_file,
            token_file=self.token_file,
        )
        self.gmail_mgr = GmailManager(
            auth_manager=self.auth_mgr,
            vault_file=self.vault_file,
            state_file=self.state_file,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_helpers(self):
        # 1. Base64 URL-safe 디코딩
        raw_text = "안녕하세요. 회의 일정 안내입니다."
        encoded = base64.urlsafe_b64encode(raw_text.encode("utf-8")).decode("ascii")
        self.assertEqual(_decode_base64_payload(encoded), raw_text)

        # 2. 날짜 파싱
        rfc_date = "Sun, 22 Nov 2026 14:30:00 +0900"
        formatted = _format_header_date(rfc_date)
        self.assertIn("2026-11-22", formatted)
        self.assertIn("14:30", formatted)

        # 3. 본문 텍스트 추출 (text/plain)
        payload = {
            "mimeType": "text/plain",
            "body": {"data": encoded},
        }
        self.assertEqual(_extract_body_text(payload), raw_text)

        # multipart parts 추출
        multipart_payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": encoded},
                }
            ],
        }
        self.assertEqual(_extract_body_text(multipart_payload), raw_text)

    def test_parse_message_detail(self):
        msg_detail = {
            "id": "msg_001",
            "threadId": "th_001",
            "snippet": "회의 관련 논의 사항...",
            "labelIds": ["INBOX", "UNREAD"],
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "프로젝트 킥오프 일정"},
                    {"name": "From", "value": "홍길동 <hong@example.com>"},
                    {"name": "Date", "value": "Sun, 22 Nov 2026 10:00:00 +0900"},
                ],
                "mimeType": "text/plain",
                "body": {"data": ""},
            },
        }
        parsed = self.gmail_mgr._parse_message_detail(msg_detail)
        self.assertEqual(parsed["id"], "msg_001")
        self.assertEqual(parsed["subject"], "프로젝트 킥오프 일정")
        self.assertEqual(parsed["from"], "홍길동 <hong@example.com>")
        self.assertTrue(parsed["isUnread"])
        self.assertIn("https://mail.google.com/mail/u/0/#inbox/msg_001", parsed["webLink"])
        self.assertIn("2026-11-22", parsed["date"])

    def test_list_messages_with_mock_service(self):
        mock_service = MagicMock()
        mock_users = MagicMock()
        mock_messages = MagicMock()

        # list mock
        mock_list_req = MagicMock()
        mock_list_req.execute.return_value = {
            "messages": [{"id": "m1"}, {"id": "m2"}]
        }
        mock_messages.list.return_value = mock_list_req

        # get mock
        def get_side_effect(userId, id, format):
            req = MagicMock()
            is_unread = id == "m1"
            req.execute.return_value = {
                "id": id,
                "threadId": f"t_{id}",
                "snippet": f"스니펫 {id}",
                "labelIds": ["UNREAD"] if is_unread else [],
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": f"제목 {id}"},
                        {"name": "From", "value": "test@example.com"},
                        {"name": "Date", "value": "Sun, 22 Nov 2026 12:00:00 +0900"},
                    ]
                },
            }
            return req

        mock_messages.get.side_effect = get_side_effect
        mock_users.messages.return_value = mock_messages
        mock_service.users.return_value = mock_users

        with patch.object(self.gmail_mgr, "get_service", return_value=mock_service):
            messages = self.gmail_mgr.list_messages(query="newer_than:7d", max_results=5)
            self.assertEqual(len(messages), 2)
            self.assertEqual(messages[0]["id"], "m1")
            self.assertTrue(messages[0]["isUnread"])
            self.assertEqual(messages[1]["id"], "m2")
            self.assertFalse(messages[1]["isUnread"])

    def test_sync_to_vault_creates_markdown_and_state(self):
        mock_messages = [
            {
                "id": "m100",
                "threadId": "t100",
                "subject": "주간 보고서",
                "from": "김팀장 <kim@company.com>",
                "date": "2026-11-22 11:00",
                "snippet": "이번 주 진행 현황입니다.",
                "isUnread": True,
                "webLink": "https://mail.google.com/mail/u/0/#inbox/m100",
            },
            {
                "id": "m101",
                "threadId": "t101",
                "subject": "뉴스레터",
                "from": "뉴스 <news@service.com>",
                "date": "2026-11-21 18:30",
                "snippet": "주요 IT 소식...",
                "isUnread": False,
                "webLink": "https://mail.google.com/mail/u/0/#inbox/m101",
            },
        ]

        with patch.object(self.gmail_mgr, "is_authenticated", return_value=True), \
             patch.object(self.gmail_mgr, "list_messages", return_value=mock_messages):
            res = self.gmail_mgr.sync_to_vault()
            self.assertTrue(res["ok"])
            self.assertEqual(res["totalCount"], 2)
            self.assertEqual(res["unreadCount"], 1)

            # 볼트 파일 생성 검증
            self.assertTrue(os.path.isfile(self.vault_file))
            with open(self.vault_file, "r", encoding="utf-8") as f:
                content = f.read()

            self.assertIn("title: \"수신 메일함\"", content)
            self.assertIn("unread_count: 1", content)
            self.assertIn("total_count: 2", content)
            self.assertIn("주간 보고서", content)
            self.assertIn("김팀장", content)
            self.assertIn("🔵 **미열람**", content)
            self.assertIn("뉴스레터", content)

            # 상태 파일 검증
            self.assertTrue(os.path.isfile(self.state_file))
            with open(self.state_file, "r", encoding="utf-8") as f:
                state = json.load(f)
            self.assertEqual(state["totalCount"], 2)
            self.assertEqual(state["unreadCount"], 1)
            self.assertEqual(state["messageIds"], ["m100", "m101"])

            # 상태 조회 검증
            status = self.gmail_mgr.get_sync_status()
            self.assertTrue(status["isAuthenticated"])
            self.assertTrue(status["vaultFileExists"])
            self.assertEqual(status["totalCount"], 2)

    def test_trash_and_delete_message(self):
        mock_service = MagicMock()
        mock_users = MagicMock()
        mock_messages = MagicMock()
        mock_service.users.return_value = mock_users
        mock_users.messages.return_value = mock_messages
        mock_messages.trash.return_value.execute.return_value = {"id": "msg_to_trash"}
        mock_messages.delete.return_value.execute.return_value = {}

        with patch.object(self.gmail_mgr, "get_service", return_value=mock_service):
            # 1. trash_message 성공
            res_trash = self.gmail_mgr.trash_message("msg_to_trash")
            self.assertTrue(res_trash)
            mock_messages.trash.assert_called_once_with(userId="me", id="msg_to_trash")

            # 2. delete_message 성공
            res_delete = self.gmail_mgr.delete_message("msg_to_delete")
            self.assertTrue(res_delete)
            mock_messages.delete.assert_called_once_with(userId="me", id="msg_to_delete")

            # 3. 빈 message_id 예외 발생
            with self.assertRaises(ValueError):
                self.gmail_mgr.trash_message("")
            with self.assertRaises(ValueError):
                self.gmail_mgr.delete_message("")


if __name__ == "__main__":
    unittest.main()
