#!/usr/bin/env python3
"""gmail.py — Google Gmail CLI 관리 도구.

터미널 또는 에이전트 환경에서 Gmail 메시지를 조회, 볼트 동기화,
휴지통 이동(trash), 영구 삭제(delete)한다.
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

from bot.google.google_auth import default_google_auth
from bot.google.gmail_manager import default_gmail_manager


def main() -> int:
    parser = argparse.ArgumentParser(description="Google Gmail CLI 관리 도구")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # 1. status
    status_parser = subparsers.add_parser("status", help="Google Gmail 인증 및 동기화 상태 확인")
    status_parser.add_argument("--json", action="store_true", help="JSON 포맷 출력")

    # 2. list
    list_parser = subparsers.add_parser("list", help="최근 메일 목록 조회")
    list_parser.add_argument("--query", "-q", default="newer_than:7d", help="Gmail 검색 쿼리 (기본 'newer_than:7d')")
    list_parser.add_argument("--max", "-m", type=int, default=15, help="최대 조회 건수 (기본 15건)")
    list_parser.add_argument("--json", action="store_true", help="JSON 포맷 출력")

    # 3. sync
    sync_parser = subparsers.add_parser("sync", help="최근 메일을 볼트(_bot/메일.md)에 동기화")
    sync_parser.add_argument("--query", "-q", default="newer_than:7d", help="Gmail 검색 쿼리")
    sync_parser.add_argument("--max", "-m", type=int, default=20, help="최대 동기화 건수")
    sync_parser.add_argument("--json", action="store_true", help="JSON 포맷 출력")

    # 4. trash (휴지통 이동)
    trash_parser = subparsers.add_parser("trash", help="메일을 휴지통으로 이동 (안전 삭제)")
    trash_parser.add_argument("message_id", help="메일 ID")
    trash_parser.add_argument("--json", action="store_true", help="JSON 포맷 출력")

    # 5. delete (영구 삭제)
    delete_parser = subparsers.add_parser("delete", help="메일을 영구 삭제")
    delete_parser.add_argument("message_id", help="메일 ID")
    delete_parser.add_argument("--json", action="store_true", help="JSON 포맷 출력")

    args = parser.parse_args()

    if args.command == "status":
        account = default_google_auth.get_account_info()
        sync_status = default_gmail_manager.get_sync_status()
        if args.json:
            print(json.dumps({"account": account, "sync": sync_status}, ensure_ascii=False, indent=2))
            return 0
        authed = account.get("isAuthenticated", False)
        email = account.get("email") or "(이메일 미확인)"
        needs_reauth = account.get("needsReauth", False)
        print(f"인증 상태:        {'✅ 연동됨' if authed else '❌ 미연동'}")
        if authed:
            print(f"계정 이메일:      {email}")
            print(f"권한 갱신 필요:    {'⚠️ 필요 (gmail.modify 누락)' if needs_reauth else '✅ 정상'}")
        print(f"볼트 파일:        {sync_status.get('vaultFile')} (존재: {sync_status.get('vaultFileExists')})")
        print(f"최근 동기화:      {sync_status.get('lastSyncDisplay') or '없음'}")
        return 0

    if args.command == "list":
        try:
            messages = default_gmail_manager.list_messages(query=args.query, max_results=args.max)
            if args.json:
                print(json.dumps(messages, ensure_ascii=False, indent=2))
                return 0
            if not messages:
                print("조회된 메일이 없습니다.")
                return 0
            for idx, m in enumerate(messages, 1):
                unread = "[안읽음]" if m.get("isUnread") else "[읽음]"
                print(f"{idx}. {unread} {m.get('subject')} (ID: {m.get('id')})")
                print(f"   발신: {m.get('from')} | 일시: {m.get('date')}")
            return 0
        except Exception as exc:
            print(f"❌ 메일 목록 조회 실패: {exc}", file=sys.stderr)
            return 1

    if args.command == "sync":
        try:
            res = default_gmail_manager.sync_to_vault(query=args.query, max_results=args.max)
            if args.json:
                print(json.dumps(res, ensure_ascii=False, indent=2))
                return 0
            print(f"✅ 볼트 동기화 완료: 총 {res.get('totalCount')}건 (안읽음 {res.get('unreadCount')}건) -> {res.get('vaultFile')}")
            return 0
        except Exception as exc:
            print(f"❌ 동기화 실패: {exc}", file=sys.stderr)
            return 1

    if args.command == "trash":
        try:
            default_gmail_manager.trash_message(args.message_id)
            if args.json:
                print(json.dumps({"ok": True, "messageId": args.message_id, "action": "trash"}, ensure_ascii=False))
            else:
                print(f"🗑️ 메일 휴지통 이동 완료 (ID: {args.message_id})")
            return 0
        except Exception as exc:
            print(f"❌ 휴지통 이동 실패: {exc}", file=sys.stderr)
            return 1

    if args.command == "delete":
        try:
            default_gmail_manager.delete_message(args.message_id)
            if args.json:
                print(json.dumps({"ok": True, "messageId": args.message_id, "action": "delete"}, ensure_ascii=False))
            else:
                print(f"💥 메일 영구 삭제 완료 (ID: {args.message_id})")
            return 0
        except Exception as exc:
            print(f"❌ 메일 삭제 실패: {exc}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
