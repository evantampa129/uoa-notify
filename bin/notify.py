#!/usr/bin/env python3
"""Central notification hub — every checker routes notifications through here.

One call does all of this and records it for read-tracking:
  1. desktop notification via notify-send, clickable → opens the source page
     (re-fired on every cron run until the item is actually read)
  2. forward once to the phone address, with the right emoji subject prefix
     and a stable [XXX-MSG-xxxxxxxx] tag that sync-read-status.py matches on
  3. append to ~/.local/log/notifications.log
  4. record in the per-source ledger: notified / forwarded / read /
     gmail_msg_id / source_url / calendar_created

Persistence (Part 8): the desktop notification repeats every cycle while the
item is unread. The forwarded email is sent exactly once — it stays unread in
Gmail, which is the cross-device signal, and re-sending would spam the inbox
and break the one-tag-one-message mapping sync relies on.

CLI:
  notify.py --source eclass --message-id 664202 --title "..." --body "..."
            --category grade --url https://... [--due 2026-09-01]
            [--no-forward] [--no-desktop] [--dry-run]

Importable:
  from notify import dispatch
"""

import argparse
import copy
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import uoa_common as U  # noqa: E402

TOOL = "notify"

# category -> (notify-send urgency, severity dot)
#
# The dot is the priority signal, not decoration: it is the part of the title
# that still reads at a glance in a notification banner or a truncated phone
# subject line. Categories that carry no urgency of their own borrow the dot
# of the urgency they map to.
URGENCY = {"urgent": ("critical", "🔴"), "grade": ("critical", "🔴"),
           "deadline": ("normal", "🟡"), "file": ("normal", "🟡"),
           "info": ("low", "🟢")}

# --urgency urgent/normal/info -> internal category
URGENCY_ALIAS = {"urgent": "urgent", "normal": "deadline", "info": "info"}


def dispatch(source, message_id, title, body="", category="info", url="",
             due="", subject=None, desktop=True, forward=True,
             calendar_created=None, dry_run=False, state=None):
    """Send one notification everywhere and record it. Returns the entry.

    Pass `state` to batch many notifications into one ledger write; otherwise
    the ledger is loaded and saved per call.
    """
    own_state = state is None
    if own_state:
        state = U.ledger_load(source)

    # A dry run must not leave a trace. The caller may persist `state` itself,
    # so snapshot this entry and put it back at the end — otherwise a rehearsal
    # would mark the item "forwarded" and the real run would skip sending it.
    had_entry = message_id in state["items"]
    snapshot = copy.deepcopy(state["items"].get(message_id)) if dry_run else None

    cfg = U.load_config()
    urgency, dot = URGENCY.get(category, URGENCY["info"])
    subject = subject or title
    tag = U.make_tag(source, message_id)
    prefix = U.forward_prefix(source, category)
    source_url = U.source_link(source, url)

    # Tag must survive the length clamp — trim the subject, never the tag.
    suffix = f" [{tag}]"
    fwd_subject = f"{prefix} {subject}"
    if len(fwd_subject) + len(suffix) > 230:
        fwd_subject = fwd_subject[:230 - len(suffix)].rstrip()
    fwd_subject += suffix

    entry = U.ledger_record(state, message_id, source=source, subject=subject,
                            category=category, url=url, due=due,
                            tag=tag, source_url=source_url,
                            date=datetime.now().strftime("%Y-%m-%d %H:%M"),
                            forward_subject=fwd_subject)
    if calendar_created is not None:
        entry["calendar_created"] = bool(calendar_created)
        entry["calendar_event_created"] = bool(calendar_created)  # legacy key

    # 1. desktop — repeats every cycle until the item is read ----------------
    if desktop and not entry.get("read"):
        label = U.SOURCE_LABEL.get(source, source)
        text = body or ""
        if due:
            text = f"Due: {due}\n{text}"
        repeat = int(entry.get("notify_count") or 0)
        head = f"{dot} {label} · {title}"
        if repeat:
            head = f"{dot} {label} · (still unread ×{repeat}) {title}"
        if dry_run:
            print(f"--- DRY RUN desktop: {head[:160]} -> {source_url} ---")
            entry["notified"] = True
        elif U.notify_send(head[:160], text[:400], urgency, TOOL,
                           url=source_url, tag=tag, source=source,
                           message_id=message_id):
            entry["notified"] = True
            entry["notify_count"] = repeat + 1
            entry["last_notified"] = datetime.now(timezone.utc).isoformat(
                timespec="seconds")

    # 2. forward — exactly once ---------------------------------------------
    if forward and not entry.get("forwarded"):
        text_body = "\n".join(filter(None, [
            f"Source:   {U.SOURCE_LABEL.get(source, source)}",
            f"Subject:  {subject}",
            f"Deadline: {due}" if due else "",
            f"Link:     {source_url}" if source_url else "",
            "", body or "(no additional text)",
            "", f"[{tag}]"]))
        html = (f'<div style="font-family:system-ui,sans-serif">'
                f'<h2 style="margin:0 0 6px">{dot} {U.esc_html(subject)}</h2>'
                f'<p style="color:#666;margin:0 0 10px">'
                f'{U.esc_html(U.SOURCE_LABEL.get(source, source))}'
                + (f' · due <b>{U.esc_html(due)}</b>' if due else "") + '</p>'
                + (f'<p><a href="{U.esc_html(source_url)}">Open original</a></p>'
                   if source_url else "")
                + f'<hr><pre style="white-space:pre-wrap;font-family:inherit">'
                  f'{U.esc_html((body or "")[:2000])}</pre>'
                  f'<p style="color:#bbb;font-size:80%">[{U.esc_html(tag)}]</p>'
                  f'</div>')
        user, _, smtp_pw = U.read_credentials(TOOL)
        if U.send_mail(cfg, user, smtp_pw, fwd_subject, text_body, html,
                       dry_run=dry_run, tool=TOOL):
            entry["forwarded"] = True
            entry["forwarded_at"] = datetime.now(timezone.utc).isoformat(
                timespec="seconds")

    U.log(TOOL, "info",
          f"{source}/{category} '{subject[:60]}' [{tag}] "
          f"notified={entry.get('notified')} forwarded={entry.get('forwarded')} "
          f"repeat={entry.get('notify_count', 0)}")

    if dry_run:                       # restore: rehearsals change nothing
        if had_entry:
            state["items"][message_id] = snapshot
        else:
            state["items"].pop(message_id, None)
        return entry

    if own_state:
        U.ledger_prune(state)
        U.ledger_save(source, state)
    return entry


def _just_notified(entry, seconds=180):
    """True if this item was alerted moments ago.

    The checker dispatches items in the current window and then calls
    renotify_unread(); without this, anything in both sets would pop up twice
    in the same run.
    """
    stamp = entry.get("last_notified")
    if not stamp:
        return False
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds() < seconds


def renotify_unread(source, dry_run=False, limit=12):
    """Part 8: re-fire the desktop alert for everything still unread.

    Called by the checkers at the end of each run, so an ignored notification
    comes back on the next cron cycle instead of vanishing.
    """
    state = U.ledger_load(source)
    pending = [e for e in state["items"].values()
               if e.get("forwarded") and not e.get("read")
               and not _just_notified(e)]
    pending.sort(key=lambda e: e.get("first_seen", ""), reverse=True)
    again = 0
    for entry in pending[:limit]:
        category = entry.get("category", "info")
        urgency, dot = URGENCY.get(category, URGENCY["info"])
        label = U.SOURCE_LABEL.get(source, source)
        repeat = int(entry.get("notify_count") or 0)
        subject = entry.get("subject", "(no subject)")
        head = f"{dot} {label} · (unread) {subject}"[:160]
        body = ""
        if entry.get("due"):
            body = f"Due: {entry['due']}"
        if dry_run:
            print(f"--- DRY RUN re-notify: {head} ---")
            again += 1
            continue
        if U.notify_send(head, body, urgency, TOOL,
                         url=U.source_link(source, entry.get("url", "")),
                         tag=entry.get("tag", ""), source=source,
                         message_id=entry.get("message_id", "")):
            entry["notify_count"] = repeat + 1
            entry["last_notified"] = datetime.now(timezone.utc).isoformat(
                timespec="seconds")
            again += 1
    if again and not dry_run:
        U.ledger_save(source, state)
    if again:
        U.log(TOOL, "info", f"re-notified {again} unread {source} item(s)")
    return again


def mark_read(source=None, message_id=None, tag=None, dry_run=False):
    """Record that the user opened an item, and make that verdict stick.

    Called by notify-open.sh the moment a notification is activated. Two
    things have to happen, in this order and for different reasons:

      1. Set `read` in the local ledger. This is what stops the desktop
         alert being re-fired on the next cron cycle, and it takes effect
         immediately, with no network involved.
      2. Push the same verdict to Gmail by removing the UNREAD label. Gmail
         is the declared source of truth for read state across devices, so
         without this step the next sync-read-status.py run would see the
         message still unread, flip the ledger back, reset notify_count and
         start notifying all over again — which is precisely the bug where
         "I clicked it and it came back".

    When the push cannot be made (offline, no assistant CLI configured), the
    entry is left flagged `read_push_pending` so sync retries it instead of
    treating the local read as stale. Returns True if anything was recorded.
    """
    if tag and not (source and message_id):
        source, state, entry = U.ledger_find_by_tag(tag)
        if entry is None:
            U.log(TOOL, "warn", f"no ledger entry for tag {tag}")
            return False
    else:
        if not (source and message_id):
            U.log(TOOL, "warn", "mark_read needs --tag, or --source and "
                                "--message-id")
            return False
        state = U.ledger_load(source)
        entry = state["items"].get(message_id)
        if entry is None:
            U.log(TOOL, "warn", f"no ledger entry for {source}/{message_id}")
            return False

    if dry_run:
        print(f"--- DRY RUN mark-read: {source}/{entry.get('tag')} ---")
        return True

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    entry["read"] = True
    entry["opened_at"] = now
    entry["read_synced_at"] = datetime.now().isoformat(timespec="seconds")

    # Attempt the push straight away; flag it pending either way so a failure
    # is retried rather than silently lost.
    entry["read_push_pending"] = True
    U.ledger_save(source, state)

    msg_id = entry.get("gmail_msg_id")
    if msg_id and U.gmail_mark_read([msg_id], tool=TOOL):
        entry["read_push_pending"] = False
        entry["read_pushed_at"] = now
        U.ledger_save(source, state)
        U.log(TOOL, "info", f"opened and marked read: {entry.get('tag')}")
    else:
        U.log(TOOL, "info",
              f"opened and marked read locally: {entry.get('tag')} "
              f"(Gmail push pending)")
    return True


def main():
    p = argparse.ArgumentParser(description="Central UoA notification hub.")
    p.add_argument("--source",
                   choices=["webmail", "eclass", "eudoxus", "department", "gmail"])
    p.add_argument("--message-id", help="unique id of the item")
    p.add_argument("--title", help="notification title")
    p.add_argument("--body", default="")
    p.add_argument("--subject", help="original subject (defaults to --title)")
    p.add_argument("--category", default="info",
                   choices=["urgent", "deadline", "grade", "file", "info"])
    p.add_argument("--urgency", help="alias for --category (urgent/normal/info)")
    p.add_argument("--url", default="")
    p.add_argument("--due", default="")
    p.add_argument("--renotify-unread", action="store_true",
                   help="re-fire desktop alerts for unread items and exit")
    p.add_argument("--mark-read", action="store_true",
                   help="record that an item was opened, and push that to "
                        "Gmail (used by notify-open.sh)")
    p.add_argument("--tag", help="[XXX-MSG-xxxxxxxx] tag, for --mark-read")
    p.add_argument("--no-forward", dest="forward", action="store_false")
    p.add_argument("--no-desktop", dest="desktop", action="store_false")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()

    if a.mark_read:
        ok = mark_read(a.source, a.message_id, a.tag, dry_run=a.dry_run)
        print(f"marked_read={ok}")
        return 0 if ok else 1

    if not a.source:
        p.error("--source is required")

    if a.renotify_unread:
        n = renotify_unread(a.source, dry_run=a.dry_run)
        print(f"re-notified={n}")
        return 0

    if not a.message_id or not a.title:
        p.error("--message-id and --title are required "
                "(unless --renotify-unread)")

    category = URGENCY_ALIAS.get(a.urgency, a.urgency) if a.urgency else a.category
    entry = dispatch(a.source, a.message_id, a.title, a.body, category,
                     a.url, a.due, a.subject, a.desktop, a.forward,
                     dry_run=a.dry_run)
    print(f"tag={entry.get('tag')} notified={entry.get('notified')} "
          f"forwarded={entry.get('forwarded')}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
