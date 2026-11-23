"""env_compat — 도메인별 환경변수(DOMAIN__KEY)와 레거시 환경변수(KEY) 간의 호환성 동기화 모듈.

.env 파일에서 환경변수의 소속 도메인(AGENT__, ANTHROPIC__, CALENDAR__, WEB__, DUCKDNS__ 등)을
명확히 구분할 수 있도록 `도메인__키=값` 네이밍을 지원하며, 기존 코드베이스 및 외부 라이브러리/CLI와의
100% 호환성을 위해 양방향으로 동기화(alias 매핑)합니다.
"""

from __future__ import annotations

import os

# (새로운 도메인 기반 키, 기존 레거시 키)
DOMAIN_ENV_MAPPINGS: list[tuple[str, str]] = [
    # ── 에이전트 / 작업공간 / 코딩 (AGENT__) ────────────────────────────
    ("AGENT__WORKDIR", "AGENT_WORKDIR"),
    ("AGENT__CODEDIR", "AGENT_CODEDIR"),
    ("AGENT__TIMEOUT", "AGENT_TIMEOUT"),
    ("AGENT__MODEL", "AGENT_MODEL"),
    ("AGENT__EXTRA_ARGS", "AGENT_EXTRA_ARGS"),
    ("AGENT__CODE_EFFORT_LEVEL", "AGENT_CODE_EFFORT_LEVEL"),
    ("AGENT__CODE_EXPERIMENTAL_AGENT_TEAMS", "AGENT_CODE_EXPERIMENTAL_AGENT_TEAMS"),
    ("AGENT__CODE_AUTO_COMPACT_WINDOW", "AGENT_CODE_AUTO_COMPACT_WINDOW"),
    ("AGENT__CODE_DISABLE_NONESSENTIAL_TRAFFIC", "AGENT_CODE_DISABLE_NONESSENTIAL_TRAFFIC"),
    ("AGENT__LOG_PATH", "BOT_LOG_PATH"),
    ("AGENT__SYSTEM_PROMPT", "OSHYUN_BOT_SYSTEM_PROMPT"),
    ("AGENT__SANDBOX", "OSHYUN_BOT_SANDBOX"),
    ("AGENT__ENGINE", "OSHYUN_BOT_ENGINE"),

    # ── LLM / API 프록시 (ANTHROPIC__) ─────────────────────────────────
    ("ANTHROPIC__MODEL", "ANTHROPIC_MODEL"),
    ("ANTHROPIC__AUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN"),
    ("ANTHROPIC__BASE_URL", "ANTHROPIC_BASE_URL"),
    ("ANTHROPIC__API_TIMEOUT_MS", "API_TIMEOUT_MS"),
    ("ANTHROPIC__DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL"),
    ("ANTHROPIC__DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL"),
    ("ANTHROPIC__DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL"),

    # ── 캘린더 사이드카 (CALENDAR__) ───────────────────────────────────
    ("CALENDAR__ICAL_URL", "GOOGLE_ICAL_URL"),
    ("CALENDAR__NOTE_PATH", "CAL_NOTE_PATH"),
    ("CALENDAR__YEARS_AHEAD", "CAL_YEARS_AHEAD"),
    ("CALENDAR__YEARS_BACK", "CAL_YEARS_BACK"),
    ("CALENDAR__INTERVAL", "CAL_INTERVAL"),
    ("CALENDAR__REFRESH_TRIGGER", "CAL_REFRESH_TRIGGER"),
    ("CALENDAR__REFRESH_POLL", "CAL_REFRESH_POLL"),
    ("CALENDAR__CREDENTIALS_FILE", "GOOGLE_CREDENTIALS_FILE"),
    ("CALENDAR__TOKEN_FILE", "GOOGLE_TOKEN_FILE"),

    # ── 웹 서버 / 오케스트레이터 (WEB__) ──────────────────────────────
    ("WEB__ENABLED", "WEB_ENABLED"),
    ("WEB__HOST", "WEB_HOST"),
    ("WEB__PORT", "WEB_PORT"),
    ("WEB__AUTH_REQUIRED", "WEB_AUTH_REQUIRED"),
    ("WEB__TOKEN", "WEB_TOKEN"),
    ("WEB__SSL_ENABLED", "WEB_SSL_ENABLED"),
    ("WEB__SSL_CERT", "WEB_SSL_CERT"),
    ("WEB__SSL_KEY", "WEB_SSL_KEY"),
    ("WEB__AIOHTTP_NOSENDFILE", "AIOHTTP_NOSENDFILE"),
    ("WEB__MASTER_PATROL_INTERVAL", "MASTER_PATROL_INTERVAL"),
    ("WEB__STATIC_DIR", "WEB_STATIC_DIR"),
    ("WEB__TTS_VOICE", "WEB_TTS_VOICE"),
    ("WEB__TTS_MODE", "WEB_TTS_MODE"),
    ("WEB__SESSION_SECRET", "WEB_SESSION_SECRET"),
    ("WEB__SESSION_TTL", "WEB_SESSION_TTL"),
    ("WEB__MAX_FAILURES", "WEB_MAX_FAILURES"),
    ("WEB__LOCKOUT_WINDOW", "WEB_LOCKOUT_WINDOW"),
    ("WEB__LOCKOUT_DURATION", "WEB_LOCKOUT_DURATION"),
    ("WEB__AUTH_DELAY", "WEB_AUTH_DELAY"),

    # ── DuckDNS / 도메인 (DUCKDNS__) ──────────────────────────────────
    ("DUCKDNS__DOMAIN", "DUCKDNS_DOMAIN"),
    ("DUCKDNS__TOKEN", "DUCKDNS_TOKEN"),

    # ── 시스템 공통 (SYSTEM__) ────────────────────────────────────────
    ("SYSTEM__DATA_DIR", "DATA_DIR"),
]


def sync_domain_env(env_dict: dict[str, str] | os._Environ[str] | None = None) -> None:
    """도메인 기반 환경변수와 레거시 환경변수 간의 양방향 동기화를 수행합니다.

    - `DOMAIN__KEY`가 설정되어 있고 레거시 키가 없으면: 레거시 키에도 값을 채웁니다.
    - 레거시 키가 설정되어 있고 `DOMAIN__KEY`가 없으면: `DOMAIN__KEY`에도 값을 채웁니다.
    - 둘 다 설정되어 있는 경우: 새로운 `DOMAIN__KEY` 값이 우선하여 레거시 키를 갱신합니다.
    """
    target = os.environ if env_dict is None else env_dict

    for domain_key, legacy_key in DOMAIN_ENV_MAPPINGS:
        has_domain = domain_key in target and str(target[domain_key]).strip() != ""
        has_legacy = legacy_key in target and str(target[legacy_key]).strip() != ""

        if has_domain and not has_legacy:
            target[legacy_key] = target[domain_key]
        elif has_legacy and not has_domain:
            target[domain_key] = target[legacy_key]
        elif has_domain and has_legacy:
            target[legacy_key] = target[domain_key]


def get_domain_env(domain_key: str, default: str | None = None, env_dict: dict[str, str] | None = None) -> str | None:
    """도메인 키를 기준으로 환경변수를 조회하며, 매핑된 레거시 키까지 순서대로 fallback 조회합니다."""
    target = os.environ if env_dict is None else env_dict

    if domain_key in target and str(target[domain_key]).strip() != "":
        return target[domain_key]

    for d_key, leg_key in DOMAIN_ENV_MAPPINGS:
        if d_key == domain_key and leg_key in target and str(target[leg_key]).strip() != "":
            return target[leg_key]

    return default
