#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
SOURCE="$ROOT/macos/SeestarMetcalfStackLauncher.applescript"
OUTPUT="$ROOT/Seestar Metcalf Stack.app"

if ! command -v osacompile >/dev/null 2>&1; then
    echo "osacompile was not found. Run this script on macOS." >&2
    exit 1
fi

case "$OUTPUT" in
    "$ROOT/"*.app) ;;
    *)
        echo "Refusing to replace an application outside $ROOT" >&2
        exit 1
        ;;
esac

if [ -e "$OUTPUT" ]; then
    rm -rf -- "$OUTPUT"
fi

osacompile -o "$OUTPUT" "$SOURCE"
echo "Wrote $OUTPUT"
