#!/usr/bin/env python3
"""Export open dated items to an .ics calendar file (Google/Apple Calendar import).

  make_ics.py [--out ~/.deadline-radar/deadlines.ics]

Each deadline becomes a 30-minute event ending at the due time, with
reminders 1 day and 3 hours before.
"""
import argparse
import os
import sys
from datetime import timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))
from common import RADAR_HOME, connect, now, parse_dt  # noqa: E402


def esc(text):
    return (text or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line):
    """RFC 5545: lines longer than 75 octets continue on lines starting with a space."""
    raw = line.encode()
    if len(raw) <= 75:
        return line
    parts, start = [], 0
    while start < len(raw):
        end = min(start + (75 if not parts else 74), len(raw))
        while end < len(raw) and (raw[end] & 0xC0) == 0x80:  # don't split a UTF-8 char
            end -= 1
        parts.append(raw[start:end].decode())
        start = end
    return "\r\n ".join(parts)


def utc(dt):
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(RADAR_HOME / "deadlines.ics"))
    args = parser.parse_args()

    conn = connect()
    # Don't export things already proven done (form receipt / sent reply) since the last alert run.
    from instincts import auto_complete
    auto_complete(conn, conn.execute("SELECT * FROM items WHERE status = 'open'").fetchall())
    rows = conn.execute("SELECT * FROM items WHERE status = 'open' AND due_at IS NOT NULL").fetchall()
    stamp = utc(now())
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Deadline Radar//EN", "CALSCALE:GREGORIAN",
             "X-WR-CALNAME:Deadline Radar"]
    for r in rows:
        due = parse_dt(r["due_at"])
        desc = "\n".join(filter(None, [r["action"], r["form_url"], r["notes"], f"Radar item #{r['id']}"]))
        lines += [
            "BEGIN:VEVENT",
            f"UID:item-{r['id']}@deadline-radar",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{utc(due - timedelta(minutes=30))}",
            f"DTEND:{utc(due)}",
            f"SUMMARY:{esc(('📌 ' if r['priority'] == 'high' else '') + r['title'])}",
            f"DESCRIPTION:{esc(desc)}",
        ]
        if r["form_url"]:
            lines.append(f"URL:{r['form_url']}")
        for trigger in ("-P1D", "-PT3H"):
            lines += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{esc(r['title'])}",
                      f"TRIGGER;RELATED=END:{trigger}", "END:VALARM"]
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")

    with open(os.path.expanduser(args.out), "w", newline="") as fh:
        fh.write("\r\n".join(fold(line) for line in lines) + "\r\n")
    print(f"Wrote {len(rows)} event(s) to {args.out}")


if __name__ == "__main__":
    main()
