#!/usr/bin/env python3
"""Send a WhatsApp message via CallMeBot or Twilio (stdlib only).

Provider is chosen by NOTIFY_PROVIDER in ~/.deadline-radar/.env:
  callmebot  CALLMEBOT_PHONE (+91...), CALLMEBOT_APIKEY
  twilio     TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, WHATSAPP_TO (+91...),
             TWILIO_WHATSAPP_FROM (default: Twilio sandbox +14155238886)
  console    just print (default; useful for testing)

  notify_whatsapp.py --test            send a test message
  notify_whatsapp.py "any text"        send that text
"""
import base64
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(__file__))
import common  # noqa: E402,F401  (loads .env)

CHUNK = 1000  # CallMeBot truncates long messages; split on line boundaries


def chunks(text, size=CHUNK):
    out, cur = [], ""
    for line in text.splitlines(keepends=True):
        if len(cur) + len(line) > size and cur:
            out.append(cur)
            cur = ""
        cur += line
    if cur:
        out.append(cur)
    return out


def _callmebot(text):
    phone = os.environ.get("CALLMEBOT_PHONE")
    key = os.environ.get("CALLMEBOT_APIKEY")
    if not phone or not key:
        raise RuntimeError("Set CALLMEBOT_PHONE and CALLMEBOT_APIKEY in ~/.deadline-radar/.env")
    for i, part in enumerate(chunks(text)):
        if i:
            time.sleep(3)  # CallMeBot rate-limits bursts
        query = urllib.parse.urlencode({"phone": phone, "text": part, "apikey": key})
        with urllib.request.urlopen(f"https://api.callmebot.com/whatsapp.php?{query}", timeout=30) as resp:
            body = resp.read().decode(errors="replace")
        if "error" in body.lower() or "invalid" in body.lower():
            raise RuntimeError(f"CallMeBot rejected the message: {body[:200]}")


def _twilio(text):
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    token = os.environ.get("TWILIO_AUTH_TOKEN")
    to = os.environ.get("WHATSAPP_TO")
    sender = os.environ.get("TWILIO_WHATSAPP_FROM", "+14155238886")
    if not (sid and token and to):
        raise RuntimeError("Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and WHATSAPP_TO in ~/.deadline-radar/.env")
    auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
    for part in chunks(text, 1500):
        data = urllib.parse.urlencode({
            "From": f"whatsapp:{sender.removeprefix('whatsapp:')}",
            "To": f"whatsapp:{to.removeprefix('whatsapp:')}",
            "Body": part,
        }).encode()
        req = urllib.request.Request(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
            data=data, headers={"Authorization": f"Basic {auth}"},
        )
        try:
            urllib.request.urlopen(req, timeout=30).read()
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Twilio error {exc.code}: {exc.read().decode(errors='replace')[:300]}")


def send(text):
    """Returns True when delivered (or printed). Raises on provider errors."""
    provider = os.environ.get("NOTIFY_PROVIDER", "console").lower()
    if provider == "callmebot":
        _callmebot(text)
    elif provider == "twilio":
        _twilio(text)
    else:
        print("----- WhatsApp (console mode) -----")
        print(text)
        print("-----------------------------------")
    return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    msg = "📡 Deadline Radar test: WhatsApp alerts are working!" if sys.argv[1] == "--test" else " ".join(sys.argv[1:])
    send(msg)
    print(f"Sent via {os.environ.get('NOTIFY_PROVIDER', 'console')}.")
