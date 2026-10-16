# Setup (about 15 minutes)

Walk the user through these one at a time. Check each step works before moving on.

## 1. Install
```bash
bash SKILL_DIR/scripts/setup.sh
```
This creates `~/.deadline-radar/` (private, chmod 700), a Python venv with the Gmail libraries, and `.env`.

## 2. Gmail read-only access (Google Cloud OAuth client)
1. Go to https://console.cloud.google.com/ → create a project (e.g. "deadline-radar").
2. **APIs & Services → Library** → enable **Gmail API**.
3. **APIs & Services → OAuth consent screen** → External → fill app name + your email → add yourself under **Test users**.
4. **Credentials → Create credentials → OAuth client ID → Desktop app** → Download JSON.
5. Save it as `~/.deadline-radar/credentials.json`.
6. Run `~/.deadline-radar/venv/bin/python SKILL_DIR/scripts/fetch_gmail.py`. A browser opens, you approve **read-only** access, and `token.json` is saved.
   - "Google hasn't verified this app": expected for personal test apps → Advanced → Continue.
   - In "Testing" mode tokens expire after 7 days. To avoid re-authorising weekly, set Publishing status → In production. For personal use you can skip Google's verification; you'll just keep seeing the unverified-app warning when you authorise.

The scope is `gmail.readonly`: the radar can never send, delete or modify mail.

## 3. WhatsApp
**CallMeBot (free, quickest):**
1. Open https://www.callmebot.com/blog/free-api-whatsapp-messages/ and save the bot number listed there in your phone contacts.
2. WhatsApp it: `I allow callmebot to send me messages`.
3. You get an API key back. In `~/.deadline-radar/.env` set `NOTIFY_PROVIDER=callmebot`, `CALLMEBOT_PHONE=+91…`, `CALLMEBOT_APIKEY=…`.

**Twilio (more reliable):** create an account → Messaging → Try WhatsApp → join the sandbox by sending the join code from your phone. Then set `NOTIFY_PROVIDER=twilio`, SID, auth token and `WHATSAPP_TO`. Sandbox sessions need you to re-send the join code if you're inactive for 72h.

Test: `python3 SKILL_DIR/scripts/notify_whatsapp.py --test`

## 4. Schedule it
```bash
python3 SKILL_DIR/scripts/setup_launchd.py --install
```
- scan every 3h (fetch + Claude extraction + alerts)
- alerts check hourly (no Claude cost)
- digest at 08:00

The Mac must be awake; launchd catches up on missed intervals after sleep. Logs are in `~/.deadline-radar/logs/`. Remove with `--uninstall`.

The scan job runs `claude -p` non-interactively with permission only for `python3` commands, Read and Write. If scans log permission errors, run `bash SKILL_DIR/scripts/run_scan.sh` by hand once to see them.

## Troubleshooting
- `count-pending` keeps growing → extraction is failing. Check `logs/scan.log` and run the extraction loop interactively.
- No WhatsApp but the log says "Sent" → provider is still `console`, or the CallMeBot key is wrong (re-run `--test`).
- An alert never fires → `db.py list --all`. The item may be done or snoozed, or its alert key is already in the `alerts` table.
- Try a future moment: `RADAR_NOW=2026-10-20T09:00 python3 SKILL_DIR/scripts/instincts.py --dry-run`
