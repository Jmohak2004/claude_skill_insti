"""Shared paths, config, time helpers and database access for Deadline Radar.

Everything user-specific (OAuth tokens, .env, the SQLite db, logs) lives in
RADAR_HOME (default ~/.deadline-radar), never inside the skill folder, so the
skill can be shared or re-installed without leaking secrets.
"""
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

RADAR_HOME = Path(os.environ.get("DEADLINE_RADAR_HOME", Path.home() / ".deadline-radar"))
DB_PATH = RADAR_HOME / "radar.db"
ENV_PATH = RADAR_HOME / ".env"


def load_env():
    """Minimal .env loader (no python-dotenv dependency). Real env vars win."""
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env()
TZ = ZoneInfo(os.environ.get("RADAR_TIMEZONE", "Asia/Kolkata"))


def env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def parse_dt(value):
    """Parse an ISO datetime; naive values are assumed to be in the local TZ."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def now():
    """Current time. RADAR_NOW lets you simulate a different moment for testing."""
    override = os.environ.get("RADAR_NOW")
    return parse_dt(override) if override else datetime.now(TZ)


def iso(dt):
    return dt.astimezone(TZ).isoformat(timespec="minutes")


def norm_title(text):
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def humanize_delta(seconds):
    """'in 2d 4h' / '3h ago' / 'in 25m'."""
    future = seconds >= 0
    s = abs(int(seconds))
    days, rem = divmod(s, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        text = f"{days}d {hours}h" if hours else f"{days}d"
    elif hours:
        text = f"{hours}h"
    else:
        text = f"{max(minutes, 1)}m"
    return f"in {text}" if future else f"{text} ago"


def fmt_due(dt):
    return dt.strftime("%a %d %b, %I:%M %p").replace(" 0", " ")


SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    thread_id TEXT,
    sender TEXT,
    recipients TEXT,
    subject TEXT,
    received_at TEXT,
    snippet TEXT,
    body TEXT,
    is_sent INTEGER DEFAULT 0,
    processed INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    kind TEXT NOT NULL,              -- deadline | form | reply | event | payment
    action TEXT,
    due_at TEXT,
    due_is_estimated INTEGER DEFAULT 0,
    priority TEXT DEFAULT 'medium',  -- high | medium | low
    form_url TEXT,
    source_message_id TEXT,
    thread_id TEXT,
    source_received_at TEXT,
    notes TEXT,
    status TEXT DEFAULT 'open',      -- open | done | cancelled
    snoozed_until TEXT,
    created_at TEXT,
    updated_at TEXT,
    completed_at TEXT,
    completed_by TEXT
);
CREATE TABLE IF NOT EXISTS alerts (
    key TEXT PRIMARY KEY,
    item_id INTEGER,
    sent_at TEXT,
    message TEXT
);
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect():
    RADAR_HOME.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def get_state(conn, key, default=None):
    row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(conn, key, value):
    conn.execute(
        "INSERT INTO state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
