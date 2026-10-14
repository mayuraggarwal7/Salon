#!/usr/bin/env bash
# One-time setup on a fresh OVH VPS (Ubuntu 22.04/24.04 or Debian 12). Run as root or with sudo:
#   curl -fsSL https://raw.githubusercontent.com/mayuraggarwal7/salon/main/deploy/ovh-setup.sh | sudo bash -s -- salon.example.com
# or, after cloning the repo:  sudo deploy/ovh-setup.sh salon.example.com
set -euo pipefail

DOMAIN="${1:?usage: ovh-setup.sh <domain>}"
# Run from inside a clone to use that clone; otherwise clone into /opt/salon.
SCRIPT_REPO="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." 2>/dev/null && pwd || true)"
if [ -n "$SCRIPT_REPO" ] && [ -f "$SCRIPT_REPO/docker-compose.yml" ]; then
  APP_DIR="${APP_DIR:-$SCRIPT_REPO}"
else
  APP_DIR="${APP_DIR:-/opt/salon}"
fi
REPO="${REPO:-https://github.com/mayuraggarwal7/salon.git}"

# Docker Engine + Compose plugin (official convenience script).
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi

# Firewall: SSH and web only.
if command -v ufw >/dev/null; then
  ufw allow OpenSSH && ufw allow 80/tcp && ufw allow 443/tcp && ufw --force enable
fi

if [ ! -d "$APP_DIR/.git" ]; then
  git clone "$REPO" "$APP_DIR"
fi
cd "$APP_DIR"

if [ ! -f .env ]; then
  rand() { openssl rand -base64 48 | tr -d '\n/+=' | cut -c1-"$1"; }
  sed -e "s|^DOMAIN=.*|DOMAIN=$DOMAIN|" \
      -e "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(rand 40)|" \
      -e "s|^DJANGO_SECRET_KEY=.*|DJANGO_SECRET_KEY=$(rand 60)|" \
      -e "s|^DJANGO_ADMIN_PASSWORD=.*|DJANGO_ADMIN_PASSWORD=$(rand 20)|" \
      -e "s|^WHATSAPP_VERIFY_TOKEN=.*|WHATSAPP_VERIFY_TOKEN=$(rand 32)|" \
      .env.example > .env
  chmod 600 .env
  echo "Created $APP_DIR/.env with generated passwords."
fi

docker compose up -d --build
echo
echo "Done. Open https://$DOMAIN once its DNS A record points at this server."
echo "Manager login: $(grep ^DJANGO_ADMIN_USERNAME= .env | cut -d= -f2), password in $APP_DIR/.env (DJANGO_ADMIN_PASSWORD)."
