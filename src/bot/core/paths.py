"""paths — 운영 산출물 경로 해석(한 곳에 모아 분리 가능하게).

봇이 운영 중 만드는 파일(세션·첨부·로그; PID·크래시 stderr 는 셸 스크립트가 관리)은 모두 DATA_DIR
한 곳에 모은다. `DATA_DIR` 환경변수로 리포 밖 임의 위치로 분리(relocate)할 수 있고,
기본값은 `<리포 루트>/data` 다. 셸의 scripts/common.sh 도 같은 규칙을 따른다.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

# 이 모듈은 src/bot/core/ 하위 → 네 단계 위가 리포 루트.
_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
ROOT_DIR = _ROOT_DIR
# 기본값을 먼저 채우고 .env 가 전부 덮어쓴다 — 셸 common.sh 의 source 순서와 같은 단순 규칙.
# DATA_DIR 로 경로가 달라질 수 있으므로 경로 계산 전에 로드한다.
load_dotenv(os.path.join(_ROOT_DIR, ".env.defaults"))
load_dotenv(os.path.join(_ROOT_DIR, ".env"), override=True)

# 도메인 기반 환경변수(DOMAIN__KEY)와 레거시 환경변수(KEY) 간 양방향 호환 동기화
from bot.core.env_compat import sync_domain_env
sync_domain_env()


def setup_ssl_certificates() -> str | None:
    """macOS Python 환경에서 [SSL: CERTIFICATE_VERIFY_FAILED] 오류를 방지하기 위해
    SSL_CERT_FILE 및 REQUESTS_CA_BUNDLE 환경변수가 비어있거나 무효하면
    certifi 번들 또는 시스템 CA 경로를 자동으로 찾아 주입한다.
    """
    current_ssl_file = os.environ.get("SSL_CERT_FILE", "").strip()
    if current_ssl_file and os.path.isfile(current_ssl_file):
        return current_ssl_file

    ca_candidates: list[str] = []
    try:
        import certifi
        cp = certifi.where()
        if cp and os.path.isfile(cp):
            ca_candidates.append(cp)
    except Exception:
        pass

    ca_candidates.extend([
        "/etc/ssl/cert.pem",
        "/private/etc/ssl/cert.pem",
        "/etc/pki/tls/certs/ca-bundle.crt",
        "/etc/ssl/certs/ca-certificates.crt",
    ])

    for ca_path in ca_candidates:
        if os.path.isfile(ca_path):
            os.environ["SSL_CERT_FILE"] = ca_path
            curr_req = os.environ.get("REQUESTS_CA_BUNDLE", "").strip()
            if not curr_req or not os.path.isfile(curr_req):
                os.environ["REQUESTS_CA_BUNDLE"] = ca_path
            curr_curl = os.environ.get("CURL_CA_BUNDLE", "").strip()
            if not curr_curl or not os.path.isfile(curr_curl):
                os.environ["CURL_CA_BUNDLE"] = ca_path
            return ca_path
    return None


setup_ssl_certificates()

# 운영 산출물 디렉토리. DATA_DIR 로 다른 위치로 분리 가능(기본: <리포 루트>/data).
DATA_DIR = os.path.abspath(
    os.path.expanduser(os.getenv("DATA_DIR", "").strip() or os.path.join(_ROOT_DIR, "data"))
)
# 로그·PID — 봇 동작에 무관한 관찰용 산출물.
LOGS_DIR = os.path.join(DATA_DIR, "logs")
# 영속 상태 — 재시작 후 봇이 읽어야 정상 동작하는 파일.
STATE_DIR = os.path.join(DATA_DIR, "state")

ATTACHED_FILES_DIR = os.path.join(DATA_DIR, "attached_files")
# 세션 응답 JSONL 로그 (최근 100 세션, 세션당 전체 스트림 이벤트).
SESSION_LOG_FILE = os.path.join(LOGS_DIR, "session_log.jsonl")
# 웹 서버 런타임 로그. web_server.py 의 capped 핸들러가 직접 기록(최근 100줄).
WEB_LOG_FILE = os.path.join(LOGS_DIR, "web-server.log")
# 봇 런타임 설정 영속화 (session_id, thinking, effort 등).
BOT_SETTINGS_FILE = os.path.join(STATE_DIR, "bot_settings.json")
# 에이전트 시스템 지침 디렉토리 (common 및 모드별 AGENTS.md).
INSTRUCTIONS_DIR = os.path.join(_ROOT_DIR, "src", "bot", "instructions")
# 사용자 커스텀 시스템 지침 (common, web 등 오버라이드) 디렉토리.
CUSTOM_INSTRUCTIONS_DIR = os.path.join(STATE_DIR, "custom_instructions")

# Let's Encrypt / SSL 기본 경로
LETSENCRYPT_DIR = os.path.join(DATA_DIR, "letsencrypt")
_DUCKDNS_DOMAIN = os.getenv("DUCKDNS_DOMAIN", "").strip() or "localhost"
DEFAULT_SSL_CERT_FILE = os.path.join(LETSENCRYPT_DIR, "config", "live", f"{_DUCKDNS_DOMAIN}.duckdns.org", "fullchain.pem")
DEFAULT_SSL_KEY_FILE = os.path.join(LETSENCRYPT_DIR, "config", "live", f"{_DUCKDNS_DOMAIN}.duckdns.org", "privkey.pem")


def _resolve_workdir() -> str:
    """볼트(작업공간) 경로 해석. AGENT_WORKDIR -> AGENT__WORKDIR -> VAULT_DIR -> CLAUDE_WORKDIR -> 기본값 ~"""
    for key in ("AGENT_WORKDIR", "AGENT__WORKDIR", "VAULT_DIR", "CLAUDE_WORKDIR"):
        v = (os.getenv(key) or "").strip()
        if v and v != "~":
            return os.path.expanduser(v)
    raw = (
        os.getenv("AGENT_WORKDIR")
        or os.getenv("AGENT__WORKDIR")
        or os.getenv("VAULT_DIR")
        or os.getenv("CLAUDE_WORKDIR")
        or ""
    ).strip()
    return os.path.expanduser(raw or "~")


def _resolve_codedir() -> str | None:
    """코드 작업공간 경로 해석. AGENT_CODEDIR -> AGENT__CODEDIR -> CODE_DIR -> CLAUDE_CODEDIR"""
    for key in ("AGENT_CODEDIR", "AGENT__CODEDIR", "CODE_DIR", "CLAUDE_CODEDIR"):
        v = (os.getenv(key) or "").strip()
        if v:
            return os.path.expanduser(v)
    return None


WORKDIR = _resolve_workdir()
CODEDIR = _resolve_codedir()
# 정기 작업(다이제스트) 정의 — 스케줄러가 매분 다시 읽는다(schedules.py).
SCHEDULES_FILE = os.path.join(STATE_DIR, "schedules.json")
# 웹 대화방 진행 중 job 레코드 — 서버 재시작/크래시 후 복구 근거.
WEB_JOBS_FILE = os.path.join(STATE_DIR, "web_jobs.json")
# 분리 실행된 claude/agy 의 stream-json 출력/에러 파일 임시 디렉토리.
JOBS_DIR = os.path.join(DATA_DIR, "jobs")
# 웹/음성 비서 대화방 영속 저장 디렉토리 (방별 실시간 스트림 JSONL 및 대화 히스토리).
CHATS_DIR = os.path.join(DATA_DIR, "chats")
# 대화방 메타데이터 인덱스 파일 (레거시).
CHAT_ROOMS_FILE = os.path.join(STATE_DIR, "chat_rooms.json")
# 통합 SQLite 대화 데이터베이스 (웹 채널 공통 SSOT).
CHAT_DB_FILE = os.path.join(STATE_DIR, "chat.db")
# claude CLI 의 설정·인증·세션 디렉토리(~/.claude) 를 봇 전용으로 분리한 위치 — AGENT_CONFIG_DIR
# 로 여기를 가리킨다. 인증(.credentials.json)은 사용자가 한 번 대화형 /login 으로 채워야 한다
# (파일복사로는 안 됨 — 토큰이 stale). 설정·봇세션만 agent_engine 이 1회 시드한다.
AGENT_HOME_DIR = os.path.join(DATA_DIR, "claude-home")
# claude CLI 스크래치(AGENT_CODE_TMPDIR) — claude-home 하위에 모아 claude 관련 디렉토리를 한 곳에.
AGENT_TMP_DIR = os.path.join(AGENT_HOME_DIR, "tmp")
# 주간 잔여 쿼터 토큰 버닝 개선 TODO 리스트 및 실행 상태
BURN_TODO_FILE = os.path.join(STATE_DIR, "burn_todo.json")
BURN_STATE_FILE = os.path.join(STATE_DIR, "burn_state.json")
# 마스터 에이전트 작업 원장(Task Ledger) — 하위 채팅에 위임한 작업과 정기 점검(patrol) 상태.
MASTER_TASKS_FILE = os.path.join(STATE_DIR, "master_tasks.json")
# 마스터 에이전트 서브 태스크 이벤트 및 콜백 상태(master_state.json).
MASTER_STATE_FILE = os.path.join(STATE_DIR, "master_state.json")
# 마스터 에이전트 4계층 메모리(Hot/Warm/Cold/Freeze) 영속화 파일.
MASTER_MEMORY_FILE = os.path.join(STATE_DIR, "master_memory.json")
# 에이전트(마스터·전문가·커스텀) 설정 및 하위 세션 전달 프롬프트 템플릿 영속화 파일.
AGENT_CONFIGS_FILE = os.path.join(STATE_DIR, "agent_configs.json")
# 동적으로 생성/편집된 에이전트 지침(AGENTS.md) 디렉토리.
RUNTIME_INSTRUCTIONS_DIR = os.path.join(STATE_DIR, "agent_instructions")


def _resolve_google_auth_path(env_key: str, default_filename: str, legacy_filename: str) -> str:
    """Google OAuth 자격증명/토큰 경로 해석. 환경변수 -> 신규 표준(또는 레거시 파일 존재 시 호환) -> 기본 경로."""
    raw = os.getenv(env_key, "").strip()
    if raw:
        exp = os.path.expanduser(raw)
        return exp if os.path.isabs(exp) else os.path.abspath(os.path.join(ROOT_DIR, exp))
    default_path = os.path.join(STATE_DIR, default_filename)
    legacy_path = os.path.join(STATE_DIR, legacy_filename)
    if not os.path.exists(default_path) and os.path.exists(legacy_path):
        return legacy_path
    return default_path


# Google API (OAuth 2.0 자격증명 및 토큰 - Calendar & Gmail 공용)
GOOGLE_CREDENTIALS_FILE = _resolve_google_auth_path(
    "GOOGLE_CREDENTIALS_FILE", "google_credentials.json", "gcal_credentials.json"
)
GOOGLE_TOKEN_FILE = _resolve_google_auth_path(
    "GOOGLE_TOKEN_FILE", "google_token.json", "gcal_token.json"
)
# 볼트 캘린더 동기화 트리거 파일
CAL_REFRESH_TRIGGER_PATH = os.path.join(
    WORKDIR, os.getenv("CAL_REFRESH_TRIGGER") or "_bot/.cal-refresh"
)
# Gmail 동기화 상태 및 볼트 메일 노트 경로
GMAIL_SYNC_STATE_FILE = os.path.join(STATE_DIR, "gmail_sync_state.json")
GMAIL_VAULT_FILE = os.path.join(
    WORKDIR, os.getenv("GMAIL_VAULT_FILE") or "_bot/메일.md"
)


def child_default_workdir(parent_agent_id: str | None) -> str | None:
    """상위 에이전트(봇개발 및 커스텀 에이전트) 하위 방의 기본 작업경로. 해당 없으면 None(기존 폴백 유지)."""
    if parent_agent_id == "__expert_coding__":
        d = CODEDIR or WORKDIR
        if d and os.path.isdir(d):
            return d
        return ROOT_DIR if os.path.isdir(ROOT_DIR) else None
    if parent_agent_id:
        try:
            import bot.core.agent_configs as agent_configs
            w = agent_configs.get_agent_workdir(str(parent_agent_id).strip())
            if w:
                return w
        except Exception:
            pass
    return None
