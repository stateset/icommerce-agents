import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_all_three_denials_are_demonstrated(engine_db):
    result = subprocess.run(
        [sys.executable, "scripts/denials.py", "--db", engine_db("store.db")],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    out = result.stdout
    assert "DENIED (agent layer)" in out
    assert out.count("DENIED") == 3
    assert "receipt" in out.lower()


def test_a_rerun_against_the_same_db_produces_all_three_denials_again(engine_db):
    """A fixed idempotency key on the over-refund attempt would make a second run
    against the same store replay the first run's idempotency conflict instead of the
    over-refund denial. Run twice against one `--db` file and assert both runs show
    all three denials -- not two denials and a conflict."""
    db_path = engine_db("store.db")

    for attempt in (1, 2):
        result = subprocess.run(
            [sys.executable, "scripts/denials.py", "--db", db_path],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"attempt {attempt} failed: {result.stderr[-3000:]}"
        out = result.stdout
        assert out.count("DENIED") == 3, f"attempt {attempt} did not show three denials: {out}"
        assert "NOT DENIED" not in out, f"attempt {attempt} had an unfired denial: {out}"
        assert "All three denials fired as expected." in out
