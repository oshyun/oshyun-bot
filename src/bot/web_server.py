"""구경로 호환 thin launcher — src/bot/web_server.py 직접 실행 요청을 신규 진입점으로 위임.

패키지 재구성(bot.web.server) 후에도 구경로 실행·외부 호출이 당분간 동작하도록 유지한다.

제거 조건: 구경로 직접 실행(`python src/bot/web_server.py`)·외부 호출(pkill 패턴,
cron 등)이 0건임을 확인한 후 삭제한다. scripts/common.sh 의 WEB_KILL_PATTERN_LEGACY
전환기 패턴도 같이 정리한다. 전환 안정화 이후에 제거한다.
"""

from bot.web.server import main

if __name__ == "__main__":
    main()
