"""Insert the mechanical Python-writer gate at every declared store write entry point.

S0 declared fifteen workspace backup/restore/DR/resilience stores in
`release/native_runtime_ownership_v1.json` as `python -> rust` and listed each
store's `writerEntryPoints`. This tool is what turns that declaration into
enforcement: it inserts

    assert_python_writer_allowed("<store>")

as the first statement of every listed function, so that under
`DEEPSEEK_RUNTIME_MODE=python_disabled` a stray Python writer fails closed before
it can touch the store.

The tool is idempotent and refuses to guess: a listed entry point that does not
exist, is not a plain function, or already has a *different* gate at its head is
an error rather than a silent skip. `--check` reports what would change without
writing.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OWNERSHIP_PATH = ROOT / "release" / "native_runtime_ownership_v1.json"
WORKSPACE_MODULES = ROOT / "deepseek_infra" / "infra" / "workspace"
GATE_CALL = "assert_python_writer_allowed"
GATE_IMPORT = "from deepseek_infra.infra.native_runtime.authority import assert_python_writer_allowed"


class GateError(RuntimeError):
    pass


def _declared() -> list[tuple[str, Path, list[str]]]:
    data = json.loads(OWNERSHIP_PATH.read_text(encoding="utf-8"))
    out: list[tuple[str, Path, list[str]]] = []
    for item in data["domains"]:
        points = item.get("writerEntryPoints")
        if not points:
            continue
        modules = {str(point).split(".", 1)[0] for point in points}
        if len(modules) != 1:
            raise GateError(f"{item['id']} spans several modules: {sorted(modules)}")
        path = WORKSPACE_MODULES / f"{modules.pop()}.py"
        out.append((item["id"], path, list(points)))
    return out


def _function_node(tree: ast.Module, qualname: str) -> ast.FunctionDef:
    name = qualname.split(".", 1)[1]
    matches = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    if len(matches) != 1:
        raise GateError(f"{qualname}: expected one top-level function, found {len(matches)}")
    return matches[0]


def _frozen_names() -> set[str]:
    """Names whose exact 4.8.0 AST body the compatibility tests compare.

    Those tests assert full `ast.dump` equality against the frozen source commit,
    so inserting any statement into one of them breaks a legitimate frozen-contract
    check. The gate must therefore sit at an internal choke point instead, and this
    function is what stops a future declaration from silently picking a frozen
    name.
    """
    names: set[str] = set()
    tests = ROOT / "tests"
    for path in sorted(tests.glob("test_native_runtime_*.py")):
        text = path.read_text(encoding="utf-8")
        if "ast.dump" not in text:
            continue
        for block in re.finditer(r"AST_NAMES = \((.*?)\)", text, re.S):
            names.update(re.findall(r'"([A-Za-z_][A-Za-z0-9_]*)"', block.group(1)))
    return names


def _existing_gate(node: ast.FunctionDef) -> str | None:
    for statement in node.body:
        if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
            continue
        call = statement.value
        if isinstance(call.func, ast.Name) and call.func.id == GATE_CALL:
            if call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
                return call.args[0].value
            return ""
    return None


def _line_of_first_statement(node: ast.FunctionDef) -> int:
    """Return the 1-based line of the first *body* statement."""
    first = node.body[0]
    # A docstring stays first so the inserted gate follows it.
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
        if len(node.body) > 1:
            return node.body[1].lineno
        end = first.end_lineno if first.end_lineno is not None else first.lineno
        return end + 1
    return first.lineno


def _apply(module_path: Path, domain: str, qualnames: list[str], *, write: bool) -> list[str]:
    source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    changes: list[str] = []

    # Compute each insertion against the *original* line numbers, then apply from
    # the bottom up so earlier insertions cannot shift later ones.
    insertions: list[tuple[int, str]] = []
    frozen = _frozen_names()
    for qualname in qualnames:
        short = qualname.split(".", 1)[1]
        if short in frozen:
            raise GateError(
                f"{qualname} is a frozen-4.8.0 reference function compared by full AST equality; "
                "declare an internal choke point instead"
            )
        node = _function_node(tree, qualname)
        current = _existing_gate(node)
        if current == domain:
            continue
        if current is not None:
            raise GateError(f"{qualname} already gates a different domain: {current!r}")
        insertions.append((_line_of_first_statement(node), qualname))

    for line_number, qualname in sorted(insertions, reverse=True):
        indent = " " * 4
        lines.insert(line_number - 1, f"{indent}{GATE_CALL}(\"{domain}\")\n")
        changes.append(f"{qualname} -> {domain}")

    if changes and write:
        body = "".join(lines)
        if GATE_IMPORT not in body:
            # Insert after the last *complete* top-level import statement so a
            # multi-line `from x import (...)` can never be split.
            parsed = ast.parse(body)
            last_import_end = 0
            for statement in parsed.body:
                if isinstance(statement, (ast.Import, ast.ImportFrom)):
                    last_import_end = statement.end_lineno or statement.lineno
                elif isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant):
                    continue  # module docstring
                else:
                    break
            import_lines = body.splitlines(keepends=True)
            import_lines.insert(last_import_end, GATE_IMPORT + "\n")
            body = "".join(import_lines)
        ast.parse(body)  # refuse to publish a file that no longer parses
        module_path.write_text(body, encoding="utf-8", newline="\n")
    return changes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gate declared store writers in Python")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    if args.check == args.write:
        print("usage: native_store_writer_gate.py [--check|--write]", file=sys.stderr)
        return 2
    try:
        total = 0
        for domain, path, qualnames in _declared():
            if not path.is_file():
                raise GateError(f"{domain}: missing module {path}")
            changes = _apply(path, domain, qualnames, write=args.write)
            for change in changes:
                print(change)
            total += len(changes)
    except GateError as exc:
        print(f"store writer gate FAIL: {exc}", file=sys.stderr)
        return 1
    verb = "gated" if args.write else "would gate"
    print(f"{verb} {total} entry points")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
