#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi

.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -e .

mkdir -p data
if [[ ! -f data/.env.myos ]]; then
  cp .env.example data/.env.myos
fi

mkdir -p "${HOME}/.local/bin"
ln -sf "$(pwd)/.venv/bin/myos" "${HOME}/.local/bin/myos"
