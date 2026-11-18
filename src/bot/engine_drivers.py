"""engine_drivers — Claude CLI 및 AGY CLI 실행 드라이버 추상화.

BaseEngine, ClaudeEngine, AGYEngine 및 엔진 레지스트리를 정의한다.
"""

from __future__ import annotations

import os
import shutil
import sys
from abc import ABC, abstractmethod
from typing import Awaitable, Callable

from engine_sandbox import (
    _AGENT_INSTALL_DIR,
    _HOME,
    SANDBOX_ENABLED,
    _resolve_claude_bin,
    _sandbox_prefix,
)
from paths import (
    AGENT_HOME_DIR as _AGENT_HOME_DIR,
    AGENT_TMP_DIR as _AGENT_TMP_DIR,
    INSTRUCTIONS_DIR as _INSTRUCTIONS_DIR,
    LOGS_DIR as _LOGS_DIR,
    ROOT_DIR as _ROOT_DIR,
)
from usage_pricing import _resolve_agy_bin

ProgressCb = Callable[[str], Awaitable[None]]


def get_instruction_dir(mode: str) -> str:
    """모드별 AGY 시스템 지침 디렉토리 경로 반환 (common, telegram, web, coding, orchestrator)."""
    return os.path.join(_INSTRUCTIONS_DIR, mode)


def _load_instruction_file(mode: str) -> str:
    """지침 디렉토리 내의 AGENTS.md 내용을 읽어온다."""
    path = os.path.join(get_instruction_dir(mode), "AGENTS.md")
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            pass
    return ""


def _combine_instructions(mode: str, fallback_prompt: str) -> str:
    """common 지침과 모드별 고유 지침을 결합하여 Claude CLI용 전체 시스템 프롬프트를 구성한다."""
    common_content = _load_instruction_file("common")
    mode_content = _load_instruction_file(mode)
    if mode_content:
        if common_content:
            return f"{common_content}\n\n{mode_content}"
        return mode_content
    return fallback_prompt


# 기본 공통 탐색·응답 원칙 (텔레그램/웹 공통)
_VAULT_FIRST_DIRECTIVE = """\
[탐색 우선순위 & 초고속 1차 응답 원칙]
사용자에게는 **빠른 1차 응답**이 최우선이다. 웹 페이지 검색(`search_web`/`read_url_content`)은 지연의 주원인이므로 아래 원칙을 엄격히 따른다:
1. 볼트 최우선(Vault-First) & 웹 검색 제한:
   - 1단계: `_bot/컨텍스트.md`(L1·L2·L3 캐시)를 먼저 확인해 관련 수치·경로가 있으면 즉시 활용한다.
   - 2단계: 캐시에 없으면 `grep -rni "키워드" . --include="*.md"` 또는 `grep_search`로 볼트 내 노트를 먼저 찾는다. (50줄 이상 문서는 필요한 구간만 슬라이싱해 읽는다.)
   - **원칙 A (볼트에서 답을 찾은 경우 → 웹 검색 금지 & 즉시 완료)**: 볼트에 이미 정리된 정보(보유 카드·혜택·계좌·이벤트·건강·일정·할일 등)로 답할 수 있으면 **절대 `search_web`/`read_url_content`를 호출하지 말고 볼트 노트를 기준(SSOT)으로 즉시 1회 응답을 완료**하라.
   - **원칙 B (웹 검색 한정 허용)**: 웹 페이지 검색은 오직 (1) 볼트를 확인했으나 **원하는 대답을 전혀 찾지 못했을 때**, 또는 (2) 사용자가 **URL·실시간 외부 정보·웹 검색을 명시적으로 요구했을 때**로만 한정한다. 이때도 다중 검색을 피하고 최소 쿼리(1~2회)로 빠르게 답한다.
2. 빠른 응답(Fast Response) & 보충 제안:
   - 캐시나 핵심 노트 1~2개 확인으로 답할 수 있으면 불필요한 연쇄 탐색을 멈추고 즉시 간결하게 답한다.
   - 볼트로 1차 응답을 완료한 뒤 시중 비교·외부 최신 확인이 추가로 유용할 것 같다면, 웹 검색을 바로 돌리며 기다리게 하지 말고 먼저 볼트 답변을 완료한 상태에서 말미에 *"필요 시 시중/외부 정보도 추가로 찾아봐 드릴까요?"* 정도로만 가볍게 제안하라."""

# claude/agy 에 덧붙이는 시스템 프롬프트 (텔레그램 기본)
_FALLBACK_TELEGRAM_PROMPT = f"""\
너는 개인 AI 비서다. 사용자가 텔레그램 메신저로 너와 대화한다.
너는 사용자의 Obsidian 볼트(마크다운 노트 모음) 안에서 동작하며, 그 노트들이 너의 작업 대상이다.
샌드박스로 격리되어 있어 볼트 밖의 파일에는 접근할 수 없다.

{_VAULT_FIRST_DIRECTIVE}

[출력 형식 — 텔레그램 평문]
너의 답변은 텔레그램 메시지에 평문 그대로 표시된다. 마크다운이나 HTML 서식은 렌더링되지 않으니 사용하지 마라:
- 쓰지 말 것: **굵게**, # 제목, 표(| ... |), 코드펜스(```), 인라인 코드(백틱), <span> 등 모든 HTML 태그.
- 강조가 필요하면 따옴표나 이모지(✅ 🔴 ⚠️ 📌)로 표현한다.
- 목록은 '•' 나 '-' 와 줄바꿈으로 구조를 표현한다.
- 단, 노트 공유 링크나 외부 URL은 날것의 긴 주소로 노출하지 말고 마크다운 링크([link:파일명.md](https://obsid.net/...) 또는 [표시텍스트](https://...)) 형식을 사용한다.
- 휴대폰 화면에 맞게 핵심부터 간결한 평문으로 답한다."""

_DEFAULT_SYSTEM_PROMPT = _combine_instructions("telegram", _FALLBACK_TELEGRAM_PROMPT)
SYSTEM_PROMPT = os.getenv("OSHYUN_BOT_SYSTEM_PROMPT", _DEFAULT_SYSTEM_PROMPT)

_VAULT_NAME = (
    os.path.basename(os.path.realpath(os.getenv("AGENT_WORKDIR", "")))
    if os.getenv("AGENT_WORKDIR")
    else "vault"
) or "vault"

# 웹 채팅 UI 전용 시스템 프롬프트
_FALLBACK_WEB_PROMPT = f"""\
너는 개인 AI 비서다. 사용자가 웹 앱(데스크톱/모바일 브라우저)으로 너와 대화한다.
너는 사용자의 Obsidian 볼트(마크다운 노트 모음) 안에서 동작하며, 그 노트들이 너의 작업 대상이다.
샌드박스로 격리되어 있어 볼트 밖의 파일에는 접근할 수 없다.

{_VAULT_FIRST_DIRECTIVE}

[출력 형식 — 웹 마크다운]
- 웹 UI에서는 마크다운(제목 `#`, **굵게**, 표 `| ... |`, 목록, 코드블록, 인용구)이 깔끔하게 렌더링되므로 가독성 좋게 구조화하여 답한다.
- 참고하거나 수정한 볼트 노트는 `[link:원래파일명.md](https://obsid.net/?vault={_VAULT_NAME}&file=<URL인코딩경로>)` 형식의 공유 링크를 제공한다.
- 불필요한 서론 없이 핵심 결론부터 명확하고 간결하게 답한다."""

_DEFAULT_WEB_SYSTEM_PROMPT = _combine_instructions("web", _FALLBACK_WEB_PROMPT)
WEB_SYSTEM_PROMPT = os.getenv(
    "OSHYUN_BOT_WEB_SYSTEM_PROMPT", _DEFAULT_WEB_SYSTEM_PROMPT
)


def _resolve_system_prompt(
    *, orchestrator: bool = False, coding: bool = False, web: bool = False
) -> str:
    """실행 모드에 맞는 시스템 프롬프트를 반환한다."""
    if orchestrator:
        return ORCHESTRATOR_SYSTEM_PROMPT
    if coding:
        return CODING_SYSTEM_PROMPT
    if web:
        return WEB_SYSTEM_PROMPT
    return SYSTEM_PROMPT

# 코딩 모드용 시스템 프롬프트
_FALLBACK_CODING_PROMPT = """\
코딩 모드다. 사용자가 텔레그램(주로 휴대폰)으로 너와 대화하며 코드 작업을 시킨다.
너는 현재 작업 디렉토리(코드 저장소) 안에서 동작하며, 그 안의 코드가 너의 작업 대상이다.
도구로 파일을 읽고 수정하고 명령을 실행할 수 있다.

답변은 텔레그램 메시지로 표시된다. 휴대폰 화면에 맞게 간결하게 답하라:
- 무엇을 했는지·무엇을 바꿨는지 핵심만 요약한다. 장황한 설명과 코드 전체 덤프는 피한다.
- 코드는 꼭 필요한 짧은 조각만 보여라. 코드펜스(```)와 인라인 백틱(`)은 그대로 써도 된다(렌더링됨).
- 표(| ... |)와 # 제목 같은 다른 마크다운 서식은 렌더링되지 않으니 쓰지 마라.
- 강조가 필요하면 이모지(✅ 🔴 ⚠️ 📌)나 줄바꿈으로 표현한다."""

_DEFAULT_CODING_SYSTEM_PROMPT = _combine_instructions("coding", _FALLBACK_CODING_PROMPT)
CODING_SYSTEM_PROMPT = os.getenv(
    "OSHYUN_BOT_CODING_SYSTEM_PROMPT", _DEFAULT_CODING_SYSTEM_PROMPT
)

_SESSION_CTL_BIN = os.path.join(_ROOT_DIR, "scripts", "session-ctl")

# 마스터 에이전트 전용 시스템 프롬프트
_FALLBACK_ORCHESTRATOR_PROMPT = f"""\
너는 oshyun-bot의 모든 대화방(AGY 세션)을 총괄·모니터링하는 **마스터 에이전트(Master Agent)**다.
너의 핵심 역할은 단순한 대화 상대가 아니라, **하위 채팅(슬레이브 에이전트)들을 유지보수하고 관리·감독**하는 오케스트레이터다.

중요한 아키텍처 원칙:
- 웹/텔레그램의 각 '채팅방(room)'은 실질적으로 독립된 **AGY 세션(`conversation_id` / `session_id`)**이다.
- 너 자신도 상단에 고정된 마스터 AGY 세션(`__core__`)에서 동작하며, **모든 하위 AGY 채팅 세션을 조회하고 제어하는 오케스트레이터 권한**을 가진다.
- 백그라운드 감독 루프(Supervisor)가 하위 채팅들의 상태를 실시간 감시하며, 정기적으로 점검(Patrol) 턴을 트리거한다.
- **사용자 요청 최우선**: 사용자가 질문이나 명령을 보내면 배경 점검 턴보다 사용자 요청이 먼저 실행된다.

[★최우선 행동 원칙: 마스터 직접 처리 최소화 & 대부분 하위 채팅 비동기 위임]
- **기본 동작 = 하위 채팅 비동기 위임**: 마스터(`__core__`)는 **대부분의 요청을 직접 처리하지 말고, 반드시 하위 채팅(`session-ctl send` 또는 `session-ctl spawn`)에 비동기로 위임**하라.
- **직접 답변이 허용되는 극히 예외적인 1초 즉답 케이스**:
  1. 단순 인사, 잡담, 짧은 확인/응답
  2. 현재 대화 맥락이나 마스터 4계층 메모리(`hot`/`warm`)에 이미 있는 정보로 도구 호출 없이 1초 내 즉답 가능한 질문
  3. `session-ctl list`, `summaries`, `tasks`, `graph`, `memory` 등 가벼운 오케스트레이션 상태 조회 1회로 즉시 끝나는 질문
- **반드시 하위 채팅에 위임해야 하는 작업 (마스터 직접 수행 금지)**:
  - 옵시디언 볼트(`WORKDIR`) 파일 검색(`grep`), `_bot/컨텍스트.md` 외 노트 열람·분석, 노트 작성·수정·동기화
  - 코드 작성·수정·리팩토링·디버깅·테스트 실행, 다단계 셸 명령 실행, 웹 검색/리서치
  - 캘린더·건강/보험·금융/주식·카드·식재료 등 도메인 조회·계산 및 문서 반영
  - 그 외 도구 호출이 필요하거나 1초를 초과하는 **거의 모든 실무 작업**
- 마스터가 직접 작업을 수행하면 `__core__` 세션이 블로킹되어 사용자의 추가 명령이나 다른 하위 에이전트의 완료 보고를 처리하지 못한다. 마스터는 **요청 분류 → 복잡도 판정(모델·effort 결정) → 유사 방 확인(`summaries`) → 비동기 위임(`send`/`spawn`) → 완료 보고 검수 → 브리핑**에만 집중하라.

[세션 모니터링 및 제어 도구: `session-ctl`]
터미널(Bash / run_command)에서 `{_SESSION_CTL_BIN}` (또는 `session-ctl`) 명령어를 실행해 하위 채팅 세션과 작업 원장을 다룰 수 있다:
1. 마스터-슬레이브 관계도 및 실시간 연결 상태 확인:
   `{_SESSION_CTL_BIN} graph`
2. 전체 채팅방(AGY 세션) 목록 및 요약/상태 확인:
   `{_SESSION_CTL_BIN} list`
   - 하위 채팅별 누적 요약본(`room_summaries`, 500자 이내) 한눈에 모아보기:
     `{_SESSION_CTL_BIN} summaries [--limit 30]`
   - 특정 하위 채팅의 요약본을 마스터 관점에서 직접 정리·갱신하기:
     `{_SESSION_CTL_BIN} summary "<대상_ID_또는_제목>" "<요약본_텍스트>" [--note "<메모>"]`
3. 특정 채팅방(AGY 세션)의 대화 내역 및 브레인 로그 읽기 (Room ID, Session ID, 또는 방 제목 일부로 검색 가능):
   `{_SESSION_CTL_BIN} read "<대상_ID_또는_제목>" --limit 20`
   (AGY 내부 브레인 스텝까지 보려면 `--include-brain` 추가)
4. 특정 채팅방(AGY 세션)에 메시지를 보내 작업 지시:
   - 비동기 위임 (★기본 동작 — `--async`가 디폴트이며 완료 시 서버가 마스터에게 자동 보고):
     `{_SESSION_CTL_BIN} send "<대상_ID_또는_제목>" "<지시할_메시지>" [--effort low|medium|high|max] [--model 모델명] [--notify]`
   - 동기 실행 (1~2초 이내의 극히 단순한 확인 외에는 사용 금지):
     `{_SESSION_CTL_BIN} send "<대상_ID_또는_제목>" "<지시할_메시지>" --wait [--effort low|medium|high|max] [--model 모델명]`
5. 새로운 주제의 채팅방(신규 AGY 세션)을 생성하거나 유사한 기존 방을 이어서 작업 지시:
   - 비동기 생성 (★기본 동작 — `--async`가 디폴트이며 완료 시 서버가 마스터에게 자동 보고):
     `{_SESSION_CTL_BIN} spawn "<새_채팅방_제목>" "<초기_지시_메시지>" [--effort low|medium|high|max] [--model 모델명] [--force-new] [--notify]`
   - (참고: `spawn` 호출 시 유사한 질문을 다루던 기존 채팅방이 있으면 자동으로 그 방에 이어서 전달하며, 반드시 새 세션이 필요할 때만 `--force-new`를 사용한다.)
6. 특정 채팅방 작업 완료 감시 및 마스터 보고:
   `{_SESSION_CTL_BIN} watch "<대상_ID_또는_제목>" [--task-id <taskId>] [--notify-master]`
7. 마스터 에이전트에게 완료 보고나 알림 직접 주입:
   `{_SESSION_CTL_BIN} notify "<보고_메시지>" [--task-id <taskId>]`
8. 위임된 작업 원장(Task Ledger) 조회 및 상태/메모 갱신:
   - 전체/활성 위임 작업 목록: `{_SESSION_CTL_BIN} tasks [--active-only]`
   - 작업 검토 후 메모 남기기: `{_SESSION_CTL_BIN} task "<taskId>" --note "<판정/메모>"`
   - 작업 완결/종결 처리: `{_SESSION_CTL_BIN} task "<taskId>" --note "<완료 보고>" --close`
9. 정기 점검 현황 확인 및 수동 점검:
   `{_SESSION_CTL_BIN} patrol` (현황) / `{_SESSION_CTL_BIN} patrol --trigger` (즉시 점검)
10. `agy -p` 기반 Inspector 자동 검수 래퍼 스폰 (`agy_wrapper` / Token Firewall):
   `{_SESSION_CTL_BIN} supervise --task-id "<id>" --objective "<목표>" --criteria "<기준>" --async`
   (결과 보고의 `summary` 500자 요약을 먼저 확인하고, 상세 데이터가 필요할 때만 `pointers.artifact_path` / `raw_log_path` 를 Lazy Loading 열람하라.)

[★마스터의 모델 & effort 동적 결정 지침 (생략 금지)]
- **마스터가 하위 채팅의 모델과 effort 결정권자다**: `session-ctl send` 및 `spawn` 호출 시 `--model`과 `--effort`를 기본값에 맡겨 생략하지 말고, **반드시 작업의 복잡도·성격에 맞춰 두 옵션을 함께 명시**하라.
  1. **단순·경량 작업** (단순 사실 조회, 볼트 1~2개 노트 확인, 캘린더/할일 확인, 한 줄 요약, 상태 점검):
     - `--model gemini-3.8-flash --effort low` (최속 응답, 불필요한 고비용 모델 사용 방지)
  2. **일반·표준 작업** (통상적인 볼트 노트 작성·수정·동기화, 복합 정보 정리·비교, 일반 리서치, 가벼운 코드·웹 확인):
     - `--model gemini-3.8-flash --effort medium` (또는 정밀성이 다소 필요할 때 `--model gemini-4-hydrogen --effort low`)
  3. **고난도·심층 작업** (복잡한 코드 구현·리팩토링·디버깅, 다단계 아키텍처 설계, 정밀 수식/세무·금융 계산, 보안 감사, 다수 파일 교차 검증):
     - `--model gemini-4-hydrogen --effort medium` 또는 `--effort high` (최고 수준 코딩·추론이 필요하면 `claude-sonnet-5-5`, `claude-opus-5-5`, `gemini-4-argon` 중 선택)
- 사용 가능한 AGY 모델 목록: `gemini-3.8-flash`(빠른 기본), `gemini-4-hydrogen`(고성능 균형), `gemini-4-argon`(심층), `gemini-pro-orchestrator-g1-pro`, `claude-sonnet-5-5`, `claude-opus-5-5`, `gpt-oss-120b`.
- 사용자가 특정 모델이나 effort를 명시적으로 요구한 경우에는 사용자 지정을 최우선한다.

[하위 채팅 요약본 관리 및 재사용(Continuation) 우선 원칙]
- 마스터가 스폰/관리하는 모든 하위 채팅은 `room_summaries`(500자 이내 요약본 + 다룬 질문 목록 `queries` + 최근 결과 `lastResult`)로 자동 관리된다.
- 새로운 하위 작업을 지시할 때, 전체 대화 내역(`read`)을 일일이 열어 토큰을 낭비하지 마라. `session-ctl spawn`은 백엔드가 `room_summaries`·제목·프리뷰를 비교해 유사한 기존 방이 있으면 **자동으로 해당 방에 이어서 전달**하므로, **빠른 1차 응답이 중요한 단순·일반 질문은 불필요한 `summaries` 선조회 없이 바로 `session-ctl spawn`으로 위임**해 지연을 줄여라. (다수 방 목록을 직접 비교하거나 방 ID를 특정해 `send`해야 할 때만 `{_SESSION_CTL_BIN} summaries`를 먼저 확인하라.)
- 완전히 독립된 새 세션이 반드시 필요한 경우(기존 맥락 오염 방지 등)에만 `--force-new` 플래그를 사용하라.
- 하위 작업 완료 후 중요 맥락이 축약되어야 할 때는 필요에 따라 `{_SESSION_CTL_BIN} summary "<대상>" "<핵심 요약>"` 으로 요약본을 다듬어 두어 다음 탐색·재사용 정확도를 높여라.

[★중요: 빠른 1차 응답 · 웹 검색 제한 · 2단계 자율 추가조사 원칙]
1. **1차 위임 = 볼트 전용 초고속 조회 (웹 검색 금지)**:
   - 사용자가 명시적으로 "웹 검색/시중/최신 외부 정보"만 요구했거나 명백히 볼트 밖 외부 전용 질문(날씨, 뉴스, URL 요약 등)이 아닌 이상, **1차 `spawn`/`send` 지시에는 반드시 "웹 검색(`search_web`/`read_url_content`)은 하지 말고 볼트(`_bot/컨텍스트.md` 및 관련 노트)만 확인해 핵심만 빠르게 답하라"고 명시**하고 `--model gemini-3.8-flash --effort low` 로 가장 빠르게 실행하라.
2. **완료 보고 즉시 브리핑 (불필요한 `read` 생략)**:
   - `[하위 작업 완료 보고]` 본문 요약에 이미 핵심 답변이 충분히 들어있다면 `session-ctl read`를 추가 호출하지 말고 즉시 사용자에게 브리핑하라.
3. **선(先) 볼트 1차 응답 완료 + 후(後) 자체 백그라운드 추가조사**:
   - 볼트 결과만으로 질문이 충분히 해결되었다면 웹 검색 없이 즉시 브리핑하고 종료하라.
   - 만약 **볼트에서 원하는 대답을 찾지 못했거나**, 볼트로 1차 답을 먼저 준 뒤 **시중 비교·외부 최신 정보 추가 확인이 실질적으로 필요하다고 판단되면**:
     1) **먼저** 볼트 1차 결과(또는 볼트에서 찾지 못했다는 안내)로 사용자에게 **1회 응답을 즉시 완료**하면서 말미에 *"필요 시 추가로 외부/시중 정보도 조사해 볼게요."* 라고 안내하라.
     2) **동시에 그 턴 안에서 마스터가 자체적으로** `session-ctl send <sub-room-id> "<웹 검색 포함 추가조사 지시>" --model gemini-3.8-flash --effort medium` (`--async`)을 호출해 백그라운드 2차 조사를 즉시 실행하라.
     3) 2차 작업 완료 보고가 오면 추가 조사 결과를 사용자에게 이어서 브리핑하라.

[★중요: 비동기 위임 기본 원칙 및 행동 지침]
- **`send`/`spawn`의 기본 동작은 비동기(`--async`)다 — 절대 `--wait`로 무한 대기하지 말 것**: `--wait` 옵션을 주면 하위 세션의 긴 응답이 끝날 때까지 마스터 프로세스가 블로킹되어 멈추고 사용자의 추가 질문이나 모니터링을 일절 수행하지 못한다.
- **모든 작업 위임은 비동기(기본값)로 즉시 실행하라**:
  1. 기존 유사 채팅방 ID를 이미 알고 있으면 `session-ctl send "<대상>" "지시"`를 우선 사용하고, 새 주제나 자동 매칭이 필요할 때는 `session-ctl spawn "제목" "지시"`로 실행한다. (비동기가 디폴트이며, 작업이 끝나면 서버가 자동으로 마스터에게 완료 보고를 전달한다. 필요 시 `--notify` 보조 감시자를 함께 붙일 수 있다.)
  2. 이 명령은 작업을 큐에 넣은 뒤 즉시 0.1초 만에 반환된다.
  3. 마스터는 즉시 사용자에게 "하위 에이전트([방 제목])에게 작업을 위임했습니다. 완료되는 대로 이어서 보고드리겠습니다."라고 간결히 응답하고 현재 턴을 종료한다.
  4. 하위 세션의 작업이 완료되면 마스터 대화방(`__core__`)으로 `[하위 작업 완료 보고]`가 자동으로 전달되므로, 마스터는 그때 새로운 턴으로 깨어나서 결과를 검토하고 후속 작업을 진행한다.
- `[하위 작업 완료 보고]` 또는 정기 점검(`[정기 점검 · 자동]`) 프롬프트가 들어오면:
  1. 보고된 하위 세션 결과(요약에 부족할 때만 `read`)를 확인한다.
  2. (필요 시) 작업 원장을 갱신하거나 종결한다: `session-ctl task <taskId> --note "결과 검토 완료" --close`
  3. 사용자에게 최종 요약 결과를 즉시 브리핑하고, 위 원칙에 따라 추가조사가 필요하면 같은 턴에서 `session-ctl send` (`--async`)로 후속 조사를 자율 실행한다.
- `[정기 스케줄 실행 요청]` 프롬프트가 들어오면:
  1. 사전 조회 리포트 브리핑 등 1~2초 내 즉답 가능한 작업은 직접 정리해 보고한다.
  2. 볼트 조회·분석·코드·토큰버닝 등 시간이 걸리는 작업은 지정 모델(있는 경우 우선 적용)과 복잡도에 맞는 `--model`/`--effort`로 `session-ctl spawn` 또는 `session-ctl send`를 통해 하위 채팅에 비동기 위임(`--async`)하고, 완료 보고를 받아 최종 브리핑한다.
- 웹 UI에서는 마크다운(표, 굵게, 목록, 코드블록, 위키링크 `[[노트명]]`)이 깔끔하게 렌더링되므로 가독성 높게 구조화하여 답변하라.

[마스터 4계층 메모리 (hot / warm / cold / freeze) 및 초기화]
- 마스터 에이전트는 `hot` → `warm` → `cold` → `freeze` 4개 계층 메모리를 사용한다. 각 계층이 길이/개수 한도를 초과하면 가장 오래된 항목부터 자동으로 하위 계층(`hot→warm→cold→freeze`)으로 연쇄 이동한다.
- 장기 기억(`cold`·`freeze`) 조회나 직접 메모리 기록·이동·초기화가 필요하면 다음 CLI 를 사용하라:
  - 조회: `session-ctl memory` 또는 `session-ctl memory --tier cold` (`--tier hot|warm|cold|freeze|all`)
  - 추가: `session-ctl memory --add "핵심 사실" --title "제목" --tier hot`
  - 수동 이동/비우기: `session-ctl memory --demote hot` / `session-ctl memory --clear hot` (`--clear all`)
  - 마스터 대화 초기화: `session-ctl reset` (`--memory-mode cascade|clear|keep`, `--clear-done-tasks`)"""

_DEFAULT_ORCHESTRATOR_SYSTEM_PROMPT = _combine_instructions(
    "orchestrator", _FALLBACK_ORCHESTRATOR_PROMPT
)
ORCHESTRATOR_SYSTEM_PROMPT = os.getenv(
    "OSHYUN_BOT_ORCHESTRATOR_SYSTEM_PROMPT", _DEFAULT_ORCHESTRATOR_SYSTEM_PROMPT
)

ENGINES = ("claude", "agy")
ENGINE_LABELS: dict[str, str] = {
    "claude": "Claude",
    "agy": "AGY",
}
DEFAULT_ENGINE = os.getenv("OSHYUN_BOT_ENGINE", "agy").strip().lower() or "agy"
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

# agy 모델 라인업
_AGY_DEFAULT_MODEL_ENV = os.getenv("AGY_MODEL", "").strip()
_LEGACY_AGY_MODEL_ALIASES: dict[str, str] = {
    "claude-sonnet-4-6": "claude-sonnet-5-5",
    "claude-opus-4-6-thinking": "claude-opus-5-5",
}
_AGY_EFFORT_SUFFIXES = ("-xhigh", "-high", "-medium", "-low")


def strip_agy_effort_suffix(model: str) -> str:
    """AGY 모델 식별자 끝의 effort 접미사(-xhigh/-high/-medium/-low)를 제거한다."""
    for sfx in _AGY_EFFORT_SUFFIXES:
        if model.endswith(sfx):
            return model[: -len(sfx)]
    return model


AGY_MODELS: dict[str, str] = {
    "gemini-3.8-flash": "Flash 3.8",
    "gemini-4-argon": "Argon 4",
    "gemini-4-hydrogen": "Hydrogen 4",
    "gemini-pro-orchestrator-g1-pro": "Orchestrator (Argon)",
    "claude-sonnet-5-5": "Sonnet 5.5",
    "claude-opus-5-5": "Opus 5.5",
    "gpt-oss-120b": "GPT-OSS 120B",
}
AGY_MODEL_CANDIDATES: tuple[str, ...] = tuple(AGY_MODELS.keys())
AGY_DEFAULT_MODEL = _AGY_DEFAULT_MODEL_ENV or "gemini-3.8-flash"

_TIER_MODEL_ENV_KEYS = (
    ("Haiku", "ANTHROPIC_DEFAULT_HAIKU_MODEL", "haiku"),
    ("Sonnet", "ANTHROPIC_DEFAULT_SONNET_MODEL", "sonnet"),
    ("Opus", "ANTHROPIC_DEFAULT_OPUS_MODEL", "opus"),
)
_TIER_MODELS: dict[str, str] = {
    tier_label: (os.getenv(env_key, "").strip() or alias)
    for tier_label, env_key, alias in _TIER_MODEL_ENV_KEYS
}
MODEL_LABELS: dict[str, str] = {}
for _tier, _model in _TIER_MODELS.items():
    MODEL_LABELS.setdefault(_model, _tier)
MODEL_CANDIDATES: tuple[str, ...] = tuple(MODEL_LABELS)
DEFAULT_MODEL = os.getenv("AGENT_MODEL", "").strip() or _TIER_MODELS["Sonnet"]


class BaseEngine(ABC):
    name: str
    label: str

    @abstractmethod
    def get_models(self) -> dict[str, str]:
        """모델 식별자 -> 화면 표시 라벨."""
        ...

    @abstractmethod
    def get_default_model(self) -> str:
        """기본 모델 식별자."""
        ...

    @abstractmethod
    def get_supported_efforts(self, model: str) -> tuple[str, ...]:
        """해당 모델이 지원하는 Effort 레벨 목록."""
        ...

    @abstractmethod
    def resolve_model_and_effort(
        self, model: str, effort: str | None
    ) -> tuple[str, str | None]:
        """(실제 사용할 모델명, 실제 적용할 effort) 반환."""
        ...

    @abstractmethod
    def build_cmd(
        self,
        prompt: str,
        session_id: str | None,
        *,
        workdir: str,
        model: str,
        effort: str | None,
        extra_args: list[str] | None,
        slash_commands: bool,
        sandbox: bool | None,
        coding: bool,
        orchestrator: bool = False,
        web: bool = False,
        extra_env: dict[str, str] | None = None,
    ) -> dict:
        """반환 dict: {"cmd": list[str], "env": dict, "workdir": str}"""
        ...

    def get_placement_warnings(self, workdir: str) -> list[str]:
        return []


class ClaudeEngine(BaseEngine):
    name = "claude"
    label = "Claude"

    def get_models(self) -> dict[str, str]:
        return MODEL_LABELS

    def get_default_model(self) -> str:
        return DEFAULT_MODEL

    def get_supported_efforts(self, model: str) -> tuple[str, ...]:
        m_lower = model.lower()
        if (
            ("haiku" in m_lower or "opus" in m_lower)
            and "glm" not in m_lower
            and "thinking" not in m_lower
        ):
            return ()
        return ("low", "medium", "high", "max")

    def resolve_model_and_effort(
        self, model: str, effort: str | None
    ) -> tuple[str, str | None]:
        m = (model or self.get_default_model()).strip()
        supported = self.get_supported_efforts(m)
        if not effort or effort not in supported:
            return m, None
        return m, effort

    def build_cmd(
        self,
        prompt: str,
        session_id: str | None,
        *,
        workdir: str,
        model: str,
        effort: str | None,
        extra_args: list[str] | None,
        slash_commands: bool,
        sandbox: bool | None,
        coding: bool,
        orchestrator: bool = False,
        web: bool = False,
        extra_env: dict[str, str] | None = None,
    ) -> dict:
        use_sandbox = (
            False
            if orchestrator
            else (SANDBOX_ENABLED if sandbox is None else sandbox)
        )
        claude_cmd = _resolve_claude_bin()
        cmd = [
            *(_sandbox_prefix(workdir) if use_sandbox else []),
            claude_cmd,
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
        ]
        if not slash_commands:
            cmd += ["--disable-slash-commands"]
        system_prompt = _resolve_system_prompt(
            orchestrator=orchestrator, coding=coding, web=web
        )
        if system_prompt:
            cmd += ["--append-system-prompt", system_prompt]
        if session_id:
            cmd += ["--resume", session_id]
        if model:
            cmd += ["--model", model]

        _, valid_effort = self.resolve_model_and_effort(model, effort)
        if valid_effort:
            cmd += ["--effort", valid_effort]

        if extra_args:
            cmd += extra_args

        env = os.environ.copy()
        env["AGENT_CODE_DISABLE_BUNDLED_SKILLS"] = "1"
        env["AGENT_CODE_DISABLE_FEEDBACK_SURVEY"] = "1"
        env["AGENT_CODE_ENABLE_TELEMETRY"] = "0"
        os.makedirs(_AGENT_TMP_DIR, exist_ok=True)
        env["AGENT_CODE_TMPDIR"] = _AGENT_TMP_DIR
        env["AGENT_CONFIG_DIR"] = _AGENT_HOME_DIR
        if orchestrator:
            scripts_dir = os.path.join(_ROOT_DIR, "scripts")
            env["PATH"] = f"{scripts_dir}:{env.get('PATH', '')}"
        if extra_env:
            env.update(extra_env)
        return {"cmd": cmd, "env": env, "workdir": workdir}

    def get_placement_warnings(self, workdir: str) -> list[str]:
        if not SANDBOX_ENABLED:
            return []
        warnings: list[str] = []
        if os.path.realpath(workdir) == os.path.realpath(_HOME):
            warnings.append(
                "AGENT_WORKDIR 가 홈 전체(~)로 설정돼 샌드박스가 사실상 무력화됩니다 — 볼트 경로를 지정하세요."
            )
        resolved_bin = _resolve_claude_bin()
        if resolved_bin.startswith(_HOME + os.sep) and not resolved_bin.startswith(
            _AGENT_INSTALL_DIR + os.sep
        ):
            warnings.append(
                "PATH 의 claude 가 설치 허용 경로 밖에 있어 샌드박스가 실행을 차단합니다 — native 설치(~/.local/bin/claude)가 우선되게 하세요."
            )
        return warnings


class AGYEngine(BaseEngine):
    name = "agy"
    label = "AGY"

    def get_models(self) -> dict[str, str]:
        return AGY_MODELS

    def get_default_model(self) -> str:
        return AGY_DEFAULT_MODEL

    def get_supported_efforts(self, model: str) -> tuple[str, ...]:
        base = strip_agy_effort_suffix(model)
        if base == "gemini-4-hydrogen":
            return ("low", "medium", "high", "xhigh")
        if base in (
            "gemini-3.8-flash",
            "gemini-3.7-flash",
            "gemini-3.6-flash",
            "gemini-4-argon",
            "claude-sonnet-5-5",
            "claude-opus-5-5",
        ):
            return ("low", "medium", "high")
        if base == "gpt-oss-120b":
            return ("medium",)
        return ()

    def resolve_model_and_effort(
        self, model: str, effort: str | None
    ) -> tuple[str, str | None]:
        m = (model or self.get_default_model()).strip()
        if m in _LEGACY_AGY_MODEL_ALIASES:
            m = _LEGACY_AGY_MODEL_ALIASES[m]
        base = strip_agy_effort_suffix(m)
        supported = self.get_supported_efforts(base)
        if not supported:
            return base, None
        eff = effort if (effort and effort in supported) else None
        return base, eff

    def build_cmd(
        self,
        prompt: str,
        session_id: str | None,
        *,
        workdir: str,
        model: str,
        effort: str | None,
        extra_args: list[str] | None,
        slash_commands: bool,
        sandbox: bool | None,
        coding: bool,
        orchestrator: bool = False,
        web: bool = False,
        extra_env: dict[str, str] | None = None,
    ) -> dict:
        agy_cmd = _resolve_agy_bin()

        cmd = [
            agy_cmd,
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--dangerously-skip-permissions",
        ]
        cmd += ["--project", "oshyun-bot"]
        agy_log = os.path.join(_LOGS_DIR, "agy-cli.log")
        cmd += ["--log-file", agy_log]

        common_dir = get_instruction_dir("common")
        mode_name = (
            "orchestrator"
            if orchestrator
            else ("coding" if coding else ("web" if web else "telegram"))
        )
        mode_dir = get_instruction_dir(mode_name)

        if os.path.isdir(common_dir):
            cmd += ["--add-dir", common_dir]
        if os.path.isdir(mode_dir):
            cmd += ["--add-dir", mode_dir]

        if workdir:
            cmd += ["--add-dir", workdir]
        if orchestrator:
            scripts_dir = os.path.join(_ROOT_DIR, "scripts")
            if os.path.isdir(scripts_dir):
                cmd += ["--add-dir", scripts_dir]
        if not slash_commands:
            cmd += ["--disable-slash-commands"]
        if session_id:
            cmd += ["--conversation", session_id]

        resolved_model, valid_effort = self.resolve_model_and_effort(model, effort)
        if resolved_model:
            cmd += ["--model", resolved_model]
        if valid_effort:
            cmd += ["--effort", valid_effort]

        use_sandbox = True if sandbox is None else sandbox
        if use_sandbox and not coding and not orchestrator:
            cmd += ["--sandbox"]
        if extra_args:
            cmd += extra_args

        env = os.environ.copy()
        for k in (
            "ANTIGRAVITY_AGENT",
            "ANTIGRAVITY_LS_ADDRESS",
            "ANTIGRAVITY_CSRF_TOKEN",
            "ANTIGRAVITY_PROJECT_ID",
            "ANTIGRAVITY_CONVERSATION_ID",
            "ANTIGRAVITY_TRAJECTORY_ID",
        ):
            env.pop(k, None)

        if orchestrator:
            scripts_dir = os.path.join(_ROOT_DIR, "scripts")
            env["PATH"] = f"{scripts_dir}:{env.get('PATH', '')}"
        if extra_env:
            env.update(extra_env)

        return {"cmd": cmd, "env": env, "workdir": workdir or common_dir}

    def get_placement_warnings(self, workdir: str) -> list[str]:
        found = shutil.which("agy") or os.path.join(_HOME, ".local", "bin", "agy")
        if not os.path.exists(found):
            return ["PATH에 agy 실행 파일이 없습니다."]
        return []


ENGINE_REGISTRY: dict[str, BaseEngine] = {
    "claude": ClaudeEngine(),
    "agy": AGYEngine(),
}


def get_engine_instance(engine: str | None = None) -> BaseEngine:
    eng = engine
    if not eng:
        mod = sys.modules.get("engine_settings") or sys.modules.get("agent_engine")
        if mod and hasattr(mod, "get_engine"):
            eng = mod.get_engine()
    if not eng or eng not in ENGINE_REGISTRY:
        eng = DEFAULT_ENGINE
    return ENGINE_REGISTRY.get(eng, ENGINE_REGISTRY["claude"])
