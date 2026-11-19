#!/usr/bin/env python3
"""src/bot/static/index.html을 css/*.css, js/*.js로 무손실 분리하는 스크립트."""

from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path

CSS_SPLITS: list[tuple[str, str | None]] = [
    ("css/base.css", None),
    ("css/layout.css", r"^    /\* ═══ 앱 레이아웃 ═══ \*/"),
    ("css/chat.css", r"^    \.messages-scroll \{"),
    ("css/views-modals.css", r"^    /\* ═══ 일반 페이지 \(설정\) ═══ \*/"),
    ("css/responsive.css", r"^    /\* ═══ 반응형 — 태블릿/모바일"),
    ("css/master-inspector.css", r"^    /\* ═══ 마스터 에이전트 모니터 인스펙터"),
]

JS_SPLITS: list[tuple[str, str | None]] = [
    ("js/core.js", None),
    ("js/rooms-sync.js", r"^    // ─── 대화방 스토리지 & 서버 동기화"),
    ("js/master-memory.js", r"^    // ─── 마스터 에이전트 모니터 인스펙터 ───"),
    ("js/ws.js", r"^    // ─── WebSocket ───"),
    ("js/markdown.js", r"^    // ─── 도구 실행 단계 & 마크다운 렌더링 ───"),
    ("js/composer.js", r"^    // ─── 대화 입력 & 전송 ───"),
    ("js/rooms-ui.js", r"^    // ─── 대화방 & 세션 ───"),
    ("js/messages.js", r"^    function onMessagesScroll\(\) \{"),
    ("js/gestures.js", r"^    // ─── 모바일 드로어 & 제스처"),
    ("js/voice.js", r"^    // ─── 세션 중단 및 음성 입력"),
    ("js/vault.js", r"^    // ─── 볼트 파일 매니저 ───"),
    ("js/settings.js", r"^    // ─── 상태 및 설정 ───"),
    ("js/init.js", r"^    // ─── 전역 단축키 ───"),
]


def dedent_4(text: str) -> str:
    lines = text.splitlines(keepends=True)
    out = [line[4:] if line.startswith("    ") else line for line in lines]
    return "".join(out).lstrip("\n").rstrip() + "\n"


def slice_by_markers(content: str, splits: list[tuple[str, str | None]]) -> list[tuple[str, str]]:
    offsets: list[tuple[str, int]] = []
    for rel_path, pattern in splits:
        if pattern is None:
            offsets.append((rel_path, 0))
            continue
        matches = list(re.finditer(pattern, content, flags=re.MULTILINE))
        if len(matches) != 1:
            raise RuntimeError(f"마커 매칭 오류 ({rel_path}): {pattern!r} -> {len(matches)}건")
        pos = matches[0].start()
        if offsets and pos <= offsets[-1][1]:
            raise RuntimeError(f"마커 순서 역전: {rel_path} (pos={pos}, prev={offsets[-1][1]})")
        offsets.append((rel_path, pos))

    slices: list[tuple[str, str]] = []
    for idx, (rel_path, start) in enumerate(offsets):
        end = offsets[idx + 1][1] if idx + 1 < len(offsets) else len(content)
        slices.append((rel_path, content[start:end]))

    # 무손실 분할 검증
    assert "".join(chunk for _, chunk in slices) == content, "분할 합계가 원본과 일치하지 않습니다."
    return slices


def apply_bugfixes(rel_path: str, code: str) -> tuple[str, list[str]]:
    fixes: list[str] = []
    if rel_path == "js/ws.js":
        old = "sendMessage(queued.text, queued.isVoiceOrigin);"
        new = "sendUserMessage(queued.text, queued.isVoiceTurn);"
        if old in code:
            code = code.replace(old, new, 1)
            fixes.append("connectWs 오프라인 큐 재전송 함수명/필드명 수정 (sendMessage -> sendUserMessage)")
    elif rel_path == "js/composer.js":
        old = "pendingChatQueue.push({ text, isVoiceOrigin });"
        new = "pendingChatQueue.push({ text, isVoiceTurn });"
        if old in code:
            code = code.replace(old, new, 1)
            fixes.append("sendUserMessage 미정의 변수 수정 (isVoiceOrigin -> isVoiceTurn)")
    elif rel_path == "js/settings.js":
        old = "(typeof findRoom === 'function') ? findRoom(currentRoomId) : null"
        new = "(typeof getRoomById === 'function') ? getRoomById(currentRoomId) : null"
        if old in code:
            code = code.replace(old, new, 1)
            fixes.append("testNotificationManual 미정의 함수 수정 (findRoom -> getRoomById)")
    return code, fixes


def main() -> None:
    parser = argparse.ArgumentParser(description="Split monolithic index.html into css/* and js/*")
    parser.add_argument(
        "--target",
        default="src/bot/static/index.html",
        help="대상 index.html 경로 (기본: src/bot/static/index.html)",
    )
    parser.add_argument("--version", default="1", help="캐시 무효화 쿼리 버전 (?v=N)")
    parser.add_argument("--dry-run", action="store_true", help="파일을 쓰지 않고 라인 수만 미리 확인")
    parser.add_argument("--fix-bugs", action="store_true", help="발견된 미정의 식별자 버그 3건 함께 수정")
    parser.add_argument("--no-backup", action="store_true", help="index.html.bak 백업 생략")
    args = parser.parse_args()

    index_path = Path(args.target)
    static_dir = index_path.parent
    raw_html = index_path.read_text(encoding="utf-8").replace("\r\n", "\n")

    # 1. <style> 추출
    style_match = re.search(r"  <style>\n([\s\S]*?)\n  </style>", raw_html)
    if not style_match:
        raise RuntimeError("<style> 블록을 찾을 수 없습니다.")
    css_body = style_match.group(1)
    css_slices = slice_by_markers(css_body, CSS_SPLITS)

    # 2. 본문 <script> 추출 (<head>의 폰트 초기화 스크립트는 유지)
    script_match = re.search(
        r"  <script>\n(    // ─── 아이콘 SVG 정의 ───[\s\S]*?)\n  </script>",
        raw_html,
    )
    if not script_match:
        raise RuntimeError("메인 <script> 블록을 찾을 수 없습니다.")
    js_body = script_match.group(1)
    js_slices = slice_by_markers(js_body, JS_SPLITS)

    # 3. 새 index.html 조립
    css_links = "\n".join(
        f'  <link rel="stylesheet" href="/{rel}?v={args.version}">'
        for rel, _ in css_slices
    )
    js_tags = "\n".join(
        f'  <script src="/{rel}?v={args.version}"></script>'
        for rel, _ in js_slices
    )

    # 뒤쪽(script)부터 치환하여 오프셋 유지
    new_html = (
        raw_html[: script_match.start()]
        + js_tags
        + raw_html[script_match.end() :]
    )
    new_html = (
        new_html[: style_match.start()]
        + css_links
        + new_html[style_match.end() :]
    )

    print(f"[검증 통과] 원본 index.html: {len(raw_html.splitlines())} lines -> 분리 후: {len(new_html.splitlines())} lines\n")
    for label, slices in [("CSS", css_slices), ("JS", js_slices)]:
        print(f"=== {label} ===")
        for rel, chunk in slices:
            lines = len(dedent_4(chunk).splitlines())
            print(f"  {rel:<28} {lines:>5} lines")
        print()

    if args.dry_run:
        print("(dry-run 모드: 실제 파일은 변경되지 않았습니다)")
        return

    if not args.no_backup:
        backup_path = index_path.with_suffix(".html.bak")
        shutil.copy2(index_path, backup_path)
        print(f"백업 생성: {backup_path}")

    applied_fixes: list[str] = []
    for rel, chunk in css_slices:
        out_path = static_dir / rel
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(dedent_4(chunk), encoding="utf-8")

    for rel, chunk in js_slices:
        code = dedent_4(chunk)
        if args.fix_bugs:
            code, fixes = apply_bugfixes(rel, code)
            applied_fixes.extend(fixes)
        out_path = static_dir / rel
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(code, encoding="utf-8")

    index_path.write_text(new_html, encoding="utf-8")
    print(f"완료: CSS {len(css_slices)}개, JS {len(js_slices)}개 생성 및 {index_path} 갱신")
    if applied_fixes:
        print("\n[적용된 버그 수정]")
        for f in applied_fixes:
            print(f"  - {f}")


if __name__ == "__main__":
    main()
