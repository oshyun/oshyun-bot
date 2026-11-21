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
from usage_pricing import _resolve_agy_bin, get_cached_agy_usage

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


def get_quota_alert_prompt(
    threshold: float = 50.0,
    cache_file: str | None = None,
) -> str:
    """AGY Gemini 잔여 쿼터가 threshold 미만이면 런타임 [QUOTA ALERT] 지시문을 반환한다."""
    usage = get_cached_agy_usage(allow_refresh=False, cache_file=cache_file)
    if not isinstance(usage, dict):
        return ""
    five_pct = (usage.get("five_hour") or {}).get("pct")
    weekly_pct = (usage.get("weekly") or {}).get("pct")
    candidates = [float(p) for p in (five_pct, weekly_pct) if isinstance(p, (int, float))]
    if not candidates:
        return ""
    min_pct = min(candidates)
    if min_pct >= threshold:
        return ""
    parts = []
    if five_pct is not None:
        parts.append(f"5시간 {five_pct:.1f}%")
    if weekly_pct is not None:
        parts.append(f"주간 {weekly_pct:.1f}%")
    status_str = ", ".join(parts)
    return (
        f"[QUOTA ALERT] 현재 AGY Gemini 잔여 쿼터가 부족합니다({status_str}; 최저 {min_pct:.1f}% < {threshold:.0f}%). "
        "고난도 추론·원인 분석·코드/아키텍처 설계·리팩토링 작업 시 자체 AGY 토큰을 소모하지 말고 "
        "1차 도구로 `gemini-consult` (`_agents/skills/gemini-consult/scripts/gemini_query.py --only-if-agy-below 50`)를 호출하여 오프로드하세요."
    )


def _resolve_system_prompt(
    *,
    orchestrator: bool = False,
    expert_vault: bool = False,
    expert_coding: bool = False,
    coding: bool = False,
    web: bool = False,
    agent_id: str | None = None,
) -> str:
    """실행 모드에 맞는 시스템 프롬프트를 반환한다."""
    target_agent_id = agent_id
    if not target_agent_id:
        if orchestrator:
            target_agent_id = "__core__"
        elif expert_vault:
            target_agent_id = "__expert_vault__"
        elif expert_coding:
            target_agent_id = "__expert_coding__"

    if target_agent_id:
        try:
            import agent_configs
            cfg = agent_configs.get_agent_config(target_agent_id)
            if cfg and cfg.get("systemPrompt"):
                common_content = _load_instruction_file("common")
                agent_sys = str(cfg["systemPrompt"]).strip()
                return f"{common_content}\n\n{agent_sys}".strip() if common_content else agent_sys
        except Exception:
            pass

    if orchestrator:
        return ORCHESTRATOR_SYSTEM_PROMPT
    if expert_vault:
        return EXPERT_VAULT_SYSTEM_PROMPT
    if expert_coding:
        return EXPERT_CODING_SYSTEM_PROMPT
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

# 세컨브레인 전문가 에이전트 시스템 프롬프트
_FALLBACK_EXPERT_VAULT_PROMPT = f"""\
너는 oshyun-bot의 **세컨브레인 전문가 에이전트(Second Brain Expert, `__expert_vault__`)**다.
마스터 에이전트(`__core__`)의 직속 지휘를 받으며, 사용자의 Obsidian 볼트를 관리·동기화·정돈한다.

[★ 절대 원칙: 사전 탐색·블로킹 제로 & 즉시 하위 워커 스폰]
1. 스폰 전 사전 탐색 금지: 전문가 자신이 `_bot/컨텍스트.md`나 볼트 노트를 `read_file`/`grep`/`find`로 미리 뒤지며 세션을 블로킹하지 마라.
2. 모든 볼트 실무 작업(조회, 노트 수정, 5대 도메인 동기화, 영수증/이미지 반영, 드리밍)은 지체 없이 `{_SESSION_CTL_BIN} spawn "<작업제목>" "<지시 + 볼트 규칙>" --parent __expert_vault__ --model gemini-3.8-flash --effort <low|medium>` 단 1회 호출로 하위 워커에게 비동기 위임하라.
3. 직접 응답 예외(1~2초 즉답): 단순 인사, 이미 주입된 메모리만으로 즉답 가능한 질문, `session-ctl list/summaries/read` 조회 1회.
4. 스폰 직후 1~2줄로 착수 보고하고 즉시 턴 종료 (`--wait` 절대 금지). 완료 보고 수신 시 핵심 결과를 정리해 보고한다."""

_DEFAULT_EXPERT_VAULT_SYSTEM_PROMPT = _combine_instructions("expert_vault", _FALLBACK_EXPERT_VAULT_PROMPT)
EXPERT_VAULT_SYSTEM_PROMPT = os.getenv(
    "OSHYUN_BOT_EXPERT_VAULT_SYSTEM_PROMPT", _DEFAULT_EXPERT_VAULT_SYSTEM_PROMPT
)

# 봇개발 전문가 에이전트 시스템 프롬프트
_FALLBACK_EXPERT_CODING_PROMPT = f"""\
너는 oshyun-bot의 **봇개발 전문가 에이전트(Lead Bot Dev Orchestrator, `__expert_coding__`)**다.
마스터 에이전트(`__core__`)의 직속 지휘를 받으며, oshyun-bot 소스코드 개발, 버그 수정, 웹 UI 개선, 단위 테스트를 총괄 조율한다.

[★ 절대 원칙: 사전 탐색·고민 제로 & 1스텝 즉시 하위 워커 스폰]
1. 스폰 전 사전 탐색·자문·고민 금지: 요청을 받으면 절대 `read_file`, `grep`, `find`, `run_command`(파일 조회/테스트), `gemini-consult` 등으로 코드를 미리 읽거나 설계를 고민하지 마라. 분석·구현·테스트는 전부 스폰된 하위 워커가 수행한다.
2. 도구 호출 단 1회(`{_SESSION_CTL_BIN} spawn "<작업제목>" "<구현 지시 + 5대 필수 원칙>" --parent __expert_coding__ --model <모델> --effort <레벨>`)로 즉시 하위 워커를 띄우고, 1~2줄 착수 보고 후 턴을 종료하라 (`--wait` 절대 금지).
3. 직접 응답 예외(1~2초 즉답): 단순 인사, 일반 아키텍처 설명, `session-ctl list/summaries/read` 조회 1회.
4. 하위 워커 지시 필수 5대 원칙:
   ① 워크트리 격리 필수 (`git worktree add -b feat/<작업명> /Users/user/repos/oshyun-bot-wt-<작업명> HEAD`)
   ② 단위 테스트 전수 검증 (`PYTHONPATH=src/bot /Users/user/repos/oshyun-bot/.venv/bin/python3 -m unittest discover -s tests`)
   ③ 코딩 후 `/simplify` 정리
   ④ main 병합, 워크트리 정리(`git worktree remove`), `./scripts/restart.sh`, `git push origin main`
   ⑤ 변경 파일·테스트·재시작 상태 보고
5. 완료 보고 수신 시 변경사항·테스트·배포 상태를 검수해 브리핑한다."""

_DEFAULT_EXPERT_CODING_SYSTEM_PROMPT = _combine_instructions("expert_coding", _FALLBACK_EXPERT_CODING_PROMPT)
EXPERT_CODING_SYSTEM_PROMPT = os.getenv(
    "OSHYUN_BOT_EXPERT_CODING_SYSTEM_PROMPT", _DEFAULT_EXPERT_CODING_SYSTEM_PROMPT
)

# 마스터 에이전트 전용 시스템 프롬프트
_FALLBACK_ORCHESTRATOR_PROMPT = f"""\
너는 oshyun-bot의 모든 대화방(AGY 세션)을 총괄·모니터링하는 **마스터 에이전트(Master Agent, `__core__`)**다.
너의 유일한 역할은 **고민·사전 탐색 없이 1~2초 내에 즉시 적절한 하위 채팅을 스폰/위임하고, 완료 보고를 브리핑하는 순수 오케스트레이터**다.

[에이전트 계층]
- `__core__` (마스터) → `__expert_vault__` (세컨브레인 전문가) / `__expert_coding__` (봇개발 전문가) → 하위 작업 워커들

[★ 절대 원칙: 고민·사전탐색 제로 & 즉시 하위 채팅 스폰/위임]
1. 마스터 직접 응답 허용(도구 호출 0~1회, 1초 즉답만):
   - 단순 인사·잡담·확인, 이미 주입된 메모리(`hot`/`warm`)나 직전 맥락으로 추가 도구 없이 답할 수 있는 질문
   - `{_SESSION_CTL_BIN} list/summaries/tasks/graph/memory` 등 오케스트레이션 상태 조회 1회
2. 그 외 모든 실무 요청 = 무조건 즉시 비동기 위임 (직접 수행·사전 탐색 절대 금지):
   - `_bot/컨텍스트.md`, 볼트 노트, 소스 파일을 `read_file`/`grep`/`find`로 미리 열어보거나 `search_web`/`gemini-consult`/스크립트를 직접 실행하지 마라.
   - 일반 볼트/개인 도메인(조회·일정·할일·건강·금융·식재료·노트·웹리서치):
     `{_SESSION_CTL_BIN} spawn "<방제목>" "<지시>" --parent __expert_vault__ --model gemini-3.8-flash --effort <low|medium>` (전면 드리밍/복합 조율 시에만 `send "__expert_vault__"`)
   - 봇/코드 개발(수정·UI·버그·테스트):
     `{_SESSION_CTL_BIN} spawn "<작업제목>" "<구현 지시 + 워크트리·테스트·/simplify·main merge·restart·push 필수>" --parent __expert_coding__ --model <모델> --effort <레벨>` (대규모 분배 시에만 `send "__expert_coding__"`)
3. 절대 `--wait` 금지: `spawn`/`send` 실행 직후 *"하위 작업([제목])을 시작했습니다. 완료 시 보고드리겠습니다."* 1~2줄만 답하고 즉시 턴 종료.
4. 완료 보고 수신 시: `[하위 작업 완료 보고]` 본문에 핵심 결과가 있으면 `read` 추가 호출 없이 즉시 사용자에게 마크다운으로 브리핑한다.
5. 모델 & effort 기준: 단순 조회 `--model gemini-3.8-flash --effort low`, 일반 노트/가벼운 수정 `--model gemini-3.8-flash --effort medium`, 코딩 작업 기본 `--model gemini-3.8-flash --effort medium` (12/31까지 Flash 50% 할인 반영, 굉장히 복잡한 코딩에 한해 `--model gemini-4-argon --effort medium`)."""

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
        expert_vault: bool = False,
        expert_coding: bool = False,
        web: bool = False,
        extra_env: dict[str, str] | None = None,
        agent_id: str | None = None,
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
        expert_vault: bool = False,
        expert_coding: bool = False,
        web: bool = False,
        extra_env: dict[str, str] | None = None,
        agent_id: str | None = None,
    ) -> dict:
        is_agent_mode = bool(orchestrator or expert_vault or expert_coding or agent_id)
        use_sandbox = (
            False
            if is_agent_mode
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
            orchestrator=orchestrator,
            expert_vault=expert_vault,
            expert_coding=expert_coding,
            coding=coding,
            web=web,
            agent_id=agent_id,
        )
        if not is_agent_mode:
            quota_alert = get_quota_alert_prompt()
            if quota_alert:
                system_prompt = f"{system_prompt}\n\n{quota_alert}" if system_prompt else quota_alert
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
        if is_agent_mode:
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
        expert_vault: bool = False,
        expert_coding: bool = False,
        web: bool = False,
        extra_env: dict[str, str] | None = None,
        agent_id: str | None = None,
    ) -> dict:
        agy_cmd = _resolve_agy_bin()
        is_agent_mode = bool(orchestrator or expert_vault or expert_coding or agent_id)

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
            else (
                "expert_vault"
                if expert_vault
                else (
                    "expert_coding"
                    if expert_coding
                    else ("coding" if coding else ("web" if web else "telegram"))
                )
            )
        )
        mode_dir = get_instruction_dir(mode_name)
        target_agent_id = agent_id or (
            "__core__" if orchestrator else ("__expert_vault__" if expert_vault else ("__expert_coding__" if expert_coding else None))
        )
        if target_agent_id:
            try:
                import agent_configs
                rt_dir = agent_configs.get_runtime_instruction_dir(target_agent_id)
                if rt_dir and os.path.isdir(rt_dir):
                    mode_dir = rt_dir
            except Exception:
                pass

        if os.path.isdir(common_dir):
            cmd += ["--add-dir", common_dir]
        if os.path.isdir(mode_dir):
            cmd += ["--add-dir", mode_dir]

        if workdir:
            cmd += ["--add-dir", workdir]
        if is_agent_mode:
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
        if use_sandbox and not coding and not is_agent_mode:
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

        if is_agent_mode:
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
