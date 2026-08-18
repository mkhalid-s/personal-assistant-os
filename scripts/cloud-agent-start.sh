#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

export PATH="$(pwd)/.venv/bin:${HOME}/.local/bin:${PATH}"
