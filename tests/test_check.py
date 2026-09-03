import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_check_passes_on_a_clean_tree():
    result = subprocess.run(
        [sys.executable, "scripts/check.py"], cwd=ROOT, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_check_fails_on_an_undocumented_engine_backend_module(tmp_path, monkeypatch):
    """The module-coverage check has to be able to fail, or it is decoration. Written
    against `check_modules_documented` directly rather than by dropping a file into
    `engine_backend/`, which would race any other test importing the package."""
    sys.path.insert(0, str(ROOT))
    from scripts import check

    package = tmp_path / "engine_backend"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "documented.py").write_text("")
    (package / "undocumented.py").write_text("")
    mapping = tmp_path / "mapping.md"
    mapping.write_text("`engine_backend/documented.py` does a thing.\n")
    readme = tmp_path / "README.md"
    readme.write_text("nothing here names the other one\n")

    monkeypatch.setattr(check, "ENGINE_BACKEND", package)
    monkeypatch.setattr(check, "MAPPING", mapping)
    monkeypatch.setattr(check, "README", readme)

    problems = check.check_modules_documented()
    assert len(problems) == 1
    assert "undocumented.py" in problems[0]
