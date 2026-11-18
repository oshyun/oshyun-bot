#!/usr/bin/env bash
# DuckDNS IP 동기화 및 Let's Encrypt 인증서 갱신 유틸리티
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

# .env 로드
if [ -f "$ROOT_DIR/.env" ]; then
    set -a
    source "$ROOT_DIR/.env"
    set +a
fi

DOMAIN="${DUCKDNS_DOMAIN:-}"
TOKEN="${DUCKDNS_TOKEN:-}"

if [ -z "$DOMAIN" ]; then
    echo "[duckdns-sync] DUCKDNS_DOMAIN 이 설정되지 않았습니다 (.env 파일 확인 필요)." >&2
    exit 1
fi

if [ -z "$TOKEN" ]; then
    echo "[duckdns-sync] DUCKDNS_TOKEN 이 설정되지 않았습니다 (.env 파일 확인 필요)." >&2
    exit 1
fi

# 1. DuckDNS IP 갱신
RESP=$(curl -s "https://www.duckdns.org/update?domains=${DOMAIN}&token=${TOKEN}")
if [ "$RESP" != "OK" ]; then
    echo "[duckdns-sync] DuckDNS IP 갱신 실패: $RESP" >&2
    exit 1
fi
echo "[duckdns-sync] DuckDNS IP 동기화 성공: ${DOMAIN}.duckdns.org"

# 2. Certbot 갱신 (인자로 --renew 가 주어졌을 때)
if [ "${1:-}" = "--renew" ]; then
    echo "[duckdns-sync] Let's Encrypt 인증서 갱신 시도..."
    "$ROOT_DIR/.venv/bin/certbot" renew \
        --config-dir "$ROOT_DIR/data/letsencrypt/config" \
        --work-dir "$ROOT_DIR/data/letsencrypt/work" \
        --logs-dir "$ROOT_DIR/data/letsencrypt/logs" \
        --http-01-port 10080
    "$SCRIPT_DIR/restart.sh" web
fi
