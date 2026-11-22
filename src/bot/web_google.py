"""web_google — Google OAuth2 인증 및 Gmail 연동 REST API 핸들러.

Google 계정 연동(OAuth2 URL 생성, Callback 수신, 수동 코드 인증, 계정 해제) 및
Gmail 수신/볼트 동기화 엔드포인트를 제공한다.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from aiohttp import web

from bot_logger import logger
from gmail_manager import default_gmail_manager
from google_auth import default_google_auth
from schedules import ensure_default_gmail_schedule

logger = logging.getLogger("oshyun-bot.web_google")


def _build_default_redirect_uri(request: web.Request) -> str:
    """요청의 호스트와 스킴을 분석하여 기본 OAuth 콜백 URL을 생성한다."""
    scheme = request.headers.get("X-Forwarded-Proto") or request.scheme or "http"
    host = request.headers.get("X-Forwarded-Host") or request.host
    return f"{scheme}://{host}/api/google/auth/callback"


async def handle_google_status(_request: web.Request) -> web.Response:
    """GET /api/google/status — 구글 계정 연동 및 Gmail 동기화 상태 반환."""
    account_info = default_google_auth.get_account_info()
    gmail_status = default_gmail_manager.get_sync_status()

    return web.json_response({
        "status": "ok",
        "account": account_info,
        "gmail": gmail_status,
    })


async def handle_google_credentials(request: web.Request) -> web.Response:
    """POST /api/google/credentials — Google OAuth client credentials JSON 저장."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    content = body.get("credentials") or body
    if not content:
        return web.json_response(
            {"status": "error", "error": "자격증명(credentials) JSON 데이터가 필요합니다."},
            status=400,
        )

    try:
        default_google_auth.save_credentials_file(content)
        return web.json_response({
            "status": "ok",
            "message": "구글 OAuth 자격증명 파일이 성공적으로 저장되었습니다.",
            "account": default_google_auth.get_account_info(),
        })
    except Exception as exc:
        logger.warning("Google 자격증명 저장 실패: %s", exc)
        return web.json_response(
            {"status": "error", "error": f"자격증명 저장 실패: {exc}"},
            status=400,
        )


async def handle_google_auth_url(request: web.Request) -> web.Response:
    """GET /api/google/auth/url — Google OAuth 로그인 브라우저 URL 생성."""
    if not default_google_auth.has_credentials:
        return web.json_response(
            {
                "status": "error",
                "error": "OAuth 클라이언트 자격증명(credentials.json)이 설정되지 않았습니다. 먼저 자격증명을 등록해주세요.",
            },
            status=400,
        )

    redirect_uri = request.query.get("redirect_uri", "").strip() or _build_default_redirect_uri(request)
    state = request.query.get("state", "").strip() or None

    try:
        auth_url = default_google_auth.get_auth_url(redirect_uri=redirect_uri, state=state)
        return web.json_response({
            "status": "ok",
            "authUrl": auth_url,
            "redirectUri": redirect_uri,
        })
    except Exception as exc:
        logger.warning("Google Auth URL 생성 실패: %s", exc)
        return web.json_response(
            {"status": "error", "error": f"인증 URL 생성 중 오류가 발생했습니다: {exc}"},
            status=500,
        )


async def handle_google_auth_callback(request: web.Request) -> web.Response:
    """GET /api/google/auth/callback — Google OAuth redirect 수신 및 토큰 교환."""
    error = request.query.get("error")
    if error:
        err_msg = f"구글 인증 거부 또는 오류: {error}"
        html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>인증 실패</title></head>
<body style="font-family:sans-serif;text-align:center;padding:50px;background:#18181b;color:#fff;">
  <h2>⚠️ 인증 실패</h2>
  <p>{err_msg}</p>
  <button onclick="window.close()" style="padding:10px 20px;margin-top:20px;cursor:pointer;">창 닫기</button>
</body>
</html>"""
        return web.Response(text=html, content_type="text/html", status=400)

    code = request.query.get("code", "").strip()
    if not code:
        return web.Response(text="인증 코드(code)가 전달되지 않았습니다.", status=400)

    redirect_uri = _build_default_redirect_uri(request)
    try:
        success = default_google_auth.exchange_code(code, redirect_uri)
    except Exception as exc:
        logger.warning("Google OAuth Callback 토큰 교환 실패: %s", exc)
        success = False

    if success:
        account_info = default_google_auth.get_account_info()
        email = account_info.get("email") or "인증 완료"
        html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>구글 연동 성공</title>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      text-align: center;
      padding: 60px 20px;
      background: #09090b;
      color: #fafafa;
    }}
    .card {{
      max-width: 440px;
      margin: 0 auto;
      background: #18181b;
      border: 1px solid #27272a;
      border-radius: 16px;
      padding: 32px 24px;
      box-shadow: 0 10px 30px rgba(0,0,0,0.5);
    }}
    .icon {{ font-size: 48px; margin-bottom: 16px; }}
    h2 {{ margin-bottom: 8px; color: #4ade80; }}
    p {{ color: #a1a1aa; font-size: 14px; line-height: 1.6; }}
    .email {{ color: #60a5fa; font-weight: bold; margin: 12px 0; word-break: break-all; }}
    .btn {{
      display: inline-block;
      margin-top: 20px;
      padding: 10px 24px;
      background: #3b82f6;
      color: #fff;
      border-radius: 8px;
      text-decoration: none;
      font-size: 14px;
      font-weight: 500;
      border: none;
      cursor: pointer;
    }}
  </style>
  <script>
    if (window.opener) {{
      try {{
        window.opener.postMessage({{ type: 'google_auth_success', email: '{email}' }}, '*');
        setTimeout(() => window.close(), 1200);
      }} catch(e) {{}}
    }}
  </script>
</head>
<body>
  <div class="card">
    <div class="icon">✅</div>
    <h2>구글 계정 연동 완료</h2>
    <p>성공적으로 연동되었습니다.</p>
    <div class="email">{email}</div>
    <p>이 창은 잠시 후 자동으로 닫힙니다.</p>
    <button class="btn" onclick="window.close(); location.href='/';">창 닫기</button>
  </div>
</body>
</html>"""
        return web.Response(text=html, content_type="text/html")
    else:
        html = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>토큰 교환 실패</title></head>
<body style="font-family:sans-serif;text-align:center;padding:50px;background:#18181b;color:#fff;">
  <h2>⚠️ 토큰 교환 실패</h2>
  <p>인증 코드로 액세스 토큰을 발급받지 못했습니다. 다시 시도해 주세요.</p>
  <button onclick="window.close()" style="padding:10px 20px;margin-top:20px;cursor:pointer;">창 닫기</button>
</body>
</html>"""
        return web.Response(text=html, content_type="text/html", status=500)


async def handle_google_auth_code(request: web.Request) -> web.Response:
    """POST /api/google/auth/code — 사용자가 직접 입력한 인증 코드로 토큰 교환."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    code = str(body.get("code") or "").strip()
    redirect_uri = str(body.get("redirectUri") or "").strip() or _build_default_redirect_uri(request)

    if not code:
        return web.json_response(
            {"status": "error", "error": "인증 코드(code)를 입력해 주세요."},
            status=400,
        )

    try:
        success = default_google_auth.exchange_code(code, redirect_uri)
        if success:
            return web.json_response({
                "status": "ok",
                "message": "구글 계정이 성공적으로 연동되었습니다.",
                "account": default_google_auth.get_account_info(),
            })
        return web.json_response(
            {"status": "error", "error": "인증 코드가 올바르지 않거나 만료되었습니다."},
            status=400,
        )
    except Exception as exc:
        logger.warning("수동 코드 인증 실패: %s", exc)
        return web.json_response(
            {"status": "error", "error": f"인증 처리 중 오류가 발생했습니다: {exc}"},
            status=400,
        )


async def handle_google_disconnect(_request: web.Request) -> web.Response:
    """POST /api/google/disconnect — 구글 계정 연동 해제 (토큰 삭제)."""
    default_google_auth.disconnect()
    return web.json_response({
        "status": "ok",
        "message": "구글 계정 연동이 해제되었습니다.",
        "account": default_google_auth.get_account_info(),
    })


async def handle_gmail_status(_request: web.Request) -> web.Response:
    """GET /api/gmail/status — Gmail 동기화 상태 반환."""
    status = default_gmail_manager.get_sync_status()
    return web.json_response({"status": "ok", "sync": status})


async def handle_gmail_sync(request: web.Request) -> web.Response:
    """POST /api/gmail/sync — 즉시 메일 수신 및 볼트 동기화 실행."""
    if not default_google_auth.is_authenticated():
        return web.json_response(
            {
                "status": "error",
                "error": "Google 계정이 연동되지 않았습니다. 먼저 웹 설정에서 구글 로그인을 진행해주세요.",
            },
            status=401,
        )

    try:
        body = await request.json()
    except Exception:
        body = {}

    query = str(body.get("query") or "newer_than:7d").strip()
    max_results = int(body.get("maxResults") or 20)

    try:
        result = default_gmail_manager.sync_to_vault(query=query, max_results=max_results)
        return web.json_response({"status": "ok", "result": result})
    except Exception as exc:
        logger.warning("Gmail 즉시 동기화 실패: %s", exc)
        return web.json_response(
            {"status": "error", "error": f"메일 동기화 중 오류가 발생했습니다: {exc}"},
            status=500,
        )


async def handle_schedules_ensure_gmail(request: web.Request) -> web.Response:
    """POST /api/schedules/ensure-gmail — Gmail 정기 수신 스케줄 등록/확보."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    cron = str(body.get("cron") or "0 8 * * *").strip()
    entry = ensure_default_gmail_schedule(cron=cron)
    if entry:
        return web.json_response({
            "status": "ok",
            "created": True,
            "item": entry,
            "message": "Gmail 정기 수신 스케줄이 성공적으로 등록되었습니다.",
        })
    return web.json_response({
        "status": "ok",
        "created": False,
        "message": "이미 Gmail 정기 수신 스케줄이 등록되어 있습니다.",
    })
