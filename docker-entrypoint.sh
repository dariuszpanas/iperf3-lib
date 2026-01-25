#!/bin/bash
set -e

# Install the project in editable mode from mounted volume (if pyproject.toml exists)
if [ -f /app/pyproject.toml ]; then
    echo "Installing project from mounted volume..."
    pip install --no-cache-dir -q -e "/app"
    # Install dev dependencies (pytest, ruff, ty, etc.)
    pip install --no-cache-dir -q pytest pytest-asyncio pytest-cov ruff
fi

# Execute the command passed to the container
exec "$@"
