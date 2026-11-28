#!/usr/bin/env bash
set -euo pipefail

if [ $# -lt 1 ]; then
  echo "Usage: $0 <username> [display_name]" >&2
  exit 1
fi

USERNAME="$1"
DISPLAY_NAME="${2:-$1}"
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OUT_FILE="$ROOT_DIR/dozzle/users.yml"

read -rsp "Password for Dozzle user '$USERNAME': " PASSWORD
echo

if [ -z "$PASSWORD" ]; then
  echo "Error: password cannot be empty." >&2
  exit 1
fi

docker run --rm amir20/dozzle:latest generate "$USERNAME" \
  --password "$PASSWORD" \
  --name "$DISPLAY_NAME" > "$OUT_FILE"

chmod 600 "$OUT_FILE"
echo "Wrote $OUT_FILE"
