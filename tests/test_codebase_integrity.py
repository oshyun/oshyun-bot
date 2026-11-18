"""test_codebase_integrity — 전체 파이썬 모듈 정적 AST 정합성 검사.

리팩토링·분리 과정에서 발생할 수 있는:
1. 미정의 심볼 참조 (NameError / UnboundLocalError)
2. 동일 모듈 내 최상위 함수·클래스·임포트 중복 정의(shadowing)
을 자동으로 검증한다.
"""

from __future__ import annotations

import ast
import builtins
import glob
import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC_DIR = os.path.join(_REPO_ROOT, "src", "bot")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

_BUILTIN_NAMES = set(dir(builtins)) | {
    "__file__",
    "__name__",
    "__doc__",
    "__package__",
    "__all__",
    "__annotations__",
}


def _get_assigned_names(target: ast.AST) -> set[str]:
    names: set[str] = set()
    if isinstance(target, ast.Name):
        names.add(target.id)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            names |= _get_assigned_names(elt)
    elif isinstance(target, ast.Starred):
        names |= _get_assigned_names(target.value)
    return names


def _collect_direct_defs(nodes: list[ast.AST]) -> set[str]:
    defs: set[str] = set()

    def _visit(node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defs.add(node.name)
            return
        if isinstance(node, ast.Lambda):
            return
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                defs.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                defs.update(_get_assigned_names(t))
        elif isinstance(node, ast.AnnAssign) and node.target:
            defs.update(_get_assigned_names(node.target))
        elif isinstance(node, ast.AugAssign):
            defs.update(_get_assigned_names(node.target))
        elif isinstance(node, ast.NamedExpr):
            defs.update(_get_assigned_names(node.target))
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            defs.update(_get_assigned_names(node.target))
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars:
                    defs.update(_get_assigned_names(item.optional_vars))
        elif isinstance(node, ast.ExceptHandler) and node.name:
            defs.add(node.name)
        for child in ast.iter_child_nodes(node):
            _visit(child)

    for n in nodes:
        _visit(n)
    return defs


class TestASTIntegrity(unittest.TestCase):
    """src/ 및 tests/ 내 모든 .py 파일에 대해 미정의 심볼 및 중복 정의가 없는지 전수 검사."""

    def test_no_undefined_names_or_duplicate_defs(self):
        py_files = sorted(
            glob.glob(os.path.join(_REPO_ROOT, "src", "**", "*.py"), recursive=True)
            + glob.glob(os.path.join(_REPO_ROOT, "tests", "**", "*.py"), recursive=True)
        )
        self.assertGreater(len(py_files), 10)

        errors: list[str] = []

        for path in py_files:
            rel_path = os.path.relpath(path, _REPO_ROOT)
            with open(path, encoding="utf-8") as f:
                src = f.read()
            tree = ast.parse(src, filename=rel_path)

            # 1. 최상위 중복 정의(Import vs def shadowing 포함) 검사
            top_defs: dict[str, int] = {}
            for node in tree.body:
                names: list[tuple[str, int]] = []
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    names.append((node.name, node.lineno))
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    for alias in node.names:
                        names.append(((alias.asname or alias.name.split(".")[0]), node.lineno))
                for name, lineno in names:
                    if name in top_defs:
                        errors.append(
                            f"[{rel_path}:{lineno}] 최상위 심볼 중복 정의(shadowing): "
                            f"'{name}' (첫 정의: {top_defs[name]}행)"
                        )
                    top_defs[name] = lineno

            # 2. 스코프 기반 미정의 심볼(NameError 원인) 검사
            def walk_node(node: ast.AST, scope_defs: set[str]) -> None:
                if isinstance(node, ast.Module):
                    new_defs = scope_defs | _collect_direct_defs(node.body)
                    for child in node.body:
                        walk_node(child, new_defs)
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for d in node.args.defaults + [
                        x for x in node.args.kw_defaults if x is not None
                    ]:
                        walk_node(d, scope_defs)
                    arg_defs = {
                        a.arg
                        for a in node.args.posonlyargs + node.args.args + node.args.kwonlyargs
                    }
                    if node.args.vararg:
                        arg_defs.add(node.args.vararg.arg)
                    if node.args.kwarg:
                        arg_defs.add(node.args.kwarg.arg)
                    new_defs = scope_defs | arg_defs | _collect_direct_defs(node.body)
                    for child in node.body:
                        walk_node(child, new_defs)
                elif isinstance(node, ast.Lambda):
                    for d in node.args.defaults + [
                        x for x in node.args.kw_defaults if x is not None
                    ]:
                        walk_node(d, scope_defs)
                    arg_defs = {
                        a.arg
                        for a in node.args.posonlyargs + node.args.args + node.args.kwonlyargs
                    }
                    if node.args.vararg:
                        arg_defs.add(node.args.vararg.arg)
                    if node.args.kwarg:
                        arg_defs.add(node.args.kwarg.arg)
                    walk_node(node.body, scope_defs | arg_defs)
                elif isinstance(node, ast.ClassDef):
                    for b in node.bases:
                        walk_node(b, scope_defs)
                    new_defs = scope_defs | _collect_direct_defs(node.body)
                    for child in node.body:
                        walk_node(child, new_defs)
                elif isinstance(
                    node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)
                ):
                    comp_defs = set(scope_defs)
                    for gen in node.generators:
                        walk_node(gen.iter, comp_defs)
                        comp_defs |= _get_assigned_names(gen.target)
                        for if_clause in gen.ifs:
                            walk_node(if_clause, comp_defs)
                    if isinstance(node, ast.DictComp):
                        walk_node(node.key, comp_defs)
                        walk_node(node.value, comp_defs)
                    else:
                        walk_node(node.elt, comp_defs)
                else:
                    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                        if node.id not in scope_defs and node.id not in _BUILTIN_NAMES:
                            errors.append(f"[{rel_path}:{node.lineno}] 미정의 심볼 참조: '{node.id}'")
                    for child in ast.iter_child_nodes(node):
                        walk_node(child, scope_defs)

            walk_node(tree, set())

        self.assertEqual(errors, [], "\n" + "\n".join(errors))


if __name__ == "__main__":
    unittest.main()
