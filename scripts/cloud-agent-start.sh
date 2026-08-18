#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

export PATH="$(pwd)/.venv/bin:${PATH}"

mkdir -p data
if [[ ! -f data/.env.myos ]]; then
  cp .env.example data/.env.myos
fi
