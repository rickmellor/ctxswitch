#!/usr/bin/env bash
# Install/upgrade the collector on this host: venv, config stub, systemd --user unit.
set -euo pipefail
cd "$(dirname "$0")/.."
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync --all-extras
mkdir -p ~/.config/ctxswitch ~/.config/systemd/user
[ -f ~/.config/ctxswitch/config.toml ] || { sed "s/^host_id = .*/host_id = \"$(hostname)\"/" config.example.toml > ~/.config/ctxswitch/config.toml; echo "wrote ~/.config/ctxswitch/config.toml — set hub.url/token"; }
cp deploy/ctx-collector.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ctx-collector
systemctl --user --no-pager status ctx-collector | head -5
echo "linger: $(loginctl show-user "$USER" -p Linger --value 2>/dev/null || echo '?') (enable with: loginctl enable-linger $USER)"
