#!/usr/bin/env python3
"""Fetch new Gmail messages (read-only) into the Deadline Radar database.

Usage:
  fetch_gmail.py                 # incremental fetch since the last run
  fetch_gmail.py --days 21       # first run / backfill window (default 14)
  fetch_gmail.py --from-json samples/sample_emails.json   # offline demo, no Gmail

Inbox messages are stored unprocessed (Claude extracts deadlines from them).
Sent messages are stored already-processed: they're only used to detect
whether you replied to a thread.
"""
import argparse
import base64
import html
import json
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))
from common import RADAR_HOME, TZ, connect, get_state, iso, now, set_state  # noqa: E402

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
MAX_BODY = 12000
MAX_ATTACHMENT_TEXT = 6000
# Promotions/social rarely carry real deadlines and would burn extraction tokens.
BASE_QUERY = "-in:spam -in:trash -category:promotions -category:social"


def get_service():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError:
        sys.exit("Google libraries missing. Run scripts/setup.sh (or use the venv python).")

    token_path = RADAR_HOME / "token.json"
    secret_path = RADAR_HOME / "credentials.json"
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not secret_path.exists():
                sys.exit(f"Missing {secret_path}. Follow references/setup.md to create an OAuth client.")
            flow = InstalledAppFlow.from_client_secrets_file(str(secret_path), SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())
        os.chmod(token_path, 0o600)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def b64decode(data):
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def html_to_text(raw):
    # Keep link targets visible so form URLs survive the stripping.
    raw = re.sub(r'(?is)<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', r"\2 (\1)", raw)
    raw = re.sub(r"(?is)<(script|style).*?</\1>", "", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", raw)
    raw = re.sub(r"<[^>]+>", "", raw)
    text = html.unescape(raw)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def pdf_text(data_bytes):
    try:
        from io import BytesIO

        from pypdf import PdfReader
    except ImportError:
        return "(PDF attached; install pypdf to extract its text)"
    try:
        reader = PdfReader(BytesIO(data_bytes))
        return "\n".join((page.extract_text() or "") for page in reader.pages)[:MAX_ATTACHMENT_TEXT]
    except Exception as exc:  # malformed PDFs are common in circulars
        return f"(could not read PDF: {exc})"


def extract_body(service, msg_id, payload):
    plain, htmls, attachments = [], [], []

    def walk(part):
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        filename = part.get("filename") or ""
        if filename and mime == "application/pdf" and body.get("attachmentId"):
            att = service.users().messages().attachments().get(
                userId="me", messageId=msg_id, id=body["attachmentId"]
            ).execute()
            raw = base64.urlsafe_b64decode(att["data"] + "=" * (-len(att["data"]) % 4))
            attachments.append(f"[Attachment: {filename}]\n{pdf_text(raw)}")
        elif mime == "text/plain" and body.get("data") and not filename:
            plain.append(b64decode(body["data"]))
        elif mime == "text/html" and body.get("data") and not filename:
            htmls.append(html_to_text(b64decode(body["data"])))
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)
    text = "\n".join(plain) if plain else "\n".join(htmls)
    if attachments:
        text += "\n\n" + "\n\n".join(attachments)
    return text[:MAX_BODY]


def store(conn, msg):
    conn.execute(
        """INSERT OR IGNORE INTO messages
           (id, thread_id, sender, recipients, subject, received_at, snippet, body, is_sent, processed)
           VALUES (:id, :thread_id, :sender, :recipients, :subject, :received_at, :snippet, :body,
                   :is_sent, :is_sent)""",
        msg,
    )


def fetch_from_gmail(conn, days):
    service = get_service()
    last = get_state(conn, "last_fetch_epoch")
    # 5-minute overlap guards against clock skew; duplicates are ignored by id.
    window = f"after:{int(last) - 300}" if last else f"newer_than:{days}d"
    query = f"{window} {BASE_QUERY}"
    started = int(now().timestamp())

    ids, page_token = [], None
    while True:
        resp = service.users().messages().list(
            userId="me", q=query, pageToken=page_token, maxResults=100
        ).execute()
        ids += [m["id"] for m in resp.get("messages", [])]
        page_token = resp.get("nextPageToken")
        if not page_token or len(ids) >= 500:
            break

    known = {r["id"] for r in conn.execute("SELECT id FROM messages")}
    added = 0
    for msg_id in ids:
        if msg_id in known:
            continue
        full = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
        headers = {h["name"].lower(): h["value"] for h in full["payload"].get("headers", [])}
        received = datetime.fromtimestamp(int(full["internalDate"]) / 1000, TZ)
        store(conn, {
            "id": msg_id,
            "thread_id": full.get("threadId"),
            "sender": headers.get("from", ""),
            "recipients": headers.get("to", ""),
            "subject": headers.get("subject", "(no subject)"),
            "received_at": iso(received),
            "snippet": html.unescape(full.get("snippet", "")),
            "body": extract_body(service, msg_id, full["payload"]),
            "is_sent": 1 if "SENT" in full.get("labelIds", []) else 0,
        })
        added += 1
        conn.commit()

    set_state(conn, "last_fetch_epoch", started)
    return added


def load_from_json(conn, path):
    with open(path) as fh:
        messages = json.load(fh)
    for m in messages:
        m.setdefault("recipients", "")
        m.setdefault("snippet", m.get("body", "")[:120])
        m["is_sent"] = int(bool(m.get("is_sent")))
        store(conn, m)
    return len(messages)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=14, help="lookback on first run (default 14)")
    parser.add_argument("--from-json", help="load messages from a JSON file instead of Gmail")
    args = parser.parse_args()

    conn = connect()
    added = load_from_json(conn, args.from_json) if args.from_json else fetch_from_gmail(conn, args.days)
    conn.commit()
    pending = conn.execute("SELECT COUNT(*) FROM messages WHERE processed = 0").fetchone()[0]
    print(f"Fetched {added} new message(s). Pending extraction: {pending}.")


if __name__ == "__main__":
    main()
