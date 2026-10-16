#!/bin/bash
# Scheduled scan: fetch Gmail -> let Claude extract deadlines (only if there is
# new mail) -> run the instincts -> health check. Called by launchd; safe to run by hand.
set -uo pipefail

SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RADAR_HOME="${DEADLINE_RADAR_HOME:-$HOME/.deadline-radar}"
export DEADLINE_RADAR_HOME="$RADAR_HOME"
PY="$RADAR_HOME/venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
S="$SKILL_DIR/scripts"

echo "=== scan $(date '+%Y-%m-%d %H:%M') ==="
# A failed fetch is recorded for health.py; keep going so already-fetched mail and alerts still work.
"$PY" "$S/fetch_gmail.py" || echo "fetch failed (see health alert)"

PENDING=$("$PY" "$S/db.py" count-pending)
if [ "$PENDING" -gt 0 ]; then
  # Resolved every run: the VS Code extension's bundled CLI moves on each update.
  CLAUDE="$("$PY" -c "import sys; sys.path.insert(0, '$S'); import common; print(common.find_claude() or '')")"
  if [ -z "$CLAUDE" ]; then
    echo "claude CLI not found"
    "$PY" "$S/health.py" --extract-result fail
  else
    echo "$PENDING email(s) pending; asking Claude ($CLAUDE) to extract."
    (cd "$RADAR_HOME" && "$CLAUDE" -p \
      "Use the deadline-radar skill to process all pending emails: extract deadlines, forms and reply requests and save them with db.py. Work in batches until count-pending is 0, then stop. Do not send notifications yourself." \
      --allowedTools "Bash(python3:*)" "Bash($PY:*)" "Read" "Write" "Glob" "Grep")
    LEFT=$("$PY" "$S/db.py" count-pending)
    if [ "$LEFT" -lt "$PENDING" ]; then
      "$PY" "$S/health.py" --extract-result ok
    else
      echo "extraction made no progress ($LEFT still pending)"
      "$PY" "$S/health.py" --extract-result fail
    fi
  fi
fi

"$PY" "$S/instincts.py"
"$PY" "$S/health.py"
