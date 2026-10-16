---
name: deadline-radar
description: Personal deadline radar over Gmail. Pulls new emails (college circulars, placement/TPO mails, fee notices, assignment and exam announcements, Google Form links, professors asking for a reply), extracts every deadline, form and reply-request into a local database, and sends WhatsApp alerts when something is due soon, overdue, a form was never filled, an email was never answered, or deadlines pile up. Use this skill whenever the user wants to scan or process their emails for deadlines, asks "what's due", "did I miss anything", "what forms haven't I filled", wants a deadline digest, wants to mark a deadline done or snooze it, export deadlines to a calendar (.ics), or set up / debug the Gmail + WhatsApp reminder automation, even if they don't say "radar".
---

# Deadline Radar

Turns a noisy inbox into a short list of things the user must act on, and nudges them on WhatsApp before (and after) they slip.

The work is split on purpose:
- **Scripts** do everything that must be exact and cheap: Gmail fetch, storage, timing rules, notifications, calendar export, scheduling.
- **You** do the part that needs judgment: reading emails and deciding what is a real, actionable deadline.

`SKILL_DIR` below means the folder containing this file. User data lives in `RADAR_HOME`: `$DEADLINE_RADAR_HOME` if set, else `~/.deadline-radar/` (db, `.env`, OAuth token, logs). The scripts read that variable themselves, so don't override it. The data scripts are stdlib-only. Run them as plain `python3 SKILL_DIR/scripts/<name>.py …` commands, without `cd … &&` or inline env-var prefixes, because scheduled runs only allow `python3` commands. `fetch_gmail.py` needs the venv (`RADAR_HOME/venv/bin/python`).

## Pick the mode from the request

| User wants | Do |
|---|---|
| Process / scan emails (also what scheduled runs ask for) | **Extraction loop** below |
| "What's due?", "this week", "did I miss anything?" | `python3 SKILL_DIR/scripts/db.py list` (`--days 7`, `--all`), then answer conversationally: overdue first, then soonest |
| Mark finished / not relevant / later | `db.py done <id>`, `db.py cancel <id>`, `db.py snooze <id> <hours>` |
| Preview or send alerts now | `python3 SKILL_DIR/scripts/instincts.py --dry-run` (preview) or without the flag (send) |
| Morning summary | `instincts.py --digest` (add `--force` to resend today) |
| Calendar file | `python3 SKILL_DIR/scripts/make_ics.py` and tell them where it was written |
| Preview a future moment ("alerts tomorrow at 9") | `instincts.py --dry-run --at 2026-10-18T09:00` |
| Is the radar itself working? | `python3 SKILL_DIR/scripts/health.py --dry-run`, plus `RADAR_HOME/logs/scan.log` |
| First-time setup or something is broken | Read `references/setup.md` and walk through it step by step |

If the user mentions a deadline in chat ("add: DBMS viva on 24th at 10am"), save it the same way as an extraction, with `source_message_id` omitted.

## Extraction loop

1. If the request is to scan fresh mail (not just process what's already pending), fetch first:
   `RADAR_HOME/venv/bin/python SKILL_DIR/scripts/fetch_gmail.py`
   If it fails (no credentials yet, expired token, offline), don't stop. Tell the user why in one line (setup or `fetch_gmail.py --auth` fixes it) and carry on with the mail already fetched.
2. Get a batch: `python3 SKILL_DIR/scripts/db.py pending`
   It returns `now`, `timezone`, the currently `open_items`, and up to 20 `messages`.
3. **Read `references/extraction.md` once per session before the first batch.** It defines what counts, how to resolve dates, and the JSON shape.
4. Write your extraction JSON to `RADAR_HOME/extractions/batch-<timestamp>.json`. List **every** message id from the batch in `processed_message_ids`, including ones with nothing actionable, or they come back forever.
5. Save it: `python3 SKILL_DIR/scripts/db.py add <that file>`. The save is all-or-nothing. If it reports errors, fix the JSON and re-run.
6. Repeat from step 2 while `remaining_after_this_batch` > 0.
7. Finish with a short summary for the user: new items (title + due), anything auto-updated (extensions, cancellations, completions), anything that looked like a scam, and anything you were unsure about. Then run `instincts.py --dry-run` so they can see what would be sent, unless this is a scheduled run that said not to notify.

Use `open_items` to avoid duplicates and to catch updates. "Deadline extended to Friday" is an update to the existing item (pass its `id`), not a new item. A form receipt or "registration successful" email is a completion of the matching open item.

## Why the alerts work the way they do

`instincts.py` runs hourly with no LLM, so it costs nothing. It auto-closes items when there is evidence: a sent reply in the same Gmail thread, or a form confirmation email. Then it sends one combined WhatsApp message for anything new: due in 48h, 24h or 3h; overdue; unfilled form; unanswered email; deadline pile-ups. Each alert key is sent once. Quiet hours (default 23:00–07:00) defer alerts rather than drop them. The extraction quality you provide (accurate `due_at`, `kind`, `form_url`) is what makes these rules fire correctly, so precision matters more than recall for noise like newsletters.
