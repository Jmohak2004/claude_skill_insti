#!/bin/bash
# One-time setup: private data folder, Python venv with Gmail libraries, .env template.
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RADAR_HOME="${DEADLINE_RADAR_HOME:-$HOME/.deadline-radar}"

mkdir -p "$RADAR_HOME/logs" "$RADAR_HOME/extractions"
chmod 700 "$RADAR_HOME"

if [ ! -x "$RADAR_HOME/venv/bin/python" ]; then
  python3 -m venv "$RADAR_HOME/venv"
fi
"$RADAR_HOME/venv/bin/pip" install --quiet --upgrade pip
"$RADAR_HOME/venv/bin/pip" install --quiet -r "$SKILL_DIR/requirements.txt"

if [ ! -f "$RADAR_HOME/.env" ]; then
  cp "$SKILL_DIR/.env.example" "$RADAR_HOME/.env"
  chmod 600 "$RADAR_HOME/.env"
  echo "Created $RADAR_HOME/.env (fill in your WhatsApp details)."
fi

echo "Setup done. Next:"
echo "  1. Put your Google OAuth client file at $RADAR_HOME/credentials.json (see references/setup.md)"
echo "  2. $RADAR_HOME/venv/bin/python $SKILL_DIR/scripts/fetch_gmail.py   # opens browser to authorise"
echo "  3. $RADAR_HOME/venv/bin/python $SKILL_DIR/scripts/notify_whatsapp.py --test"
echo "  4. python3 $SKILL_DIR/scripts/setup_launchd.py --install"
