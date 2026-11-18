"""gcal_manager — Google Calendar API v3 연동 매니저.

Google 계정(OAuth 2.0)으로 캘린더 일정을 조회, 빠른 등록(quickAdd), 상세 생성,
삭제하고, 일정이 변경되면 볼트 동기화 트리거(_bot/.cal-refresh)를 터치하여
로컬 캘린더 노트(_bot/캘린더.md)도 즉각 실시간 동기화한다.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from paths import (
    CAL_REFRESH_TRIGGER_PATH,
    GOOGLE_CREDENTIALS_FILE,
    GOOGLE_TOKEN_FILE,
)

logger = logging.getLogger("oshyun-bot.gcal")

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/calendar.events",
]

_WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]


def _format_iso_time(iso_str: str) -> tuple[str, bool]:
    """ISO 날짜/일시 문자열을 읽어 ('M/D (요일) HH:MM', all_day 여부) 반환."""
    if not iso_str:
        return "", False
    # 종일 일정: 'YYYY-MM-DD'
    if len(iso_str) == 10 and iso_str.count("-") == 2:
        try:
            d = datetime.strptime(iso_str, "%Y-%m-%d").date()
            wd = _WEEKDAYS[d.weekday()]
            return f"{d.month}/{d.day} ({wd})", True
        except ValueError:
            return iso_str, True
    # 시간 일정: 'YYYY-MM-DDTHH:MM:SS...'
    try:
        # 파이썬 fromisoformat 은 +09:00 형태 지원
        dt = datetime.fromisoformat(iso_str)
        # 로컬 시간대로 변환
        local_dt = dt.astimezone()
        wd = _WEEKDAYS[local_dt.weekday()]
        return f"{local_dt.month}/{local_dt.day} ({wd}) {local_dt.strftime('%H:%M')}", False
    except Exception:
        return iso_str, False


def format_event_range(start_dict: dict[str, str], end_dict: dict[str, str]) -> str:
    """이벤트 start/end 딕셔너리를 읽어 사람이 읽기 쉬운 기간 텍스트로 포맷팅."""
    start_raw = start_dict.get("dateTime") or start_dict.get("date") or ""
    end_raw = end_dict.get("dateTime") or end_dict.get("date") or ""

    start_str, start_all_day = _format_iso_time(start_raw)
    end_str, end_all_day = _format_iso_time(end_raw)

    if start_all_day:
        return f"{start_str} (종일)"
    if start_str and end_str:
        # 같은 날짜면 종료 시간만 뒷부분에 표시
        if start_str.split()[0] == end_str.split()[0] and " " in end_str:
            end_time_only = end_str.split()[-1]
            return f"{start_str} ~ {end_time_only}"
        return f"{start_str} ~ {end_str}"
    return start_str or end_str or "시간 미정"


class GoogleCalendarManager:
    """Google Calendar API v3 클라이언트 래퍼."""

    def __init__(
        self,
        credentials_file: str | None = None,
        token_file: str | None = None,
        trigger_path: str | None = None,
    ) -> None:
        self.credentials_file = credentials_file or GOOGLE_CREDENTIALS_FILE
        self.token_file = token_file or GOOGLE_TOKEN_FILE
        self.trigger_path = trigger_path or CAL_REFRESH_TRIGGER_PATH

    @property
    def has_credentials(self) -> bool:
        """클라이언트 비밀 키(credentials.json) 파일 존재 여부."""
        return os.path.isfile(self.credentials_file)

    @property
    def has_token(self) -> bool:
        """인증 토큰(token.json) 파일 존재 여부."""
        return os.path.isfile(self.token_file)

    def is_authenticated(self) -> bool:
        """유효한 인증 정보가 있거나 리프레시 가능한지 확인."""
        creds = self.get_credentials()
        return creds is not None and creds.valid

    def get_credentials(self) -> Any:
        """저장된 토큰을 읽고 필요 시 자동 리프레시하여 Credentials 객체를 반환."""
        if not self.has_token:
            return None
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials

            creds = Credentials.from_authorized_user_file(self.token_file, SCOPES)
            if creds and creds.expired and creds.refresh_token:
                logger.info("Google Calendar 토큰 만료됨, 자동 갱신 시도")
                creds.refresh(Request())
                self._save_credentials(creds)
            return creds if (creds and creds.valid) else None
        except Exception as exc:
            logger.warning("Google Calendar 인증 토큰 로드/갱신 실패: %s", exc)
            return None

    def _save_credentials(self, creds: Any) -> None:
        """Credentials 객체를 token.json 파일로 원자적 저장."""
        os.makedirs(os.path.dirname(os.path.abspath(self.token_file)), exist_ok=True)
        with open(self.token_file, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
        logger.info("Google Calendar 토큰 저장 완료: %s", self.token_file)

    def get_auth_url(self, redirect_uri: str = "http://localhost:8080/") -> str:
        """OAuth 2.0 웹 브라우저 인증 URL 생성."""
        if not self.has_credentials:
            raise FileNotFoundError(
                f"OAuth 클라이언트 설정 파일이 없습니다: {self.credentials_file}\n"
                "Google Cloud Console에서 OAuth 2.0 클라이언트 ID(데스크톱 앱) JSON을 "
                "다운로드하여 해당 경로에 저장해주세요."
            )
        from google_auth_oauthlib.flow import InstalledAppFlow

        flow = InstalledAppFlow.from_client_secrets_file(
            self.credentials_file,
            scopes=SCOPES,
            redirect_uri=redirect_uri,
        )
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )
        return auth_url

    def finish_auth_with_code(
        self, code: str, redirect_uri: str = "http://localhost:8080/"
    ) -> bool:
        """사용자가 입력한 인증 코드로 토큰을 교환하고 저장."""
        if not self.has_credentials:
            raise FileNotFoundError(f"OAuth 설정 파일이 없습니다: {self.credentials_file}")
        from google_auth_oauthlib.flow import InstalledAppFlow

        flow = InstalledAppFlow.from_client_secrets_file(
            self.credentials_file,
            scopes=SCOPES,
            redirect_uri=redirect_uri,
        )
        flow.fetch_token(code=code.strip())
        creds = flow.credentials
        if creds and creds.valid:
            self._save_credentials(creds)
            return True
        return False

    def get_service(self) -> Any:
        """Google Calendar v3 Resource 객체 생성."""
        creds = self.get_credentials()
        if not creds:
            raise PermissionError(
                "Google Calendar 인증이 필요합니다. 먼저 /cal_auth 명령어로 인증하세요."
            )
        from googleapiclient.discovery import build

        return build("calendar", "v3", credentials=creds, cache_discovery=False)

    def trigger_vault_refresh(self) -> None:
        """볼트 내 캘린더 사이드카 갱신 트리거(_bot/.cal-refresh)를 터치한다."""
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.trigger_path)), exist_ok=True)
            Path(self.trigger_path).touch()
            logger.info("볼트 캘린더 동기화 트리거 생성: %s", self.trigger_path)
        except Exception as exc:
            logger.warning("볼트 캘린더 트리거 생성 실패: %s", exc)

    def quick_add(self, text: str, calendar_id: str = "primary") -> dict[str, Any]:
        """자연어 문장을 받아 구글 캘린더의 quickAdd API로 일정을 생성한다.

        예: '내일 15:00 치과 진료', 'Friday 7pm Team Dinner at Gangnam'
        """
        service = self.get_service()
        event = service.events().quickAdd(calendarId=calendar_id, text=text).execute()
        self.trigger_vault_refresh()

        summary = event.get("summary", "(제목 없음)")
        start_dict = event.get("start", {})
        end_dict = event.get("end", {})
        time_str = format_event_range(start_dict, end_dict)

        return {
            "id": event.get("id"),
            "summary": summary,
            "time_str": time_str,
            "html_link": event.get("htmlLink", ""),
            "raw": event,
        }

    def create_event(
        self,
        summary: str,
        start_time: datetime | str,
        end_time: datetime | str | None = None,
        description: str = "",
        location: str = "",
        all_day: bool = False,
        calendar_id: str = "primary",
    ) -> dict[str, Any]:
        """명시적인 시작/종료 일시로 새 일정을 생성한다."""
        service = self.get_service()

        if all_day:
            start_body = {"date": start_time if isinstance(start_time, str) else start_time.strftime("%Y-%m-%d")}
            if end_time:
                end_body = {"date": end_time if isinstance(end_time, str) else end_time.strftime("%Y-%m-%d")}
            else:
                end_body = start_body
        else:
            s_iso = start_time if isinstance(start_time, str) else start_time.isoformat()
            if end_time:
                e_iso = end_time if isinstance(end_time, str) else end_time.isoformat()
            else:
                # 종료시간 미지정 시 1시간 후로 기본 설정
                if isinstance(start_time, datetime):
                    e_iso = (start_time + timedelta(hours=1)).isoformat()
                else:
                    e_iso = s_iso
            start_body = {"dateTime": s_iso}
            end_body = {"dateTime": e_iso}

        body: dict[str, Any] = {
            "summary": summary,
            "start": start_body,
            "end": end_body,
        }
        if description:
            body["description"] = description
        if location:
            body["location"] = location

        event = service.events().insert(calendarId=calendar_id, body=body).execute()
        self.trigger_vault_refresh()

        time_str = format_event_range(event.get("start", {}), event.get("end", {}))
        return {
            "id": event.get("id"),
            "summary": event.get("summary", summary),
            "time_str": time_str,
            "html_link": event.get("htmlLink", ""),
            "raw": event,
        }

    def list_upcoming_events(
        self, days: int = 7, max_results: int = 20, calendar_id: str = "primary"
    ) -> list[dict[str, Any]]:
        """다가오는 일정 목록을 조회한다."""
        service = self.get_service()
        now = datetime.now(timezone.utc)
        time_min = now.isoformat()
        time_max = (now + timedelta(days=days)).isoformat()

        events_result = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                maxResults=max_results,
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
        items = events_result.get("items", [])
        parsed_events: list[dict[str, Any]] = []

        for item in items:
            summary = item.get("summary", "(제목 없음)")
            start = item.get("start", {})
            end = item.get("end", {})
            time_str = format_event_range(start, end)
            parsed_events.append(
                {
                    "id": item.get("id"),
                    "summary": summary,
                    "time_str": time_str,
                    "html_link": item.get("htmlLink", ""),
                    "location": item.get("location", ""),
                    "description": item.get("description", ""),
                }
            )

        return parsed_events

    def delete_event(self, event_id: str, calendar_id: str = "primary") -> bool:
        """일정 ID로 캘린더에서 일정을 삭제한다."""
        service = self.get_service()
        service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
        self.trigger_vault_refresh()
        return True
