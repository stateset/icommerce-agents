import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("app", ["storefront", "portal"])
def test_the_web_app_builds(app):
    if not (ROOT / "node_modules").is_dir():
        pytest.skip("npm install has not run")
    result = subprocess.run(
        ["npm", "run", "build", "--workspace", f"web/{app}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr[-3000:]
