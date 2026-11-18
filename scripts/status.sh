#!/usr/bin/env bash
# oshyun 서비스 상태 확인. 사용법: ./scripts/status.sh
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

web_enabled="${WEB_ENABLED:-${VOICE_WEB_ENABLED:-1}}"
if [ "$web_enabled" = "0" ]; then
  echo "웹 서버             - 비활성 (WEB_ENABLED=0)"
elif pid="$(web_pid)"; then
  host="${WEB_HOST:-${VOICE_WEB_HOST:-127.0.0.1}}"
  port="${WEB_PORT:-${VOICE_WEB_PORT:-8765}}"
  echo "웹 서버             ● 실행 중 (PID $pid)"
  echo "  주소:    http://${host}:${port}"
  echo "  로그:    $WEB_LOGFILE"
else
  echo "웹 서버             ○ 중지됨"
fi

if pid="$(cal_pid)"; then
  echo "캘린더 사이드카     ● 실행 중 (PID $pid)"
  echo "  노트:    ${CAL_NOTE_PATH:-_bot/캘린더.md}"
  echo "  로그:    $CAL_LOGFILE"
elif [ -n "${GOOGLE_ICAL_URL:-}" ]; then
  echo "캘린더 사이드카     ○ 중지됨"
else
  echo "캘린더 사이드카     - 비활성 (GOOGLE_ICAL_URL 미설정)"
fi
