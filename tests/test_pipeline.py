"""End-to-end regression test for the deterministic half of Deadline Radar.

Runs the real scripts against the sample inbox in a throwaway RADAR_HOME,
playing Claude's role with evals/files/seed_extraction.json.

    python3 -m unittest discover -s tests -v
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {**os.environ, "DEADLINE_RADAR_HOME": self.tmp.name, "NOTIFY_PROVIDER": "console",
                    "RADAR_QUIET_HOURS": "23-7", "RADAR_NOW": "2026-10-17T11:00"}
        self.run_script("fetch_gmail.py", "--from-json", str(ROOT / "samples" / "sample_emails.json"))
        self.run_script("db.py", "add", str(ROOT / "evals" / "files" / "seed_extraction.json"))

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self, name, *args, now=None, check=True, env_extra=None, timeout=120):
        env = dict(self.env, **(env_extra or {}))
        if now:
            env["RADAR_NOW"] = now
        result = subprocess.run([sys.executable, str(SCRIPTS / name), *args], env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=timeout)
        if check and result.returncode != 0:
            self.fail(f"{name} failed:\n{result.stderr}")
        return result

    def db(self):
        conn = sqlite3.connect(Path(self.tmp.name) / "radar.db")
        conn.row_factory = sqlite3.Row
        return conn

    def status(self, item_id):
        return self.db().execute("SELECT status FROM items WHERE id = ?", (item_id,)).fetchone()[0]

    def test_extraction_saved_and_inbox_cleared(self):
        self.assertEqual(self.run_script("db.py", "count-pending").stdout.strip(), "0")
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM items").fetchone()[0], 7)

    def test_form_receipt_auto_completes(self):
        out = self.run_script("instincts.py").stdout
        self.assertIn("Auto-completed: #1 Infosys placement registration", out)
        self.assertEqual(self.status(1), "done")

    def test_alerts_fire_once(self):
        first = self.run_script("instincts.py").stdout
        for expected in ("DBMS assignment 3", "Sem VI elective preference form",
                         "Reply to Prof. Mehta", "pile-up"):
            self.assertIn(expected, first)
        self.assertIn("All quiet", self.run_script("instincts.py", now="2026-10-17T11:30").stdout)

    def test_sent_reply_closes_reply_item(self):
        sent = Path(self.tmp.name) / "sent.json"
        sent.write_text(json.dumps([{"id": "s100", "thread_id": "t003", "sender": "me", "subject": "Re",
                                     "received_at": "2026-10-17T19:00:00+05:30", "body": "done",
                                     "is_sent": True}]))
        self.run_script("fetch_gmail.py", "--from-json", str(sent))
        self.run_script("instincts.py", now="2026-10-18T09:00")
        self.assertEqual(self.status(4), "done")

    def test_overdue_and_three_hour_stage(self):
        out = self.run_script("instincts.py", now="2026-10-19T10:30").stdout
        self.assertIn("Missed / overdue", out)
        self.assertIn("CN lab journal submission", out)

    def test_quiet_hours_defer(self):
        out = self.run_script("instincts.py", now="2026-10-19T02:00").stdout
        self.assertIn("Quiet hours", out)
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 0)

    def test_dry_run_records_nothing(self):
        self.run_script("instincts.py", "--dry-run")
        conn = self.db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 0)
        self.assertEqual(self.status(1), "open")

    def test_extension_rearms_reminders(self):
        self.run_script("instincts.py", now="2026-10-23T18:00")  # fee due 24 Oct 17:00 -> 24h alert
        update = Path(self.tmp.name) / "ext.json"
        update.write_text(json.dumps({"items": [{"id": 3, "due_at": "2026-10-30T17:00:00+05:30"}]}))
        self.run_script("db.py", "add", str(update))
        out = self.run_script("instincts.py", now="2026-10-29T18:00").stdout
        self.assertIn("Sem V exam fee", out)

    def test_invalid_extraction_is_rejected_atomically(self):
        bad = Path(self.tmp.name) / "bad.json"
        bad.write_text(json.dumps({"items": [{"title": "ok item", "kind": "deadline"},
                                             {"title": "x", "kind": "bogus", "due_at": "next friday"}]}))
        result = self.run_script("db.py", "add", str(bad), check=False)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM items").fetchone()[0], 7)

    def test_digest_once_per_day(self):
        self.assertIn("Due today", self.run_script("instincts.py", "--digest", now="2026-10-19T08:00").stdout)
        self.assertIn("already sent", self.run_script("instincts.py", "--digest", now="2026-10-19T09:00").stdout)

    def test_ics_export(self):
        out_path = Path(self.tmp.name) / "d.ics"
        self.run_script("make_ics.py", "--out", str(out_path))
        raw = out_path.read_bytes()  # read_text() would turn the required CRLFs into \n
        # 5 dated items, minus the Infosys form whose receipt is already in the inbox
        self.assertEqual(raw.count(b"BEGIN:VEVENT"), 4)
        self.assertNotIn(b"Infosys placement registration", raw)
        self.assertTrue(all(len(line) <= 75 for line in raw.split(b"\r\n")))


    # ------------------------------------------------------------ regressions for audit fixes
    def test_reminder_with_same_form_link_does_not_complete_form(self):
        reminder = Path(self.tmp.name) / "reminder.json"
        reminder.write_text(json.dumps([{
            "id": "r1", "thread_id": "tr1", "sender": "Student Council", "subject": "Reminder: elective form",
            "received_at": "2026-10-17T12:00:00+05:30",
            "body": "Gentle reminder to fill https://forms.gle/ElectSem6Pref9 before allocation."}]))
        self.run_script("fetch_gmail.py", "--from-json", str(reminder))
        self.run_script("instincts.py", now="2026-10-17T13:00")
        self.assertEqual(self.status(6), "open")

    def test_high_priority_gets_72h_warning(self):
        out = self.run_script("instincts.py", "--dry-run", "--at", "2026-10-21T19:00").stdout
        self.assertIn("Sem V exam fee", out)  # high priority, due 24 Oct 17:00 -> inside 72h

    def test_timestamps_normalised(self):
        stored = self.db().execute("SELECT received_at FROM messages WHERE id = 's001'").fetchone()[0]
        self.assertEqual(stored, "2026-10-14T11:20:00+05:30")

    def test_offline_mode_skips_gmail(self):
        result = self.run_script("fetch_gmail.py", check=False, env_extra={"RADAR_OFFLINE": "1"})
        self.assertEqual(result.returncode, 0)
        self.assertIn("Offline mode", result.stdout)

    def test_headless_fetch_fails_fast_instead_of_hanging(self):
        result = self.run_script("fetch_gmail.py", check=False, timeout=60)
        self.assertNotEqual(result.returncode, 0)

    def test_health_reports_gmail_and_extraction_failures(self):
        conn = self.db()
        conn.execute("INSERT INTO state VALUES ('last_fetch_error', '2026-10-17T09:00:00+05:30 "
                     "AuthNeeded: Gmail token could not be refreshed')")
        conn.execute("INSERT INTO messages (id, subject, received_at, processed) "
                     "VALUES ('p1', 'x', '2026-10-17T10:00:00+05:30', 0)")
        conn.commit()
        out = self.run_script("health.py", "--extract-result", "fail").stdout   # gmail alert (once/day)
        out += self.run_script("health.py", "--extract-result", "fail").stdout  # streak hits 2 -> extract alert
        self.assertIn("Gmail sync is failing", out)
        self.assertIn("--auth", out)
        self.assertIn("couldn't process new mail on the last 2 scans", out)
        self.assertIn("already reported", self.run_script("health.py").stdout)

    def test_run_scan_end_to_end_with_stand_in_claude(self):
        """The real scheduled pipeline (run_scan.sh), with a fake `claude` that applies the seed extraction."""
        fresh = tempfile.TemporaryDirectory()
        self.addCleanup(fresh.cleanup)
        fake = Path(fresh.name) / "fake-claude"
        fake.write_text(f"#!/bin/bash\n{sys.executable} {SCRIPTS / 'db.py'} add "
                        f"{ROOT / 'evals' / 'files' / 'seed_extraction.json'}\n")
        fake.chmod(0o755)
        env = dict(self.env, DEADLINE_RADAR_HOME=fresh.name, RADAR_OFFLINE="1", CLAUDE_BIN=str(fake))
        subprocess.run([sys.executable, str(SCRIPTS / "fetch_gmail.py"), "--from-json",
                        str(ROOT / "samples" / "sample_emails.json")], env=env, check=True, capture_output=True)
        result = subprocess.run(["bash", str(SCRIPTS / "run_scan.sh")], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("8 email(s) pending", result.stdout)
        self.assertIn("Auto-completed: #1 Infosys placement registration", result.stdout)
        self.assertIn("Radar healthy", result.stdout)

    def test_run_scan_without_claude_raises_health_alert(self):
        fresh = tempfile.TemporaryDirectory()
        self.addCleanup(fresh.cleanup)
        broken = Path(fresh.name) / "broken-claude"
        broken.write_text("#!/bin/bash\nexit 1\n")
        broken.chmod(0o755)
        env = dict(self.env, DEADLINE_RADAR_HOME=fresh.name, RADAR_OFFLINE="1", CLAUDE_BIN=str(broken))
        subprocess.run([sys.executable, str(SCRIPTS / "fetch_gmail.py"), "--from-json",
                        str(ROOT / "samples" / "sample_emails.json")], env=env, check=True, capture_output=True)
        for _ in range(2):
            result = subprocess.run(["bash", str(SCRIPTS / "run_scan.sh")], env=env, capture_output=True, text=True)
        self.assertIn("extraction made no progress", result.stdout)
        self.assertIn("Deadline Radar needs attention", result.stdout)


if __name__ == "__main__":
    unittest.main()
