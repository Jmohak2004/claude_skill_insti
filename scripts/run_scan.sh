#!/bin/bash
# Scheduled scan: fetch Gmail -> let Claude extract deadlines (only if there is
# new mail) -> run the instincts. Called by launchd; safe to run by hand too.
set -uo pipefail

SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RADAR_HOME="${DEADLINE_RADAR_HOME:-$HOME/.deadline-radar}"
PY="$RADAR_HOME/venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
CLAUDE="${CLAUDE_BIN:-claude}"

echo "=== scan $(date '+%Y-%m-%d %H:%M') ==="
"$PY" "$SKILL_DIR/scripts/fetch_gmail.py" || { echo "fetch failed"; exit 1; }

PENDING=$("$PY" "$SKILL_DIR/scripts/db.py" count-pending)
if [ "$PENDING" -gt 0 ]; then
  echo "$PENDING email(s) pending; asking Claude to extract."
  cd "$RADAR_HOME" && "$CLAUDE" -p \
    "Use the deadline-radar skill to process all pending emails: extract deadlines, forms and reply requests and save them with db.py. Work in batches until count-pending is 0, then stop. Do not send notifications yourself." \
    --allowedTools "Bash(python3:*)" "Bash($PY:*)" "Read" "Write" \
    || echo "claude extraction failed (will retry next run)"
fi

"$PY" "$SKILL_DIR/scripts/instincts.py"
