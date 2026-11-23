"""google_auth — Google OAuth 2.0 통합 자격증명 및 토큰 관리 모듈.

Google Calendar 및 Gmail API에 필요한 공통 OAuth 2.0 인증, 토큰 저장/갱신,
웹 Redirect Flow 및 계정 정보 조회를 안전하게 처리한다.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from paths import GOOGLE_CREDENTIALS_FILE, GOOGLE_TOKEN_FILE

logger = logging.getLogger("oshyun-bot.google_auth")

# Google OAuth2 응답 시 구글이 openid 등을 자동으로 추가할 때 Scope has changed 예외 방지
os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"

DEFAULT_GOOGLE_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
]


class GoogleAuthManager:
    """Google OAuth 2.0 통합 인증 매니저."""

    def __init__(
        self,
        credentials_file: str | None = None,
        token_file: str | None = None,
        scopes: list[str] | None = None,
    ) -> None:
        self.credentials_file = credentials_file or GOOGLE_CREDENTIALS_FILE
        self.token_file = token_file or GOOGLE_TOKEN_FILE
        self.scopes = list(scopes) if scopes is not None else list(DEFAULT_GOOGLE_SCOPES)

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

            creds = Credentials.from_authorized_user_file(self.token_file, self.scopes)
            if creds and creds.expired and creds.refresh_token:
                logger.info("Google OAuth 토큰 만료됨, 자동 갱신 시도")
                creds.refresh(Request())
                self._save_credentials(creds)
            return creds if (creds and creds.valid) else None
        except Exception as exc:
            logger.warning("Google OAuth 인증 토큰 로드/갱신 실패: %s", exc)
            return None

    def _save_credentials(self, creds: Any) -> None:
        """Credentials 객체를 token.json 파일로 원자적 저장."""
        from json_store import write_json_atomic

        token_dir = os.path.dirname(os.path.abspath(self.token_file))
        os.makedirs(token_dir, exist_ok=True)
        try:
            data = json.loads(creds.to_json())
            write_json_atomic(self.token_file, data, indent=2)
            logger.info("Google OAuth 토큰 저장 완료: %s", self.token_file)
        except Exception:
            with open(self.token_file, "w", encoding="utf-8") as f:
                f.write(creds.to_json())

    def save_credentials_file(self, content: dict | str) -> bool:
        """사용자가 업로드하거나 입력한 client credentials를 파일로 저장."""
        from json_store import write_json_atomic

        cred_dir = os.path.dirname(os.path.abspath(self.credentials_file))
        os.makedirs(cred_dir, exist_ok=True)
        if isinstance(content, str):
            try:
                data = json.loads(content)
            except Exception as exc:
                raise ValueError(f"유효한 JSON 형식이 아닙니다: {exc}") from exc
        elif isinstance(content, dict):
            data = content
        else:
            raise TypeError("content는 dict 또는 str 이어야 합니다.")

        # 최소한 installed 또는 web 키 확인
        if "installed" not in data and "web" not in data:
            raise ValueError("Google OAuth credentials JSON은 'installed' 또는 'web' 키를 포함해야 합니다.")

        write_json_atomic(self.credentials_file, data, indent=2)
        logger.info("Google OAuth 자격증명 파일 저장 완료: %s", self.credentials_file)
        return True

    def get_auth_url(self, redirect_uri: str, state: str | None = None) -> str:
        """OAuth 2.0 웹 브라우저 인증 URL 생성."""
        if not self.has_credentials:
            raise FileNotFoundError(
                f"OAuth 클라이언트 설정 파일이 없습니다: {self.credentials_file}\n"
                "Google Cloud Console에서 OAuth 2.0 클라이언트 ID JSON을 설정해주세요."
            )
        from google_auth_oauthlib.flow import Flow

        flow = Flow.from_client_secrets_file(
            self.credentials_file,
            scopes=self.scopes,
            redirect_uri=redirect_uri,
            autogenerate_code_verifier=False,
        )
        kwargs: dict[str, Any] = {
            "access_type": "offline",
            "include_granted_scopes": "true",
            "prompt": "consent",
        }
        if state:
            kwargs["state"] = state

        auth_url, _ = flow.authorization_url(**kwargs)
        return auth_url

    def exchange_code(self, code: str, redirect_uri: str) -> bool:
        """인증 코드로 토큰을 교환하고 저장."""
        if not self.has_credentials:
            raise FileNotFoundError(f"OAuth 설정 파일이 없습니다: {self.credentials_file}")
        from google_auth_oauthlib.flow import Flow

        flow = Flow.from_client_secrets_file(
            self.credentials_file,
            scopes=self.scopes,
            redirect_uri=redirect_uri,
            autogenerate_code_verifier=False,
        )
        flow.fetch_token(code=code.strip())
        creds = flow.credentials
        if creds and creds.valid:
            self._save_credentials(creds)
            return True
        return False

    def disconnect(self) -> bool:
        """연동 해제: 저장된 token.json 파일을 삭제한다."""
        if os.path.exists(self.token_file):
            try:
                os.remove(self.token_file)
                logger.info("Google OAuth 토큰 파일 삭제 완료: %s", self.token_file)
                return True
            except Exception as exc:
                logger.warning("Google OAuth 토큰 파일 삭제 실패: %s", exc)
                return False
        return False

    def get_account_info(self) -> dict[str, Any]:
        """연동 상태 및 계정 이메일 조회."""
        info: dict[str, Any] = {
            "hasCredentials": self.has_credentials,
            "hasToken": self.has_token,
            "isAuthenticated": False,
            "email": None,
            "scopes": self.scopes,
            "credentialsPath": self.credentials_file,
            "tokenPath": self.token_file,
        }
        creds = self.get_credentials()
        if not creds:
            return info

        info["isAuthenticated"] = True

        # 계정 이메일 확인 시도 (UserInfo API 또는 ID Token)
        try:
            from googleapiclient.discovery import build

            oauth2_service = build("oauth2", "v2", credentials=creds, cache_discovery=False)
            user_info = oauth2_service.userinfo().get().execute()
            if isinstance(user_info, dict) and user_info.get("email"):
                info["email"] = user_info.get("email")
        except Exception as exc:
            logger.debug("Google UserInfo 조회 생략/실패: %s", exc)

        # UserInfo API 실패 시 token.json 파일 내에서 email 필드 확인
        if not info["email"] and self.has_token:
            try:
                with open(self.token_file, "r", encoding="utf-8") as f:
                    token_data = json.load(f)
                    if isinstance(token_data, dict) and token_data.get("email"):
                        info["email"] = token_data.get("email")
            except Exception:
                pass

        return info


# 기본 싱글톤 인스턴스
default_google_auth = GoogleAuthManager()
