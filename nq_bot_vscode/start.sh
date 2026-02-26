#!/bin/bash
# ============================================================
# NQ Trading Bot — Quick Start
# ============================================================
# This script sets up the environment and launches the dashboard.
#
# Usage:
#   chmod +x start.sh
#   ./start.sh
# ============================================================

set -e

echo "========================================"
echo "  NQ Trading Bot — Setup & Launch"
echo "========================================"

# Check Python version
echo ""
echo "[1/4] Checking Python..."
python3 --version || { echo "Python 3.11+ required. Install from python.org"; exit 1; }

# Create virtual environment if it doesn't exist
if [ ! -d "venv" ]; then
    echo ""
    echo "[2/4] Creating virtual environment..."
    python3 -m venv venv
else
    echo ""
    echo "[2/4] Virtual environment exists."
fi

# Activate venv
source venv/bin/activate

# Install dependencies
echo ""
echo "[3/4] Installing dependencies..."
pip install -r requirements.txt --quiet

# Copy env template if no .env exists
if [ ! -f ".env" ]; then
    echo ""
    echo "Creating .env from template..."
    cp .env.template .env
    echo "  ⚠  Edit .env with your credentials before running the bot!"
fi

# Launch dashboard
echo ""
echo "[4/4] Launching dashboard server..."
echo ""
echo "========================================"
echo "  Dashboard: http://localhost:8080"
echo "  API docs:  http://localhost:8080/docs"
echo "========================================"
echo ""
echo "  Press Ctrl+C to stop."
echo ""

python -m uvicorn dashboard.server:app --host 0.0.0.0 --port 8080 --reload
