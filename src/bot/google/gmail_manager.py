"""gmail_manager — Google Gmail API v1 연동 및 볼트 노트 동기화 매니저.

Google 계정(OAuth 2.0)으로 최근/읽지 않은 이메일을 조회 및 파싱하고,
Obsidian 볼트(_bot/메일.md)에 정제된 마크다운 형태로 동기화한다.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any

from bot_logger import logger, vault_log
from google_auth import GoogleAuthManager, default_google_auth
from json_store import read_json, write_json_atomic
from paths import GMAIL_SYNC_STATE_FILE, GMAIL_VAULT_FILE

_GMAIL_BASE_URL = "https://mail.google.com/mail/u/0/#inbox"


def _decode_base64_payload(data_str: str) -> str:
    """URL-safe base64 인코딩된 메일 본문 문자열을 디코딩한다."""
    if not data_str:
        return ""
    try:
        # 패딩 보정
        padded = data_str + "=" * (-len(data_str) % 4)
        raw_bytes = base64.urlsafe_b64decode(padded.encode("ascii"))
        return raw_bytes.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _extract_body_text(payload: dict[str, Any]) -> str:
    """메일 payload 트리 구조에서 text/plain 또는 text/html 본문 텍스트를 추출한다."""
    if not payload:
        return ""

    mime_type = payload.get("mimeType", "")
    body_data = payload.get("body", {}).get("data", "")

    if mime_type == "text/plain" and body_data:
        return _decode_base64_payload(body_data).strip()

    # multipart 인 경우 parts 순회
    parts = payload.get("parts", [])
    plain_text = ""
    for part in parts:
        part_mime = part.get("mimeType", "")
        if part_mime == "text/plain":
            data = part.get("body", {}).get("data", "")
            if data:
                return _decode_base64_payload(data).strip()
        elif part.get("parts"):
            nested = _extract_body_text(part)
            if nested:
                return nested
        elif part_mime == "text/html" and not plain_text:
            data = part.get("body", {}).get("data", "")
            if data:
                plain_text = _decode_base64_payload(data).strip()

    if plain_text:
        return plain_text
    if body_data:
        return _decode_base64_payload(body_data).strip()
    return ""


def _format_header_date(date_str: str) -> str:
    """RFC 2822 날짜 헤더를 'YYYY-MM-DD HH:MM' 형태의 로컬 시각으로 포맷팅."""
    if not date_str:
        return ""
    try:
        dt = parsedate_to_datetime(date_str)
        local_dt = dt.astimezone()
        return local_dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return date_str


class GmailManager:
    """Gmail API v1 클라이언트 및 볼트 동기화 매니저."""

    def __init__(
        self,
        auth_manager: GoogleAuthManager | None = None,
        vault_file: str | None = None,
        state_file: str | None = None,
    ) -> None:
        self.auth = auth_manager or default_google_auth
        self.vault_file = vault_file or GMAIL_VAULT_FILE
        self.state_file = state_file or GMAIL_SYNC_STATE_FILE

    def is_authenticated(self) -> bool:
        """Google 인증이 유효한지 확인."""
        return self.auth.is_authenticated()

    def get_service(self) -> Any:
        """Gmail API v1 Resource 객체 반환."""
        creds = self.auth.get_credentials()
        if not creds:
            raise PermissionError(
                "Google 계정 연동이 필요합니다. 먼저 웹 설정에서 구글 로그인을 진행해주세요."
            )
        from googleapiclient.discovery import build

        return build("gmail", "v1", credentials=creds, cache_discovery=False)

    def _parse_message_detail(self, detail: dict[str, Any]) -> dict[str, Any]:
        """Gmail 메시지 상세 딕셔너리를 프론트/볼트 렌더링용 구조체로 변환."""
        mid = detail.get("id", "")
        thread_id = detail.get("threadId", "")
        snippet = detail.get("snippet", "").strip()
        labels = detail.get("labelIds", []) or []
        is_unread = "UNREAD" in labels

        payload = detail.get("payload", {})
        headers = payload.get("headers", []) or []
        header_map: dict[str, str] = {}
        for h in headers:
            k = h.get("name", "").lower()
            header_map[k] = h.get("value", "")

        subject = header_map.get("subject") or "(제목 없음)"
        from_str = header_map.get("from") or "(발신자 미상)"
        to_str = header_map.get("to") or ""
        raw_date = header_map.get("date") or ""
        date_str = _format_header_date(raw_date)

        # 본문 스니펫 (snippet이 없으면 body 추출본에서 150자)
        body_text = _extract_body_text(payload)
        preview_text = snippet or (body_text[:150].replace("\n", " ").strip() if body_text else "")

        return {
            "id": mid,
            "threadId": thread_id,
            "subject": subject,
            "from": from_str,
            "to": to_str,
            "date": date_str,
            "rawDate": raw_date,
            "snippet": preview_text,
            "isUnread": is_unread,
            "labels": labels,
            "webLink": f"{_GMAIL_BASE_URL}/{mid}",
        }

    def list_messages(
        self,
        query: str = "newer_than:7d",
        max_results: int = 15,
    ) -> list[dict[str, Any]]:
        """지정된 쿼리로 메일 목록을 조회하고 파싱하여 반환."""
        service = self.get_service()
        res = service.users().messages().list(
            userId="me",
            q=query,
            maxResults=max_results,
        ).execute()

        msg_refs = res.get("messages", [])
        if not msg_refs:
            return []

        results: list[dict[str, Any]] = []
        for ref in msg_refs:
            try:
                detail = service.users().messages().get(
                    userId="me",
                    id=ref["id"],
                    format="full",
                ).execute()
                parsed = self._parse_message_detail(detail)
                results.append(parsed)
            except Exception as exc:
                logger.warning("Gmail 메시지 [%s] 조회 실패: %s", ref.get("id"), exc)

        return results

    def get_unread_count(self) -> int:
        """읽지 않은 메일 개수를 신속하게 조회."""
        try:
            service = self.get_service()
            res = service.users().messages().list(
                userId="me",
                q="is:unread",
                maxResults=1,
            ).execute()
            return int(res.get("resultSizeEstimate", 0))
        except Exception:
            return 0

    def sync_to_vault(
        self,
        query: str = "newer_than:7d",
        max_results: int = 20,
    ) -> dict[str, Any]:
        """최근 메일을 조회하여 볼트 노트(_bot/메일.md)에 동기화 저장한다."""
        if not self.is_authenticated():
            raise PermissionError("Google 계정 인증이 필요합니다.")

        messages = self.list_messages(query=query, max_results=max_results)
        unread_count = sum(1 for m in messages if m.get("isUnread"))
        now = datetime.now().astimezone()
        now_str = now.strftime("%Y-%m-%d %H:%M")
        iso_str = now.isoformat(timespec="seconds")

        # 볼트 마크다운 본문 생성
        lines = [
            "---",
            'title: "수신 메일함"',
            f'updated: "{now_str}"',
            f"unread_count: {unread_count}",
            f"total_count: {len(messages)}",
            f'last_sync: "{iso_str}"',
            "---",
            "",
            "# 📬 수신 메일함",
            "",
            f"> 💡 **동기화 시각**: {now_str}  ",
            f"> 📊 **현황**: 총 **{len(messages)}건** (안읽은 메일 **{unread_count}건**)  ",
            f"> 🔗 **Gmail 열기**: [{_GMAIL_BASE_URL}]({_GMAIL_BASE_URL})",
            "",
            "## 📩 최근 및 미열람 메일 목록",
            "",
        ]

        if not messages:
            lines.extend([
                "_조회된 최근 메일이 없습니다._",
                "",
            ])
        else:
            # 상단에 요약 테이블
            lines.extend([
                "| 상태 | 발신자 | 제목 | 수신일시 |",
                "| :---: | :--- | :--- | :--- |",
            ])
            for m in messages:
                status_icon = "🔵 **미열람**" if m.get("isUnread") else "⚪ 읽음"
                sender = m.get("from", "").replace("|", "-")
                # 발신자 이름만 추출 시도 (예: 홍길동 <hong@...> -> 홍길동)
                if "<" in sender:
                    sender = sender.split("<")[0].strip() or sender
                title = m.get("subject", "(제목 없음)").replace("|", "-")
                link = m.get("webLink", "")
                date_s = m.get("date", "")
                lines.append(f"| {status_icon} | {sender} | [{title}]({link}) | {date_s} |")

            lines.extend(["", "---", "", "## 📋 상세 메일 요약", ""])
            for idx, m in enumerate(messages, start=1):
                badge = "🔵 [안읽음]" if m.get("isUnread") else "⚪ [읽음]"
                title = m.get("subject", "(제목 없음)")
                link = m.get("webLink", "")
                sender = m.get("from", "")
                date_s = m.get("date", "")
                snippet = m.get("snippet", "")

                lines.extend([
                    f"### {idx}. {badge} [{title}]({link})",
                    f"- **발신자**: `{sender}`",
                    f"- **수신시각**: {date_s}",
                ])
                if snippet:
                    lines.extend([
                        f"- **내용 미리보기**: {snippet}",
                    ])
                lines.append("")

        content = "\n".join(lines)

        # 볼트 디렉토리 생성 및 원자적 파일 저장
        vault_dir = os.path.dirname(os.path.abspath(self.vault_file))
        os.makedirs(vault_dir, exist_ok=True)
        tmp_vault = f"{self.vault_file}.tmp.{os.getpid()}"
        with open(tmp_vault, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_vault, self.vault_file)

        # 동기화 상태 파일 업데이트
        state_data = {
            "lastSync": iso_str,
            "lastSyncDisplay": now_str,
            "totalCount": len(messages),
            "unreadCount": unread_count,
            "vaultFile": self.vault_file,
            "messageIds": [m["id"] for m in messages],
        }
        write_json_atomic(self.state_file, state_data, indent=2)

        vault_log("📧 Gmail 동기화", f"메일 {len(messages)}건 수신 완료 (안읽음 {unread_count}건)")
        logger.info(
            "Gmail 동기화 완료: 총 %d건 (미열람 %d건) -> %s",
            len(messages),
            unread_count,
            self.vault_file,
        )

        return {
            "ok": True,
            "totalCount": len(messages),
            "unreadCount": unread_count,
            "vaultFile": self.vault_file,
            "lastSync": iso_str,
            "messages": messages,
        }

    def get_sync_status(self) -> dict[str, Any]:
        """동기화 상태 정보를 반환."""
        state = read_json(self.state_file, {}, expect_type=dict)
        return {
            "isAuthenticated": self.is_authenticated(),
            "vaultFile": self.vault_file,
            "vaultFileExists": os.path.isfile(self.vault_file),
            "lastSync": state.get("lastSync"),
            "lastSyncDisplay": state.get("lastSyncDisplay"),
            "totalCount": state.get("totalCount", 0),
            "unreadCount": state.get("unreadCount", 0),
        }


# 기본 싱글톤 인스턴스
default_gmail_manager = GmailManager()
