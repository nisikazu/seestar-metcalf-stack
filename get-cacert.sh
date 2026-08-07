#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
OUTPUT=${1:-"$ROOT/cacert.pem"}
URL='https://curl.se/ca/cacert.pem'

mkdir -p "$(dirname -- "$OUTPUT")"
echo "Downloading public CA bundle from $URL"
curl --fail --location --output "$OUTPUT" "$URL"

if ! grep -q -- '-----BEGIN CERTIFICATE-----' "$OUTPUT"; then
    rm -f -- "$OUTPUT"
    echo "Downloaded file does not look like a PEM CA bundle: $OUTPUT" >&2
    exit 1
fi

echo "Wrote $OUTPUT"
