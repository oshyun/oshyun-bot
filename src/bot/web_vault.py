"""web_vault — Obsidian 볼트 계층 파일 탐색, 최근 파일, 노트 열람/저장/생성 REST API 모듈."""

from __future__ import annotations

import os
import sys
import urllib.parse
import uuid

from aiohttp import web

from bot_logger import vault_log
from paths import WORKDIR

EXCLUDED_DIR_NAMES = frozenset({".git", ".obsidian", ".trash", ".idea", ".vscode", "node_modules"})
ALLOWED_NOTE_EXTENSIONS = frozenset({".md", ".base", ".canvas", ".txt"})


def _get_workdir() -> str:
    mod = sys.modules.get("web_server")
    if mod and hasattr(mod, "WORKDIR"):
        return mod.WORKDIR
    return WORKDIR


def _resolve_vault_absolute_path(relative_path: str) -> str:
    """볼트 루트(WORKDIR)를 기준으로 상대 경로를 검증하여 경로 탈출(..)을 엄격히 차단한다."""
    workdir = _get_workdir()
    if not workdir:
        raise web.HTTPBadRequest(text="볼트 작업공간(WORKDIR)이 설정되지 않았습니다.")

    canonical_vault_root = os.path.realpath(workdir)
    normalized_relative = os.path.normpath(relative_path.strip().lstrip("/\\"))
    if normalized_relative.startswith("..") or "/../" in normalized_relative:
        raise web.HTTPForbidden(text="볼트 외부 경로 접근은 허용되지 않습니다.")

    resolved_path = os.path.realpath(os.path.join(canonical_vault_root, normalized_relative))
    if not (resolved_path == canonical_vault_root or resolved_path.startswith(canonical_vault_root + os.sep)):
        raise web.HTTPForbidden(text="볼트 범위를 벗어난 경로입니다.")

    return resolved_path


def _build_directory_tree(directory_path: str, vault_root: str, max_depth: int = 4) -> list[dict]:
    """재귀적으로 볼트 내 폴더와 마크다운/베이스 문서를 계층 트리 구조로 수집한다."""
    if max_depth <= 0:
        return []

    collected_entries: list[dict] = []
    try:
        items = sorted(os.listdir(directory_path))
    except OSError:
        return []

    for item_name in items:
        if item_name.startswith(".") or item_name in EXCLUDED_DIR_NAMES:
            continue
        full_item_path = os.path.join(directory_path, item_name)
        relative_item_path = os.path.relpath(full_item_path, vault_root)

        if os.path.isdir(full_item_path):
            sub_children = _build_directory_tree(full_item_path, vault_root, max_depth - 1)
            collected_entries.append({
                "name": item_name,
                "path": relative_item_path,
                "type": "directory",
                "children": sub_children,
            })
        elif os.path.isfile(full_item_path):
            _, file_extension = os.path.splitext(item_name)
            if file_extension.lower() in ALLOWED_NOTE_EXTENSIONS:
                file_stat = os.stat(full_item_path)
                collected_entries.append({
                    "name": item_name,
                    "path": relative_item_path,
                    "type": "file",
                    "extension": file_extension.lower(),
                    "size": file_stat.st_size,
                    "modified": int(file_stat.st_mtime),
                })

    return collected_entries


async def handle_vault_tree(request: web.Request) -> web.Response:
    """GET /api/vault/tree — Obsidian 볼트의 전체 폴더 및 노트 트리 목록 조회."""
    workdir = _get_workdir()
    canonical_vault_root = os.path.realpath(workdir)
    tree_items = _build_directory_tree(canonical_vault_root, canonical_vault_root)
    vault_name = os.path.basename(canonical_vault_root) or "vault"
    return web.json_response({
        "status": "ok",
        "ok": True,
        "vaultName": vault_name,
        "rootPath": canonical_vault_root,
        "items": tree_items,
    })


async def handle_vault_recent(request: web.Request) -> web.Response:
    """GET /api/vault/recent — 최근 수정된 볼트 노트 목록(최대 25개) 조회."""
    workdir = _get_workdir()
    canonical_vault_root = os.path.realpath(workdir)
    recent_note_candidates: list[dict] = []

    try:
        limit = max(1, min(100, int(request.query.get("limit", "25"))))
    except ValueError:
        limit = 25

    for root_dir, dir_names, file_names in os.walk(canonical_vault_root):
        dir_names[:] = [d for d in dir_names if not d.startswith(".") and d not in EXCLUDED_DIR_NAMES]
        for f_name in file_names:
            if f_name.startswith("."):
                continue
            _, file_extension = os.path.splitext(f_name)
            if file_extension.lower() in ALLOWED_NOTE_EXTENSIONS:
                full_path = os.path.join(root_dir, f_name)
                try:
                    file_stat = os.stat(full_path)
                    relative_path = os.path.relpath(full_path, canonical_vault_root)
                    recent_note_candidates.append({
                        "name": f_name,
                        "path": relative_path,
                        "type": "file",
                        "extension": file_extension.lower(),
                        "size": file_stat.st_size,
                        "modified": int(file_stat.st_mtime),
                    })
                except OSError:
                    pass

    recent_note_candidates.sort(key=lambda item: item["modified"], reverse=True)
    top_items = recent_note_candidates[:limit]
    return web.json_response({
        "status": "ok",
        "ok": True,
        "items": top_items,
        "recentFiles": top_items,
    })


async def handle_vault_get_file(request: web.Request) -> web.Response:
    """GET /api/vault/file?path=... — 특정 마크다운 노트의 본문 및 메타데이터 읽기."""
    target_relative_path = request.query.get("path", "").strip()
    if not target_relative_path:
        return web.json_response({"error": "path 파라미터가 비어 있습니다."}, status=400)

    try:
        absolute_file_path = _resolve_vault_absolute_path(target_relative_path)
    except web.HTTPException as exc:
        return web.json_response({"error": exc.text}, status=exc.status_code)

    if not os.path.isfile(absolute_file_path):
        return web.json_response({"error": "파일을 찾을 수 없습니다."}, status=404)

    try:
        with open(absolute_file_path, "r", encoding="utf-8", errors="replace") as f_reader:
            content_text = f_reader.read()
        file_stat = os.stat(absolute_file_path)
    except OSError as exc:
        return web.json_response({"error": f"파일 열람 실패: {exc}"}, status=500)

    workdir = _get_workdir()
    vault_name = os.path.basename(os.path.realpath(workdir)) or "vault"
    encoded_rel_path = urllib.parse.quote(target_relative_path)
    obsid_url = f"https://obsid.net/?vault={vault_name}&file={encoded_rel_path}"

    return web.json_response({
        "status": "ok",
        "ok": True,
        "name": os.path.basename(absolute_file_path),
        "path": target_relative_path,
        "content": content_text,
        "size": file_stat.st_size,
        "modified": int(file_stat.st_mtime),
        "obsidUrl": obsid_url,
    })


async def handle_vault_save_file(request: web.Request) -> web.Response:
    """POST /api/vault/file — 마크다운 노트 내용 저장/업데이트."""
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "유효하지 않은 JSON 요청입니다."}, status=400)

    target_relative_path = str(payload.get("path") or "").strip()
    content_text = str(payload.get("content") or "")
    if not target_relative_path:
        return web.json_response({"error": "path 필드가 누락되었습니다."}, status=400)

    try:
        absolute_file_path = _resolve_vault_absolute_path(target_relative_path)
    except web.HTTPException as exc:
        return web.json_response({"error": exc.text}, status=exc.status_code)

    parent_directory = os.path.dirname(absolute_file_path)
    os.makedirs(parent_directory, exist_ok=True)

    temp_write_path = f"{absolute_file_path}.tmp.{uuid.uuid4().hex[:6]}"
    try:
        with open(temp_write_path, "w", encoding="utf-8") as f_writer:
            f_writer.write(content_text)
        os.replace(temp_write_path, absolute_file_path)
        file_stat = os.stat(absolute_file_path)
    except OSError as exc:
        if os.path.exists(temp_write_path):
            try:
                os.remove(temp_write_path)
            except OSError:
                pass
        return web.json_response({"error": f"파일 저장 실패: {exc}"}, status=500)

    workdir = _get_workdir()
    vault_name = os.path.basename(os.path.realpath(workdir)) or "vault"
    encoded_rel_path = urllib.parse.quote(target_relative_path)
    obsid_url = f"https://obsid.net/?vault={vault_name}&file={encoded_rel_path}"

    mod = sys.modules.get("web_server")
    vault_log_fn = getattr(mod, "vault_log", vault_log) if mod else vault_log
    vault_log_fn("📝 노트저장", f"[세컨드브레인] {target_relative_path} ({len(content_text)}자)")
    return web.json_response({
        "status": "ok",
        "ok": True,
        "success": True,
        "name": os.path.basename(absolute_file_path),
        "path": target_relative_path,
        "content": content_text,
        "size": file_stat.st_size,
        "modified": int(file_stat.st_mtime),
        "obsidUrl": obsid_url,
    })


async def handle_vault_create_file(request: web.Request) -> web.Response:
    """POST /api/vault/create — 새 마크다운 노트 또는 폴더 생성."""
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "유효하지 않은 JSON 요청입니다."}, status=400)

    target_relative_path = str(payload.get("path") or "").strip()
    initial_content = str(payload.get("content") or "")
    is_directory_creation = bool(
        payload.get("isDirectory", payload.get("is_directory", False))
    )

    if not target_relative_path:
        return web.json_response({"error": "path 필드가 누락되었습니다."}, status=400)

    if not is_directory_creation and not any(target_relative_path.endswith(ext) for ext in ALLOWED_NOTE_EXTENSIONS):
        target_relative_path = f"{target_relative_path}.md"

    try:
        absolute_target_path = _resolve_vault_absolute_path(target_relative_path)
    except web.HTTPException as exc:
        return web.json_response({"error": exc.text}, status=exc.status_code)

    if os.path.exists(absolute_target_path):
        return web.json_response({"error": "이미 동일한 이름의 파일 또는 폴더가 존재합니다."}, status=409)

    mod = sys.modules.get("web_server")
    vault_log_fn = getattr(mod, "vault_log", vault_log) if mod else vault_log

    if is_directory_creation:
        os.makedirs(absolute_target_path, exist_ok=True)
        vault_log_fn("📁 폴더생성", f"[세컨드브레인] {target_relative_path}")
        return web.json_response({
            "status": "ok",
            "ok": True,
            "success": True,
            "path": target_relative_path,
            "type": "directory",
        })

    parent_dir = os.path.dirname(absolute_target_path)
    os.makedirs(parent_dir, exist_ok=True)
    try:
        with open(absolute_target_path, "w", encoding="utf-8") as f_creator:
            f_creator.write(initial_content)
        file_stat = os.stat(absolute_target_path)
    except OSError as exc:
        return web.json_response({"error": f"노트 생성 실패: {exc}"}, status=500)

    vault_log_fn("✨ 새노트생성", f"[세컨드브레인] {target_relative_path}")
    return web.json_response({
        "status": "ok",
        "ok": True,
        "success": True,
        "path": target_relative_path,
        "type": "file",
        "size": file_stat.st_size,
        "modified": int(file_stat.st_mtime),
    })
