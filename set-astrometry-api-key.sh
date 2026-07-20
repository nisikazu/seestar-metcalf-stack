#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
KEY=${1:-}

if [ -z "$KEY" ]; then
    printf "Astrometry.net API key: "
    IFS= read -r KEY
fi

if [ -z "$KEY" ]; then
    echo "API key cannot be empty." >&2
    exit 1
fi

umask 077
printf '%s\n' "$KEY" > "$ROOT/.astrometry_api_key"
chmod 600 "$ROOT/.astrometry_api_key"
echo "Saved $ROOT/.astrometry_api_key"
