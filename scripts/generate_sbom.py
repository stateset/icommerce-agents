"""Generate a compact SPDX 2.3 inventory for the installed Python and locked npm graph."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent


def _spdx_id(ecosystem: str, name: str, version: str) -> str:
    value = re.sub(r"[^A-Za-z0-9.-]", "-", f"{ecosystem}-{name}-{version}")
    return f"SPDXRef-Package-{value}"


def _package(ecosystem: str, name: str, version: str) -> dict[str, Any]:
    purl_name = quote(name, safe="@/")
    return {
        "SPDXID": _spdx_id(ecosystem, name, version),
        "name": name,
        "versionInfo": version,
        "downloadLocation": "NOASSERTION",
        "filesAnalyzed": False,
        "licenseConcluded": "NOASSERTION",
        "licenseDeclared": "NOASSERTION",
        "externalRefs": [
            {
                "referenceCategory": "PACKAGE-MANAGER",
                "referenceType": "purl",
                "referenceLocator": f"pkg:{ecosystem}/{purl_name}@{quote(version, safe='')}",
            }
        ],
    }


def installed_python_packages() -> list[dict[str, Any]]:
    found: set[tuple[str, str]] = set()
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name")
        version = distribution.version
        if name and version:
            found.add((name, version))
    return [
        _package("pypi", name, version)
        for name, version in sorted(found, key=lambda item: (item[0].lower(), item[1]))
    ]


def locked_npm_packages(lock_path: Path) -> list[dict[str, Any]]:
    lock = json.loads(lock_path.read_text())
    found: set[tuple[str, str]] = set()
    for location, item in lock.get("packages", {}).items():
        if not location or "node_modules/" not in location or not isinstance(item, dict):
            continue
        name = location.rsplit("node_modules/", 1)[-1]
        version = item.get("version")
        if isinstance(version, str):
            found.add((name, version))
    return [
        _package("npm", name, version)
        for name, version in sorted(found, key=lambda item: (item[0].lower(), item[1]))
    ]


def build_sbom(*, created_at: datetime | None = None, commit_sha: str | None = None) -> dict:
    created = (created_at or datetime.now(UTC)).astimezone(UTC)
    if commit_sha is None:
        commit_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    packages = installed_python_packages() + locked_npm_packages(ROOT / "package-lock.json")
    return {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"stateset-icommerce-agents-{commit_sha[:12]}",
        "documentNamespace": (f"https://github.com/stateset/icommerce-agents/sbom/{commit_sha}"),
        "creationInfo": {
            "created": created.isoformat().replace("+00:00", "Z"),
            "creators": ["Tool: scripts/generate_sbom.py"],
        },
        "packages": packages,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(build_sbom(), indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
