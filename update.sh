#!/usr/bin/env sh
set -eu
profile=""
if [ -f .env ] && grep -q '^DOMAIN=.' .env; then profile="--profile https"; fi
# shellcheck disable=SC2086
docker compose $profile up -d --build
docker image prune -f >/dev/null 2>&1 || true
echo "Clipboard Sync updated. Data remains in ./data."
