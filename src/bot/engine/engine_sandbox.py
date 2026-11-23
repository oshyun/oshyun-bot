"""engine_sandbox — macOS Seatbelt sandbox-exec 격리 프로파일 및 환경 구성.

claude/agy 실행 시 파일시스템 접근을 작업공간(workdir) 및 전용 홈 디렉토리로 제한한다.
"""

from __future__ import annotations

import os
import shutil
import sys
from typing import Any

from bot.core.paths import AGENT_HOME_DIR as _AGENT_HOME_DIR, AGENT_TMP_DIR as _AGENT_TMP_DIR, ATTACHED_FILES_DIR as _ATTACHED_FILES_DIR, JOBS_DIR as _JOBS_DIR

_HOME = os.path.expanduser("~")
_AGENT_INSTALL_DIR = os.path.join(_HOME, ".local", "share", "claude")
_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_PROFILE = os.path.join(_PKG_DIR, "sandbox.sb")

SANDBOX_ENABLED = (
    sys.platform == "darwin"
    and os.getenv("OSHYUN_BOT_SANDBOX", "1") != "0"
    and os.path.exists("/usr/bin/sandbox-exec")
    and os.path.exists(_PROFILE)
)


def _resolve_claude_bin() -> str:
    """claude 실행 파일의 실제 경로(심링크 해석). 호출 시마다 재해석한다."""
    found = shutil.which("claude") or os.path.join(_HOME, ".local", "bin", "claude")
    return os.path.realpath(found)


def _seed_claude_home() -> None:
    """AGENT_HOME_DIR(=AGENT_CONFIG_DIR)가 없으면 ~/.claude 에서 설정·봇세션만 1회 시드한다."""
    dst = _AGENT_HOME_DIR
    src = os.path.join(_HOME, ".claude")
    try:
        if os.path.exists(dst):
            return
        os.makedirs(dst, exist_ok=True)
        for name in ("settings.json", "settings.local.json"):
            settings_src = os.path.join(src, name)
            if os.path.isfile(settings_src):
                shutil.copy2(settings_src, os.path.join(dst, name))
        home_json = os.path.join(_HOME, ".claude.json")
        if os.path.isfile(home_json):
            shutil.copy2(home_json, os.path.join(dst, ".claude.json"))
        proj_src = os.path.join(src, "projects")
        if os.path.isdir(proj_src):
            for wd in {
                w
                for w in (os.getenv("AGENT_WORKDIR"), os.getenv("AGENT_CODEDIR"))
                if w
            }:
                enc = os.path.realpath(os.path.expanduser(wd)).replace("/", "-")
                enc_project_src = os.path.join(proj_src, enc)
                if os.path.isdir(enc_project_src):
                    shutil.copytree(
                        enc_project_src,
                        os.path.join(dst, "projects", enc),
                        dirs_exist_ok=True,
                    )
    except Exception:
        pass


_seed_claude_home()


def _sandbox_prefix(workdir: str) -> list[str]:
    """sandbox-exec 인자 목록."""
    params = {
        "HOME": _HOME,
        "WORKDIR": workdir,
        "AGENT_HOME": _AGENT_HOME_DIR,
        "AGENT_INSTALL": _AGENT_INSTALL_DIR,
        "AGENT_JSON": os.path.join(_AGENT_HOME_DIR, ".claude.json"),
        "ATTACHED_FILES_DIR": _ATTACHED_FILES_DIR,
        "JOBS_DIR": _JOBS_DIR,
        "AGENT_TMP_DIR": _AGENT_TMP_DIR,
        "AGY_CLI_HOME": os.path.join(_HOME, ".gemini", "antigravity-cli"),
        "AGY_HOME": os.path.join(_HOME, ".gemini", "antigravity"),
        "LIB_KEYCHAINS": os.path.join(_HOME, "Library", "Keychains"),
        "LIB_PREFS": os.path.join(_HOME, "Library", "Preferences"),
    }
    return [
        "sandbox-exec",
        "-f",
        _PROFILE,
        *[item for k, v in params.items() for item in ("-D", f"{k}={v}")],
    ]


def _get_engine_instance_helper(engine: str | None = None) -> Any:
    mod = sys.modules.get("bot.engine.engine_drivers") or sys.modules.get("bot.engine.agent_engine")
    if mod and hasattr(mod, "get_engine_instance"):
        return mod.get_engine_instance(engine)
    from bot.engine.engine_drivers import get_engine_instance

    return get_engine_instance(engine)


def sandbox_placement_warnings(workdir: str) -> list[str]:
    """샌드박스 및 배치 경고 목록을 활성 엔진 인스턴스로부터 얻는다."""
    return _get_engine_instance_helper().get_placement_warnings(workdir)


def _resolve_agy_model_effort(model: str, effort: str | None) -> str:
    """agy 모델명에 effort 레벨을 반영한다(호환성 래퍼)."""
    engine = _get_engine_instance_helper("agy")
    resolved_model, _ = engine.resolve_model_and_effort(model, effort)
    return resolved_model
