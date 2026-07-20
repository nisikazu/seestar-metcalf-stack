#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if [ -x "$ROOT/seestar-metcalf-stack-macos" ]; then
    exec "$ROOT/seestar-metcalf-stack-macos" "$@"
fi

if [ -n "${PYTHON:-}" ]; then
    exec "$PYTHON" "$ROOT/scripts/moving_target_pipeline.py" "$@"
fi

if [ -x "$ROOT/.venv/bin/python3" ]; then
    exec "$ROOT/.venv/bin/python3" "$ROOT/scripts/moving_target_pipeline.py" "$@"
fi

if command -v python3 >/dev/null 2>&1; then
    exec python3 "$ROOT/scripts/moving_target_pipeline.py" "$@"
fi

echo "Python 3 was not found. Run ./setup-macos.sh first." >&2
exit 1
