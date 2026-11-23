#!/usr/bin/env bash
# oshyun 서비스 기동 (백그라운드).
# 사용법:
#   ./scripts/start.sh         # 전체 서비스 기동 (web, cal)
#   ./scripts/start.sh web     # 웹 서버만 기동
#   ./scripts/start.sh cal     # 캘린더 사이드카만 기동
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/common.sh"
cd "${PROJECT_ROOT}"

[ -x "$PY" ] || {
  echo "가상환경이 없습니다: $PY"
  echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
}

# 운영 산출물 디렉토리 보장. LOGS_DIR(pid/log 전용)도 함께 생성.
mkdir -p "$LOGS_DIR"

start_web() {
  local web_enabled="${WEB_ENABLED:-${VOICE_WEB_ENABLED:-1}}"
  if [ "$web_enabled" = "0" ]; then
    echo "웹 서버             - 비활성 (WEB_ENABLED=0 설정됨)"
    return 0
  fi

  if pid="$(web_pid)"; then
    echo "웹 서버             ● 이미 실행 중입니다 (PID $pid)."
    return 0
  fi

  local host="${WEB_HOST:-${VOICE_WEB_HOST:-127.0.0.1}}"
  local port="${WEB_PORT:-${VOICE_WEB_PORT:-8765}}"

  # 고아 인스턴스 사전 정리
  kill_web_processes
  sleep 0.3

  nohup "$PY" -m bot.web.server > "$WEB_ERRFILE" 2>&1 &
  echo $! > "$WEB_PIDFILE"
  sleep 1
  if pid="$(web_pid)"; then
    echo "웹 서버             ● 시작 완료 (PID $pid) — http://${host}:${port} (로그: $WEB_LOGFILE)"
  else
    echo "웹 서버             ❌ 기동 실패 — stderr 확인: $WEB_ERRFILE"
    tail -n 20 "$WEB_ERRFILE" || true
    return 1
  fi
}

start_cal() {
  if [ -z "${GOOGLE_ICAL_URL:-}" ]; then
    echo "캘린더 사이드카     - 비활성 (GOOGLE_ICAL_URL 미설정)"
    return 0
  fi

  if pid="$(cal_pid)"; then
    echo "캘린더 사이드카     ● 이미 실행 중입니다 (PID $pid)."
    return 0
  fi

  # 고아 인스턴스 사전 정리
  pkill -f "${PROJECT_ROOT}/src/cal-sync/calendar_sync.py" 2>/dev/null || true
  sleep 0.3

  nohup "$PY" "${PROJECT_ROOT}/src/cal-sync/calendar_sync.py" > "$CAL_ERRFILE" 2>&1 &
  echo $! > "$CAL_PIDFILE"
  sleep 1
  if pid="$(cal_pid)"; then
    echo "캘린더 사이드카     ● 시작 완료 (PID $pid) — 로그: $CAL_LOGFILE"
  else
    echo "캘린더 사이드카     ❌ 기동 실패 — stderr 확인: $CAL_ERRFILE"
    tail -n 20 "$CAL_ERRFILE" || true
    return 1
  fi
}

TARGET="${1:-all}"
case "$TARGET" in
  web)
    start_web
    ;;
  cal)
    start_cal
    ;;
  all)
    start_web
    start_cal
    ;;
  *)
    echo "사용법: $0 [web|cal|all]"
    exit 1
    ;;
esac
