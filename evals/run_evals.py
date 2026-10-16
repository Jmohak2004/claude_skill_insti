#!/usr/bin/env python3
"""Whole-app evals: run each prompt in evals.json through real headless Claude Code
with the skill installed, then grade the resulting database, files and answer.

  python3 evals/run_evals.py                  # all evals, 4 at a time
  python3 evals/run_evals.py --ids 1 4 14     # a subset
  python3 evals/run_evals.py --jobs 6 --out /tmp/radar-evals

Each eval gets a fresh DEADLINE_RADAR_HOME, NOTIFY_PROVIDER=console and RADAR_OFFLINE=1
(no Gmail, no WhatsApp), and the same tool permissions as the scheduled job.
Writes <out>/eval-<id>-<name>/{answer.md, grading.json, run.json} and <out>/summary.md.
Requires the skill to be installed at ~/.claude/skills/deadline-radar.
"""
import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from common import find_claude  # noqa: E402

ALLOWED_TOOLS = ["Bash(python3:*)", "Read", "Write", "Glob", "Grep"]


# ---------------------------------------------------------------- grading helpers
class Ctx:
    def __init__(self, home, answer, env):
        self.home, self.answer, self.env = Path(home), answer or "", env
        self.conn = sqlite3.connect(self.home / "radar.db")
        self.conn.row_factory = sqlite3.Row

    def items(self, where="1=1", *params):
        return self.conn.execute(f"SELECT * FROM items WHERE {where}", params).fetchall()

    def find(self, *words, status=None):
        """Items whose title/action/notes contain all words (case-insensitive)."""
        out = []
        for r in self.items():
            blob = " ".join(str(r[k] or "") for k in ("title", "action", "notes", "form_url")).lower()
            if all(w.lower() in blob for w in words) and (status is None or r["status"] == status):
                out.append(r)
        return out

    def scalar(self, sql):
        return self.conn.execute(sql).fetchone()[0]

    def says(self, *patterns):
        return all(re.search(p, self.answer, re.I) for p in patterns)

    def script(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "scripts" / args[0]), *args[1:]], env=self.env,
                              capture_output=True, text=True).stdout


def due(row, prefix):
    return bool(row) and (row["due_at"] or "").startswith(prefix)


def check(text, passed, evidence=""):
    return {"text": text, "passed": bool(passed), "evidence": str(evidence)[:300]}


def first(rows):
    return rows[0] if rows else None


def evid(row):
    return dict(row) if row else "no matching item"


# ---------------------------------------------------------------- per-eval checks
def g1(c):
    fee, infy_form, test = first(c.find("fee")), first(c.find("infosys", "regist")), first(c.find("aptitude"))
    dbms, reply = first(c.find("assignment 3")) or first(c.find("normali")), first(c.items("kind = 'reply'"))
    dry = c.script("instincts.py", "--dry-run")
    return [
        check("count-pending is 0", c.scalar("SELECT COUNT(*) FROM messages WHERE processed = 0") == 0),
        check("No item from the Coursera promo (s004)", not c.items("source_message_id = 's004'")),
        check("Exam fee uses the extended date 24 Oct 17:00", due(fee, "2026-10-24T17:00"), evid(fee)),
        check("Infosys registration done or auto-closed",
              (infy_form and infy_form["status"] == "done") or "Auto-completed" in dry and "Infosys" in dry,
              evid(infy_form)),
        check("Infosys aptitude test event on 22 Oct 10:00", due(test, "2026-10-22T10:00"), evid(test)),
        check("DBMS assignment due Mon 19 Oct 10:00", due(dbms, "2026-10-19T10:00"), evid(dbms)),
        check("Reply item for Prof. Mehta with no due date", reply and reply["due_at"] is None, evid(reply)),
        check("Answer mentions the fee extension", c.says(r"extend|moved|pushed|postpone|new (last )?date")),
    ]


def g2(c):
    lab = first(c.find("lab manual"))
    return [check("Lab manual due Sat 17 Oct 17:00", due(lab, "2026-10-17T17:00"), evid(lab)),
            check("Mentions staff room", lab and "staff" in f"{lab['action']} {lab['notes']}".lower(), evid(lab))]


def g3(c):
    lecture = [r for r in c.find("guest lecture") + c.find("cloud security") if r["status"] == "open"]
    return [check("No open guest-lecture item", not lecture, [dict(r) for r in lecture]),
            check("Dry-run alerts don't mention the lecture",
                  "cloud security" not in c.script("instincts.py", "--dry-run").lower())]


def g4(c):
    scam = c.items("source_message_id = 't10' AND status = 'open'")
    return [check("No open item from the scam email (t10)", not scam, [dict(r) for r in scam]),
            check("Answer warns about a scam/phishing email", c.says(r"scam|phish|fraud|suspicious|fake")),
            check("No items from the OTP (t04) or webinar promo (t05)",
                  not c.items("source_message_id IN ('t04', 't05')"))]


def g5(c):
    sih = c.items("form_url LIKE '%SIHinternal2026%' AND status = 'open'")
    return [check("Exactly one open SIH form item", len(sih) == 1, [dict(r) for r in sih]),
            check("SIH item is a form due 22 Oct", sih and sih[0]["kind"] == "form" and due(sih[0], "2026-10-22"),
                  evid(first(sih)))]


def g6(c):
    sch = first(c.find("scholarship", status="open")) or first(c.find("scholarship"))
    blob = f"{sch['action']} {sch['notes']}".lower() if sch else ""
    return [check("Scholarship renewal due 30 Oct 15:00", due(sch, "2026-10-30T15:00"), evid(sch)),
            check("Documents listed", "income" in blob and "aadhaar" in blob, blob)]


def g7(c):
    nov = c.items("due_at LIKE '2026-11-10%'")
    ia2 = c.items("due_at LIKE '2026-10-26T09:30%'")
    return [check("Marks query due 10 Nov (DD/MM)", bool(nov), [dict(r) for r in nov]),
            check("IA-2 on 26 Oct 09:30", bool(ia2), [dict(r) for r in ia2]),
            check("November answer includes the marks query", c.says(r"quer"))]


def g8(c):
    slides = first(c.items("source_message_id = 't08'"))
    return [check("Slides due 16 Oct 18:00", due(slides, "2026-10-16T18:00"), evid(slides)),
            check("Marked estimated", slides and slides["due_is_estimated"] == 1, evid(slides)),
            check("Answer says it's overdue", c.says(r"overdue|missed|already passed|was due|past"))]


def g9(c):
    hr = first(c.find("hackerrank.com/test"))
    return [check("Assessment due 19 Oct 10:00", due(hr, "2026-10-19T10:00"), evid(hr)),
            check("Test link saved", hr is not None),
            check("High priority", hr and hr["priority"] == "high", evid(hr))]


def g10(c):
    offer = first(c.find("brightlabs")) or first(c.find("offer"))
    return [check("Offer acceptance due 23 Oct", due(offer, "2026-10-23"), evid(offer)),
            check("Marked estimated", offer and offer["due_is_estimated"] == 1, evid(offer)),
            check("High priority", offer and offer["priority"] == "high", evid(offer))]


def g11(c):
    interview = c.items("kind = 'event' AND due_at LIKE '2026-10-21T15:00%'")
    reply = c.items("kind = 'reply' AND source_message_id = 'i02'")
    grid = first(c.find("grid"))
    engage = [r for r in c.find("engage") if r["status"] == "open"]
    return [check("Interview event 21 Oct 15:00", bool(interview), [dict(r) for r in interview]),
            check("Reply item to confirm availability (i02)", bool(reply)),
            check("Nothing from LinkedIn (i04)", not c.items("source_message_id = 'i04'")),
            check("Microsoft Engage not left open", not engage, [dict(r) for r in engage]),
            check("GRiD due 17 Oct 23:59", due(grid, "2026-10-17T23:59"), evid(grid))]


def g12(c):
    return [check("No re-extraction (still 7 items)", c.scalar("SELECT COUNT(*) FROM items") == 7),
            check("Lists DBMS assignment and CN journal", c.says(r"DBMS", r"journal")),
            check("Mentions Monday pile-up", c.says(r"(both|two|2).{0,80}(Mon|19)|(Mon|19).{0,80}(both|two|2)"))]


def g13(c):
    five, six = first(c.items("id = 5")), first(c.items("id = 6"))
    now = datetime.fromisoformat(c.env["RADAR_NOW"] + "+05:30")
    snooze_ok = six and six["snoozed_until"] and \
        timedelta(hours=20) <= datetime.fromisoformat(six["snoozed_until"]) - now <= timedelta(hours=28)
    others = c.items("id IN (2, 3, 5, 7) AND status != 'open' AND id != 5")
    return [check("DBMS assignment (#5) done", five and five["status"] == "done", evid(five)),
            check("Elective form (#6) snoozed ~24h", snooze_ok, evid(six)),
            check("No other items changed", not others, [dict(r) for r in others])]


def g14(c):
    infy = first(c.items("id = 1"))
    return [check("Says DBMS assignment is overdue", c.says(r"DBMS", r"overdue|missed|passed|ago|late")),
            check("Flags CN journal due soon", c.says(r"journal")),
            check("Mentions unfilled elective form", c.says(r"elective")),
            check("Infosys registration recognised as done", infy and infy["status"] == "done", evid(infy))]


def g15(c):
    viva = first(c.find("viva"))
    return [check("Viva saved as an event", viva and viva["kind"] == "event", evid(viva)),
            check("Due 24 Oct 10:00", due(viva, "2026-10-24T10:00"), evid(viva)),
            check("High priority", viva and viva["priority"] == "high", evid(viva))]


def g16(c):
    ics = c.home / "deadlines.ics"
    events = ics.read_text().count("BEGIN:VEVENT") if ics.exists() else 0
    dated = c.scalar("SELECT COUNT(*) FROM items WHERE status = 'open' AND due_at IS NOT NULL")
    return [check("deadlines.ics written", ics.exists()),
            check("One event per open dated item", events == dated, f"{events} events / {dated} items"),
            check("Explains importing into Google Calendar", c.says(r"import"))]


def g17(c):
    return [check("Nothing recorded as sent (dry run)", c.scalar("SELECT COUNT(*) FROM alerts") == 0),
            check("Preview shows DBMS assignment and CN journal", c.says(r"DBMS", r"journal"))]


def g18(c):
    return [check("Starts with setup.sh / OAuth credentials", c.says(r"setup\.sh", r"credentials|OAuth")),
            # setup.md says to go one step at a time, so WhatsApp details may come in a later turn
            check("Covers the WhatsApp step (CallMeBot/Twilio)", c.says(r"callmebot|twilio")),
            # Placeholders like "your-key" are fine; a real CallMeBot key is a run of digits.
            check("No invented API key", not re.search(r"APIKEY=\d{4,}", c.answer, re.I))]


def g19(c):
    return [check("Explains extraction failing", c.says(r"extract|claude|process")),
            check("Backlog cleared or concrete fix given",
                  c.scalar("SELECT COUNT(*) FROM messages WHERE processed = 0") == 0 or c.says(r"run_scan|--auth|log"))]


def g20(c):
    infy = first(c.items("id = 1"))
    return [check("Lists DBMS assignment and CN journal for tomorrow", c.says(r"DBMS", r"journal")),
            check("Mentions the elective form as pending", c.says(r"elective")),
            check("Infosys registration not pending (auto-closed)", infy and infy["status"] == "done", evid(infy))]


GRADERS = {i: globals()[f"g{i}"] for i in range(1, 21)}


# ---------------------------------------------------------------- runner
def run_one(ev, out_dir, claude, timeout):
    work = out_dir / f"eval-{ev['id']}-{ev['name']}"
    home = work / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "DEADLINE_RADAR_HOME": str(home), "NOTIFY_PROVIDER": "console",
           "RADAR_OFFLINE": "1", **ev.get("env", {})}
    env.setdefault("RADAR_NOW", "2026-10-17T11:00")
    for cmd in ev.get("setup", []):
        subprocess.run(cmd.replace("python3 ", f"{sys.executable} ", 1), shell=True, cwd=ROOT, env=env,
                       check=True, capture_output=True)
    started = time.time()
    try:
        proc = subprocess.run([claude, "-p", ev["prompt"], "--output-format", "json",
                               "--allowedTools", *ALLOWED_TOOLS], cwd=home, env=env, capture_output=True,
                              text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        meta = json.loads(proc.stdout or "{}")
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        meta = {"result": f"(run failed: {exc})", "is_error": True}
    answer = meta.get("result", "")
    (work / "answer.md").write_text(answer)
    run = {"duration_s": round(time.time() - started, 1), "cost_usd": meta.get("total_cost_usd"),
           "turns": meta.get("num_turns"), "is_error": meta.get("is_error")}
    (work / "run.json").write_text(json.dumps(run, indent=1))
    try:
        results = GRADERS[ev["id"]](Ctx(home, answer, env))
    except Exception as exc:
        results = [check("grader crashed", False, repr(exc))]
    passed = sum(r["passed"] for r in results)
    (work / "grading.json").write_text(json.dumps({"expectations": results, "summary": {
        "passed": passed, "failed": len(results) - passed, "total": len(results),
        "pass_rate": round(passed / len(results), 2)}}, indent=1, ensure_ascii=False))
    print(f"[{ev['id']:>2}] {ev['name']:<30} {passed}/{len(results)}  {run['duration_s']}s", flush=True)
    return ev, results, run


def regrade(out_dir):
    evals = {e["id"]: e for e in json.loads((ROOT / "evals" / "evals.json").read_text())["evals"]}
    total = passed = 0
    for work in sorted(out_dir.glob("eval-*")):
        ev = evals[int(work.name.split("-")[1])]
        env = {**os.environ, "DEADLINE_RADAR_HOME": str(work / "home"), "NOTIFY_PROVIDER": "console",
               "RADAR_OFFLINE": "1", "RADAR_NOW": "2026-10-17T11:00", **ev.get("env", {})}
        results = GRADERS[ev["id"]](Ctx(work / "home", (work / "answer.md").read_text(), env))
        p = sum(r["passed"] for r in results)
        total, passed = total + len(results), passed + p
        print(f"[{ev['id']:>2}] {ev['name']:<30} {p}/{len(results)}")
        for r in results:
            if not r["passed"]:
                print(f"      FAIL {r['text']}: {r['evidence'][:150]}")
    print(f"\nOverall: {passed}/{total}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ids", type=int, nargs="*")
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--regrade", metavar="DIR", help="re-grade an existing run without calling Claude")
    parser.add_argument("--out", default=str(ROOT.parent / "deadline-radar-workspace" /
                                             datetime.now().strftime("run-%Y%m%d-%H%M")))
    args = parser.parse_args()

    if args.regrade:
        regrade(Path(args.regrade).resolve())
        return
    claude = find_claude()
    if not claude:
        sys.exit("Claude Code CLI not found.")
    evals = json.loads((ROOT / "evals" / "evals.json").read_text())["evals"]
    if args.ids:
        evals = [e for e in evals if e["id"] in args.ids]
    out_dir = Path(args.out).resolve()  # Claude runs with cwd=home, so paths must be absolute
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Running {len(evals)} eval(s) with {claude}\n→ {out_dir}\n", flush=True)

    with ThreadPoolExecutor(args.jobs) as pool:
        done = list(pool.map(lambda e: run_one(e, out_dir, claude, args.timeout), evals))

    lines = ["# Deadline Radar eval run", "", "| # | eval | passed | time | cost |", "|---|---|---|---|---|"]
    total = passed = 0
    for ev, results, run in sorted(done, key=lambda d: d[0]["id"]):
        p = sum(r["passed"] for r in results)
        total, passed = total + len(results), passed + p
        cost = f"${run['cost_usd']:.3f}" if run.get("cost_usd") else "-"
        lines.append(f"| {ev['id']} | {ev['name']} | {p}/{len(results)} | {run['duration_s']}s | {cost} |")
    lines += ["", f"**Overall: {passed}/{total} checks passed ({passed / max(total, 1):.0%})**", "", "## Failures"]
    for ev, results, _ in sorted(done, key=lambda d: d[0]["id"]):
        for r in results:
            if not r["passed"]:
                lines.append(f"- **#{ev['id']} {ev['name']}**: {r['text']}. Evidence: `{r['evidence'][:200]}`")
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n")
    print(f"\nOverall: {passed}/{total} checks passed. Summary: {out_dir / 'summary.md'}")


if __name__ == "__main__":
    main()
