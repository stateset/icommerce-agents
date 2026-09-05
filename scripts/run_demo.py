#!/usr/bin/env python3
"""Run the reference demo: the FastAPI host on :8000, and with ``--web`` the two Next.js
web apps (``web/storefront``, ``web/portal``) alongside it. ``--web`` degrades to a
warning rather than crashing if a web app's ``package.json`` is missing.

``--tour`` runs ``scripts/tour.py`` against this same running host over HTTP
(``run_tour(..., base_url=...)``), once the host answers on :8000, so the store has a
placed order and both evidence kinds in it before you open a browser -- no API key
needed. In this ``base_url`` mode the shopping calls (session, cart, checkout,
orders) go to the running host over real HTTP instead of a second in-process
``TestClient`` app; the merchant section still opens a second, pinned ``EngineStore``
handle on ``--db``'s file, concurrently with the host's own -- safe because of that
pin. See ``scripts/tour.py``'s docstring for the full shape of that distinction.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
STOREFRONT_WEB = REPO_ROOT / "web" / "storefront"
MERCHANT_WEB = REPO_ROOT / "web" / "portal"


def _run_web_app(path: Path, port: int) -> subprocess.Popen | None:
    if not (path / "package.json").exists():
        print(f"warning: {path} has no package.json yet; skipping its dev server")
        return None
    return subprocess.Popen(["npm", "run", "dev", "--", "--port", str(port)], cwd=path)


def _wait_for_host(base_url: str, timeout: float = 30.0) -> bool:
    """Poll ``GET /capabilities`` until the host answers or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base_url}/capabilities", timeout=1.0).is_success:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    return False


def main() -> int:
    from host.logs import configure_logging

    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--web", action="store_true", help="also start the storefront and merchant web apps"
    )
    parser.add_argument(
        "--tour",
        action="store_true",
        help=(
            "once the host is up, run scripts/tour.py against it over HTTP so the "
            "store has a placed order and both evidence kinds to look at"
        ),
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
    base_url = "http://127.0.0.1:8000"
    try:
        if args.tour:
            if _wait_for_host(base_url):
                from scripts.tour import main as tour_main

                tour_rc = tour_main(["--db", args.db, "--base-url", base_url])
                if tour_rc != 0:
                    print(f"warning: --tour exited {tour_rc}")
            else:
                print("warning: host never came up on :8000; skipping --tour")
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
    from host.logs import configure_logging

    configure_logging()
    db_path = os.environ.get("DEMO_DB_PATH", str(REPO_ROOT / "data" / "demo.db"))
    return create_app(db_path)


if __name__ == "__main__":
    raise SystemExit(main())
