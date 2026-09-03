"""Drift check: exits non-zero when the code and the documentation about it disagree.

Four checks:

1. Neither engine backend (`EngineStorefront`, `EngineMerchant`) has an unimplemented
   abstract method left over -- `__abstractmethods__` must be empty on both.
2. The `vendor/commerce-agents` submodule's checked-out commit matches the commit
   recorded in `docs/mapping.md`'s fenced ``submodule-commit`` line.
3. Every function in `engine_backend/` that reads through `store.readonly_sql()` --
   the read-only SQL fallback for a method the engine's Python binding does not expose
   -- is named in `docs/mapping.md`.
4. Every function in `engine_backend/` that opens its own connection with
   `sqlite3.connect(` -- a direct-SQL *write* path, the more dangerous category, since
   it bypasses the binding's own validation entirely -- is named there too.
5. Every module in `engine_backend/` is named in `docs/mapping.md` or `README.md`. This
   is check 3's drift class one level up: a module added without a line about it
   anywhere is how `custom_objects.py` and `listings.py` reached a release candidate
   undocumented.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAPPING = ROOT / "docs" / "mapping.md"
README = ROOT / "README.md"
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


def _functions_containing(needle: str) -> set[str]:
    """Every top-level function/method name in engine_backend/ whose body contains
    `needle`, directly or via a nested closure defined inside it."""
    names: set[str] = set()
    for path in sorted(ENGINE_BACKEND.glob("*.py")):
        source_text = path.read_text()
        tree = ast.parse(source_text, filename=str(path))
        for node in _top_level_defs(tree):
            source = ast.get_source_segment(source_text, node) or ""
            if needle in source:
                names.add(node.name)
    return names


def _documented(mapping_text: str, name: str) -> bool:
    """Whether `name` appears as a backticked reference -- `` `name` ``, `` `name()` ``,
    or qualified as `` `Class.name` `` -- rather than merely as a substring anywhere in
    the file. A bare substring match would silently pass if a function's name happened
    to appear inside unrelated prose or another identifier."""
    reference = re.compile(
        r"`(?:[A-Za-z_][A-Za-z0-9_]*\.)*" + re.escape(name) + r"(\(\)|\(\.\.\.\))?`"
    )
    return reference.search(mapping_text) is not None


def check_sql_paths_documented() -> list[str]:
    """Both direct-SQL categories must be named in `docs/mapping.md`: the read-only
    fallbacks that go through `store.readonly_sql()`, and the write paths that open
    their own `sqlite3.connect(` connection."""
    if not MAPPING.is_file():
        return [f"{MAPPING} does not exist"]
    mapping_text = MAPPING.read_text()
    problems = []
    for needle, what in (
        ("readonly_sql(", "reads through store.readonly_sql()"),
        ("sqlite3.connect(", "opens its own SQLite connection with sqlite3.connect()"),
    ):
        for name in sorted(_functions_containing(needle)):
            if not _documented(mapping_text, name):
                problems.append(
                    f"{name!r} {what} but is not named as a backticked reference "
                    f"(e.g. `{name}`) in {MAPPING.relative_to(ROOT)}"
                )
    return problems


def check_modules_documented() -> list[str]:
    """Every module in `engine_backend/` is named in `docs/mapping.md` or `README.md`.

    A module name is matched as a backticked reference the same way a function name is,
    so a bare mention inside an unrelated path or sentence does not count. `__init__.py`
    is exempt: it is the package's own export list, not a module with behavior to
    describe.
    """
    problems = []
    prose = "\n".join(path.read_text() for path in (MAPPING, README) if path.is_file())
    for path in sorted(ENGINE_BACKEND.glob("*.py")):
        if path.name == "__init__.py":
            continue
        stem, filename = path.stem, path.name
        # `engine_backend/listings.py`, `listings.py`, or `listings` all count.
        if any(re.search(r"`[\w./]*" + re.escape(name) + r"`", prose) for name in (filename, stem)):
            continue
        problems.append(
            f"engine_backend/{filename} is named in neither docs/mapping.md nor README.md"
        )
    return problems


def main() -> int:
    problems: list[str] = []
    problems += check_no_abstract_methods()
    problems += check_submodule_commit()
    problems += check_sql_paths_documented()
    problems += check_modules_documented()

    if problems:
        print("scripts/check.py found drift:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("scripts/check.py: no drift found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
