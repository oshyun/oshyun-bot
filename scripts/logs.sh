#!/usr/bin/env bash
# 로그 실시간 출력. 사용법: ./scripts/logs.sh [web|cal|err]
#   인자 없음 / 'web' → 웹 서버 로그
#   'cal'             → 캘린더 사이드카 로그
#   'err'             → 최근 stderr 로그
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

case "${1:-web}" in
  cal)
    touch "$CAL_LOGFILE" 2>/dev/null || true
    exec tail -n 50 -f "$CAL_LOGFILE"
    ;;
  err)
    exec tail -n 30 -f "$WEB_ERRFILE" "$CAL_ERRFILE"
    ;;
  web|*)
    touch "$WEB_LOGFILE" 2>/dev/null || true
    exec tail -n 50 -f "$WEB_LOGFILE"
    ;;
esac
