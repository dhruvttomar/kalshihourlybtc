#!/usr/bin/env bash
# setup_droplet.sh — Run ONCE on a fresh Digital Ocean Ubuntu 22.04 droplet.
#
# Usage:
#   scp kalshi_btc_bot/scripts/setup_droplet.sh root@<ip>:~
#   ssh root@<ip> bash setup_droplet.sh

set -euo pipefail

REPO_URL="git@github.com:dhruvttomar/kalshihourlybtc.git"
BOT_DIR="/opt/kalshibot/kalshi_btc_bot"
DEPLOY_KEY="/root/.ssh/github_deploy"

echo "=== [1/6] Installing Docker and git ==="
apt-get update -qq
apt-get install -y -qq git curl
# Use Docker's official install script — works on all Ubuntu versions
if ! command -v docker &>/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi
apt-get install -y -qq docker-compose-plugin 2>/dev/null || true
systemctl enable --now docker
echo "Docker $(docker --version)"

echo ""
echo "=== [2/6] Generating GitHub deploy key ==="
if [ ! -f "$DEPLOY_KEY" ]; then
  ssh-keygen -t ed25519 -C "kalshibot-deploy" -f "$DEPLOY_KEY" -N ""
fi
echo ""
echo "  ┌─────────────────────────────────────────────────────────────┐"
echo "  │  Add this deploy key to GitHub before continuing:          │"
echo "  │  https://github.com/dhruvttomar/kalshihourlybtc/settings/keys/new │"
echo "  │  Title: kalshibot-do  |  Read access only  |  paste below: │"
echo "  └─────────────────────────────────────────────────────────────┘"
echo ""
cat "$DEPLOY_KEY.pub"
echo ""
read -rp "  Press ENTER once you've added the deploy key to GitHub... "

# Configure SSH to use this key for github.com
cat >> /root/.ssh/config <<EOF

Host github.com
  IdentityFile $DEPLOY_KEY
  StrictHostKeyChecking no
EOF
chmod 600 /root/.ssh/config

echo ""
echo "=== [3/6] Cloning repo ==="
mkdir -p /opt/kalshibot
if [ -d "/opt/kalshibot/.git" ]; then
  echo "  Repo already cloned — pulling latest"
  git -C /opt/kalshibot pull
else
  git clone "$REPO_URL" /opt/kalshibot
fi

echo ""
echo "=== [4/6] Writing .env ==="
echo "  Paste your full .env contents below, then press Ctrl-D on a NEW blank line:"
cat > "$BOT_DIR/.env"
chmod 600 "$BOT_DIR/.env"
echo "  .env written."

echo ""
echo "=== [5/6] Writing Kalshi private key ==="
KEY_PATH=$(grep '^KALSHI_KEY_PATH=' "$BOT_DIR/.env" | cut -d= -f2- | tr -d '"' | tr -d "'")
if [ -z "$KEY_PATH" ]; then
  KEY_PATH="/opt/kalshibot/kalshi_btc_bot/keys/kalshi.pem"
  echo "  KALSHI_KEY_PATH not in .env — will use $KEY_PATH"
  echo "  Add this line to your .env: KALSHI_KEY_PATH=$KEY_PATH"
fi
mkdir -p "$(dirname "$KEY_PATH")"
echo "  Paste your Kalshi .pem private key below, then press Ctrl-D on a NEW blank line:"
cat > "$KEY_PATH"
chmod 600 "$KEY_PATH"
echo "  Key written to $KEY_PATH"

echo ""
echo "=== [6/6] Starting bot ==="
cd "$BOT_DIR"
mkdir -p data logs
docker compose up -d --build

echo ""
echo "================================================================"
echo "  Bot is running!"
echo ""
echo "  Logs:       docker compose -f $BOT_DIR/docker-compose.yml logs -f"
echo "  Stats:      docker compose -f $BOT_DIR/docker-compose.yml exec bot python scripts/live_stats.py"
echo "  Stop:       docker compose -f $BOT_DIR/docker-compose.yml down"
echo "  Update:     bash $BOT_DIR/scripts/deploy.sh"
echo "================================================================"
