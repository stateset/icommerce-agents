#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
git submodule update --init --recursive
if command -v uv >/dev/null 2>&1; then
  uv venv --python 3.12 .venv
else
  python3.12 -m venv .venv
fi
.venv/bin/python -m ensurepip --upgrade >/dev/null 2>&1 || true
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements-dev.txt
echo "Installed. Activate with: source .venv/bin/activate"
