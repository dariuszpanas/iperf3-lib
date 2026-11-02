#!/usr/bin/env bash
set -euo pipefail

# Keep the image's development environment aligned with its baked-in lockfile.
if [[ ! -f /app/pyproject.toml || ! -f /app/uv.lock ]]; then
    echo "error: /app must contain pyproject.toml and uv.lock" >&2
    exit 2
fi
if [[ "${IPERF3_FORCE_UV_SYNC:-0}" == "1" || ! -f /opt/venv/pyvenv.cfg ]]; then
    uv sync --frozen --dev
fi

exec "$@"
