#!/bin/sh
# Generates a self-signed cert for local development into nginx/certs/.
set -eu
dir="$(dirname "$0")/certs"
mkdir -p "$dir"
openssl req -x509 -nodes -newkey rsa:2048 -days 365 \
  -keyout "$dir/server.key" -out "$dir/server.crt" \
  -subj "/CN=localhost" -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"
echo "Wrote $dir/server.crt and $dir/server.key"
