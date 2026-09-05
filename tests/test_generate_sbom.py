import json
from datetime import UTC, datetime

from scripts.generate_sbom import build_sbom, locked_npm_packages


def test_locked_npm_packages_are_spdx_packages(tmp_path):
    lock = tmp_path / "package-lock.json"
    lock.write_text(
        json.dumps(
            {
                "packages": {
                    "": {"name": "root"},
                    "node_modules/react": {"version": "19.1.0"},
                    "node_modules/@scope/tool": {"version": "2.0.0"},
                }
            }
        )
    )
    packages = locked_npm_packages(lock)
    assert [(item["name"], item["versionInfo"]) for item in packages] == [
        ("@scope/tool", "2.0.0"),
        ("react", "19.1.0"),
    ]
    assert packages[0]["externalRefs"][0]["referenceLocator"].startswith("pkg:npm/")


def test_sbom_binds_document_to_commit():
    document = build_sbom(created_at=datetime(2026, 9, 4, tzinfo=UTC), commit_sha="a" * 40)
    assert document["spdxVersion"] == "SPDX-2.3"
    assert document["documentNamespace"].endswith("a" * 40)
    assert document["packages"]
