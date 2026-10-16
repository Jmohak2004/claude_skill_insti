#!/usr/bin/env python3
"""Watch the watcher: tell the user on WhatsApp when the radar itself breaks.

A silent radar is worse than none: you'd assume there's nothing due. This
checks the pipeline and sends at most one message per problem per day.

  health.py                          check and alert
  health.py --extract-result ok|fail record how the Claude extraction step went
  health.py --dry-run                print problems, send nothing

Problems detected:
  gmail      fetch failing (expired/revoked token, no network) for > RADAR_HEALTH_HOURS (default 12)
  extract    Claude extraction failed on the last 2+ scans (pending mail piling up)
"""
import argparse
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(__file__))
from common import connect, env_int, get_state, iso, now, parse_dt, set_state  # noqa: E402
from notify_whatsapp import send  # noqa: E402


def problems(conn, current):
    found = []
    grace = timedelta(hours=env_int("RADAR_HEALTH_HOURS", 12))

    error = get_state(conn, "last_fetch_error", "")
    if error:
        last_ok = get_state(conn, "last_fetch_ok")
        if not last_ok or current - parse_dt(last_ok) > grace:
            hint = ("Run in Terminal: ~/.deadline-radar/venv/bin/python <skill>/scripts/fetch_gmail.py --auth"
                    if "auth" in error.lower() or "token" in error.lower() or "credentials" in error.lower()
                    else "Check your internet connection and ~/.deadline-radar/logs/scan.log")
            found.append(("gmail", f"📭 Gmail sync is failing, so new deadlines aren't being picked up.\n"
                                   f"   {error.split(' ', 1)[-1][:160]}\n   → {hint}"))

    streak = int(get_state(conn, "extract_fail_streak", "0") or 0)
    pending = conn.execute("SELECT COUNT(*) FROM messages WHERE processed = 0").fetchone()[0]
    if streak >= 2 and pending:
        found.append(("extract", f"🧠 Claude couldn't process new mail on the last {streak} scans "
                                 f"({pending} emails waiting).\n   → Open Claude Code and say "
                                 f"\"process my pending emails\", or check logs/scan.log"))
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--extract-result", choices=["ok", "fail"])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    conn = connect()
    current = now()
    if args.extract_result:
        streak = int(get_state(conn, "extract_fail_streak", "0") or 0)
        set_state(conn, "extract_fail_streak", 0 if args.extract_result == "ok" else streak + 1)
        conn.commit()

    found = problems(conn, current)
    sent = {r["key"] for r in conn.execute("SELECT key FROM alerts WHERE key LIKE 'health:%'")}
    fresh = [(f"health:{name}:{current.date()}", text) for name, text in found
             if f"health:{name}:{current.date()}" not in sent]
    if not fresh:
        print("Radar healthy." if not found else "Known problems already reported today.")
        return
    text = "🛠️ *Deadline Radar needs attention*\n\n" + "\n\n".join(t for _, t in fresh)
    if args.dry_run:
        print(text)
        return
    try:
        send(text)
    except Exception as exc:
        print(f"Could not send health alert: {exc}", file=sys.stderr)
        sys.exit(3)
    conn.executemany("INSERT OR IGNORE INTO alerts (key, item_id, sent_at, message) VALUES (?, NULL, ?, ?)",
                     [(k, iso(current), t[:500]) for k, t in fresh])
    conn.commit()
    print(f"Sent {len(fresh)} health alert(s).")


if __name__ == "__main__":
    main()
