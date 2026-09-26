#!/usr/bin/env bash
set -euo pipefail

# Keep the image's development environment aligned with its baked-in lockfile.
if [[ -f /app/pyproject.toml && -f /app/uv.lock ]]; then
    uv sync --frozen --dev
else
    echo "error: /app must contain pyproject.toml and uv.lock" >&2
    exit 2
fi

exec "$@"
