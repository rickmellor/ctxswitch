#!/usr/bin/env bash
# Bootstrap this host: uv + venv, then `ctx install` (config stub, collector unit, Claude hooks, pi extension).
#   deploy/install-collector.sh [--hub URL --token T] [--claude|--pi|--collector] [--dry-run]
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:$PATH"       # uv lives here; non-login ssh shells miss it
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync --all-extras
exec .venv/bin/ctx install "$@"
