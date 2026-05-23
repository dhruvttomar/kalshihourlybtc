#!/usr/bin/env bash
# setup_droplet.sh — Run ONCE on a fresh Digital Ocean Ubuntu 22.04 droplet.
#
# What it does:
#   1. Installs Docker + git
#   2. Clones the repo to /opt/kalshibot
#   3. Prompts you to paste your .env content
#   4. Prompts you to paste your Kalshi private key (.pem)
#   5. Starts the bot via docker compose
#
# Usage (run as root or with sudo):
#   curl -fsSL <raw_url_of_this_script> | bash
#   — or —
#   scp scripts/setup_droplet.sh root@<droplet_ip>:~
#   ssh root@<droplet_ip> bash setup_droplet.sh

set -euo pipefail

REPO_URL="git@github.com:dhruvttomar/kalshihourlybtc.git"
BOT_DIR="/opt/kalshibot/kalshi_btc_bot"

echo "=== [1/5] Installing Docker and git ==="
apt-get update -qq
apt-get install -y -qq docker.io docker-compose-plugin git curl

systemctl enable --now docker
echo "Docker $(docker --version)"

echo ""
echo "=== [2/5] Cloning repo ==="
mkdir -p /opt/kalshibot
if [ -d "/opt/kalshibot/.git" ]; then
  echo "  Repo already cloned — pulling latest"
  git -C /opt/kalshibot pull
else
  git clone "$REPO_URL" /opt/kalshibot
fi

echo ""
echo "=== [3/5] Pasting .env ==="
echo "  Paste your .env contents below, then press Ctrl-D on a new line:"
cat > "$BOT_DIR/.env"
echo "  .env written."

echo ""
echo "=== [4/5] Pasting Kalshi private key ==="
# Read KALSHI_KEY_PATH from .env to know where to write the key
KEY_PATH=$(grep '^KALSHI_KEY_PATH=' "$BOT_DIR/.env" | cut -d= -f2- | tr -d '"' | tr -d "'")
if [ -z "$KEY_PATH" ]; then
  KEY_PATH="/opt/kalshibot/kalshi_private_key.pem"
  echo "  KALSHI_KEY_PATH not set in .env — defaulting to $KEY_PATH"
fi
mkdir -p "$(dirname "$KEY_PATH")"
echo "  Paste your Kalshi private key (.pem) below, then press Ctrl-D on a new line:"
cat > "$KEY_PATH"
chmod 600 "$KEY_PATH"
echo "  Key written to $KEY_PATH"

echo ""
echo "=== [5/5] Starting bot ==="
cd "$BOT_DIR"
mkdir -p data logs
docker compose up -d --build

echo ""
echo "================================================================"
echo "  Bot is running. Commands:"
echo "  View logs:    docker compose -f $BOT_DIR/docker-compose.yml logs -f"
echo "  Live stats:   docker compose -f $BOT_DIR/docker-compose.yml exec bot python scripts/live_stats.py"
echo "  Stop:         docker compose -f $BOT_DIR/docker-compose.yml down"
echo "  Update:       bash $BOT_DIR/../scripts/deploy.sh"
echo "================================================================"
