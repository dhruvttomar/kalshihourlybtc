#!/usr/bin/env bash
# deploy.sh — Pull latest code and restart bot on the Digital Ocean server.
#
# Run this from the server whenever you push a new commit:
#   ssh root@<droplet_ip> bash /opt/kalshibot/kalshi_btc_bot/scripts/deploy.sh
#
# The .env and private key are NOT in git — they stay on the server untouched.

set -euo pipefail

BOT_DIR="/opt/kalshibot/kalshi_btc_bot"
REPO_DIR="/opt/kalshibot"

echo "=== Pulling latest code ==="
git -C "$REPO_DIR" pull

echo ""
echo "=== Rebuilding and restarting container ==="
cd "$BOT_DIR"
docker compose up -d --build

echo ""
echo "=== Bot status ==="
docker compose ps

echo ""
echo "=== Recent logs (last 30 lines) ==="
docker compose logs --tail=30
