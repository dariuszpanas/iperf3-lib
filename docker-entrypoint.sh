#!/usr/bin/env bash
set -euo pipefail

# A bind-mounted checkout can differ from the files baked into the image. Keep
# /opt/venv aligned with its committed lockfile before running the command.
if [[ -f /app/pyproject.toml && -f /app/uv.lock ]]; then
    uv sync --frozen --dev
else
    echo "error: /app must contain pyproject.toml and uv.lock" >&2
    exit 2
fi

exec "$@"
