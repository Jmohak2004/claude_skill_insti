# Extraction guide

Goal: from each email, pull out only things the **user personally has to do or attend** before a point in time. One wrong alert at 7 AM costs the user's trust more than one missed newsletter item, so lean toward precision.

## What counts

| kind | Use for | Examples |
|---|---|---|
| `form` | Anything to fill/register via a link | Google/MS Forms, placement drive registration, elective selection, feedback forms, hackathon sign-up |
| `payment` | Money to pay by a date | Fee payment, exam fee, hostel dues, fine |
| `deadline` | Something to submit/do | Assignment, lab journal, project report, document submission, scholarship application on a portal |
| `event` | Something to attend at a time | Viva, exam, placement test, interview slot, mandatory seminar |
| `reply` | A person directly asks the user to respond/confirm | "Please confirm your availability", "send me your resume by EOD", "reply with your roll no." |

Ignore: newsletters, promotions, generic announcements with no action for the user, OTPs, receipts for things already done (but see completions), events that are optional marketing ("join our webinar!"), and emails the user sent.

**Suspicious emails**: treat these as likely scams. Signs include a demand for a "processing fee" to release money, an official-sounding sender on a free-mail domain (gmail.com, outlook.com), shortened links (bit.ly), or a "within 24 hours or cancelled" threat. Don't save them as a `payment`, because an alert would push the user to pay. Mark them processed and warn the user in your summary.

When one email contains several actions (a circular with "register by 18th, test on 22nd"), create one item per action.

## Field rules

- **title**: short and specific, max ~60 chars, naming the thing, not the email: "TCS placement registration", "DBMS assignment 3", "Sem V exam fee". Never "Important notice".
- **action**: one imperative line telling the user exactly what to do: "Fill the form with resume + CGPA", "Pay ₹1,500 on the ERP portal", "Reply to Prof. Mehta confirming slot".
- **due_at**: ISO-8601 with offset in the user's timezone (from `pending.timezone`), e.g. `2026-10-20T17:00:00+05:30`.
  - Resolve relative dates against the **email's `received_at`**, not `now`. "By Friday" in a Wednesday email is the coming Friday.
  - Date without time: use `23:59` and set `due_is_estimated: true`. For an `event` with no time, use `09:00`, estimated.
  - "EOD" = 18:00 of that day. "Tomorrow morning" = 10:00 next day. Both are estimated.
  - Indian formats: "18/10" and "18-10-2026" are day/month.
  - "Within N hours of this email" counts from `received_at`. "N working days" skips Sat/Sun; mark it estimated, since public holidays may shift it.
  - Hinglish/Hindi: "kal" means tomorrow when it's about a deadline, "parso" is the day after, and "5 baje" is 17:00 unless the context says morning.
  - No deadline at all (e.g. "please fill this form", or a reply request): `due_at: null`. The instincts nudge these after a delay.
- **priority**: `high` for placements/internships, exams, fees, anything with a penalty or "mandatory"/"last date". `low` for optional things. Otherwise `medium`.
- **form_url**: the exact form link if present (copy it whole). Its unique id is how receipt emails get matched later.
- **source_message_id**: the message `id` it came from.
- **notes**: optional; anything the user needs at action time (venue, documents to bring, eligibility).

## Updates, cancellations, completions

Check `open_items` first:
- **Extension or changed time** → an item with that existing `id` and only the changed fields, e.g. `{"id": 7, "due_at": "...", "notes": "Extended per mail on 17 Oct"}`.
- **Cancelled / postponed indefinitely** → `{"id": 7, "status": "cancelled"}`.
- **Completed**: a form receipt ("Your response has been recorded", "Thanks for filling out X"), payment receipt, "registration successful", or "we received your submission" that clearly matches an open item → add `{"item_id": 7, "evidence_message_id": "<msg id>"}` to `completions`. Only do this when the match is clear; a wrong completion silences a real deadline.
- Same thing announced again (reminder email) → don't duplicate. Update the existing item only if it adds info.
- **Original and its follow-up in the same batch** (the item has no `id` yet): fold the outcome into the new item. Use the extended `due_at` and mention the extension in `notes`. Set `"status": "cancelled"` if it was called off. If a receipt shows it's already done, still create it with `"status": "done"`, so the record exists and a later reminder email won't recreate it as open.

## Output shape

```json
{
  "items": [
    {
      "title": "Infosys placement registration",
      "kind": "form",
      "action": "Fill the registration form with resume and 10th/12th marks",
      "due_at": "2026-10-18T23:59:00+05:30",
      "due_is_estimated": true,
      "priority": "high",
      "form_url": "https://forms.gle/AbC123xyz",
      "source_message_id": "18f2a9c1",
      "notes": "Eligibility: CGPA ≥ 7, no live KTs"
    },
    { "id": 4, "due_at": "2026-10-24T17:00:00+05:30", "notes": "Extended (mail 17 Oct)" }
  ],
  "completions": [
    { "item_id": 2, "evidence_message_id": "18f2b0d7" }
  ],
  "processed_message_ids": ["18f2a9c1", "18f2aa02", "18f2b0d7"]
}
```

`processed_message_ids` must list every message in the batch, actionable or not.
