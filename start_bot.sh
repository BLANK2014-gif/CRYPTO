#!/usr/bin/env bash
set -e

# Ensure working directory is the repository directory
cd "$(dirname "$0")"

echo "============================================================================"
echo "      ROOSTOO QUANTITATIVE TRADING BOT — AUTONOMOUS LAUNCHER"
echo "============================================================================"
echo ""

# Activate virtualenv if present
if [ -f ".venv/bin/activate" ]; then
    echo "[OK] Activating virtual environment (.venv)..."
    source .venv/bin/activate
elif [ -f "venv/bin/activate" ]; then
    echo "[OK] Activating virtual environment (venv)..."
    source venv/bin/activate
fi

# Enable browser autolaunch
export AUTOLAUNCH_BROWSER=1

echo "[OK] Environment initialized."
echo "Starting Quantitative Trading Bot..."
echo "Dashboard available at http://localhost:8080"
echo ""
echo "----------------------------------------------------------------------------"
echo " OPERATOR CONTROLS:"
echo "   * To STOP gracefully : Press CTRL+C in this console, OR"
echo "                          Run ./stop_bot.sh in another terminal, OR"
echo "                          Click 'STOP BOT' on the web dashboard."
echo "----------------------------------------------------------------------------"
echo ""

exec python3 main.py
