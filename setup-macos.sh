#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON=${PYTHON:-python3}

if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "Python 3 was not found." >&2
    echo "Install Python from https://www.python.org/downloads/macos/ or Homebrew." >&2
    exit 1
fi

"$PYTHON" -m venv "$ROOT/.venv"
"$ROOT/.venv/bin/python3" -m pip install --upgrade pip
"$ROOT/.venv/bin/python3" -m pip install -r "$ROOT/requirements.txt"

if command -v siril-cli >/dev/null 2>&1; then
    echo "Siril CLI: $(command -v siril-cli)"
elif [ -x /Applications/Siril.app/Contents/MacOS/siril-cli ] || \
     [ -x /Applications/SiriL.app/Contents/MacOS/siril-cli ] || \
     [ -x /Applications/Siril.app/Contents/MacOS/siril ] || \
     [ -x /Applications/SiriL.app/Contents/MacOS/siril ]; then
    echo "Siril application found in /Applications."
else
    echo "Siril CLI was not found." >&2
    echo "Install Siril from https://siril.org/download/ or run: brew install --cask siril" >&2
fi

chmod +x "$ROOT/seestar-metcalf-stack.sh" "$ROOT/set-astrometry-api-key.sh" "$ROOT/macos/build-droplet.sh"

if command -v osacompile >/dev/null 2>&1; then
    "$ROOT/macos/build-droplet.sh"
fi

echo "Setup complete."
echo "Run: $ROOT/seestar-metcalf-stack.sh /path/to/Target_sub"
