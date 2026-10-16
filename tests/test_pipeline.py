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

    def run_script(self, name, *args, now=None, check=True):
        env = dict(self.env, RADAR_NOW=now) if now else self.env
        result = subprocess.run([sys.executable, str(SCRIPTS / name), *args], env=env,
                                capture_output=True, text=True)
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
        self.assertEqual(raw.count(b"BEGIN:VEVENT"), 5)
        self.assertTrue(all(len(line) <= 75 for line in raw.split(b"\r\n")))


if __name__ == "__main__":
    unittest.main()
