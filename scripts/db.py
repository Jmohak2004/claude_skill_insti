#!/usr/bin/env python3
"""Deadline Radar database CLI.

  db.py pending [--limit 20] [--max-body 4000]   JSON batch of unprocessed emails + open items
  db.py count-pending                            number of unprocessed emails
  db.py add <extraction.json>                    apply Claude's extraction (atomic)
  db.py list [--all] [--days N] [--json]         show items
  db.py done <id> [<id> ...]                     mark finished
  db.py cancel <id> [<id> ...]                   mark no longer relevant
  db.py reopen <id>
  db.py snooze <id> <hours>                      silence alerts for a while
"""
import argparse
import json
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(__file__))
from common import (TZ, connect, fmt_due, humanize_delta, iso, norm_title, now,  # noqa: E402
                    parse_dt)

KINDS = {"deadline", "form", "reply", "event", "payment"}
PRIORITIES = {"high", "medium", "low"}
ITEM_FIELDS = ("title", "kind", "action", "due_at", "due_is_estimated", "priority",
               "form_url", "source_message_id", "notes")


def open_items(conn):
    return conn.execute(
        "SELECT * FROM items WHERE status = 'open' ORDER BY due_at IS NULL, due_at"
    ).fetchall()


def compact(item):
    return {k: item[k] for k in ("id", "title", "kind", "due_at", "form_url", "status") if item[k] is not None}


# ---------------------------------------------------------------- pending
def cmd_pending(conn, args):
    rows = conn.execute(
        "SELECT * FROM messages WHERE processed = 0 ORDER BY received_at LIMIT ?", (args.limit,)
    ).fetchall()
    remaining = conn.execute("SELECT COUNT(*) FROM messages WHERE processed = 0").fetchone()[0]
    print(json.dumps({
        "now": iso(now()),
        "timezone": str(TZ),
        "remaining_after_this_batch": max(remaining - len(rows), 0),
        "open_items": [compact(i) for i in open_items(conn)],
        "messages": [{
            "id": r["id"],
            "thread_id": r["thread_id"],
            "from": r["sender"],
            "subject": r["subject"],
            "received_at": r["received_at"],
            "body": (r["body"] or r["snippet"] or "")[: args.max_body],
        } for r in rows],
    }, indent=1, ensure_ascii=False))


def cmd_count_pending(conn, _args):
    print(conn.execute("SELECT COUNT(*) FROM messages WHERE processed = 0").fetchone()[0])


# ---------------------------------------------------------------- add
def validate_item(raw, errors, idx, existing_ids):
    item = {k: raw.get(k) for k in ITEM_FIELDS}
    if raw.get("id") is not None:
        if raw["id"] not in existing_ids:
            errors.append(f"items[{idx}]: id {raw['id']} does not exist")
        item["id"] = raw["id"]
    elif not item["title"]:
        errors.append(f"items[{idx}]: title is required for new items")
    if item["kind"] is not None and item["kind"] not in KINDS:
        errors.append(f"items[{idx}]: kind must be one of {sorted(KINDS)}")
    if item["priority"] is not None and item["priority"] not in PRIORITIES:
        errors.append(f"items[{idx}]: priority must be one of {sorted(PRIORITIES)}")
    if item["due_at"]:
        try:
            item["due_at"] = iso(parse_dt(item["due_at"]))
        except ValueError:
            errors.append(f"items[{idx}]: due_at '{item['due_at']}' is not ISO-8601")
    if raw.get("status") is not None:
        if raw["status"] not in {"open", "done", "cancelled"}:
            errors.append(f"items[{idx}]: status must be open, done or cancelled")
        item["status"] = raw["status"]
    item["due_is_estimated"] = int(bool(item["due_is_estimated"])) if item["due_is_estimated"] is not None else None
    return item


def find_duplicate(conn, item):
    """Same form link or same normalised title among open items = same thing."""
    if item.get("form_url"):
        row = conn.execute(
            "SELECT id FROM items WHERE status = 'open' AND form_url = ?", (item["form_url"],)
        ).fetchone()
        if row:
            return row["id"]
    title = norm_title(item.get("title"))
    for row in open_items(conn):
        if title and norm_title(row["title"]) == title:
            return row["id"]
    return None


def cmd_add(conn, args):
    with open(args.file) as fh:
        data = json.load(fh)
    existing_ids = {r["id"] for r in conn.execute("SELECT id FROM items")}
    known_msgs = {r["id"]: r for r in conn.execute("SELECT id, thread_id, received_at FROM messages")}

    errors = []
    items = [validate_item(raw, errors, i, existing_ids) for i, raw in enumerate(data.get("items", []))]
    completions = data.get("completions", [])
    for i, c in enumerate(completions):
        if c.get("item_id") not in existing_ids:
            errors.append(f"completions[{i}]: item_id {c.get('item_id')} does not exist")
    for i, item in enumerate(items):
        if "id" not in item and item["source_message_id"] and item["source_message_id"] not in known_msgs:
            errors.append(f"items[{i}]: source_message_id {item['source_message_id']} is unknown")
    if errors:
        print("Nothing saved. Fix these and re-run:\n  " + "\n  ".join(errors), file=sys.stderr)
        sys.exit(1)

    stamp = iso(now())
    created, updated = [], []
    for item in items:
        target = item.pop("id", None) or find_duplicate(conn, item)
        if target:
            fields = {k: v for k, v in item.items() if v is not None}
            if fields:
                sets = ", ".join(f"{k} = ?" for k in fields)
                conn.execute(f"UPDATE items SET {sets}, updated_at = ? WHERE id = ?",
                             (*fields.values(), stamp, target))
            updated.append(target)
        else:
            src = known_msgs.get(item["source_message_id"])
            item.setdefault("kind", "deadline")
            cur = conn.execute(
                """INSERT INTO items (title, kind, action, due_at, due_is_estimated, priority, form_url,
                       source_message_id, notes, thread_id, source_received_at, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (item["title"], item["kind"] or "deadline", item["action"], item["due_at"],
                 item["due_is_estimated"] or 0, item["priority"] or "medium", item["form_url"],
                 item["source_message_id"], item["notes"],
                 src["thread_id"] if src else None, src["received_at"] if src else stamp,
                 item.get("status") or "open", stamp, stamp),
            )
            created.append(cur.lastrowid)

    for c in completions:
        conn.execute(
            "UPDATE items SET status = 'done', completed_at = ?, completed_by = ?, updated_at = ? WHERE id = ?",
            (stamp, f"email:{c.get('evidence_message_id', '?')}", stamp, c["item_id"]),
        )

    processed = data.get("processed_message_ids", [])
    conn.executemany("UPDATE messages SET processed = 1 WHERE id = ?", [(m,) for m in processed])
    conn.commit()
    print(f"Saved: {len(created)} new {created}, {len(updated)} updated {updated}, "
          f"{len(completions)} completed, {len(processed)} message(s) marked processed.")


# ---------------------------------------------------------------- list / status changes
def cmd_list(conn, args):
    # Close anything already proven done (form receipt, sent reply) so answers aren't stale.
    from instincts import auto_complete
    auto_complete(conn, conn.execute("SELECT * FROM items WHERE status = 'open'").fetchall())
    where = "1=1" if args.all else "status = 'open'"
    rows = conn.execute(f"SELECT * FROM items WHERE {where} ORDER BY due_at IS NULL, due_at").fetchall()
    current = now()
    if args.days is not None:
        horizon = current + timedelta(days=args.days)
        rows = [r for r in rows if r["due_at"] and parse_dt(r["due_at"]) <= horizon]
    if args.json:
        print(json.dumps([dict(r) for r in rows], indent=1, ensure_ascii=False))
        return
    print(f"Now: {fmt_due(current)} ({TZ})")
    if not rows:
        print("Nothing on the radar.")
        return
    for r in rows:
        if r["due_at"]:
            due = parse_dt(r["due_at"])
            when = f"{fmt_due(due)} ({humanize_delta((due - current).total_seconds())})"
            if r["due_is_estimated"]:
                when += " ~est"
        else:
            when = "no fixed date"
        flag = "" if r["status"] == "open" else f" [{r['status']}]"
        print(f"#{r['id']:<4} {r['kind']:<8} {r['title']} | {when}{flag}")
        if r["action"]:
            print(f"       -> {r['action']}")
        if r["form_url"]:
            print(f"       {r['form_url']}")


def set_status(conn, ids, status, by="manual"):
    stamp = iso(now())
    for item_id in ids:
        conn.execute(
            "UPDATE items SET status = ?, completed_at = ?, completed_by = ?, updated_at = ? WHERE id = ?",
            (status, stamp if status == "done" else None, by if status == "done" else None, stamp, item_id),
        )
    conn.commit()
    print(f"Marked {status}: {ids}")


def cmd_snooze(conn, args):
    until = now() + timedelta(hours=args.hours)
    conn.execute("UPDATE items SET snoozed_until = ? WHERE id = ?", (iso(until), args.id))
    conn.commit()
    print(f"#{args.id} snoozed until {fmt_due(until)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pending")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--max-body", type=int, default=4000)
    sub.add_parser("count-pending")
    p = sub.add_parser("add")
    p.add_argument("file")
    p = sub.add_parser("list")
    p.add_argument("--all", action="store_true")
    p.add_argument("--days", type=int)
    p.add_argument("--json", action="store_true")
    for name in ("done", "cancel"):
        p = sub.add_parser(name)
        p.add_argument("ids", type=int, nargs="+")
    p = sub.add_parser("reopen")
    p.add_argument("ids", type=int, nargs="+")
    p = sub.add_parser("snooze")
    p.add_argument("id", type=int)
    p.add_argument("hours", type=float)
    args = parser.parse_args()

    conn = connect()
    if args.cmd == "pending":
        cmd_pending(conn, args)
    elif args.cmd == "count-pending":
        cmd_count_pending(conn, args)
    elif args.cmd == "add":
        cmd_add(conn, args)
    elif args.cmd == "list":
        cmd_list(conn, args)
    elif args.cmd == "done":
        set_status(conn, args.ids, "done")
    elif args.cmd == "cancel":
        set_status(conn, args.ids, "cancelled")
    elif args.cmd == "reopen":
        set_status(conn, args.ids, "open")
    elif args.cmd == "snooze":
        cmd_snooze(conn, args)


if __name__ == "__main__":
    main()
