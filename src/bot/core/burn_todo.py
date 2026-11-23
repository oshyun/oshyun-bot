"""burn_todo — 주간 잔여 쿼터 토큰 버닝 개선 TODO 리스트 관리.

주간 리셋 직전 잔여 토큰이 충분할 때 워크트리에서 실행할 프로젝트 개선 작업 백로그를 영속화(burn_todo.json)한다.
json_store 의 원자적 저장(tmp 쓰기 → os.replace)을 따르며, 초기 상태에서는 유용한 기본 개선 작업 프리셋을 제공한다.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from bot.core.json_store import read_json, write_json_atomic
from bot.core.paths import BURN_TODO_FILE

# 기본 개선 작업 프리셋
DEFAULT_BURN_TASKS: list[dict[str, Any]] = [
    {
        "id": "burn-test-suite",
        "title": "핵심 단위 테스트(pytest) 커버리지 확충",
        "category": "test",
        "workdir_type": "repo",  # repo (워크트리에서 코드 개선) or vault (볼트 개선)
        "prompt": (
            "src/bot/core/ 디렉토리의 핵심 모듈(cron.py, json_store.py, paths.py 등)에 대한 "
            "pytest 단위 테스트를 tests/ 디렉토리에 작성하거나 보강해줘. "
            "독립적으로 실행 가능한 테스트여야 하며, 외부 API 의존성은 mock 처리해. "
            "작업 완료 후 반드시 pytest 를 실행해 통과하는지 검증하고 변경사항을 커밋해줘."
        ),
        "priority": 1,
        "status": "pending",
        "repeatable": True,
        "last_run": None,
        "result_summary": None,
    },
    {
        "id": "burn-refactor-audit",
        "title": "비대 모듈(master_memory.py/orchestrator.py) 복잡도 분석 및 리팩토링",
        "category": "refactor",
        "workdir_type": "repo",
        "prompt": (
            "src/bot/master/master_memory.py 와 src/bot/web/orchestrator.py 의 비대해진 함수와 중복 로직을 분석해줘. "
            "안전하게 분리할 수 있는 헬퍼 함수나 서브 모듈을 추출하고, 기존 기능이 깨지지 않도록 리팩토링해줘. "
            "문법 오류가 없는지 `python3 -m py_compile ...` 로 검증하고 요약 리포트를 커밋 메시지에 남겨줘."
        ),
        "priority": 2,
        "status": "pending",
        "repeatable": True,
        "last_run": None,
        "result_summary": None,
    },
    {
        "id": "burn-typing-audit",
        "title": "타입 힌트 및 예외 처리 견고성 보강",
        "category": "typing",
        "workdir_type": "repo",
        "prompt": (
            "src/bot/ 내 모든 파이썬 파일의 함수 매개변수 및 반환값에 typing 힌트를 점검·보강해줘. "
            "특히 bare except(`except:`) 또는 의미 없이 삼켜지는 예외(`pass`)가 있는지 전수 점검하여 "
            "명시적 예외 처리와 로깅으로 보완해줘."
        ),
        "priority": 3,
        "status": "pending",
        "repeatable": True,
        "last_run": None,
        "result_summary": None,
    },
    {
        "id": "burn-security-audit",
        "title": "샌드박스(sandbox.sb) 및 경로 탈출 정적 감사",
        "category": "security",
        "workdir_type": "repo",
        "prompt": (
            "src/bot/engine/engine_sandbox.py(sandbox.sb) 파일의 Seatbelt 샌드박스 정책과 core/paths.py, engine/agent_engine.py 의 경로 처리 로직을 점검해줘. "
            "의도치 않은 홈 디렉토리 접근이나 파일 유출 가능성이 있는지 정적 분석하고, "
            "보안 분석 보고서(docs/security-audit.md)를 작성하거나 정책을 보강해줘."
        ),
        "priority": 4,
        "status": "pending",
        "repeatable": True,
        "last_run": None,
        "result_summary": None,
    },
    {
        "id": "burn-vault-dreaming",
        "title": "Obsidian 볼트 드리밍 및 지식 정돈",
        "category": "dreaming",
        "workdir_type": "vault",
        "prompt": (
            "Obsidian 볼트의 스크래치패드.md 를 확인하고 미분류 메모를 적절한 장기 노트로 이전 정리해줘. "
            "깨진 위키링크나 고립 노트를 점검하고, dreaming 규칙(_agents/skills/dreaming/SKILL.md)에 따라 정돈해줘."
        ),
        "priority": 5,
        "status": "pending",
        "repeatable": True,
        "last_run": None,
        "result_summary": None,
    },
]


def list_all() -> list[dict[str, Any]]:
    """모든 TODO 항목을 반환한다. 파일이 없으면 기본 프리셋을 시드한다."""
    items = read_json(BURN_TODO_FILE, None, expect_type=list)
    if items is None:
        items = list(DEFAULT_BURN_TASKS)
        write_json_atomic(BURN_TODO_FILE, items, indent=2)
    return items


def add(
    title: str,
    prompt: str,
    *,
    category: str = "custom",
    priority: int = 10,
    workdir_type: str = "repo",
    repeatable: bool = False,
) -> dict[str, Any]:
    """새 개선 작업을 추가하고 저장된 항목을 반환한다."""
    items = list_all()
    task_id = f"burn-{uuid.uuid4().hex[:6]}"
    entry: dict[str, Any] = {
        "id": task_id,
        "title": title.strip(),
        "category": category.strip(),
        "workdir_type": workdir_type,
        "prompt": prompt.strip(),
        "priority": priority,
        "status": "pending",
        "repeatable": repeatable,
        "last_run": None,
        "result_summary": None,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    items.append(entry)
    # 우선순위 기준 정렬
    items.sort(key=lambda x: (x.get("priority", 999), x.get("id", "")))
    write_json_atomic(BURN_TODO_FILE, items, indent=2)
    return entry


def remove(task_id: str) -> bool:
    """task_id 로 작업을 삭제한다. 삭제 성공 시 True."""
    items = list_all()
    kept = [x for x in items if x.get("id") != task_id]
    if len(kept) == len(items):
        return False
    write_json_atomic(BURN_TODO_FILE, kept, indent=2)
    return True


def get_task(task_id: str) -> dict[str, Any] | None:
    """특정 task_id의 항목을 조회한다."""
    for item in list_all():
        if item.get("id") == task_id:
            return item
    return None


def get_next_pending() -> dict[str, Any] | None:
    """우선순위가 가장 높은 대기(pending) 작업을 반환한다."""
    items = list_all()
    pending = [x for x in items if x.get("status") == "pending"]
    if not pending:
        # 모든 작업이 완료되었으나 repeatable 인 작업이 있다면 다시 1순위 후보로 탐색 가능
        repeatables = [x for x in items if x.get("repeatable") and x.get("status") == "completed"]
        if repeatables:
            repeatables.sort(key=lambda x: (x.get("priority", 999), x.get("last_run") or ""))
            return repeatables[0]
        return None
    pending.sort(key=lambda x: x.get("priority", 999))
    return pending[0]


def update_task(task_id: str, **kwargs) -> bool:
    """작업의 필드를 업데이트한다."""
    items = list_all()
    found = False
    for item in items:
        if item.get("id") == task_id:
            for k, v in kwargs.items():
                item[k] = v
            found = True
            break
    if found:
        write_json_atomic(BURN_TODO_FILE, items, indent=2)
    return found


def reset_all() -> int:
    """완료되거나 실패한 모든 작업을 다시 'pending' 상태로 리셋한다."""
    items = list_all()
    count = 0
    for item in items:
        if item.get("status") in ("completed", "failed"):
            item["status"] = "pending"
            count += 1
    write_json_atomic(BURN_TODO_FILE, items, indent=2)
    return count
