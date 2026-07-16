#!/usr/bin/env python3
"""Find university-related mail in Gmail, categorise it and act on it.

Gmail is reached through the Gmail MCP connector via a headless `claude -p`
call, because a cron job has no MCP client of its own. If the CLI or the
network is unavailable the script logs it and exits cleanly — the local
checkers keep working without it.
"""

import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import uoa_common as U      # noqa: E402
from notify import dispatch  # noqa: E402

TOOL = "check-gmail-university"
SOURCE = "gmail"

SEARCH_PROMPT = """Search my Gmail for UNREAD messages received in the last {days} days
that are university-related. Treat a message as university-related if the sender
address contains any of: uoa.gr, di.uoa.gr, eclass.uoa.gr, eudoxus.gr, eudoxus;
OR the sender name or subject contains any of: καθηγητ, γραμματεία, γραμματεια,
professor, secretary, διδάσκ, διδασκ, university, πανεπιστήμιο.

EXCLUDE this system's own forwarded notifications: skip any message whose
subject contains a tag in square brackets of the form [UOA-MSG-...],
[ECL-MSG-...], [EDX-MSG-...], [DPT-MSG-...] or [GML-MSG-...]. Add
-subject:"MSG-" to the search to filter them out at the source.

Return a JSON array. One object per message, with exactly these keys:
  "id"       - the Gmail message id
  "from"     - sender, "Name <address>"
  "subject"  - the subject line
  "date"     - ISO date of receipt, YYYY-MM-DD
  "snippet"  - first ~300 characters of the body
  "attachments" - array of attachment filenames (empty array if none)
If there are no matching messages return exactly: []"""


def save_attachments_to_drive(msg, dry_run=False):
    """Ask Drive to file this message's attachments under University/<sender>."""
    sender = (msg.get("from") or "unknown").split("<")[0].strip() or "unknown"
    names = ", ".join(msg.get("attachments") or [])
    if not names:
        return False
    if dry_run:
        print(f"--- DRY RUN drive: would file {names} under University/{sender} ---")
        return True
    prompt = (
        f"In Google Drive, make sure a folder path 'University/{sender}' exists "
        f"(create the folders if missing). Then copy the attachments "
        f"({names}) from Gmail message id {msg['id']} into that folder. "
        f"Reply with the single word DONE when finished, or CANNOT if the "
        f"attachments cannot be copied.")
    out = U.mcp_ask(prompt, U.DRIVE_TOOLS + U.GMAIL_READ_TOOLS, tool=TOOL)
    ok = bool(out) and "DONE" in out.upper()
    U.log(TOOL, "info" if ok else "warn",
          f"drive filing for '{msg.get('subject','')[:50]}': {(out or 'no reply')[:120]}")
    return ok


def main():
    p = argparse.ArgumentParser(description="Check Gmail for university mail.")
    p.add_argument("--notify", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--days", type=int, default=3)
    p.add_argument("--calendar", action="store_true", default=True)
    p.add_argument("--no-calendar", dest="calendar", action="store_false")
    p.add_argument("--drive", action="store_true", default=True,
                   help="file attachments into Google Drive")
    p.add_argument("--no-drive", dest="drive", action="store_false")
    p.add_argument("--no-forward", dest="forward", action="store_false", default=True)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    cfg = U.load_config()
    state = U.ledger_load(SOURCE)

    messages = U.mcp_json(SEARCH_PROMPT.format(days=args.days),
                          U.GMAIL_READ_TOOLS, tool=TOOL, default=None)
    if messages is None:
        U.log(TOOL, "warn", "Gmail unavailable this run; nothing to do")
        if args.json:
            print(json.dumps({"generated": datetime.now().isoformat(timespec="seconds"),
                              "available": False, "count": 0, "messages": [],
                              "unread": sum(1 for v in state["items"].values()
                                            if not v.get("read"))},
                             ensure_ascii=False, indent=2))
        return 0
    if not isinstance(messages, list):
        U.log(TOOL, "warn", "Gmail returned an unexpected shape; skipping")
        return 0

    out = []
    skipped_own = 0
    for m in messages:
        if not isinstance(m, dict) or not m.get("id"):
            continue
        # Skip this system's own forwards. They arrive in Gmail from the
        # @uoa.gr address and would otherwise be picked up as fresh
        # university mail, re-notified and re-calendared — an endless echo of
        # our own output. The subject tag identifies them exactly.
        if U.tag_in(m.get("subject", "")):
            skipped_own += 1
            continue
        mid = f"gmail-{m['id']}"
        info = U.classify(m.get("subject", ""), m.get("snippet", ""), cfg)
        item = {"id": mid, "source": SOURCE, "from": m.get("from", ""),
                "subject": m.get("subject", "(no subject)"),
                "date": m.get("date", ""), "snippet": m.get("snippet", ""),
                "attachments": m.get("attachments") or [], **info}
        out.append(item)

        known = state["items"].get(mid, {})
        category = U.category_for(item)

        if args.notify and not known.get("forwarded"):
            dispatch(SOURCE, mid, item["subject"][:120],
                     body=f"From: {item['from']}\n\n{item['snippet'][:800]}",
                     category=category, due=U.due_str(item) if item["due_date"] else "",
                     forward=args.forward, dry_run=args.dry_run, state=state)
        else:
            U.ledger_record(state, mid, source=SOURCE, subject=item["subject"],
                            category=category, date=item["date"],
                            due=item["due_date"] or "")

        if args.calendar and item["due_date"]:
            if not state["items"].get(mid, {}).get("calendar_event_created"):
                ok = U.calendar_add_deadline(
                    f"📧 {item['subject'][:80]}", item["due_date"], item["due_time"],
                    f"Gmail — from {item['from']}", "Gmail university mail",
                    dry_run=args.dry_run, tool=TOOL)
                U.ledger_record(state, mid, calendar_event_created=ok)

        if args.drive and item["attachments"]:
            if not state["items"].get(mid, {}).get("drive_saved"):
                ok = save_attachments_to_drive(m, dry_run=args.dry_run)
                U.ledger_record(state, mid, drive_saved=ok)

    if args.json:
        print(json.dumps({"generated": datetime.now().isoformat(timespec="seconds"),
                          "available": True, "count": len(out), "messages": out,
                          "unread": sum(1 for v in state["items"].values()
                                        if not v.get("read"))},
                         ensure_ascii=False, indent=2))
    elif not args.notify:
        if not out:
            print("No university-related unread Gmail")
        for i in out:
            print(f"{U.BUCKET_ICON[i['bucket']]} {i['subject']}")
            print(f"   {i['from']}  ·  {i['date']}")
            if i["due_date"]:
                print(f"   Due: {U.due_str(i)}")
            if i["attachments"]:
                print(f"   📎 {', '.join(i['attachments'])}")
            print("-" * 60)

    U.ledger_prune(state)
    if getattr(args, "dry_run", False):
        U.log(TOOL, "info", "dry run: state not persisted")
    else:
        U.ledger_save(SOURCE, state)
    U.log(TOOL, "info", f"{len(out)} university message(s) in Gmail"
                        + (f"; skipped {skipped_own} of our own forwards"
                           if skipped_own else ""))
    # ---- Part 8: anything still unread comes back next cycle --------------
    if args.notify and not getattr(args, "dry_run", False):
        try:
            from notify import renotify_unread
            renotify_unread('gmail')
        except Exception as exc:                      # never break the run
            U.log(TOOL, "warn", f"re-notify pass failed: {exc}")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
