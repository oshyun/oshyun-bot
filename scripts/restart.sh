#!/usr/bin/env bash
# oshyun 서비스 재시작.
# 사용법:
#   ./scripts/restart.sh         # 전체 서비스 재시작 (web, cal)
#   ./scripts/restart.sh web     # 웹 서버만 재시작
#   ./scripts/restart.sh cal     # 캘린더 사이드카만 재시작
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET="${1:-all}"
"${SCRIPT_DIR}/stop.sh" "$TARGET" || true
"${SCRIPT_DIR}/start.sh" "$TARGET"
