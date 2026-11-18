#!/usr/bin/env bash
# 봇 전용 claude config(DATA_DIR/claude-home)로 claude 를 연다.
#
# 봇은 AGENT_CONFIG_DIR 로 이 디렉토리를 쓴다(설정·세션·히스토리를 사용자 개인 ~/.claude 와 분리).
# 인증은 Keychain(securityd) 으로 가지만, 새 config 디렉토리는 **한 번 대화형 /login** 을 해야
# 그 계정 컨텍스트가 잡힌다(파일 복사로는 토큰이 stale 라 401). 이 스크립트로 그 로그인/점검을 한다.
#
# 사용 예:
#   ./scripts/claude-login.sh            # 대화형 claude — 안에서 /login 으로 인증
#   ./scripts/claude-login.sh /status    # 봇 config 의 로그인/사용량 상태 확인
#   ./scripts/claude-login.sh --version  # 동작 확인
#
# 반드시 **실제 터미널(TTY)** 에서 실행한다 — 비대화형 컨텍스트에선 claude 가 --print 모드로 빠진다.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

# 봇 런타임과 동일한 환경을 맞춘다(같은 config·스크래치를 보도록).
export AGENT_CONFIG_DIR="${DATA_DIR}/claude-home"
export AGENT_CODE_TMPDIR="${DATA_DIR}/claude-tmp"
mkdir -p "$AGENT_CONFIG_DIR" "$AGENT_CODE_TMPDIR"

echo "AGENT_CONFIG_DIR=${AGENT_CONFIG_DIR}"
echo "로그인이 안 돼 있으면 claude 안에서 /login 을 실행하세요."
exec "${OSHYUN_BOT_ENGINE:-claude}" "$@"
