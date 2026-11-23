"""agent_configs — 에이전트 레벨 설정(마스터/세컨브레인/봇개발 및 사용자 커스텀 에이전트) 영속화 및 하위 프롬프트 조립 모듈.

각 에이전트(오케스트레이터/전문가)의 메타데이터와 동작 정책을 `data/state/agent_configs.json`에 영속 관리한다:
1. 에이전트 정의:
   - `id`: 에이전트 룸 식별자 (`__core__`, `__expert_vault__`, `__expert_coding__`, `__custom_<slug>__`)
   - `name`: 표시 이름 (예: "마스터 오케스트레이터", "세컨브레인", "봇개발", "가계부 분석가")
   - `title`: 대화방 전체 제목 (예: "세컨브레인 에이전트")
   - `emoji`: 대표 아이콘/이모지 (예: "👑", "🧠", "💻", "💰")
   - `color`: 포인트 색상 HEX (예: "#3b82f6", "#d97706", "#10b981")
   - `parentAgentId`: 상위 소속 에이전트 ID (`None` 또는 `"__core__"` 등)
   - `roleDescription`: 에이전트 역할 및 목적 요약 설명
   - `workdir`: 에이전트 자신 및 하위 채팅의 기본 작업 디렉토리 (비어 있으면 기본 볼트/코드 경로)
   - `model` / `effort`: 에이전트 자신의 기본 AI 모델 및 추론 강도
   - `childModel` / `childEffort`: 하위 채팅 스폰 시 기본으로 사용할 AI 모델 및 추론 강도
   - `systemPrompt`: 에이전트 자신의 전용 시스템 지침 (AGENTS.md 오버라이드)
   - `childPromptTemplate`: 이 에이전트가 하위 세션을 스폰/지시할 때 하위 채팅에 주입되는 프롬프트 템플릿·동작 규칙
   - `injectChildTemplate`: 하위 채팅 디스패치(`spawn`/`send`) 시 `childPromptTemplate` 자동 주입 여부 (기본 `True`)
   - `isBuiltin`: 기본 제공 에이전트 여부 (`True`이면 삭제 불가, 기본값 복원 가능)
2. 하위 채팅 전달 프롬프트 조립 및 미리보기(Preview):
   - 실제로 하위 세션이 실행될 때 조합되는 시스템 지침(`common/AGENTS.md` + `web/AGENTS.md`),
     작업 디렉토리(`workdir`), 모델/Effort, 상위 에이전트 주입 프롬프트(`childPromptTemplate`),
     그리고 구체적 작업 지시(`message`)의 최종 결합 결과를 투명하게 조립·미리보기한다.
"""

from __future__ import annotations

import copy
import os
import re
import threading
import time
import uuid
from typing import Any

from bot.core.json_store import read_json, write_json_atomic
from bot.core.paths import AGENT_CONFIGS_FILE, CODEDIR, INSTRUCTIONS_DIR, RUNTIME_INSTRUCTIONS_DIR, WORKDIR

CORE_ORCHESTRATOR_ROOM_ID = "__core__"
EXPERT_GENERAL_ROOM_ID = "__expert_general__"
EXPERT_VAULT_ROOM_ID = "__expert_vault__"
EXPERT_CODING_ROOM_ID = "__expert_coding__"
BUILTIN_AGENT_IDS: tuple[str, ...] = (
    CORE_ORCHESTRATOR_ROOM_ID,
    EXPERT_GENERAL_ROOM_ID,
    EXPERT_VAULT_ROOM_ID,
    EXPERT_CODING_ROOM_ID,
)

_lock = threading.Lock()


def _read_repo_instruction(mode: str) -> str:
    """리포지토리 기본 instructions/<mode>/AGENTS.md 파일 내용을 읽는다."""
    path = os.path.join(INSTRUCTIONS_DIR, mode, "AGENTS.md")
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            pass
    return ""


def _builtin_defaults() -> dict[str, dict[str, Any]]:
    """기본 내장 3대 에이전트(__core__, __expert_vault__, __expert_coding__)의 초기 기본 설정을 반환한다."""
    vault_dir = WORKDIR
    code_dir = CODEDIR or WORKDIR
    return {
        CORE_ORCHESTRATOR_ROOM_ID: {
            "id": CORE_ORCHESTRATOR_ROOM_ID,
            "name": "마스터 오케스트레이터",
            "title": "마스터 에이전트",
            "emoji": "👑",
            "color": "#3b82f6",
            "parentAgentId": None,
            "memoryScope": "master",
            "sessionNamespace": "core_",
            "roleDescription": "모든 하위 에이전트 및 채팅방(AGY 세션)을 총괄 조율하고 완료 보고를 브리핑하는 최상위 마스터 에이전트",
            "workdir": vault_dir,
            "model": "gemini-3.8-flash",
            "effort": "medium",
            "childModel": "gemini-3.8-flash",
            "childEffort": "low",
            "systemPrompt": _read_repo_instruction("orchestrator"),
            "childPromptTemplate": (
                "[마스터 직속 하위 세션 동작 규칙]\n"
                "1. 불필요한 서론 없이 핵심 결론과 수치·근거부터 명확히 보고하라.\n"
                "2. 볼트 정보 조회 시 `_bot/컨텍스트.md`를 우선 확인하고, 웹 검색은 볼트에 정보가 없을 때만 최소한으로 수행하라."
            ),
            "injectChildTemplate": False,
            "isBuiltin": True,
            "order": 0,
        },
        EXPERT_GENERAL_ROOM_ID: {
            "id": EXPERT_GENERAL_ROOM_ID,
            "name": "일반 대화",
            "title": "일반 대화",
            "emoji": "💬",
            "color": "#64748b",
            "parentAgentId": CORE_ORCHESTRATOR_ROOM_ID,
            "memoryScope": "general",
            "sessionNamespace": "general_",
            "roleDescription": "특정 도메인 제약 없이 빠르고 자유롭게 대화하는 순수 범용 비서",
            "workdir": vault_dir,
            "model": "gemini-3.8-flash",
            "effort": "medium",
            "childModel": "gemini-3.8-flash",
            "childEffort": "low",
            "systemPrompt": "",
            "childPromptTemplate": "",
            "injectChildTemplate": False,
            "isBuiltin": True,
            "order": 1,
        },
        EXPERT_VAULT_ROOM_ID: {
            "id": EXPERT_VAULT_ROOM_ID,
            "name": "세컨브레인",
            "title": "세컨브레인 에이전트",
            "emoji": "🧠",
            "color": "#d97706",
            "parentAgentId": CORE_ORCHESTRATOR_ROOM_ID,
            "memoryScope": "second_brain",
            "sessionNamespace": "vault_",
            "roleDescription": "Obsidian 볼트 지식 탐색 및 5대 도메인(건강, 할일, 식재료, 금융, 차량) 동기화 전담 전문가 에이전트",
            "workdir": vault_dir,
            "model": "gemini-3.8-flash",
            "effort": "medium",
            "childModel": "gemini-3.8-flash",
            "childEffort": "medium",
            "systemPrompt": _read_repo_instruction("expert_vault"),
            "childPromptTemplate": (
                "[세컨브레인 하위 워커 동작 규칙]\n"
                "1. 탐색 우선순위: `_bot/컨텍스트.md`(L1→L2→L3) 우선 확인 후 `grep -rni` 검색을 활용하라 (웹 검색은 볼트에 없을 때만 최소화).\n"
                "2. 5대 도메인 동시 동기화: 건강/실손보험, 할일, 식재료/살림, 금융/카드/자산, 자동차/유지비 변동 시 연관 노트를 일괄 갱신하라.\n"
                "3. 문서 컨벤션: 위키링크 `[[노트명]]` 및 `obsid.net` 공유 링크를 제공하고, 마크다운 표 내 `\\|` 이스케이프를 준수하라."
            ),
            "injectChildTemplate": True,
            "isBuiltin": True,
            "order": 2,
        },
        EXPERT_CODING_ROOM_ID: {
            "id": EXPERT_CODING_ROOM_ID,
            "name": "봇개발",
            "title": "봇개발 에이전트",
            "emoji": "💻",
            "color": "#3b82f6",
            "parentAgentId": CORE_ORCHESTRATOR_ROOM_ID,
            "memoryScope": "bot_dev",
            "sessionNamespace": "coding_",
            "roleDescription": "oshyun-bot 소스코드 개발, 버그 수정, 웹 UI 개선, 단위 테스트 및 배포 총괄 전문가 에이전트",
            "workdir": code_dir,
            "model": "gemini-3.8-flash",
            "effort": "medium",
            "childModel": "gemini-3.8-flash",
            "childEffort": "medium",
            "systemPrompt": _read_repo_instruction("expert_coding"),
            "childPromptTemplate": (
                "[봇개발 하위 워커 동작 규칙]\n"
                "1. 반드시 고유 `git worktree`를 생성하여 격리된 환경에서 코드를 수정하라 (`main` 직접 수정 절대 금지).\n"
                "2. 작업 전후 단위 테스트(`PYTHONPATH=src .venv/bin/python3 -m unittest discover -s tests`)를 실행해 전수 통과를 확인하라.\n"
                "3. 구현 완료 후 `/simplify` 정리, `main` 병합, 워크트리 제거, 필요한 서비스 재시작 및 `git push origin main` 후 변경 파일·테스트 결과를 요약 보고하라."
            ),
            "injectChildTemplate": True,
            "isBuiltin": True,
            "order": 3,
        },
    }


def _sanitize_agent_id(raw_id: str | None, name: str = "") -> str:
    """커스텀 에이전트 ID를 `__custom_<slug>__` 형식으로 정규화한다."""
    clean = str(raw_id or "").strip()
    if clean in BUILTIN_AGENT_IDS:
        return clean
    if clean.startswith("__custom_") and clean.endswith("__") and len(clean) > 11:
        inner = re.sub(r"[^a-zA-Z0-9_\-]", "", clean[9:-2]).strip("_-").lower()
        if inner:
            return f"__custom_{inner}__"
    slug = re.sub(r"[^a-zA-Z0-9_\-]", "", clean).strip("_-").lower()
    if not slug and name:
        slug = re.sub(r"[^a-zA-Z0-9_\-]", "", name).strip("_-").lower()
    if not slug:
        slug = uuid.uuid4().hex[:8]
    return f"__custom_{slug[:32]}__"


def _resolve_workdir_path(raw_workdir: str | None, fallback: str | None = None) -> str:
    """상대 경로(볼트 기준 하위 폴더 등) 또는 `~` 경로를 절대 경로로 해석한다."""
    w = str(raw_workdir or "").strip()
    if not w:
        return fallback or WORKDIR
    expanded = os.path.expanduser(w)
    if os.path.isabs(expanded):
        return os.path.abspath(expanded)
    return os.path.abspath(os.path.join(WORKDIR, expanded))


def _has_parent_cycle(
    agent_id: str,
    proposed_parent: str | None,
    all_agents_raw: dict[str, Any] | None = None,
) -> bool:
    """parentAgentId 체인 상에서 순환(A -> B -> A)이 발생하는지 검사한다."""
    if not proposed_parent or proposed_parent == CORE_ORCHESTRATOR_ROOM_ID:
        return False
    if proposed_parent == agent_id:
        return True
    if not all_agents_raw or not isinstance(all_agents_raw, dict):
        return False

    visited = {agent_id, proposed_parent}
    cur = proposed_parent
    depth = 0
    while cur and cur != CORE_ORCHESTRATOR_ROOM_ID and depth < 30:
        depth += 1
        parent_rec = all_agents_raw.get(cur)
        if not isinstance(parent_rec, dict):
            break
        p = parent_rec.get("parentAgentId")
        if not p or p == CORE_ORCHESTRATOR_ROOM_ID:
            break
        p_str = str(p).strip()
        if p_str in visited:
            return True
        visited.add(p_str)
        cur = p_str
    return False


def _normalize_agent_record(
    agent_id: str,
    raw: dict[str, Any],
    defaults_map: dict[str, dict[str, Any]],
    order_fallback: int = 10,
    all_agents_raw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """단일 에이전트 설정 레코드를 정규화한다."""
    is_builtin = agent_id in defaults_map
    base = copy.deepcopy(defaults_map[agent_id]) if is_builtin else {
        "id": agent_id,
        "name": "전문 에이전트",
        "title": "전문 에이전트",
        "emoji": "🤖",
        "color": "#10b981",
        "parentAgentId": CORE_ORCHESTRATOR_ROOM_ID,
        "memoryScope": f"expert_{agent_id.strip('_')}",
        "sessionNamespace": f"{agent_id.strip('_')}_",
        "roleDescription": "사용자 정의 전문 에이전트",
        "workdir": WORKDIR,
        "model": "gemini-3.8-flash",
        "effort": "medium",
        "childModel": "gemini-3.8-flash",
        "childEffort": "medium",
        "systemPrompt": "",
        "childPromptTemplate": "",
        "injectChildTemplate": True,
        "isBuiltin": False,
        "order": order_fallback,
    }

    name = str(raw.get("name") if raw.get("name") is not None else base["name"]).strip() or base["name"]
    title = str(raw.get("title") if raw.get("title") is not None else "").strip()
    if not title:
        title = base["title"] if is_builtin else (name if name.endswith("에이전트") else f"{name} 에이전트")

    emoji = str(raw.get("emoji") if raw.get("emoji") is not None else base["emoji"]).strip() or base["emoji"]
    color = str(raw.get("color") if raw.get("color") is not None else base["color"]).strip() or base["color"]

    if agent_id == CORE_ORCHESTRATOR_ROOM_ID:
        parent_id = None
    else:
        raw_parent = raw.get("parentAgentId")
        parent_id = str(raw_parent).strip() if raw_parent else (base.get("parentAgentId") or CORE_ORCHESTRATOR_ROOM_ID)
        if parent_id == agent_id or _has_parent_cycle(agent_id, parent_id, all_agents_raw):
            parent_id = CORE_ORCHESTRATOR_ROOM_ID

    role_desc = str(
        raw.get("roleDescription") if raw.get("roleDescription") is not None else base["roleDescription"]
    ).strip()

    raw_workdir = raw.get("workdir")
    workdir = _resolve_workdir_path(raw_workdir, fallback=base["workdir"]) if raw_workdir else base["workdir"]

    model = str(raw.get("model") if raw.get("model") is not None else base["model"]).strip() or base["model"]
    effort = str(raw.get("effort") if raw.get("effort") is not None else base["effort"]).strip() or base["effort"]
    child_model = str(
        raw.get("childModel") if raw.get("childModel") is not None else base["childModel"]
    ).strip() or model
    child_effort = str(
        raw.get("childEffort") if raw.get("childEffort") is not None else base["childEffort"]
    ).strip() or effort

    sys_prompt = (
        str(raw["systemPrompt"]).strip()
        if "systemPrompt" in raw and raw["systemPrompt"] is not None
        else base["systemPrompt"]
    )
    if is_builtin and not sys_prompt:
        sys_prompt = base["systemPrompt"]

    child_tpl = (
        str(raw["childPromptTemplate"]).strip()
        if "childPromptTemplate" in raw and raw["childPromptTemplate"] is not None
        else base["childPromptTemplate"]
    )
    inject_tpl = (
        bool(raw["injectChildTemplate"])
        if "injectChildTemplate" in raw and raw["injectChildTemplate"] is not None
        else bool(base.get("injectChildTemplate", True))
    )

    try:
        order_val = int(raw.get("order") if raw.get("order") is not None else base["order"])
    except (TypeError, ValueError):
        order_val = order_fallback

    return {
        "id": agent_id,
        "name": name[:40],
        "title": title[:60],
        "emoji": emoji[:8],
        "color": color[:24],
        "parentAgentId": parent_id,
        "memoryScope": base["memoryScope"],
        "sessionNamespace": base["sessionNamespace"],
        "roleDescription": role_desc[:300],
        "workdir": workdir,
        "model": model,
        "effort": effort,
        "childModel": child_model,
        "childEffort": child_effort,
        "systemPrompt": sys_prompt,
        "childPromptTemplate": child_tpl,
        "injectChildTemplate": inject_tpl,
        "isBuiltin": is_builtin,
        "builtin": is_builtin,
        "order": order_val,
        "updatedAt": float(raw.get("updatedAt") or time.time()),
    }


_BUILTIN_REPO_MODE_MAP: dict[str, str] = {
    CORE_ORCHESTRATOR_ROOM_ID: "orchestrator",
    EXPERT_GENERAL_ROOM_ID: "general",
    EXPERT_VAULT_ROOM_ID: "expert_vault",
    EXPERT_CODING_ROOM_ID: "expert_coding",
}


def _sync_runtime_instruction_file(agent: dict[str, Any]) -> str | None:
    """에이전트의 systemPrompt를 런타임 지침 디렉토리(`data/state/agent_instructions/<id>/AGENTS.md`)에 동기화한다."""
    agent_id = str(agent.get("id") or "").strip()
    if not agent_id:
        return None
    safe_dir_name = re.sub(r"[^a-zA-Z0-9_\-]", "_", agent_id.strip("_")) or "custom"
    target_dir = os.path.join(RUNTIME_INSTRUCTIONS_DIR, safe_dir_name)
    os.makedirs(target_dir, exist_ok=True)
    target_file = os.path.join(target_dir, "AGENTS.md")

    sys_prompt = str(agent.get("systemPrompt") or "").strip()
    if not sys_prompt:
        name = agent.get("name") or agent_id
        role_desc = agent.get("roleDescription") or "전문 에이전트"
        child_tpl = str(agent.get("childPromptTemplate") or "").strip()
        lines = [
            f"# {name} 지침 ({agent_id})",
            "",
            f"너는 oshyun-bot의 **{name} (`{agent_id}`)** 다.",
            f"역할: {role_desc}",
            "",
            "## 행동 원칙",
            f"1. 하위 작업 스폰 시 `session-ctl spawn \"<작업제목>\" \"<지시>\" --parent {agent_id} --model {agent.get('childModel') or 'gemini-3.8-flash'} --effort {agent.get('childEffort') or 'medium'}` 명령으로 비동기 위임하거나, 작업 디렉토리(`{agent.get('workdir') or WORKDIR}`) 내에서 직접 전문 작업을 수행할 수 있다.",
            "2. 사용자에게 항상 명확하고 간결하게 핵심 결론부터 한국어로 보고한다.",
        ]
        if child_tpl:
            lines += ["", "## 하위 세션 기본 전달 규칙", child_tpl]
        sys_prompt = "\n".join(lines)

    try:
        with open(target_file, "w", encoding="utf-8") as f:
            f.write(sys_prompt + "\n")
    except OSError:
        return None
    return target_dir


def get_runtime_instruction_dir(agent_id: str) -> str | None:
    """특정 에이전트의 런타임 지침 디렉토리를 반환한다.
    내장 에이전트의 systemPrompt가 원본 리포지토리 지침과 동일하면 리포지토리 지침 경로를 그대로 반환한다.
    """
    cfg = get_agent_config(agent_id)
    if not cfg:
        return None
    clean_id = str(cfg.get("id") or "").strip()
    repo_mode = _BUILTIN_REPO_MODE_MAP.get(clean_id)
    if repo_mode:
        repo_default_md = _read_repo_instruction(repo_mode)
        cur_sys_md = str(cfg.get("systemPrompt") or "").strip()
        if not cur_sys_md or cur_sys_md == repo_default_md:
            repo_dir = os.path.join(INSTRUCTIONS_DIR, repo_mode)
            if os.path.isdir(repo_dir):
                return repo_dir
    return _sync_runtime_instruction_file(cfg)


def _load_all_unlocked() -> dict[str, dict[str, Any]]:
    defaults_map = _builtin_defaults()
    stored = read_json(AGENT_CONFIGS_FILE, {}, expect_type=dict)
    raw_agents = stored.get("agents") if isinstance(stored.get("agents"), dict) else {}

    agents: dict[str, dict[str, Any]] = {}
    for b_id in BUILTIN_AGENT_IDS:
        raw_rec = raw_agents.get(b_id) if isinstance(raw_agents.get(b_id), dict) else {}
        agents[b_id] = _normalize_agent_record(b_id, raw_rec, defaults_map, all_agents_raw=raw_agents)

    order_idx = 10
    for k, raw_rec in raw_agents.items():
        if k in BUILTIN_AGENT_IDS or not isinstance(raw_rec, dict):
            continue
        clean_id = _sanitize_agent_id(k, str(raw_rec.get("name") or ""))
        if clean_id in BUILTIN_AGENT_IDS:
            continue
        agents[clean_id] = _normalize_agent_record(clean_id, raw_rec, defaults_map, order_fallback=order_idx, all_agents_raw=raw_agents)
        order_idx += 1

    return agents


def _save_all_unlocked(agents: dict[str, dict[str, Any]]) -> None:
    payload = {
        "version": 1,
        "updatedAt": time.time(),
        "agents": agents,
    }
    write_json_atomic(AGENT_CONFIGS_FILE, payload)
    for ag in agents.values():
        _sync_runtime_instruction_file(ag)


def list_agent_configs() -> list[dict[str, Any]]:
    """등록된 모든 에이전트(내장 + 커스텀) 설정 목록을 정렬하여 반환한다."""
    with _lock:
        agents = _load_all_unlocked()
        ordered = sorted(
            agents.values(),
            key=lambda a: (0 if a.get("isBuiltin") else 1, int(a.get("order") or 0), str(a.get("id") or "")),
        )
        return [copy.deepcopy(a) for a in ordered]


def get_orchestrator_room_ids() -> tuple[str, ...]:
    """현재 활성화된 모든 상위/전문가 에이전트 ID 튜플을 반환한다."""
    with _lock:
        agents = _load_all_unlocked()
        return tuple(agents.keys())


def is_orchestrator_room(room_id: str | None) -> bool:
    """주어진 room_id가 관리형 에이전트(마스터, 세컨브레인, 봇개발, 커스텀 에이전트)인지 여부를 반환한다."""
    if not room_id:
        return False
    clean = str(room_id).strip()
    if clean in BUILTIN_AGENT_IDS:
        return True
    with _lock:
        agents = _load_all_unlocked()
        return clean in agents


def get_agent_config(agent_id: str | None) -> dict[str, Any] | None:
    """특정 에이전트 ID의 설정 레코드를 반환한다."""
    if not agent_id:
        return None
    clean = str(agent_id).strip()
    with _lock:
        agents = _load_all_unlocked()
        found = agents.get(clean)
        return copy.deepcopy(found) if found else None


def get_agent_workdir(agent_id: str | None) -> str | None:
    """에이전트(또는 부모 에이전트)의 지정 작업 디렉토리를 반환한다."""
    cfg = get_agent_config(agent_id)
    if not cfg:
        return None
    return str(cfg.get("workdir") or "").strip() or None


def upsert_agent_config(payload: dict[str, Any]) -> dict[str, Any]:
    """기존 에이전트 설정을 수정하거나 신규 커스텀 에이전트를 생성·저장한다."""
    if not isinstance(payload, dict):
        raise ValueError("설정 데이터 객체가 필요합니다.")

    raw_id = str(payload.get("id") or payload.get("agentId") or "").strip()
    raw_name = str(payload.get("name") or "").strip()

    with _lock:
        defaults_map = _builtin_defaults()
        agents = _load_all_unlocked()

        if raw_id in agents:
            target_id = raw_id
            existing = agents[target_id]
        else:
            if not raw_name:
                raise ValueError("신규 에이전트 이름(name)을 입력해주세요.")
            target_id = _sanitize_agent_id(raw_id, raw_name)
            if target_id in agents and not raw_id:
                target_id = f"{target_id[:-2]}_{uuid.uuid4().hex[:4]}__"
            existing = agents.get(target_id, {})

        merged_raw = dict(existing)
        for field in (
            "name",
            "title",
            "emoji",
            "color",
            "parentAgentId",
            "roleDescription",
            "workdir",
            "model",
            "effort",
            "childModel",
            "childEffort",
            "systemPrompt",
            "childPromptTemplate",
            "injectChildTemplate",
            "order",
        ):
            if field in payload and payload[field] is not None:
                merged_raw[field] = payload[field]

        merged_raw["updatedAt"] = time.time()
        normalized = _normalize_agent_record(
            target_id,
            merged_raw,
            defaults_map,
            order_fallback=len(agents) + 1,
            all_agents_raw=agents,
        )
        agents[target_id] = normalized
        _save_all_unlocked(agents)
        return copy.deepcopy(normalized)


def reset_builtin_agent_config(agent_id: str) -> dict[str, Any]:
    """기본 내장 에이전트(__core__, __expert_vault__, __expert_coding__) 설정을 초기 기본값으로 복원한다."""
    clean = str(agent_id or "").strip()
    defaults_map = _builtin_defaults()
    if clean not in defaults_map:
        raise ValueError(f"기본 내장 에이전트가 아닙니다: {agent_id}")

    with _lock:
        agents = _load_all_unlocked()
        agents[clean] = copy.deepcopy(defaults_map[clean])
        agents[clean]["updatedAt"] = time.time()
        _save_all_unlocked(agents)
        return copy.deepcopy(agents[clean])


def delete_custom_agent_config(agent_id: str) -> bool:
    """사용자 정의 커스텀 에이전트를 삭제한다 (내장 에이전트는 삭제 불가)."""
    clean = str(agent_id or "").strip()
    if not clean or clean in BUILTIN_AGENT_IDS:
        raise ValueError("기본 내장 에이전트(마스터, 세컨브레인, 봇개발)는 삭제할 수 없습니다.")

    with _lock:
        agents = _load_all_unlocked()
        if clean not in agents:
            return False
        agents.pop(clean, None)
        # 삭제된 에이전트를 부모로 두고 있던 다른 커스텀 에이전트는 마스터 직속으로 재배치
        for ag in agents.values():
            if ag.get("parentAgentId") == clean:
                ag["parentAgentId"] = CORE_ORCHESTRATOR_ROOM_ID
        _save_all_unlocked(agents)

        safe_dir_name = re.sub(r"[^a-zA-Z0-9_\-]", "_", clean.strip("_"))
        target_dir = os.path.join(RUNTIME_INSTRUCTIONS_DIR, safe_dir_name)
        if os.path.isdir(target_dir):
            try:
                import shutil
                shutil.rmtree(target_dir, ignore_errors=True)
            except Exception:
                pass
        return True


def compose_child_dispatch_prompt(
    parent_agent_id: str | None,
    raw_message: str,
    *,
    raw_prompt: bool = False,
) -> str:
    """상위 에이전트가 하위 채팅에 작업을 지시할 때 `childPromptTemplate`을 결합한 최종 프롬프트를 반환한다."""
    msg = str(raw_message or "").strip()
    if not msg or raw_prompt or not parent_agent_id:
        return msg
    cfg = get_agent_config(parent_agent_id)
    if not cfg or not cfg.get("injectChildTemplate", True):
        return msg
    tpl = str(cfg.get("childPromptTemplate") or "").strip()
    if not tpl or tpl in msg:
        return msg
    return f"{tpl}\n\n[작업 지시]\n{msg}"


def build_agent_prompt_preview(
    agent_id: str,
    sample_task: str = "",
    *,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """해당 에이전트 자신에게 주입되는 시스템 지침과, 하위 채팅 스폰 시 전달되는 전체 프롬프트 조립 결과를 미리보기로 생성한다."""
    cfg = get_agent_config(agent_id)
    if not cfg:
        defaults_map = _builtin_defaults()
        cfg = _normalize_agent_record(
            _sanitize_agent_id(agent_id, (overrides or {}).get("name") or "커스텀"),
            overrides or {},
            defaults_map,
        )
    elif overrides and isinstance(overrides, dict):
        defaults_map = _builtin_defaults()
        merged = {**cfg, **overrides}
        cfg = _normalize_agent_record(cfg["id"], merged, defaults_map)

    common_md = _read_repo_instruction("common")
    web_md = _read_repo_instruction("web")
    agent_sys_md = str(cfg.get("systemPrompt") or "").strip()
    child_tpl = str(cfg.get("childPromptTemplate") or "").strip()
    inject_enabled = bool(cfg.get("injectChildTemplate", True))
    workdir = str(cfg.get("workdir") or WORKDIR).strip()
    child_model = str(cfg.get("childModel") or cfg.get("model") or "gemini-3.8-flash").strip()
    child_effort = str(cfg.get("childEffort") or cfg.get("effort") or "medium").strip()

    sample = str(sample_task or "").strip() or "예시: 최근 데이터 확인 및 요약 보고서를 작성해줘."
    if inject_enabled and child_tpl:
        assembled_child_message = f"{child_tpl}\n\n[작업 지시]\n{sample}"
    else:
        assembled_child_message = sample

    spawn_cmd_preview = (
        f'session-ctl spawn "<작업제목>" "<작업지시>" '
        f'--parent {cfg["id"]} --model {child_model} --effort {child_effort}'
    )
    agy_child_cli_preview = (
        f'agy -p "<조립된_하위_프롬프트>" --model {child_model} --effort {child_effort} '
        f'--add-dir {os.path.join(INSTRUCTIONS_DIR, "common")} '
        f'--add-dir {os.path.join(INSTRUCTIONS_DIR, "web")} '
        f'--add-dir {workdir}'
    )

    combined_sys = f"{common_md}\n\n{agent_sys_md}".strip() if common_md else agent_sys_md

    return {
        "ok": True,
        "agentId": cfg["id"],
        "agentName": cfg["name"],
        "workdir": workdir,
        "childWorkdir": workdir,
        "model": cfg.get("model"),
        "effort": cfg.get("effort"),
        "childModel": child_model,
        "childEffort": child_effort,
        "injectChildTemplate": inject_enabled,
        "assembledChildUserPrompt": assembled_child_message,
        "spawnCliExample": spawn_cmd_preview,
        "childCliInvocation": agy_child_cli_preview,
        "agentRuntimeSystemPrompt": combined_sys,
        "agentSystemPrompt": {
            "commonInstructions": common_md,
            "agentInstructions": agent_sys_md,
            "combined": combined_sys,
        },
        "childSessionPreview": {
            "childSystemInstructions": f"{common_md}\n\n{web_md}".strip(),
            "injectedChildTemplate": child_tpl if inject_enabled else "(자동 주입 비활성화됨)",
            "sampleTask": sample,
            "assembledUserPrompt": assembled_child_message,
            "spawnCommand": spawn_cmd_preview,
            "cliInvocation": agy_child_cli_preview,
        },
    }
