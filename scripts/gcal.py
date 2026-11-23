#!/usr/bin/env python3
"""gcal.py — Google Calendar CLI 도구.

터미널 또는 에이전트 환경에서 Google Calendar 일정을 조회, 추가, 삭제하고 인증 상태를 관리한다.
"""

from __future__ import annotations

import argparse
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_ROOT, "src", "bot")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from bot.google.gcal_manager import GoogleCalendarManager


def main() -> int:
    parser = argparse.ArgumentParser(description="Google Calendar CLI 관리 도구")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # 1. status
    subparsers.add_parser("status", help="Google 캘린더 OAuth 인증 및 설정 상태 확인")

    # 2. auth
    auth_parser = subparsers.add_parser("auth", help="OAuth 인증 시작 또는 코드 입력")
    auth_parser.add_argument("--code", help="브라우저에서 발급받은 인증 코드", default=None)

    # 3. list
    list_parser = subparsers.add_parser("list", help="다가오는 일정 목록 조회")
    list_parser.add_argument("--days", type=int, default=7, help="조회할 일수 (기본 7일)")

    # 4. add
    add_parser = subparsers.add_parser("add", help="빠른 일정 등록 (자연어 지원)")
    add_parser.add_argument("text", help="일정 내용 (예: '내일 15:00 치과 진료')")

    # 5. delete
    del_parser = subparsers.add_parser("delete", help="일정 삭제")
    del_parser.add_argument("event_id", help="삭제할 이벤트 ID")

    args = parser.parse_args()
    mgr = GoogleCalendarManager()

    if args.command == "status":
        print(f"Credentials 파일: {mgr.credentials_file} (존재: {mgr.has_credentials})")
        print(f"Token 파일:       {mgr.token_file} (존재: {mgr.has_token})")
        print(f"인증 상태:        {'✅ 인증 완료' if mgr.is_authenticated() else '❌ 미인증'}")
        return 0

    if args.command == "auth":
        if args.code:
            try:
                ok = mgr.finish_auth_with_code(args.code)
                if ok:
                    print("✅ Google 캘린더 인증 완료!")
                    return 0
                print("❌ 인증 실패 (유효하지 않은 코드)")
                return 1
            except Exception as exc:
                print(f"❌ 인증 실패: {exc}")
                return 1
        else:
            if not mgr.has_credentials:
                print(f"❌ OAuth 클라이언트 설정 파일이 없습니다: {mgr.credentials_file}")
                print("Google Cloud Console에서 데스크톱 앱 클라이언트 ID JSON을 저장해주세요.")
                return 1
            url = mgr.get_auth_url()
            print(f"아래 URL을 브라우저에서 열어 구글 로그인을 승인하세요:\n\n{url}\n")
            print("발급된 인증 코드를 다음과 같이 입력하세요:")
            print("python scripts/gcal.py auth --code <코드>")
            return 0

    if not mgr.is_authenticated():
        print("❌ Google 캘린더 인증이 필요합니다. 먼저 다음 명령으로 인증하세요:")
        print("python scripts/gcal.py auth")
        return 1

    if args.command == "list":
        events = mgr.list_upcoming_events(days=args.days)
        if not events:
            print(f"향후 {args.days}일간 예정된 일정이 없습니다.")
            return 0
        print(f"📅 다가오는 일정 ({len(events)}건):")
        for idx, ev in enumerate(events, 1):
            print(f"  {idx}. [{ev['id']}] {ev['summary']} — {ev['time_str']}")
        return 0

    if args.command == "add":
        res = mgr.quick_add(args.text)
        print("✅ 일정 등록 완료!")
        print(f"  ID:    {res['id']}")
        print(f"  제목:  {res['summary']}")
        print(f"  일시:  {res['time_str']}")
        if res.get("html_link"):
            print(f"  링크:  {res['html_link']}")
        return 0

    if args.command == "delete":
        mgr.delete_event(args.event_id)
        print(f"🗑️ 일정 삭제 완료: {args.event_id}")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
