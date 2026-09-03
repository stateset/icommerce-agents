#!/usr/bin/env python3
"""Run the reference demo: the FastAPI host on :8000, and with ``--web`` the two Next.js
web apps (``web/storefront``, ``web/portal``) alongside it. ``--web`` degrades to a
warning rather than crashing if a web app's ``package.json`` is missing.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STOREFRONT_WEB = REPO_ROOT / "web" / "storefront"
MERCHANT_WEB = REPO_ROOT / "web" / "portal"


def _run_web_app(path: Path, port: int) -> subprocess.Popen | None:
    if not (path / "package.json").exists():
        print(f"warning: {path} has no package.json yet; skipping its dev server")
        return None
    return subprocess.Popen(["npm", "run", "dev", "--", "--port", str(port)], cwd=path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--web", action="store_true", help="also start the storefront and merchant web apps"
    )
    parser.add_argument("--db", default=str(REPO_ROOT / "data" / "demo.db"))
    args = parser.parse_args()

    Path(args.db).parent.mkdir(parents=True, exist_ok=True)

    processes: list[subprocess.Popen] = []
    if args.web:
        for path, port in ((STOREFRONT_WEB, 3000), (MERCHANT_WEB, 3100)):
            process = _run_web_app(path, port)
            if process is not None:
                processes.append(process)

    uvicorn_env = {"DEMO_DB_PATH": args.db}
    env = os.environ | uvicorn_env
    host_process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "scripts.run_demo:build_app",
            "--factory",
            "--host",
            "0.0.0.0",
            "--port",
            "8000",
        ],
        cwd=REPO_ROOT,
        env=env,
    )
    try:
        host_process.wait()
    except KeyboardInterrupt:
        pass
    finally:
        host_process.terminate()
        for process in processes:
            process.terminate()
    return 0


def build_app():
    """The uvicorn factory target: ``host.app.create_app`` over ``DEMO_DB_PATH``."""
    from host.app import create_app

    db_path = os.environ.get("DEMO_DB_PATH", str(REPO_ROOT / "data" / "demo.db"))
    return create_app(db_path)


if __name__ == "__main__":
    raise SystemExit(main())
