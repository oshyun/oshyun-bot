"""engine_settings — 봇 엔진/모델/세션 영속 설정 및 파일 정리.

bot_settings.json 파일 기반으로 엔진(AGY/Claude), 선택 모델, 추론 effort,
세션 식별자, 코딩 모드 및 유휴 시간 리셋을 관리한다.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import time

from bot.engine.engine_drivers import AGY_DEFAULT_MODEL, AGY_MODEL_CANDIDATES, AGY_MODELS, DEFAULT_ENGINE, DEFAULT_MODEL, ENGINE_REGISTRY, ENGINES, MODEL_CANDIDATES, _LEGACY_AGY_MODEL_ALIASES, get_engine_instance, strip_agy_effort_suffix
from bot.core.json_store import read_json, write_json_atomic
from bot.core.paths import BOT_SETTINGS_FILE as _SETTINGS_FILE

logger = logging.getLogger("oshyun-bot.engine_settings")

_DEFAULTS: dict = {
    "engine": DEFAULT_ENGINE,
    "effort": "medium",
    "model": DEFAULT_MODEL,
    "claude_model": DEFAULT_MODEL,
    "agy_model": AGY_DEFAULT_MODEL,
    "session_id": None,
    "coding_session_id": None,
    "core_session_id": None,
    "claude_session_id": None,
    "claude_coding_session_id": None,
    "claude_core_session_id": None,
    "agy_session_id": None,
    "agy_coding_session_id": None,
    "agy_core_session_id": None,
    "coding_mode": False,
    "verbose": True,
    "last_activity": None,
    "coding_last_activity": None,
    "core_last_activity": None,
}


def _normalize_settings(stored: dict | None) -> dict:
    """저장된 설정 딕셔너리에 기본값과 모델/엔진 정규화를 적용한다(추가 키 보존)."""
    if stored is None:
        return dict(_DEFAULTS)
    settings = dict(stored)
    for k, v in _DEFAULTS.items():
        settings.setdefault(k, v)
    if settings.get("engine") not in ENGINES:
        settings["engine"] = DEFAULT_ENGINE
    if MODEL_CANDIDATES and settings.get("claude_model") not in MODEL_CANDIDATES:
        settings["claude_model"] = DEFAULT_MODEL
    agy_m = settings.get("agy_model") or ""
    if agy_m in _LEGACY_AGY_MODEL_ALIASES:
        agy_m = _LEGACY_AGY_MODEL_ALIASES[agy_m]
        settings["agy_model"] = agy_m
    stripped = strip_agy_effort_suffix(agy_m)
    if stripped != agy_m and stripped in AGY_MODELS:
        agy_m = stripped
        settings["agy_model"] = agy_m
    if AGY_MODEL_CANDIDATES and settings.get("agy_model") not in AGY_MODEL_CANDIDATES:
        settings["agy_model"] = AGY_DEFAULT_MODEL
    if settings.get("engine") == "agy":
        settings["model"] = settings["agy_model"]
    else:
        settings["model"] = settings["claude_model"]
    return settings


def _load_settings() -> dict:
    stored = read_json(_SETTINGS_FILE, None, expect_type=dict)
    if stored is None:
        return dict(_DEFAULTS)
    settings = _normalize_settings(stored)
    _save_settings(settings)
    return settings


def _sync_from_disk() -> dict:
    """디스크(bot_settings.json)의 최신 설정·세션 상태를 메모리(_settings)로 동기화한다."""
    stored = read_json(_SETTINGS_FILE, None, expect_type=dict)
    if stored is None:
        return _settings
    normalized = _normalize_settings(stored)
    _settings.clear()
    _settings.update(normalized)
    return _settings


def _get_model_from_settings(eng: str | None = None) -> str:
    e = eng or _settings.get("engine", DEFAULT_ENGINE)
    if e == "agy":
        return _settings.get("agy_model") or AGY_DEFAULT_MODEL
    return _settings.get("claude_model") or _settings.get("model") or DEFAULT_MODEL


def _save_settings(settings: dict) -> None:
    write_json_atomic(_SETTINGS_FILE, settings, indent=2)


_settings = _load_settings()


def get_engine() -> str:
    _sync_from_disk()
    return _settings.get("engine", DEFAULT_ENGINE)


def set_engine(engine: str) -> None:
    if engine not in ENGINE_REGISTRY:
        return
    _sync_from_disk()
    _settings["engine"] = engine
    _settings["model"] = _get_model_from_settings(engine)
    cur_effort = _settings.get("effort")
    supported = get_supported_efforts(engine, _settings["model"])
    if cur_effort and cur_effort not in supported:
        _settings["effort"] = None
    _save_settings(_settings)


def get_model_candidates(engine: str | None = None) -> tuple[str, ...]:
    inst = get_engine_instance(engine)
    return tuple(inst.get_models().keys())


def get_model_labels(engine: str | None = None) -> dict[str, str]:
    inst = get_engine_instance(engine)
    return inst.get_models()


def get_all_model_labels() -> dict[str, str]:
    """모든 등록 엔진의 {모델키: 표시명} 합집합 (모델명 사전 검증·오류 보고용)."""
    merged: dict[str, str] = {}
    for eng in ENGINE_REGISTRY:
        merged.update(get_engine_instance(eng).get_models())
    return merged


def get_supported_efforts(
    engine: str | None = None, model: str | None = None
) -> tuple[str, ...]:
    inst = get_engine_instance(engine)
    m = model or get_model()
    return inst.get_supported_efforts(m)


def get_session(ns: str = "") -> str | None:
    _sync_from_disk()
    eng = _settings.get("engine", DEFAULT_ENGINE)
    eng_key = f"{eng}_{ns}session_id"
    if eng_key in _settings and _settings.get(eng_key):
        return _settings.get(eng_key)
    if eng == "claude":
        return _settings.get(f"{ns}session_id")
    return None


def set_session(session_id: str, ns: str = "") -> None:
    _sync_from_disk()
    eng = _settings.get("engine", DEFAULT_ENGINE)
    _settings[f"{eng}_{ns}session_id"] = session_id
    if eng == "claude":
        _settings[f"{ns}session_id"] = session_id
    _save_settings(_settings)


def clear_session(ns: str = "") -> None:
    _sync_from_disk()
    eng = _settings.get("engine", DEFAULT_ENGINE)
    eng_key = f"{eng}_{ns}session_id"
    changed = False
    if _settings.get(eng_key) is not None:
        _settings[eng_key] = None
        changed = True
    if eng == "claude" and _settings.get(f"{ns}session_id") is not None:
        _settings[f"{ns}session_id"] = None
        changed = True
    if _settings.get(f"{ns}last_activity") is not None:
        _settings[f"{ns}last_activity"] = None
        changed = True
    if changed:
        _save_settings(_settings)


def delete_session_files(session_id: str | None) -> dict[str, any]:
    """특정 세션 ID에 해당하는 AGY/Claude 디스크 저장소 파일 및 인메모리 세션을 영구 삭제한다."""
    if not session_id or not isinstance(session_id, str):
        return {"deleted": False, "reason": "empty_session_id"}
    cleaned_id = session_id.strip()
    if not re.match(r"^[a-zA-Z0-9_\-]+$", cleaned_id):
        return {"deleted": False, "reason": "invalid_session_id"}

    deleted_paths: list[str] = []

    agy_targets = [
        os.path.expanduser(f"~/.gemini/antigravity/brain/{cleaned_id}"),
        os.path.expanduser(f"~/.gemini/antigravity/conversations/{cleaned_id}.db"),
        os.path.expanduser(f"~/.gemini/antigravity/annotations/{cleaned_id}.pbtxt"),
        os.path.expanduser(f"~/.gemini/antigravity/presence/{cleaned_id}"),
    ]
    for target in agy_targets:
        if os.path.isdir(target):
            try:
                shutil.rmtree(target, ignore_errors=True)
                deleted_paths.append(target)
            except Exception as e:
                logger.warning("AGY 세션 디렉토리 삭제 실패 (%s): %s", target, e)
        elif os.path.isfile(target):
            try:
                os.remove(target)
                deleted_paths.append(target)
            except Exception as e:
                logger.warning("AGY 세션 파일 삭제 실패 (%s): %s", target, e)

    _sync_from_disk()
    changed = False
    for k in list(_settings.keys()):
        if "session_id" in k and _settings.get(k) == cleaned_id:
            _settings[k] = None
            changed = True
    if changed:
        _save_settings(_settings)

    logger.info("세션 파일 삭제 완료: %s (삭제 항목 %d개)", cleaned_id, len(deleted_paths))
    return {"deleted": True, "sessionId": cleaned_id, "deletedPaths": deleted_paths}


def get_last_activity(ns: str = "") -> float | None:
    _sync_from_disk()
    return _settings.get(f"{ns}last_activity")


def update_last_activity(ns: str = "") -> None:
    _sync_from_disk()
    _settings[f"{ns}last_activity"] = time.time()
    _save_settings(_settings)


def check_and_reset_if_idle(ns: str = "", idle_seconds: float = 6 * 3600) -> bool:
    """마지막 대화로부터 idle_seconds 이상 지났으면 세션을 자동 초기화하고 True 반환."""
    sess = get_session(ns)
    if not sess:
        return False
    last = get_last_activity(ns)
    if last is not None and (time.time() - last) > idle_seconds:
        clear_session(ns)
        return True
    return False


def set_model(model: str) -> None:
    _sync_from_disk()
    eng = _settings.get("engine", DEFAULT_ENGINE)
    clean_model = model.strip()
    if clean_model in _LEGACY_AGY_MODEL_ALIASES:
        clean_model = _LEGACY_AGY_MODEL_ALIASES[clean_model]

    inst = get_engine_instance(eng)
    avail_models = inst.get_models()
    if avail_models and clean_model not in avail_models:
        base = (
            clean_model.removesuffix("-high")
            .removesuffix("-medium")
            .removesuffix("-low")
        )
        if base in avail_models:
            clean_model = base
        else:
            logger.warning("[set_model] 엔진 %s에 지원되지 않는 모델: %s", eng, clean_model)
            return

    if eng == "agy":
        _settings["agy_model"] = clean_model
    else:
        _settings["claude_model"] = clean_model
    _settings["model"] = clean_model
    cur_effort = _settings.get("effort")
    supported = get_supported_efforts(eng, clean_model)
    if cur_effort and cur_effort not in supported:
        _settings["effort"] = supported[0] if supported else None
    _save_settings(_settings)


def get_model() -> str:
    _sync_from_disk()
    return _get_model_from_settings()


def set_effort(level: str | None) -> None:
    _sync_from_disk()
    _settings["effort"] = level
    _save_settings(_settings)


def get_effort() -> str | None:
    _sync_from_disk()
    return _settings.get("effort")


def get_coding_mode() -> bool:
    _sync_from_disk()
    return bool(_settings.get("coding_mode", False))


def set_coding_mode(enabled: bool) -> None:
    _sync_from_disk()
    _settings["coding_mode"] = enabled
    _save_settings(_settings)


def get_verbose() -> bool:
    _sync_from_disk()
    return bool(_settings.get("verbose", True))


def set_verbose(enabled: bool) -> None:
    _sync_from_disk()
    _settings["verbose"] = enabled
    _save_settings(_settings)
