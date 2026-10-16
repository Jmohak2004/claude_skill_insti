#!/usr/bin/env python3
"""Deadline Radar instincts: decide what deserves a WhatsApp nudge right now.

Pure rules, no LLM, so it is cheap to run every hour.

  instincts.py              check everything, send one combined alert if needed
  instincts.py --digest     morning summary (once per day)
  instincts.py --dry-run    print what would be sent, record nothing
  RADAR_NOW=2026-10-20T09:00 instincts.py --dry-run    simulate a moment in time

Rules (each alert fires once; keys are stored in the `alerts` table):
  approaching  due within 48h, 24h, 3h (re-arms if the deadline changes)
  overdue      deadline passed and the item is still open (only within 7 days)
  form         form link with no confirmation email yet
  unreplied    someone asked for a reply and nothing was sent in that thread
  clash        several deadlines bunched on one day / within a few hours
Before alerting, items are auto-closed when evidence exists: a reply you sent
in the thread, or a form-confirmation email ("Your response has been recorded").
"""
import argparse
import os
import re
import sys
from collections import defaultdict
from datetime import timedelta

sys.path.insert(0, os.path.dirname(__file__))
from common import (connect, env_int, fmt_due, humanize_delta, iso, norm_title, now,  # noqa: E402
                    parse_dt)
from notify_whatsapp import send  # noqa: E402

STAGES = [(3, "3h"), (24, "24h"), (48, "48h")]
OVERDUE_GRACE_DAYS = 7
CONFIRM_PATTERNS = re.compile(
    r"your response has been recorded|thanks for filling out|thank you for filling|"
    r"thank you for (your )?submi|we (have )?received your (response|submission|application)|"
    r"copy of your responses|response receipt|registration (is )?(successful|confirmed)|"
    r"successfully (registered|submitted)",
    re.I,
)
KIND_EMOJI = {"form": "📝", "reply": "↩️", "payment": "💸", "event": "📅", "deadline": "⏰"}


def form_token(url):
    """The unique bit of a form link, which also appears in its receipt email."""
    if not url:
        return None
    for pattern in (r"forms/d/(?:e/)?([\w-]{15,})", r"forms\.gle/([\w-]{6,})",
                    r"forms\.office\.com/.*?[?&]id=([\w-]{10,})"):
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    return None


def title_overlap(title, text):
    words = [w for w in norm_title(title).split() if len(w) > 2]
    if not words:
        return 0
    hay = norm_title(text)
    return sum(1 for w in words if w in hay) / len(words)


# ------------------------------------------------------------------ auto-complete
def auto_complete(conn, items, write=True):
    closed = []
    stamp = iso(now())
    for item in items:
        evidence = None
        since = item["source_received_at"] or item["created_at"]
        if item["kind"] == "reply" and item["thread_id"]:
            row = conn.execute(
                "SELECT id FROM messages WHERE is_sent = 1 AND thread_id = ? AND received_at > ? LIMIT 1",
                (item["thread_id"], since),
            ).fetchone()
            if row:
                evidence = f"auto:reply-sent:{row['id']}"
        elif item["kind"] == "form":
            token = form_token(item["form_url"])
            for msg in conn.execute(
                "SELECT id, subject, body FROM messages WHERE is_sent = 0 AND received_at > ?", (since,)
            ):
                text = f"{msg['subject']}\n{msg['body'] or ''}"
                if msg["id"] == item["source_message_id"]:
                    continue
                if (token and token in text) or (
                    CONFIRM_PATTERNS.search(text) and title_overlap(item["title"], text[:1500]) >= 0.6
                ):
                    evidence = f"auto:form-confirmation:{msg['id']}"
                    break
        if evidence and not write:
            closed.append(item)
        elif evidence:
            conn.execute(
                "UPDATE items SET status = 'done', completed_at = ?, completed_by = ?, updated_at = ? WHERE id = ?",
                (stamp, evidence, stamp, item["id"]),
            )
            closed.append(item)
    conn.commit()
    return closed


# ------------------------------------------------------------------ rules
def line_for(item, current, prefix=None):
    emoji = prefix or KIND_EMOJI.get(item["kind"], "⏰")
    text = f"{emoji} *{item['title']}*"
    if item["due_at"]:
        due = parse_dt(item["due_at"])
        text += f" — {fmt_due(due)} ({humanize_delta((due - current).total_seconds())})"
        if item["due_is_estimated"]:
            text += " ~"
    if item["action"]:
        text += f"\n   → {item['action']}"
    if item["form_url"]:
        text += f"\n   {item['form_url']}"
    return text + f"  [#{item['id']}]"


def collect_alerts(conn, items, current):
    """Returns {section: [(key, item_id, text)]} for alerts not sent before."""
    sent = {r["key"] for r in conn.execute("SELECT key FROM alerts")}
    reply_after = timedelta(hours=env_int("RADAR_REPLY_AFTER_HOURS", 24))
    form_nudge_after = timedelta(hours=env_int("RADAR_FORM_NUDGE_HOURS", 48))
    sections = defaultdict(list)

    def add(section, key, item, text):
        if key not in sent:
            sections[section].append((key, item["id"] if item else None, text))

    for item in items:
        if item["snoozed_until"] and parse_dt(item["snoozed_until"]) > current:
            continue
        age = current - parse_dt(item["source_received_at"] or item["created_at"])

        if item["due_at"]:
            due = parse_dt(item["due_at"])
            left = due - current
            if left.total_seconds() < 0:
                if -left <= timedelta(days=OVERDUE_GRACE_DAYS):
                    add("overdue", f"{item['id']}:{item['due_at']}:overdue", item, line_for(item, current, "🔴"))
            else:
                for hours, label in STAGES:
                    if left <= timedelta(hours=hours):
                        # Key includes due_at so an extended deadline re-arms its reminders.
                        add("soon", f"{item['id']}:{item['due_at']}:{label}", item, line_for(item, current))
                        break
        else:
            if item["kind"] == "form" and age >= form_nudge_after:
                add("forms", f"{item['id']}:form-nudge", item, line_for(item, current))
            elif item["kind"] == "reply" and age >= reply_after:
                add("replies", f"{item['id']}:unreplied", item, line_for(item, current))

    # Clashes: look a week ahead for bunched deadlines.
    upcoming = sorted(
        (i for i in items if i["due_at"] and current <= parse_dt(i["due_at"]) <= current + timedelta(days=7)),
        key=lambda i: i["due_at"],
    )
    by_day = defaultdict(list)
    for i in upcoming:
        by_day[parse_dt(i["due_at"]).date()].append(i)
    clash_count = env_int("RADAR_CLASH_COUNT", 3)
    for day, group in by_day.items():
        close_pair = any(
            parse_dt(b["due_at"]) - parse_dt(a["due_at"]) <= timedelta(hours=3) for a, b in zip(group, group[1:])
        )
        if len(group) >= clash_count or (len(group) >= 2 and close_pair):
            ids = ",".join(str(i["id"]) for i in group)
            names = ", ".join(i["title"] for i in group)
            add("clash", f"clash:{day}:{ids}", None,
                f"⚠️ {len(group)} things due {day.strftime('%a %d %b')}: {names}")
    return sections


SECTION_TITLES = [
    ("overdue", "🔴 *Missed / overdue*"),
    ("soon", "⏰ *Due soon*"),
    ("forms", "📝 *Forms you haven't filled*"),
    ("replies", "↩️ *Waiting on your reply*"),
    ("clash", "⚠️ *Heads-up: pile-up*"),
]


def in_quiet_hours(current):
    spec = os.environ.get("RADAR_QUIET_HOURS", "23-7")
    try:
        start, end = (int(x) for x in spec.split("-"))
    except ValueError:
        return False
    h = current.hour
    return (start <= h or h < end) if start > end else (start <= h < end)


def build_digest(items, current):
    today_end = current.replace(hour=23, minute=59)
    week_end = current + timedelta(days=7)
    groups = defaultdict(list)
    for i in items:
        if i["due_at"]:
            due = parse_dt(i["due_at"])
            if due < current:
                groups["overdue"].append(i)
            elif due <= today_end:
                groups["today"].append(i)
            elif due <= week_end:
                groups["week"].append(i)
        elif i["kind"] == "form":
            groups["forms"].append(i)
        elif i["kind"] == "reply":
            groups["replies"].append(i)
    parts = [f"☀️ *Deadline Radar — {current.strftime('%a %d %b')}*"]
    labels = [("overdue", "🔴 Overdue"), ("today", "📌 Due today"), ("week", "🗓️ Next 7 days"),
              ("forms", "📝 Forms pending"), ("replies", "↩️ Replies pending")]
    for key, label in labels:
        if groups[key]:
            parts.append(f"\n{label}")
            parts += [line_for(i, current, "•") for i in groups[key]]
    if len(parts) == 1:
        parts.append("Nothing pending. Enjoy the day! 🎉")
    parts.append("\nReply to Claude: “mark #id done” when finished.")
    return "\n".join(parts)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--digest", action="store_true", help="send the daily summary")
    parser.add_argument("--dry-run", action="store_true", help="print only, record nothing")
    parser.add_argument("--force", action="store_true", help="ignore quiet hours / already-sent digest")
    args = parser.parse_args()

    conn = connect()
    current = now()
    items = conn.execute("SELECT * FROM items WHERE status = 'open'").fetchall()

    closed = auto_complete(conn, items, write=not args.dry_run)
    if closed:
        print("Auto-completed: " + ", ".join(f"#{i['id']} {i['title']}" for i in closed))
    closed_ids = {i["id"] for i in closed}
    items = [i for i in items if i["id"] not in closed_ids]

    if args.digest:
        key = f"digest:{current.date()}"
        if conn.execute("SELECT 1 FROM alerts WHERE key = ?", (key,)).fetchone() and not args.force:
            print("Digest already sent today.")
            return
        text = build_digest(items, current)
        records = [(key, None)]
    else:
        if in_quiet_hours(current) and not args.force:
            print("Quiet hours: deferring alerts.")
            return
        sections = collect_alerts(conn, items, current)
        if not any(sections.values()):
            print("All quiet. Nothing new to alert.")
            return
        parts = ["📡 *Deadline Radar*"]
        if closed:
            parts.append("✅ Auto-closed: " + ", ".join(i["title"] for i in closed))
        records = []
        for section, title in SECTION_TITLES:
            if sections.get(section):
                parts.append(f"\n{title}")
                for key, item_id, text in sections[section]:
                    parts.append(text)
                    records.append((key, item_id))
        text = "\n".join(parts)

    if args.dry_run:
        print(text)
        return
    send(text)
    stamp = iso(current)
    conn.executemany(
        "INSERT OR IGNORE INTO alerts (key, item_id, sent_at, message) VALUES (?, ?, ?, ?)",
        [(k, i, stamp, text[:500]) for k, i in records],
    )
    conn.commit()
    print(f"Sent {len(records)} alert(s).")


if __name__ == "__main__":
    main()
