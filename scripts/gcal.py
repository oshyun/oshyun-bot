#!/usr/bin/env python3
"""gcal.py — Google Calendar CLI 관리 도구.

터미널 또는 에이전트 환경에서 Google Calendar 일정을 조회, 빠른 추가(자연어),
명시적 상세 등록(create), 삭제하고 볼트 캘린더(_bot/캘린더.md)와 원스텝 동기화한다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from bot.google.gcal_manager import GoogleCalendarManager


def main() -> int:
    parser = argparse.ArgumentParser(description="Google Calendar CLI 관리 도구")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # 1. status
    status_parser = subparsers.add_parser("status", help="Google 캘린더 OAuth 인증 및 설정 상태 확인")
    status_parser.add_argument("--json", action="store_true", help="JSON 포맷 출력")

    # 2. auth
    auth_parser = subparsers.add_parser("auth", help="OAuth 인증 시작 또는 코드 입력")
    auth_parser.add_argument("--code", help="브라우저에서 발급받은 인증 코드", default=None)

    # 3. list
    list_parser = subparsers.add_parser("list", help="다가오는 일정 목록 조회")
    list_parser.add_argument("--days", type=int, default=7, help="조회할 일수 (기본 7일)")
    list_parser.add_argument("--json", action="store_true", help="JSON 포맷 출력")

    # 4. create (명시적 상세 일정 등록 — 에이전트 최우선 권장)
    create_parser = subparsers.add_parser("create", help="명시적 파라미터로 일정 생성 (에이전트 권장)")
    create_parser.add_argument("--title", "--summary", dest="title", required=True, help="일정 제목 (예: '보컬레슨')")
    create_parser.add_argument("--start", required=True, help="시작 일시 (예: '2026-11-28 19:00' 또는 '2026-11-28T19:00:00+09:00')")
    create_parser.add_argument("--end", default=None, help="종료 일시 (생략 시 시작일시 기준 1시간 뒤 자동 계산)")
    create_parser.add_argument("--desc", "--description", dest="desc", default="", help="일정 설명/메모")
    create_parser.add_argument("--loc", "--location", dest="loc", default="", help="장소")
    create_parser.add_argument("--all-day", action="store_true", help="종일 일정 여부")
    create_parser.add_argument("--no-sync", action="store_true", help="볼트(_bot/캘린더.md) 즉시 동기화 생략")
    create_parser.add_argument("--json", action="store_true", help="JSON 포맷 출력")

    # 5. add (자연어 빠른 일정 등록)
    add_parser = subparsers.add_parser("add", help="빠른 일정 등록 (자연어 지원)")
    add_parser.add_argument("text", help="일정 내용 (예: '내일 15:00 치과 진료')")
    add_parser.add_argument("--no-sync", action="store_true", help="볼트(_bot/캘린더.md) 즉시 동기화 생략")
    add_parser.add_argument("--json", action="store_true", help="JSON 포맷 출력")

    # 6. sync (볼트 사이드카 즉시 1회 동기화)
    sync_parser = subparsers.add_parser("sync", help="볼트(_bot/캘린더.md) 캘린더 사이드카 즉시 동기화")
    sync_parser.add_argument("--json", action="store_true", help="JSON 포맷 출력")

    # 7. delete
    del_parser = subparsers.add_parser("delete", help="일정 삭제")
    del_parser.add_argument("event_id", help="삭제할 이벤트 ID")
    del_parser.add_argument("--no-sync", action="store_true", help="볼트(_bot/캘린더.md) 즉시 동기화 생략")
    del_parser.add_argument("--json", action="store_true", help="JSON 포맷 출력")

    args = parser.parse_args()
    mgr = GoogleCalendarManager()

    if args.command == "status":
        authed = mgr.is_authenticated()
        if args.json:
            print(json.dumps({
                "credentials_file": mgr.credentials_file,
                "has_credentials": mgr.has_credentials,
                "token_file": mgr.token_file,
                "has_token": mgr.has_token,
                "authenticated": authed,
            }, ensure_ascii=False, indent=2))
            return 0
        print(f"Credentials 파일: {mgr.credentials_file} (존재: {mgr.has_credentials})")
        print(f"Token 파일:       {mgr.token_file} (존재: {mgr.has_token})")
        print(f"인증 상태:        {'✅ 인증 완료' if authed else '❌ 미인증'}")
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
        err_msg = "Google 캘린더 인증이 필요합니다. 먼저 python scripts/gcal.py auth 명령으로 인증하세요."
        if getattr(args, "json", False):
            print(json.dumps({"error": err_msg, "authenticated": False}, ensure_ascii=False))
        else:
            print(f"❌ {err_msg}")
        return 1

    if args.command == "sync":
        synced = mgr.sync_vault_now()
        if args.json:
            print(json.dumps({"status": "success", "synced": synced, "message": "볼트 캘린더 동기화 완료"}, ensure_ascii=False))
        else:
            print(f"✅ 볼트 캘린더 노트(_bot/캘린더.md) 동기화 완료 (즉시반영: {synced})")
        return 0

    if args.command == "list":
        events = mgr.list_upcoming_events(days=args.days)
        if args.json:
            print(json.dumps({"status": "success", "days": args.days, "count": len(events), "events": events}, ensure_ascii=False, indent=2))
            return 0
        if not events:
            print(f"향후 {args.days}일간 예정된 일정이 없습니다.")
            return 0
        print(f"📅 다가오는 일정 ({len(events)}건):")
        for idx, ev in enumerate(events, 1):
            print(f"  {idx}. [{ev['id']}] {ev['summary']} — {ev['time_str']}")
        return 0

    if args.command == "create":
        sync_vault = not args.no_sync
        res = mgr.create_event(
            summary=args.title,
            start_time=args.start,
            end_time=args.end,
            description=args.desc,
            location=args.loc,
            all_day=args.all_day,
            sync_vault=sync_vault,
        )
        if args.json:
            out = {
                "status": "success",
                "id": res["id"],
                "summary": res["summary"],
                "time_str": res["time_str"],
                "html_link": res.get("html_link", ""),
                "synced_vault": sync_vault,
            }
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0

        print("✅ 구글 캘린더 일정 등록 완료!")
        print(f"  ID:      {res['id']}")
        print(f"  제목:    {res['summary']}")
        print(f"  일시:    {res['time_str']}")
        if res.get("html_link"):
            print(f"  링크:    {res['html_link']}")
        if sync_vault:
            print("  볼트동기화: _bot/캘린더.md 실시간 반영 완료")
        return 0

    if args.command == "add":
        sync_vault = not args.no_sync
        res = mgr.quick_add(args.text, sync_vault=sync_vault)
        if args.json:
            out = {
                "status": "success",
                "id": res["id"],
                "summary": res["summary"],
                "time_str": res["time_str"],
                "html_link": res.get("html_link", ""),
                "synced_vault": sync_vault,
            }
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0

        print("✅ 일정 빠른 등록 완료!")
        print(f"  ID:      {res['id']}")
        print(f"  제목:    {res['summary']}")
        print(f"  일시:    {res['time_str']}")
        if res.get("html_link"):
            print(f"  링크:    {res['html_link']}")
        if sync_vault:
            print("  볼트동기화: _bot/캘린더.md 실시간 반영 완료")
        return 0

    if args.command == "delete":
        sync_vault = not args.no_sync
        mgr.delete_event(args.event_id, sync_vault=sync_vault)
        if args.json:
            print(json.dumps({"status": "success", "deleted_id": args.event_id, "synced_vault": sync_vault}, ensure_ascii=False))
            return 0
        print(f"🗑️ 일정 삭제 완료: {args.event_id}")
        if sync_vault:
            print("  볼트동기화: _bot/캘린더.md 실시간 반영 완료")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
