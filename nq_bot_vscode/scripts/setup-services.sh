#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────
# NQ Trading Bot — systemd Service Installer
# ──────────────────────────────────────────────────────────────
# Installs two services:
#   1. nq-trading-bot@<user>  — The paper trading bot (auto-restarts on crash)
#   2. nq-stats-publisher@<user> — Publishes live stats to GitHub Pages
#
# Usage:
#   sudo bash scripts/setup-services.sh
#   sudo bash scripts/setup-services.sh --uninstall
#
# After install:
#   sudo systemctl start nq-trading-bot@$USER
#   sudo systemctl start nq-stats-publisher@$USER
#   journalctl -u nq-trading-bot@$USER -f     # follow logs
#   sudo systemctl status nq-trading-bot@$USER # check status
# ──────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_DIR="/etc/systemd/system"
TARGET_USER="${SUDO_USER:-$USER}"

if [[ "$1" == "--uninstall" ]] 2>/dev/null; then
    echo "Stopping and removing NQ Trading Bot services..."
    systemctl stop "nq-trading-bot@${TARGET_USER}" 2>/dev/null || true
    systemctl stop "nq-stats-publisher@${TARGET_USER}" 2>/dev/null || true
    systemctl disable "nq-trading-bot@${TARGET_USER}" 2>/dev/null || true
    systemctl disable "nq-stats-publisher@${TARGET_USER}" 2>/dev/null || true
    rm -f "${SERVICE_DIR}/nq-trading-bot@.service"
    rm -f "${SERVICE_DIR}/nq-stats-publisher@.service"
    systemctl daemon-reload
    echo "Services removed."
    exit 0
fi

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: Run with sudo:  sudo bash $0"
    exit 1
fi

echo "Installing NQ Trading Bot services for user: ${TARGET_USER}"
echo ""

# Copy service files (template instances use %i for username)
cp "${SCRIPT_DIR}/nq-trading-bot.service" "${SERVICE_DIR}/nq-trading-bot@.service"
cp "${SCRIPT_DIR}/nq-stats-publisher.service" "${SERVICE_DIR}/nq-stats-publisher@.service"

# Reload systemd
systemctl daemon-reload

# Enable services (start on boot)
systemctl enable "nq-trading-bot@${TARGET_USER}"
systemctl enable "nq-stats-publisher@${TARGET_USER}"

echo ""
echo "══════════════════════════════════════════════════════════"
echo "  Services installed successfully!"
echo "══════════════════════════════════════════════════════════"
echo ""
echo "  Start the bot:"
echo "    sudo systemctl start nq-trading-bot@${TARGET_USER}"
echo ""
echo "  Start the stats publisher:"
echo "    sudo systemctl start nq-stats-publisher@${TARGET_USER}"
echo ""
echo "  View live logs:"
echo "    journalctl -u nq-trading-bot@${TARGET_USER} -f"
echo "    journalctl -u nq-stats-publisher@${TARGET_USER} -f"
echo ""
echo "  Check status:"
echo "    sudo systemctl status nq-trading-bot@${TARGET_USER}"
echo ""
echo "  Stop:"
echo "    sudo systemctl stop nq-trading-bot@${TARGET_USER}"
echo ""
echo "  The bot auto-restarts on crash (30s delay, max 5 retries per 5min)."
echo "  The publisher auto-restarts on crash (15s delay, max 10 retries per 5min)."
echo "══════════════════════════════════════════════════════════"
