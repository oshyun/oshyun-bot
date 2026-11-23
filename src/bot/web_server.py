"""구경로 호환 thin launcher — src/bot/web_server.py 직접 실행 요청을 신규 진입점으로 위임.

패키지 재구성(bot.web.server) 후에도 구경로 실행·외부 호출이 당분간 동작하도록 유지한다.
"""

from bot.web.server import main

if __name__ == "__main__":
    main()
