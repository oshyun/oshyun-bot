"""test_gcal_manager — GoogleCalendarManager 기능 및 모킹 단위 테스트."""

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

from bot.google.gcal_manager import GoogleCalendarManager, format_event_range


class TestGoogleCalendarManager(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cred_file = os.path.join(self.temp_dir.name, "credentials.json")
        self.token_file = os.path.join(self.temp_dir.name, "token.json")
        self.trigger_file = os.path.join(self.temp_dir.name, ".cal-refresh")
        self.mgr = GoogleCalendarManager(
            credentials_file=self.cred_file,
            token_file=self.token_file,
            trigger_path=self.trigger_file,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_format_event_range(self):
        # 종일 일정
        start = {"date": "2026-11-05"}
        end = {"date": "2026-11-05"}
        formatted = format_event_range(start, end)
        self.assertIn("11/5", formatted)
        self.assertIn("종일", formatted)

        # 시간 지정 일정 (동일 날짜)
        start_dt = {"dateTime": "2026-11-05T14:00:00+09:00"}
        end_dt = {"dateTime": "2026-11-05T15:00:00+09:00"}
        formatted_dt = format_event_range(start_dt, end_dt)
        self.assertIn("14:00", formatted_dt)
        self.assertIn("15:00", formatted_dt)

    def test_status_checks(self):
        # 초기 상태: 파일 없음
        self.assertFalse(self.mgr.has_credentials)
        self.assertFalse(self.mgr.has_token)
        self.assertFalse(self.mgr.is_authenticated())

        # credentials 파일 생성
        with open(self.cred_file, "w") as f:
            f.write("{}")
        self.assertTrue(self.mgr.has_credentials)
        self.assertFalse(self.mgr.is_authenticated())

    def test_trigger_vault_refresh(self):
        self.assertFalse(os.path.exists(self.trigger_file))
        self.mgr.trigger_vault_refresh()
        self.assertTrue(os.path.exists(self.trigger_file))

    def test_quick_add_with_mock_service(self):
        mock_service = MagicMock()
        mock_events = MagicMock()
        mock_quick = MagicMock()
        mock_quick.execute.return_value = {
            "id": "event_123",
            "summary": "치과 진료",
            "start": {"dateTime": "2026-11-06T15:00:00+09:00"},
            "end": {"dateTime": "2026-11-06T16:00:00+09:00"},
            "htmlLink": "https://calendar.google.com/event?eid=123",
        }
        mock_events.quickAdd.return_value = mock_quick
        mock_service.events.return_value = mock_events

        with patch.object(self.mgr, "get_service", return_value=mock_service):
            res = self.mgr.quick_add("내일 15:00 치과 진료")
            self.assertEqual(res["id"], "event_123")
            self.assertEqual(res["summary"], "치과 진료")
            self.assertIn("15:00", res["time_str"])
            self.assertTrue(os.path.exists(self.trigger_file))
            mock_events.quickAdd.assert_called_once_with(
                calendarId="primary", text="내일 15:00 치과 진료"
            )

    def test_create_event_with_mock_service(self):
        mock_service = MagicMock()
        mock_events = MagicMock()
        mock_insert = MagicMock()
        mock_insert.execute.return_value = {
            "id": "event_456",
            "summary": "팀 미팅",
            "start": {"dateTime": "2026-11-07T10:00:00+09:00"},
            "end": {"dateTime": "2026-11-07T11:00:00+09:00"},
            "htmlLink": "https://calendar.google.com/event?eid=456",
        }
        mock_events.insert.return_value = mock_insert
        mock_service.events.return_value = mock_events

        with patch.object(self.mgr, "get_service", return_value=mock_service):
            res = self.mgr.create_event(
                summary="팀 미팅",
                start_time="2026-11-07T10:00:00+09:00",
                end_time="2026-11-07T11:00:00+09:00",
            )
            self.assertEqual(res["id"], "event_456")
            self.assertEqual(res["summary"], "팀 미팅")
            self.assertTrue(os.path.exists(self.trigger_file))

    def test_list_upcoming_events(self):
        mock_service = MagicMock()
        mock_events = MagicMock()
        mock_list = MagicMock()
        mock_list.execute.return_value = {
            "items": [
                {
                    "id": "e1",
                    "summary": "회의",
                    "start": {"dateTime": "2026-11-08T14:00:00+09:00"},
                    "end": {"dateTime": "2026-11-08T15:00:00+09:00"},
                    "htmlLink": "http://link1",
                },
                {
                    "id": "e2",
                    "summary": "휴가",
                    "start": {"date": "2026-11-09"},
                    "end": {"date": "2026-11-09"},
                    "htmlLink": "http://link2",
                },
            ]
        }
        mock_events.list.return_value = mock_list
        mock_service.events.return_value = mock_events

        with patch.object(self.mgr, "get_service", return_value=mock_service):
            events = self.mgr.list_upcoming_events(days=7)
            self.assertEqual(len(events), 2)
            self.assertEqual(events[0]["id"], "e1")
            self.assertEqual(events[0]["summary"], "회의")
            self.assertEqual(events[1]["id"], "e2")
            self.assertEqual(events[1]["summary"], "휴가")

    def test_delete_event(self):
        mock_service = MagicMock()
        mock_events = MagicMock()
        mock_del = MagicMock()
        mock_del.execute.return_value = None
        mock_events.delete.return_value = mock_del
        mock_service.events.return_value = mock_events

        with patch.object(self.mgr, "get_service", return_value=mock_service):
            ok = self.mgr.delete_event("e1")
            self.assertTrue(ok)
            mock_events.delete.assert_called_once_with(calendarId="primary", eventId="e1")
            self.assertTrue(os.path.exists(self.trigger_file))

    def test_unauthenticated_error(self):
        with self.assertRaises(PermissionError):
            self.mgr.get_service()


if __name__ == "__main__":
    unittest.main()
