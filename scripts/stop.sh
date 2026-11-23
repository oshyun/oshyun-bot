#!/usr/bin/env bash
# oshyun 서비스 종료.
# 사용법:
#   ./scripts/stop.sh         # 전체 서비스 종료 (web, cal)
#   ./scripts/stop.sh web     # 웹 서버만 종료
#   ./scripts/stop.sh cal     # 캘린더 사이드카만 종료
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

stop_web() {
  stop_proc "웹 서버" "$WEB_PIDFILE"
  kill_web_processes
}

stop_cal() {
  stop_proc "캘린더 사이드카" "$CAL_PIDFILE"
  pkill -f "${PROJECT_ROOT}/src/cal-sync/calendar_sync.py" 2>/dev/null || true
}

TARGET="${1:-all}"
case "$TARGET" in
  web)
    stop_web
    ;;
  cal)
    stop_cal
    ;;
  all)
    stop_web
    stop_cal
    ;;
  *)
    echo "사용법: $0 [web|cal|all]"
    exit 1
    ;;
esac
