#!/usr/bin/env sh
set -eu

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required: https://docs.docker.com/engine/install/" >&2
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose v2 is required." >&2
  exit 1
fi

data_dir=${DATA_DIR:-./data}
mkdir -p "$data_dir/uploads"
chmod 700 "$data_dir"

if [ ! -f .env ]; then
  secret=$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')
  cat > .env <<EOF
SECRET_KEY=$secret
BIND_ADDRESS=${BIND_ADDRESS:-0.0.0.0}
PORT=${PORT:-5000}
DATA_DIR=${DATA_DIR:-./data}
COOKIE_SECURE=0
ALLOWED_ORIGINS=
MAX_UPLOAD_MB=25
TRUST_PROXY_HOPS=0
DOMAIN=${DOMAIN:-}
EOF
  chmod 600 .env
fi

if [ -n "${DOMAIN:-}" ]; then
  sed -i "s|^DOMAIN=.*|DOMAIN=$DOMAIN|" .env
  sed -i "s|^COOKIE_SECURE=.*|COOKIE_SECURE=1|" .env
  sed -i "s|^ALLOWED_ORIGINS=.*|ALLOWED_ORIGINS=https://$DOMAIN|" .env
  sed -i "s|^BIND_ADDRESS=.*|BIND_ADDRESS=127.0.0.1|" .env
  sed -i "s|^TRUST_PROXY_HOPS=.*|TRUST_PROXY_HOPS=1|" .env
  docker compose --profile https up -d --build
  echo "Clipboard Sync is ready at https://$DOMAIN/setup"
else
  docker compose up -d --build
  port=$(sed -n 's/^PORT=//p' .env)
  echo "Clipboard Sync is ready at http://SERVER_IP:${port:-5000}/setup"
  echo "For Internet use, configure HTTPS or rerun with DOMAIN=sync.example.com ./install.sh"
fi
