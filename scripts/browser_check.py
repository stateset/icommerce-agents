"""Run both built applications against an isolated keyless host and real browser.

Uses temporary commerce data and available loopback ports. All owned child processes
are stopped on success, failure, or interruption. Requires Node 22 and Playwright's
Chromium installation; missing prerequisites fail instead of silently skipping.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)


def wait_ready(process: subprocess.Popen, url: str) -> None:
    deadline = time.monotonic() + 45
    with httpx.Client(trust_env=False, timeout=1) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"server exited before readiness: {url}")
            try:
                if client.get(url).is_success:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
    raise TimeoutError(f"server readiness timed out: {url}")


def main() -> int:
    version = subprocess.check_output(["node", "--version"], text=True).strip()
    if int(version.lstrip("v").split(".")[0]) < 22:
        raise RuntimeError("browser verification requires Node 22 or newer")
    with (
        tempfile.TemporaryDirectory(prefix="icommerce-browser-") as temporary,
        ExitStack() as stack,
    ):
        work = Path(temporary)
        ports = {free_port() for _ in range(3)}
        while len(ports) < 3:
            ports.add(free_port())
        host_port, storefront_port, portal_port = sorted(ports)
        host_url = f"http://127.0.0.1:{host_port}"
        storefront_url = f"http://127.0.0.1:{storefront_port}"
        portal_url = f"http://127.0.0.1:{portal_port}"
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("ICOMMERCE_", "ANTHROPIC_", "NEXT_PUBLIC_"))
        }
        env.update(
            ANTHROPIC_API_KEY="",
            PYTHON_DOTENV_DISABLED="1",
            ICOMMERCE_ENVIRONMENT="development",
            ICOMMERCE_AUTH_MODE="demo",
            ICOMMERCE_API_URL=host_url,
            ICOMMERCE_ALLOWED_ORIGINS=f"{storefront_url},{portal_url}",
            DEMO_DB_PATH=str(work / "store.db"),
            PORTAL_URL=portal_url,
            STOREFRONT_URL=storefront_url,
        )

        def start(name: str, command: list[str]) -> subprocess.Popen:
            log = stack.enter_context((work / f"{name}.log").open("w+"))
            process = subprocess.Popen(
                command,
                cwd=ROOT,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            stack.callback(stop, process)
            return process

        try:
            host = start(
                "host",
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "scripts.run_demo:build_app",
                    "--factory",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(host_port),
                ],
            )
            wait_ready(host, f"{host_url}/readyz")
            subprocess.run(
                [
                    sys.executable,
                    "scripts/tour.py",
                    "--db",
                    env["DEMO_DB_PATH"],
                    "--base-url",
                    host_url,
                ],
                cwd=ROOT,
                env=env,
                check=True,
                timeout=90,
            )
            for name, port, url in (
                ("storefront", storefront_port, storefront_url),
                ("portal", portal_port, portal_url),
            ):
                process = start(
                    name,
                    [
                        "node",
                        "node_modules/next/dist/bin/next",
                        "start",
                        f"web/{name}",
                        "--hostname",
                        "127.0.0.1",
                        "--port",
                        str(port),
                    ],
                )
                wait_ready(process, url)
            subprocess.run(
                ["node", "scripts/pw_check.mjs"], cwd=ROOT, env=env, check=True, timeout=90
            )
        except BaseException:
            for log in work.glob("*.log"):
                print(f"{log.name}:\n{log.read_text()[-6000:]}", file=sys.stderr)
            raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
