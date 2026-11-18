#!/usr/bin/env bash
# 공통 환경/헬퍼 (다른 스크립트에서 source 하여 사용).
# PROJECT_ROOT 계산 → .env 로드 → 헬퍼 정의.
set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# 기본값 먼저 로드, .env 가 덮어씀.
source "${PROJECT_ROOT}/.env.defaults" 2>/dev/null || true
source "${PROJECT_ROOT}/.env" 2>/dev/null || true

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
# 운영 산출물(로그/PID/세션/첨부) 디렉토리. DATA_DIR 로 리포 밖으로 분리 가능(기본: <루트>/data).
# 파이썬 src/bot/paths.py 와 같은 규칙.
DATA_DIR="${DATA_DIR:-${PROJECT_ROOT}/data}"
LOGS_DIR="${DATA_DIR}/logs"

# 캘린더 사이드카(calendar_sync.py) — 웹 서버와 별개 프로세스.
# 로그는 calendar_sync.py 가 직접 기록(최근 100줄 유지)하고,
# 셸은 stderr(경고/크래시)만 CAL_ERRFILE 로 받는다.
CAL_PIDFILE="${LOGS_DIR}/cal-sync.pid"
CAL_LOGFILE="${LOGS_DIR}/cal-sync.log"
CAL_ERRFILE="${LOGS_DIR}/cal-sync.err.log"

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
