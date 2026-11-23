#!/usr/bin/env bash
# 공통 환경/헬퍼 (다른 스크립트에서 source 하여 사용).
# PROJECT_ROOT 계산 → .env 로드 → 헬퍼 정의.
set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# 기본값 먼저 로드, .env 가 덮어씀.
source "${PROJECT_ROOT}/.env.defaults" 2>/dev/null || true
source "${PROJECT_ROOT}/.env" 2>/dev/null || true

# 도메인 기반 환경변수(DOMAIN__KEY)와 레거시 환경변수(KEY) 간 양방향 동기화
_sync_env_pair() {
  local d_key="$1" l_key="$2"
  local d_val l_val
  eval "d_val=\${${d_key}:-}"
  eval "l_val=\${${l_key}:-}"
  if [ -n "$d_val" ] && [ -z "$l_val" ]; then
    eval "export ${l_key}=\"\$d_val\""
  elif [ -n "$l_val" ] && [ -z "$d_val" ]; then
    eval "export ${d_key}=\"\$l_val\""
  elif [ -n "$d_val" ] && [ -n "$l_val" ]; then
    eval "export ${l_key}=\"\$d_val\""
  fi
}

_sync_domain_env() {
  _sync_env_pair "TELEGRAM__BOT_TOKEN" "TELEGRAM_BOT_TOKEN"
  _sync_env_pair "TELEGRAM__ALLOWED_CHAT_IDS" "ALLOWED_CHAT_IDS"
  _sync_env_pair "TELEGRAM__LOG_PATH" "BOT_LOG_PATH"
  _sync_env_pair "AGENT__WORKDIR" "AGENT_WORKDIR"
  _sync_env_pair "AGENT__CODEDIR" "AGENT_CODEDIR"
  _sync_env_pair "AGENT__TIMEOUT" "AGENT_TIMEOUT"
  _sync_env_pair "AGENT__MODEL" "AGENT_MODEL"
  _sync_env_pair "AGENT__EXTRA_ARGS" "AGENT_EXTRA_ARGS"
  _sync_env_pair "AGENT__CODE_EFFORT_LEVEL" "AGENT_CODE_EFFORT_LEVEL"
  _sync_env_pair "AGENT__CODE_EXPERIMENTAL_AGENT_TEAMS" "AGENT_CODE_EXPERIMENTAL_AGENT_TEAMS"
  _sync_env_pair "AGENT__CODE_AUTO_COMPACT_WINDOW" "AGENT_CODE_AUTO_COMPACT_WINDOW"
  _sync_env_pair "AGENT__CODE_DISABLE_NONESSENTIAL_TRAFFIC" "AGENT_CODE_DISABLE_NONESSENTIAL_TRAFFIC"
  _sync_env_pair "AGENT__LOG_PATH" "BOT_LOG_PATH"
  _sync_env_pair "ANTHROPIC__MODEL" "ANTHROPIC_MODEL"
  _sync_env_pair "ANTHROPIC__AUTH_TOKEN" "ANTHROPIC_AUTH_TOKEN"
  _sync_env_pair "ANTHROPIC__BASE_URL" "ANTHROPIC_BASE_URL"
  _sync_env_pair "ANTHROPIC__API_TIMEOUT_MS" "API_TIMEOUT_MS"
  _sync_env_pair "ANTHROPIC__DEFAULT_HAIKU_MODEL" "ANTHROPIC_DEFAULT_HAIKU_MODEL"
  _sync_env_pair "ANTHROPIC__DEFAULT_SONNET_MODEL" "ANTHROPIC_DEFAULT_SONNET_MODEL"
  _sync_env_pair "ANTHROPIC__DEFAULT_OPUS_MODEL" "ANTHROPIC_DEFAULT_OPUS_MODEL"
  _sync_env_pair "CALENDAR__ICAL_URL" "GOOGLE_ICAL_URL"
  _sync_env_pair "CALENDAR__NOTE_PATH" "CAL_NOTE_PATH"
  _sync_env_pair "CALENDAR__YEARS_AHEAD" "CAL_YEARS_AHEAD"
  _sync_env_pair "CALENDAR__YEARS_BACK" "CAL_YEARS_BACK"
  _sync_env_pair "CALENDAR__INTERVAL" "CAL_INTERVAL"
  _sync_env_pair "CALENDAR__REFRESH_TRIGGER" "CAL_REFRESH_TRIGGER"
  _sync_env_pair "CALENDAR__REFRESH_POLL" "CAL_REFRESH_POLL"
  _sync_env_pair "CALENDAR__CREDENTIALS_FILE" "GOOGLE_CREDENTIALS_FILE"
  _sync_env_pair "CALENDAR__TOKEN_FILE" "GOOGLE_TOKEN_FILE"
  _sync_env_pair "WEB__ENABLED" "WEB_ENABLED"
  _sync_env_pair "WEB__HOST" "WEB_HOST"
  _sync_env_pair "WEB__PORT" "WEB_PORT"
  _sync_env_pair "WEB__AUTH_REQUIRED" "WEB_AUTH_REQUIRED"
  _sync_env_pair "WEB__TOKEN" "WEB_TOKEN"
  _sync_env_pair "WEB__SSL_ENABLED" "WEB_SSL_ENABLED"
  _sync_env_pair "WEB__AIOHTTP_NOSENDFILE" "AIOHTTP_NOSENDFILE"
  _sync_env_pair "WEB__MASTER_PATROL_INTERVAL" "MASTER_PATROL_INTERVAL"
  _sync_env_pair "DUCKDNS__DOMAIN" "DUCKDNS_DOMAIN"
  _sync_env_pair "DUCKDNS__TOKEN" "DUCKDNS_TOKEN"
  _sync_env_pair "SYSTEM__DATA_DIR" "DATA_DIR"
}
_sync_domain_env

# macOS aiohttp FileResponse sendfile 커널 버그 방지 (HTTP 헤더 누락 및 HTTP/0.9 오류 방지)
export AIOHTTP_NOSENDFILE="${AIOHTTP_NOSENDFILE:-1}"

# 基本 CA 번들 환경변수가 가리키는 파일이 존재하지 않으면 제거(샌드박스 환경변수 오염 방지)
for cert_var in SSL_CERT_FILE REQUESTS_CA_BUNDLE CURL_CA_BUNDLE AWS_CA_BUNDLE GIT_SSL_CAINFO NODE_EXTRA_CA_CERTS PIP_CERT npm_config_cafile; do
  eval "val=\${${cert_var}:-}"
  if [ -n "${val:-}" ] && [ ! -f "$val" ]; then
    unset "$cert_var"
  fi
done

PY="${PROJECT_ROOT}/.venv/bin/python"
# 패키지 루트(src)를 PYTHONPATH 에 포함 — python -m bot.web.server 기동용.
PYTHONPATH="${PROJECT_ROOT}/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH
# 운영 산출물(로그/PID/세션/첨부) 디렉토리. DATA_DIR 로 리포 밖으로 분리 가능(기본: <루트>/data).
# 파이썬 src/bot/core/paths.py 와 같은 규칙.
DATA_DIR="${DATA_DIR:-${PROJECT_ROOT}/data}"
LOGS_DIR="${DATA_DIR}/logs"

# 캘린더 사이드카(calendar_sync.py) — 웹 서버와 별개 프로세스.
# 로그는 calendar_sync.py 가 직접 기록(최근 100줄 유지)하고,
# 셸은 stderr(경고/크래시)만 CAL_ERRFILE 로 받는다.
CAL_PIDFILE="${LOGS_DIR}/cal-sync.pid"
CAL_LOGFILE="${LOGS_DIR}/cal-sync.log"
CAL_ERRFILE="${LOGS_DIR}/cal-sync.err.log"

# 웹 서버 프로세스 정리 pkill 패턴 SSOT — start.sh/stop.sh 가 공유한다.
# 두 패턴 모두 본 체크아웃의 venv 인터프리터 절대경로(${PY})로 앵커링한다 —
# 앵커가 없으면 pkill 부분일치로 다른 체크아웃/워크트리의 웹 서버나
# 에디터·tail 등 우연히 경로를 포함하는 프로세스까지 죽일 수 있다.
# 첫째 패턴: 구경로 thin launcher(src/bot/web_server.py) 전환기 정리용,
# 둘째 패턴: 신규 모듈 기동(python -m bot.web.server) 프로세스.
WEB_KILL_PATTERN_LEGACY="${PY} .*src/bot/web_server\.py"
WEB_KILL_PATTERN_MODULE="${PY} -m bot\.web\.server"
kill_web_processes() {
  pkill -f "$WEB_KILL_PATTERN_LEGACY" 2>/dev/null || true  # 구경로 thin launcher 정리
  pkill -f "$WEB_KILL_PATTERN_MODULE" 2>/dev/null || true
}

# 웹 서버(web_server.py) — Flutter Web & 음성 비서/볼트 파일 매니저 독립 프로세스.
# 로그는 web_server.py 가 직접 기록(최근 100줄 유지)하고,
# 셸은 stderr(경고/크래시)만 WEB_ERRFILE 로 받는다.
WEB_PIDFILE="${LOGS_DIR}/web-server.pid"
WEB_LOGFILE="${LOGS_DIR}/web-server.log"
WEB_ERRFILE="${LOGS_DIR}/web-server.err.log"

# 범용: pidfile 의 프로세스가 살아 있으면 PID 출력 후 0, 아니면 1.
proc_pid() {
  local pidfile="$1" pid
  [ -f "$pidfile" ] || return 1
  pid="$(cat "$pidfile" 2>/dev/null || true)"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null || return 1
  echo "$pid"
}

# 서비스가 실행 중이면 PID 를 stdout 으로 출력하고 0, 아니면 1 을 반환한다.
web_pid() { proc_pid "$WEB_PIDFILE"; }
cal_pid() { proc_pid "$CAL_PIDFILE"; }

# 범용 종료: <라벨> <pidfile>. 정상 종료(최대 ~6초) 후 안 죽으면 SIGKILL.
stop_proc() {
  local label="$1" pidfile="$2" pid
  if ! pid="$(proc_pid "$pidfile")"; then
    echo "$label: 실행 중이지 않습니다."
    rm -f "$pidfile"
    return 0
  fi
  kill "$pid"
  for _ in $(seq 1 30); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.2
  done
  if kill -0 "$pid" 2>/dev/null; then
    echo "$label: 정상 종료 실패 → 강제 종료(SIGKILL)"
    kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f "$pidfile"
  echo "$label 종료 (PID $pid)"
}
