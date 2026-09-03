"""Drift check: exits non-zero when the code and the documentation about it disagree.

Three checks:

1. Neither engine backend (`EngineStorefront`, `EngineMerchant`) has an unimplemented
   abstract method left over -- `__abstractmethods__` must be empty on both.
2. The `vendor/commerce-agents` submodule's checked-out commit matches the commit
   recorded in `docs/mapping.md`'s fenced ``submodule-commit`` line.
3. Every function in `engine_backend/` that reads through `store.readonly_sql()` --
   the read-only SQL fallback for a method the engine's Python binding does not expose
   -- is named in `docs/mapping.md`.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAPPING = ROOT / "docs" / "mapping.md"
ENGINE_BACKEND = ROOT / "engine_backend"

SUBMODULE_COMMIT_RE = re.compile(r"submodule-commit:\s*([0-9a-f]{40})")


def check_no_abstract_methods() -> list[str]:
    problems = []
    from engine_backend.kernel import KernelClient
    from engine_backend.merchant import EngineMerchant
    from engine_backend.store import EngineStore
    from engine_backend.storefront import EngineStorefront

    store = EngineStore(":memory:")
    kernel_stub = KernelClient.__new__(KernelClient)
    for name, cls, args in (
        ("EngineStorefront", EngineStorefront, (store,)),
        ("EngineMerchant", EngineMerchant, (store, kernel_stub)),
    ):
        instance = cls(*args)
        abstract = getattr(type(instance), "__abstractmethods__", frozenset())
        if abstract:
            problems.append(f"{name} still has abstract methods: {sorted(abstract)}")
    return problems


def _current_submodule_commit() -> str:
    result = subprocess.run(
        ["git", "submodule", "status", "vendor/commerce-agents"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    line = result.stdout.strip()
    # Format: " <sha> vendor/commerce-agents (heads/main)" -- a leading '+' or '-'
    # instead of a space means the checkout doesn't match what's recorded in git, which
    # is a drift condition in its own right, so strip any leading status character.
    sha = line.lstrip("+- ").split()[0]
    return sha


def check_submodule_commit() -> list[str]:
    if not MAPPING.is_file():
        return [f"{MAPPING} does not exist"]
    text = MAPPING.read_text()
    match = SUBMODULE_COMMIT_RE.search(text)
    if match is None:
        return [f"{MAPPING} has no fenced 'submodule-commit: <sha>' line"]
    documented = match.group(1)
    actual = _current_submodule_commit()
    if documented != actual:
        return [
            f"docs/mapping.md records submodule commit {documented}, "
            f"but vendor/commerce-agents is checked out at {actual}"
        ]
    return []


def _top_level_defs(node: ast.AST) -> list[ast.AsyncFunctionDef | ast.FunctionDef]:
    """Module-level functions and class methods only -- not a nested closure defined
    inside one of them (e.g. `list_variants`'s inner `body`), whose generic name would
    otherwise collide with unrelated prose in the documentation."""
    defs: list[ast.AsyncFunctionDef | ast.FunctionDef] = []
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.AsyncFunctionDef | ast.FunctionDef):
            defs.append(child)
        elif isinstance(child, ast.ClassDef):
            defs.extend(_top_level_defs(child))
    return defs


def _readonly_sql_functions() -> set[str]:
    """Every top-level function/method name in engine_backend/ whose body calls
    `store.readonly_sql()` or `self.store.readonly_sql()`, directly or via a nested
    closure defined inside it."""
    names: set[str] = set()
    for path in sorted(ENGINE_BACKEND.glob("*.py")):
        source_text = path.read_text()
        tree = ast.parse(source_text, filename=str(path))
        for node in _top_level_defs(tree):
            source = ast.get_source_segment(source_text, node) or ""
            if "readonly_sql(" in source:
                names.add(node.name)
    return names


def check_readonly_sql_fallbacks_documented() -> list[str]:
    """Each name must appear as a backticked reference (`` `name` `` or `` `name(...)` ``,
    optionally inside a fenced code span), not merely as a substring anywhere in the
    file -- a bare substring match would silently pass if a function's name happened to
    appear inside unrelated prose or another identifier."""
    if not MAPPING.is_file():
        return [f"{MAPPING} does not exist"]
    mapping_text = MAPPING.read_text()
    problems = []
    for name in sorted(_readonly_sql_functions()):
        reference = re.compile(r"`" + re.escape(name) + r"(\(\)|\(\.\.\.\))?`")
        if reference.search(mapping_text) is None:
            problems.append(
                f"{name!r} reads through store.readonly_sql() but is not named as a "
                f"backticked reference (e.g. `{name}`) in {MAPPING.relative_to(ROOT)}"
            )
    return problems


def main() -> int:
    problems: list[str] = []
    problems += check_no_abstract_methods()
    problems += check_submodule_commit()
    problems += check_readonly_sql_fallbacks_documented()

    if problems:
        print("scripts/check.py found drift:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("scripts/check.py: no drift found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
