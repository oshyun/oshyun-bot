"""web_auth — 웹 게이트웨이 비밀 토큰 인증, 세션 쿠키 서명 및 무차별 대입 방어 모듈."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import secrets
import sys
import time

from aiohttp import web

from bot_logger import logger

# 보안 토큰 게이트웨이 설정
WEB_AUTH_REQUIRED = os.getenv("WEB_AUTH_REQUIRED", os.getenv("VOICE_WEB_AUTH_REQUIRED", "1")).strip() != "0"
WEB_TOKEN = os.getenv("WEB_TOKEN", os.getenv("VOICE_WEB_TOKEN", "")).strip()
if not WEB_TOKEN:
    WEB_TOKEN = secrets.token_urlsafe(24)
    logger.warning("⚠️ [보안 게이트웨이] WEB_TOKEN이 지정되지 않아 임시 보안 토큰이 발급되었습니다: %s", WEB_TOKEN)

AUTH_SESSION_SECRET = (
    os.getenv("WEB_SESSION_SECRET", os.getenv("VOICE_WEB_SESSION_SECRET", "")).strip()
    or hmac.new(WEB_TOKEN.encode("utf-8"), b"oshyun-session-secret-salt", hashlib.sha256).hexdigest()
)
AUTH_COOKIE_NAME = "oshyun_auth_session"
AUTH_SESSION_TTL_SECONDS = int(os.getenv("WEB_SESSION_TTL", os.getenv("VOICE_WEB_SESSION_TTL", str(30 * 86400))))
AUTH_MAX_FAILURES = int(os.getenv("WEB_MAX_FAILURES", os.getenv("VOICE_WEB_MAX_FAILURES", "5")))
AUTH_LOCKOUT_WINDOW_SECONDS = int(os.getenv("WEB_LOCKOUT_WINDOW", os.getenv("VOICE_WEB_LOCKOUT_WINDOW", "600")))  # 10분
AUTH_LOCKOUT_DURATION_SECONDS = int(os.getenv("WEB_LOCKOUT_DURATION", os.getenv("VOICE_WEB_LOCKOUT_DURATION", "900")))  # 15분
AUTH_DELAY_SECONDS = float(os.getenv("WEB_AUTH_DELAY", os.getenv("VOICE_WEB_AUTH_DELAY", "0.5")))

# IP별 실패 이력 {ip: [실패타임스탬프, ...]}
_failed_auth_attempts: dict[str, list[float]] = {}


def _get_mod_val(attr_name: str, fallback: any) -> any:
    mod = sys.modules.get("web_server")
    if mod and hasattr(mod, attr_name):
        return getattr(mod, attr_name)
    return fallback


def _get_client_ip(request: web.Request) -> str:
    """클라이언트의 실제 IP 주소를 추출한다 (프록시 헤더 고려)."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        client_ip = forwarded.split(",")[0].strip()
        if client_ip:
            return client_ip
    return request.remote or "unknown"


def _is_ip_locked(ip: str) -> bool:
    """해당 IP가 브루트포스 방어 정책으로 차단 상태인지 검사한다."""
    now = time.time()
    history = _failed_auth_attempts.get(ip, [])
    # 윈도우 내 실패 이력만 보존
    valid_history = [t for t in history if now - t < AUTH_LOCKOUT_WINDOW_SECONDS]
    _failed_auth_attempts[ip] = valid_history
    if len(valid_history) >= AUTH_MAX_FAILURES:
        last_failed = max(valid_history)
        if now - last_failed < AUTH_LOCKOUT_DURATION_SECONDS:
            return True
    return False


def _record_failed_attempt(ip: str) -> None:
    """인증 실패 기록을 추가한다."""
    _failed_auth_attempts.setdefault(ip, []).append(time.time())


def _reset_failed_attempts(ip: str) -> None:
    """인증 성공 시 해당 IP의 실패 기록을 초기화한다."""
    _failed_auth_attempts.pop(ip, None)


def _verify_token(token: str | None) -> bool:
    """상수 시간(constant-time) 비교로 비밀 토큰을 검증한다."""
    curr_token = _get_mod_val("WEB_TOKEN", WEB_TOKEN)
    if not token or not curr_token:
        return False
    try:
        return hmac.compare_digest(token.strip().encode("utf-8"), curr_token.encode("utf-8"))
    except Exception:
        return False


def _create_session_cookie_value() -> str:
    """암호학적으로 서명된 세션 쿠키 문자열을 생성한다."""
    curr_secret = _get_mod_val("AUTH_SESSION_SECRET", AUTH_SESSION_SECRET)
    ts = str(int(time.time()))
    sig = hmac.new(
        curr_secret.encode("utf-8"),
        ts.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{ts}.{sig}"


def _verify_session_cookie(cookie_val: str | None) -> bool:
    """세션 쿠키의 서명 및 만료 시각을 검증한다."""
    if not cookie_val or "." not in cookie_val:
        return False
    ts_str, sig = cookie_val.split(".", 1)
    try:
        ts = int(ts_str)
    except ValueError:
        return False
    now = time.time()
    # 30일 초과 만료 또는 미래 시각 방어
    if now - ts > AUTH_SESSION_TTL_SECONDS or ts > now + 60:
        return False
    curr_secret = _get_mod_val("AUTH_SESSION_SECRET", AUTH_SESSION_SECRET)
    expected_sig = hmac.new(
        curr_secret.encode("utf-8"),
        ts_str.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(sig, expected_sig)


def _is_authenticated(request: web.Request) -> bool:
    """쿠키, Bearer 헤더, 쿼리 파라미터를 통해 요청이 인증되었는지 판별한다."""
    if not _get_mod_val("WEB_AUTH_REQUIRED", WEB_AUTH_REQUIRED):
        return True

    # 1. 세션 쿠키 확인
    cookie_val = request.cookies.get(AUTH_COOKIE_NAME)
    if _verify_session_cookie(cookie_val):
        return True

    # 2. Authorization 헤더 확인 (Bearer <token>)
    auth_header = request.headers.get("Authorization", "").strip()
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
        if _verify_token(token):
            return True

    # 3. WebSocket 및 직접 링크용 쿼리 파라미터 확인 (?token=...)
    query_token = request.query.get("token", "").strip()
    if query_token and _verify_token(query_token):
        return True

    return False


def render_login_page(error_message: str | None = None) -> str:
    """독립적이고 안전한 다크 테마 비밀 토큰 로그인 페이지 HTML을 렌더링한다."""
    error_html = (
        f'<div class="error-banner">{error_message}</div>' if error_message else ""
    )
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <link rel="icon" type="image/svg+xml" href="/favicon.svg">
  <link rel="alternate icon" href="/favicon.ico">
  <link rel="apple-touch-icon" href="/icons/icon-192.png">
  <title>oshyun-bot Gateway</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: linear-gradient(135deg, #F0FDF4 0%, #F8FAFC 100%);
      color: #0F172A;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Apple SD Gothic Neo", "Pretendard", sans-serif;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 16px;
    }}
    .auth-card {{
      width: 100%;
      max-width: 400px;
      background: #FFFFFF;
      border: 1px solid #E2E8F0;
      border-radius: 20px;
      padding: 36px 28px;
      box-shadow: 0 20px 40px rgba(16, 185, 129, 0.08), 0 2px 10px rgba(0, 0, 0, 0.04);
      text-align: center;
    }}
    .shield-icon {{
      width: 56px;
      height: 56px;
      margin: 0 auto 16px;
      background: #161B26;
      border: 1px solid rgba(122, 162, 247, 0.28);
      border-radius: 16px;
      display: flex;
      align-items: center;
      justify-content: center;
      box-shadow: 0 8px 20px rgba(15, 19, 29, 0.24);
    }}
    .shield-icon svg {{
      width: 30px;
      height: 30px;
      fill: none;
      stroke: #7AA2F7;
      stroke-width: 1.5;
      stroke-linecap: round;
      stroke-linejoin: round;
    }}
    h1 {{
      font-size: 20px;
      font-weight: 700;
      letter-spacing: -0.5px;
      margin-bottom: 8px;
      color: #0F172A;
    }}
    p.desc {{
      font-size: 13px;
      color: #64748B;
      line-height: 1.5;
      margin-bottom: 24px;
    }}
    .error-banner {{
      background: rgba(239, 68, 68, 0.1);
      border: 1px solid rgba(239, 68, 68, 0.25);
      color: #DC2626;
      font-size: 13px;
      padding: 10px 14px;
      border-radius: 10px;
      margin-bottom: 18px;
      text-align: left;
    }}
    .input-group {{
      margin-bottom: 20px;
      text-align: left;
    }}
    label {{
      display: block;
      font-size: 12px;
      font-weight: 600;
      color: #475569;
      margin-bottom: 6px;
      margin-left: 2px;
    }}
    input[type="password"] {{
      width: 100%;
      background: #F8FAFC;
      border: 1px solid #CBD5E1;
      border-radius: 12px;
      color: #0F172A;
      font-size: 15px;
      padding: 14px 16px;
      outline: none;
      transition: all 0.2s ease;
      letter-spacing: 2px;
    }}
    input[type="password"]:focus {{
      border-color: #10B981;
      background: #FFFFFF;
      box-shadow: 0 0 0 3px rgba(16, 185, 129, 0.2);
    }}
    button.submit-btn {{
      width: 100%;
      background: linear-gradient(135deg, #10B981 0%, #059669 100%);
      color: #FFFFFF;
      border: none;
      border-radius: 12px;
      padding: 14px;
      font-size: 15px;
      font-weight: 600;
      cursor: pointer;
      box-shadow: 0 6px 16px rgba(16, 185, 129, 0.3);
      transition: transform 0.1s ease, filter 0.2s ease;
    }}
    button.submit-btn:hover {{
      filter: brightness(1.08);
      transform: translateY(-1px);
    }}
    button.submit-btn:active {{
      transform: scale(0.98);
    }}
    .footer-note {{
      margin-top: 24px;
      font-size: 11px;
      color: #94A3B8;
      line-height: 1.4;
    }}
  </style>
</head>
<body>
  <div class="auth-card">
    <div class="shield-icon">
      <svg viewBox="0 0 16 16">
        <circle cx="8" cy="2" r="0.8"/>
        <line x1="8" y1="2.9" x2="8" y2="5"/>
        <rect x="2.5" y="5" width="11" height="8.5" rx="1.5"/>
        <circle cx="6" cy="9" r="0.8"/>
        <circle cx="10" cy="9" r="0.8"/>
        <line x1="5.5" y1="13.5" x2="5.5" y2="15"/>
        <line x1="10.5" y1="13.5" x2="10.5" y2="15"/>
      </svg>
    </div>
    <h1>oshyun-bot Gateway</h1>
    <p class="desc">외부 네트워크 보호 게이트웨이입니다.<br>접속을 허용하려면 보안 비밀 토큰을 입력하세요.</p>
    {error_html}
    <form id="auth-form" method="POST" action="/login">
      <div class="input-group">
        <label for="token">SECRET TOKEN</label>
        <input type="password" id="token" name="token" required autofocus placeholder="••••••••••••••••" autocomplete="current-password" />
      </div>
      <button type="submit" id="submit-btn" class="submit-btn">인증 및 접속</button>
    </form>
    <div class="footer-note">
      🔒 5회 연속 인증 실패 시 IP가 15분간 차단됩니다.<br>보안 서명 세션이 유지되는 동안 재인증이 필요하지 않습니다.
    </div>
  </div>
  <script>
    const form = document.getElementById('auth-form');
    const submitBtn = document.getElementById('submit-btn');
    form.addEventListener('submit', async (e) => {{
      const token = document.getElementById('token').value.trim();
      if (!token) return;
      if (window.fetch) {{
        e.preventDefault();
        submitBtn.disabled = true;
        submitBtn.innerText = '인증 확인 중...';
        try {{
          const res = await fetch('/login', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ token }})
          }});
          const data = await res.json();
          if (res.ok && data.ok) {{
            window.location.href = data.redirect || '/';
          }} else {{
            alert(data.error || '인증에 실패했습니다.');
            submitBtn.disabled = false;
            submitBtn.innerText = '인증 및 접속';
          }}
        }} catch (err) {{
          form.submit();
        }}
      }}
    }});
  </script>
</body>
</html>"""


def _redirect_response(location: str) -> web.Response:
    """표준 302 리다이렉트 응답 객체를 생성한다."""
    return web.Response(status=302, headers={"Location": location})


async def handle_login_page(request: web.Request) -> web.Response:
    """GET /login — 이미 인증된 경우 메인으로 이동하고, 미인증 시 로그인 화면을 렌더링한다."""
    if _is_authenticated(request):
        return _redirect_response("/")
    return web.Response(text=render_login_page(), content_type="text/html")


async def handle_login_submit(request: web.Request) -> web.Response:
    """POST /login — 비밀 토큰을 검증하고 암호 서명 세션 쿠키를 발급한다."""
    client_ip = _get_client_ip(request)

    if _is_ip_locked(client_ip):
        msg = "인증 실패 횟수 초과(5회)로 접속이 차단되었습니다. 15분 후 다시 시도해 주세요."
        if request.headers.get("Content-Type", "").startswith("application/json"):
            return web.json_response({"ok": False, "error": msg}, status=429)
        return web.Response(text=render_login_page(error_message=msg), content_type="text/html", status=429)

    delay = _get_mod_val("AUTH_DELAY_SECONDS", AUTH_DELAY_SECONDS)
    if delay > 0:
        await asyncio.sleep(delay)

    token: str | None = None
    is_json = request.headers.get("Content-Type", "").startswith("application/json")
    if is_json:
        try:
            payload = await request.json()
            token = payload.get("token")
        except Exception:
            token = None
    else:
        try:
            post_data = await request.post()
            token = str(post_data.get("token", "")).strip() or None
        except Exception:
            token = None

    if _verify_token(token):
        _reset_failed_attempts(client_ip)
        cookie_val = _create_session_cookie_value()
        if is_json:
            response = web.json_response({"ok": True, "redirect": "/"})
        else:
            response = _redirect_response("/")
        response.set_cookie(
            AUTH_COOKIE_NAME,
            cookie_val,
            max_age=AUTH_SESSION_TTL_SECONDS,
            httponly=True,
            samesite="Lax",
            path="/",
        )
        logger.info("보안 게이트웨이 인증 성공 (IP: %s)", client_ip)
        return response

    _record_failed_attempt(client_ip)
    logger.warning("보안 게이트웨이 인증 실패 (IP: %s)", client_ip)
    err_text = "비밀 토큰이 올바르지 않습니다."
    if is_json:
        return web.json_response({"ok": False, "error": err_text}, status=401)
    return web.Response(text=render_login_page(error_message=err_text), content_type="text/html", status=401)


async def handle_logout(request: web.Request) -> web.Response:
    """POST /logout — 세션 쿠키를 무효화하고 로그인 페이지로 리다이렉트한다."""
    is_json = request.headers.get("Content-Type", "").startswith("application/json")
    if is_json:
        response = web.json_response({"ok": True, "redirect": "/login"})
    else:
        response = _redirect_response("/login")
    response.del_cookie(AUTH_COOKIE_NAME, path="/")
    return response


def _is_local_loopback_callback(request: web.Request) -> bool:
    """X-Forwarded-For 가 없는 순수 로컬 루프백의 태스크 이벤트 콜백 요청인지 판별한다."""
    if request.method != "POST" or request.path != "/v1/callbacks/task-events":
        return False
    if request.headers.get("X-Forwarded-For") or request.headers.get("X-Real-IP"):
        return False
    remote = (request.remote or "").strip()
    return remote in ("127.0.0.1", "::1", "localhost")


@web.middleware
async def auth_middleware(request: web.Request, handler):
    """보안 게이트웨이 미들웨어: 비인증 요청을 엄격히 차단한다."""
    # 1. OPTIONS 프리플라이트는 통과
    if request.method == "OPTIONS":
        return await handler(request)

    # 2. 공개 허용 엔드포인트 (로그인, 정적 PWA 메타데이터, 로컬 루프백 래퍼 콜백)
    if (
        request.path in ("/login", "/logout", "/favicon.ico", "/favicon.svg", "/manifest.json", "/sw.js")
        or request.path.startswith("/icons/")
        or _is_local_loopback_callback(request)
    ):
        return await handler(request)

    # 3. 인증 여부 검증
    if _is_authenticated(request):
        response = await handler(request)
        if _get_mod_val("WEB_AUTH_REQUIRED", WEB_AUTH_REQUIRED) and request.query.get("token"):
            query_token = request.query.get("token", "").strip()
            if _verify_token(query_token) and not _verify_session_cookie(request.cookies.get(AUTH_COOKIE_NAME)):
                try:
                    cookie_val = _create_session_cookie_value()
                    response.set_cookie(
                        AUTH_COOKIE_NAME,
                        cookie_val,
                        max_age=AUTH_SESSION_TTL_SECONDS,
                        httponly=True,
                        samesite="Lax",
                        path="/",
                    )
                except Exception:
                    pass
        return response

    # 4. 미인증 차단
    if request.path == "/ws":
        return web.Response(status=401, text="Unauthorized: Token or session required")

    is_api = (
        request.path.startswith("/api/")
        or request.path.startswith("/v1/")
        or "application/json" in request.headers.get("Accept", "")
    )
    if is_api:
        return web.json_response(
            {"error": "Unauthorized", "message": "비밀 토큰 인증이 필요합니다."},
            status=401,
        )

    return _redirect_response("/login")


@web.middleware
async def cors_middleware(request: web.Request, handler):
    """Flutter Web 개발 서버 및 브라우저 클라이언트에서 CORS 제약 없이 호출할 수 있게 허용한다."""
    if request.method == "OPTIONS":
        response = web.Response(status=204)
    else:
        response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response
