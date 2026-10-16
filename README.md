# 📡 Deadline Radar: a Claude skill

**Never miss a college deadline buried in your inbox again.**

Deadline Radar is a [Claude Code](https://claude.com/claude-code) skill. It reads your Gmail (read-only) and pulls out every deadline, Google Form and "please reply" request from circulars, placement mails, fee notices and professors' emails. Then it **nudges you on WhatsApp**:

- ⏰ **Due soon**: 48h, 24h and 3h before each deadline (plus 72h for high-priority items)
- 🔴 **Overdue**: you missed it and it's still open
- 📝 **Form not filled**: a form link arrived, but no "Your response has been recorded" email ever did
- ↩️ **Waiting on your reply**: someone asked you to respond, and nothing is in your Sent folder for that thread
- ⚠️ **Pile-up**: three things land on the same day
- ☀️ **Morning digest**: today, this week, pending forms and replies
- 🛠️ **Self-check**: if Gmail sign-in expires or Claude can't process mail, you get a message about the radar itself instead of a silent radar

It also **closes items by itself** when it sees the proof (a form receipt in your inbox or your reply in the thread), so you don't have to tick things off by hand.

```
📡 Deadline Radar
✅ Auto-closed: Infosys placement registration

⏰ Due soon
⏰ DBMS assignment 3 (Normalization) — Mon 19 Oct, 10:00 AM (in 1d 23h)
   → Submit on Google Classroom  [#5]

📝 Forms you haven't filled
📝 Sem VI elective preference form
   → Fill elective preferences (first come first serve)
   https://forms.gle/...  [#6]

↩️ Waiting on your reply
↩️ Reply to Prof. Mehta: DBMS mini-project group  [#4]

⚠️ Heads-up: pile-up
⚠️ 2 things due Mon 19 Oct: DBMS assignment 3, CN lab journal submission
```

## How it works

```
 launchd (every 3h)                         launchd (hourly)          launchd (8 AM)
        │                                          │                        │
        ▼                                          ▼                        ▼
 fetch_gmail.py ──▶ Claude reads new mail ──▶ radar.db ──▶ instincts.py ──▶ WhatsApp
 (read-only API)    (skill: judgment —         (SQLite)   (pure rules,     (CallMeBot
                     dates, forms, replies,               no LLM, free)     or Twilio)
                     extensions, scams)
```

The split is deliberate:

- **Claude** only does what needs judgment: knowing that *"kal tak, 5 baje"* means tomorrow at 17:00, that *"10/11"* is 10 November, that a "deadline extended" email updates an existing item, and that a "pay ₹999 to release your scholarship" email is a scam.
- **Scripts** do everything that must be exact and cheap: fetching mail, storage, timing rules, sending messages and scheduling. Claude only runs when new mail arrives. The hourly alert checks cost nothing.

| File | Role |
|---|---|
| `SKILL.md` | Tells Claude which mode to use (scan, query, mark done, setup) |
| `references/extraction.md` | Rules for reading emails: what counts, how to resolve dates, the JSON format |
| `references/setup.md` | Step-by-step setup for Gmail OAuth, WhatsApp and launchd |
| `scripts/fetch_gmail.py` | Gmail fetch with the `gmail.readonly` scope; picks up only new mail and extracts PDF attachment text |
| `scripts/db.py` | Pending batches, all-or-nothing saves with validation, list/done/snooze |
| `scripts/instincts.py` | Alert rules, auto-completion, quiet hours, digest |
| `scripts/health.py` | Warns you when Gmail sync or extraction is failing |
| `scripts/notify_whatsapp.py` | CallMeBot / Twilio / console |
| `scripts/make_ics.py` | Calendar export with reminders |
| `scripts/setup_launchd.py` | macOS scheduling (finds the Claude CLI even when it is bundled inside VS Code) |

## Install

```bash
git clone https://github.com/Jmohak2004/claude_skill_insti.git
ln -s "$PWD/claude_skill_insti" ~/.claude/skills/deadline-radar
bash claude_skill_insti/scripts/setup.sh
```

Then open Claude Code and say **"set up deadline radar"**. Claude walks you through the Google OAuth client, the WhatsApp opt-in and scheduling, as described in [`references/setup.md`](references/setup.md).

Your data stays on your machine in `~/.deadline-radar/` (folder permissions 700): the OAuth token, `.env`, the database and logs. Nothing personal lives in this repo.

## Try it without Gmail

```bash
export DEADLINE_RADAR_HOME=/tmp/radar-demo NOTIFY_PROVIDER=console RADAR_NOW=2026-10-17T11:00
python3 scripts/fetch_gmail.py --from-json samples/sample_emails.json
python3 scripts/db.py add evals/files/seed_extraction.json   # what Claude would extract
python3 scripts/db.py list
python3 scripts/instincts.py                                  # prints the WhatsApp message
RADAR_NOW=2026-10-19T08:00 python3 scripts/instincts.py --digest
```

## Example prompts

Things you can say to Claude once the skill is installed:

1. `scan my emails and tell me what deadlines i have coming up`
2. `what's due this week?`
3. `did i miss anything??`
4. `which forms haven't I filled yet?`
5. `anything people are waiting on me for?`
6. `i submitted the DBMS assignment, mark it done`
7. `snooze the hackathon reminder till tomorrow evening`
8. `add to my radar: DBMS viva on 24th at 10am, its important`
9. `the fee deadline got extended to 30th, update it`
10. `can you put all my deadlines in google calendar`
11. `what whatsapp alerts would i get tomorrow morning at 9?`
12. `send me today's digest now`
13. `check my internship mails and track all the deadlines`
14. `did any of my college notices have deadlines in the attachments?`
15. `bhai kal kya kya submit karna hai? aur koi form bacha hai kya?`
16. `i want whatsapp messages when something is due, how do i start?`
17. `switch my alerts from callmebot to twilio`
18. `my whatsapp alerts stopped and count-pending keeps going up, whats wrong`

## Testing

[![tests](https://github.com/Jmohak2004/claude_skill_insti/actions/workflows/tests.yml/badge.svg)](https://github.com/Jmohak2004/claude_skill_insti/actions/workflows/tests.yml)

```bash
python3 -m unittest discover -s tests -v    # 19 pipeline tests, no network (also run in CI)
python3 evals/run_evals.py --jobs 5         # 20 prompts through real headless Claude, auto-graded
```

The unit tests include the real scheduled job (`run_scan.sh`) run with a stand-in for Claude, and with a broken Claude to check that the health alert fires. `run_evals.py` gives each prompt a fresh data folder, runs `claude -p` with the same restricted permissions as the scheduled job, and grades the resulting database, `.ics` file and answer. It writes `summary.md` plus a `grading.json` per eval.

- [`evals/evals.json`](evals/evals.json): **20 test prompts** with checkable expectations for Claude's side. They cover Hinglish dates, a cancellation in the same batch, scam detection, duplicate reminders, deadlines inside PDF attachments, DD/MM dates, "within 72 hours", "5 working days", and an interview that is both an event and a reply. They run against [`evals/files/`](evals/files/) and can be run with the [skill-creator](https://github.com/anthropics/skills) eval loop.
- [`evals/trigger_evals.json`](evals/trigger_evals.json): **20 trigger queries**. Ten should activate the skill and ten are near-misses that shouldn't (for example "write an email asking for an extension" or "Gmail API send example"). Use these to tune the skill's description.

## Limitations

- Google Forms sends a confirmation email only when the form has "send me a copy" turned on. Otherwise, reply "mark #id done" yourself.
- CallMeBot can only send messages, so you can't reply "done" on WhatsApp. Two-way replies would need Twilio plus a small webhook.
- Scheduling uses launchd, so the Mac has to be awake. A cloud cron job (GitHub Actions or a Claude routine) is on the roadmap.
