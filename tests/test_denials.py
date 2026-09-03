import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_all_three_denials_are_demonstrated(tmp_path):
    result = subprocess.run(
        [sys.executable, "scripts/denials.py", "--db", str(tmp_path / "store.db")],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    out = result.stdout
    assert "DENIED (agent layer)" in out
    assert out.count("DENIED") == 3
    assert "receipt" in out.lower()
