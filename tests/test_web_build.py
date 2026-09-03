import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Next 16 (web/storefront, web/portal) requires Node >= 20.9. Below that, `next build`
# fails with an ENOENT on its own build manifest rather than a clear version error, so
# this guard checks the version explicitly and skips with a clear message instead.
MIN_NODE_VERSION = (20, 9)


def _node_version() -> tuple[int, int] | None:
    """Node's reported version, or `None` when it cannot be determined.

    Everything unparseable returns `None` so the caller skips: a `node` that is absent
    or exits non-zero, and output this does not recognise. `"v22"` with no minor, or a
    nightly's `"v23.0.0-nightly..."`, used to raise `ValueError` out of the unpacking or
    the `int()`, past the `except` above, and fail the test rather than skip it.
    """
    try:
        result = subprocess.run(["node", "--version"], capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    # e.g. "v18.20.8\n"
    match = re.match(r"v?(\d+)(?:\.(\d+))?", result.stdout.strip())
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2) or 0)


@pytest.mark.parametrize("app", ["storefront", "portal"])
def test_the_web_app_builds(app):
    if not (ROOT / "node_modules").is_dir():
        pytest.skip("npm install has not run")
    version = _node_version()
    if version is None:
        pytest.skip("node is not on PATH")
    if version < MIN_NODE_VERSION:
        pytest.skip(
            f"Node {version[0]}.{version[1]} is too old for Next 16 (needs "
            f"Node >= {MIN_NODE_VERSION[0]}.{MIN_NODE_VERSION[1]}, e.g. `nvm use 22`)"
        )
    result = subprocess.run(
        ["npm", "run", "build", "--workspace", f"web/{app}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr[-3000:]


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("v18.20.8\n", (18, 20)),
        ("v22\n", (22, 0)),
        ("v23.0.0-nightly20260101\n", (23, 0)),
        ("not a version\n", None),
        ("", None),
    ],
)
def test_node_version_parses_or_returns_none(output, expected, monkeypatch):
    """`None` means "skip", so anything unparseable must reach it rather than raise:
    `"v22"` used to raise `ValueError` past the `except` and fail the test outright."""

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert _node_version() == expected
