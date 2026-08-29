#!/usr/bin/env python3
"""Check the UoA mailbox (mail.uoa.gr IMAP SSL 993) for unread mail.

Credentials come from ~/.uoa-mail-creds (line 1 username, line 2 password).
Nothing is hardcoded. IMAP settings are unchanged from the working version.

  --notify   desktop notification per new unread email
  --urgent   email deadlines <=3 days away to the phone address
  --json     machine-readable output on stdout
  --summary  one-line summary
  --calendar write .ics invites and queue deadlines for Google Calendar
  --daily-summary  email the categorised 24h digest
"""

import argparse
import email
import email.utils
import html as html_mod
import imaplib
import json
import os
import re
import socket
import ssl
import sys
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import uoa_common as U  # noqa: E402

TOOL = "check-uoa-mail"
STATE = "uoa-mail-seen"
ICS_DIR = os.path.expanduser("~/.local/share/check-uoa-mail/ics")


def decode_hdr(value):
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except Exception:
        return value.strip()


def body_text(msg):
    plain, html_parts = [], []
    for part in msg.walk():
        if part.get_content_maintype() != "text" or part.get_filename():
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception:
            continue
        if not payload:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except (LookupError, UnicodeDecodeError):
            text = payload.decode("utf-8", errors="replace")
        (plain if part.get_content_type() == "text/plain" else html_parts).append(text)
    if not plain and html_parts:
        raw = re.sub(r"(?is)<(script|style).*?</\1>", " ", "\n".join(html_parts))
        raw = re.sub(r"(?s)<[^>]+>", " ", raw)
        plain.append(html_mod.unescape(raw))
    return re.sub(r"[ \t]+", " ", "\n".join(plain))


def connect_imap(cfg, user, password):
    host, port = cfg["imap"]["host"], cfg.getint("imap", "port")
    try:
        conn = imaplib.IMAP4_SSL(host, port, timeout=cfg.getint("imap", "timeout"))
    except socket.gaierror:
        U.die(TOOL, f"cannot resolve {host} — no internet or DNS is down", 2)
    except (socket.timeout, TimeoutError):
        U.die(TOOL, f"timed out connecting to {host}:{port}", 2)
    except ssl.SSLError as exc:
        U.die(TOOL, f"TLS error talking to {host}: {exc}", 2)
    except OSError as exc:
        U.die(TOOL, f"cannot reach {host}:{port}: {exc}", 2)
    try:
        conn.login(user, password)
    except imaplib.IMAP4.error as exc:
        detail = exc.args[0].decode() if exc.args and isinstance(exc.args[0], bytes) else exc
        try:
            conn.logout()
        except Exception:
            pass
        U.die(TOOL, f"IMAP login failed for {user}: {detail}", 3)
    except (socket.timeout, TimeoutError):
        U.die(TOOL, "timed out during IMAP login", 2)
    return conn


def fetch_unread(conn, cfg, since):
    mailbox = cfg["imap"]["mailbox"]
    if conn.select(mailbox, readonly=True)[0] != "OK":
        U.die(TOOL, f"cannot open mailbox {mailbox}")
    # SINCE is day-granular; search a day wide and filter on Date: below.
    search_date = (since - timedelta(days=1)).strftime("%d-%b-%Y")
    typ, data = conn.search(None, "UNSEEN", "SINCE", search_date)
    if typ != "OK":
        U.die(TOOL, "IMAP search failed")

    out = []
    for uid in data[0].split():
        typ, raw = conn.fetch(uid, "(BODY.PEEK[])")   # PEEK keeps it unread
        if typ != "OK" or not raw or not isinstance(raw[0], tuple):
            continue
        msg = email.message_from_bytes(raw[0][1])
        try:
            dt = email.utils.parsedate_to_datetime(msg.get("Date", ""))
        except (TypeError, ValueError):
            dt = None
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt < since:
                continue
        atts = []
        for part in msg.walk():
            fname = part.get_filename()
            if not fname:
                continue
            fname = decode_hdr(fname)
            if not re.search(r"\.(pdf|docx?|pptx?|xlsx?)$", fname, re.I):
                continue
            try:
                blob = part.get_payload(decode=True)
            except Exception:
                blob = None
            if blob:
                atts.append((fname, blob))

        out.append({
            "attachments": atts,
            "id": decode_hdr(msg.get("Message-ID", "")) or f"uid-{uid.decode()}",
            "source": "uoa-mail",
            "from": decode_hdr(msg.get("From", "(unknown sender)")),
            "subject": decode_hdr(msg.get("Subject", "(no subject)")),
            "date": dt.astimezone().strftime("%Y-%m-%d %H:%M") if dt else "(no date)",
            "sort_key": dt.timestamp() if dt else 0,
            "body": body_text(msg),
        })
    out.sort(key=lambda m: m["sort_key"], reverse=True)
    return out


def urgent_email(cfg, user, pw, m, dry_run):
    subject = f"🔴 UoA URGENT: {m['subject']}"
    lines = [f"From:     {m['from']}", f"Received: {m['date']}"]
    if m["due_date"]:
        lines.append(f"Deadline: {U.due_str(m)}")
    if m["keywords"]:
        lines.append(f"Matched:  {', '.join(m['keywords'])}")
    lines += ["", (m["body"][:1500].strip() or "(no body text)")]
    body_html = U.esc_html(m["body"][:1500].strip() or "(no body text)")
    html = (f'<div style="font-family:system-ui,sans-serif">'
            f'<h2 style="margin:0 0 8px">🔴 {U.esc_html(m["subject"])}</h2>'
            f'<p><b>From:</b> {U.esc_html(m["from"])}<br>'
            f'<b>Received:</b> {U.esc_html(m["date"])}<br>'
            + (f'<b>Deadline:</b> {U.esc_html(U.due_str(m))}<br>' if m["due_date"] else "")
            + f'</p><hr><pre style="white-space:pre-wrap;font-family:inherit">'
              f'{body_html}</pre></div>')
    att = []
    if m["due_date"]:
        ics = U.build_ics(m["id"], f"UoA: {m['subject']}", m["due_date"],
                          m["due_time"], f"From: {m['from']}\n\n{m['body'][:600]}")
        att.append((ics.encode("utf-8"), "text", "calendar", "uoa-deadline.ics"))
    return U.send_mail(cfg, user, pw, subject, "\n".join(lines), html,
                       attachments=att, dry_run=dry_run, tool=TOOL)


def summary_email(cfg, user, pw, messages, hours, dry_run):
    today = datetime.now().strftime("%A %d %B %Y")
    buckets = {"urgent": [], "week": [], "info": []}
    for m in messages:
        buckets[m["bucket"]].append(m)
    n = len(messages)
    subject = (f"UoA Daily Summary — {n} unread"
               + (f" · {len(buckets['urgent'])} urgent" if buckets["urgent"] else "")
               + f" ({datetime.now().strftime('%d %b')})")
    text = [f"UoA unread mail, last {hours}h — {today}", "=" * 52, ""]
    html = [f'<div style="font-family:system-ui,sans-serif;max-width:680px">'
            f'<h2 style="margin:0">UoA Daily Summary</h2>'
            f'<p style="color:#666">Last {hours}h · {U.esc_html(today)} · {n} unread</p>']
    if n == 0:
        text.append("Nothing unread.")
        html.append("<p>Nothing unread.</p>")
    for key in ("urgent", "week", "info"):
        grp = buckets[key]
        if not grp:
            continue
        head = f"{U.BUCKET_ICON[key]} {U.BUCKET_LABEL[key]} — {len(grp)}"
        text += [head, "-" * len(head)]
        html.append(f'<h3>{U.esc_html(head)}</h3><ul>')
        for m in grp:
            text.append(f"  • {m['subject']}")
            text.append(f"    {m['from']} · {m['date']}")
            if m["due_date"]:
                text.append(f"    due {U.due_str(m)}")
            html.append(f'<li><b>{U.esc_html(m["subject"])}</b><br>'
                        f'<span style="color:#666;font-size:90%">{U.esc_html(m["from"])} · '
                        f'{U.esc_html(m["date"])}</span>'
                        + (f'<br><span style="color:#c00">due {U.esc_html(U.due_str(m))}</span>'
                           if m["due_date"] else "") + "</li>")
        text.append("")
        html.append("</ul>")
    html.append("</div>")
    return U.send_mail(cfg, user, pw, subject, "\n".join(text), "".join(html),
                       dry_run=dry_run, tool=TOOL)


def summarize(messages):
    n = len(messages)
    if n == 0:
        return "No unread UoA emails"
    line = f"{n} unread UoA email{'s' if n != 1 else ''}"
    sec = sum(1 for m in messages
              if any(h in (m["from"] + " " + m["subject"]).lower()
                     for h in U.SECRETARY_HINTS))
    if sec:
        line += f", {sec} from secretary"
    urg = sum(1 for m in messages if m.get("bucket") == "urgent")
    if urg:
        line += f", 🔴 {urg} urgent"
    return line



def _mark_calendar(state, message_id, created):
    """Record on the ledger entry whether a Calendar event was made.

    Operates on the in-memory state dict — the caller persists it — because
    this checker's state file and its ledger file are one and the same.
    """
    entry = state.get("items", {}).get(message_id)
    if entry is not None:
        entry["calendar_created"] = bool(created)
        entry["calendar_event_created"] = bool(created)


def main():
    p = argparse.ArgumentParser(description="Check mail.uoa.gr for unread mail.")
    p.add_argument("--notify", action="store_true")
    p.add_argument("--urgent", "--urgent-alerts", dest="urgent", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--summary", action="store_true")
    p.add_argument("--calendar", action="store_true",
                   help="create Google Calendar events for detected deadlines")
    p.add_argument("--no-calendar", dest="calendar", action="store_false")
    p.add_argument("--save-attachments", action="store_true", default=True,
                   help="archive PDF/DOCX attachments into the Obsidian vault")
    p.add_argument("--no-attachments", dest="save_attachments",
                   action="store_false")
    p.set_defaults(calendar=True)
    p.add_argument("--daily-summary", action="store_true")
    p.add_argument("--list-deadlines", action="store_true")
    p.add_argument("--mark-synced", metavar="ID")
    p.add_argument("--test-email", action="store_true")
    p.add_argument("--hours", type=int, default=24)
    p.add_argument("--resend", action="store_true",
                   help="ignore seen-state and act on everything again")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    cfg = U.load_config()
    state = U.load_state(STATE)
    state.setdefault("notified", {})
    state.setdefault("alerted", {})
    state.setdefault("deadlines", [])

    if args.list_deadlines:
        print(json.dumps(state["deadlines"], ensure_ascii=False, indent=2))
        return 0
    if args.mark_synced:
        hit = False
        for d in state["deadlines"]:
            if d["message_id"] == args.mark_synced:
                d["calendar_synced"] = True
                hit = True
        U.save_state(STATE, state)
        print("marked" if hit else "no such queued deadline")
        return 0 if hit else 1

    user, password, smtp_pw = U.read_credentials(TOOL)

    if args.test_email:
        ok = U.send_mail(cfg, user, smtp_pw, "[ok] check-uoa-mail test",
                         "SMTP works.", dry_run=args.dry_run, tool=TOOL)
        print("test email sent" if ok else "test email FAILED")
        return 0 if ok else 4

    since = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    conn = connect_imap(cfg, user, password)
    try:
        messages = fetch_unread(conn, cfg, since)
    except imaplib.IMAP4.abort as exc:
        U.die(TOOL, f"IMAP connection aborted: {exc}", 2)
    except (socket.timeout, TimeoutError):
        U.die(TOOL, "timed out while reading mail", 2)
    except OSError as exc:
        U.die(TOOL, f"network error while reading mail: {exc}", 2)
    finally:
        for fn in (conn.close, conn.logout):
            try:
                fn()
            except Exception:
                pass

    for m in messages:
        # The message's own send date must not be mistaken for a deadline.
        try:
            own = datetime.strptime(m["date"][:10], "%Y-%m-%d").date()
        except ValueError:
            own = None
        m.update(U.classify(m["subject"], m["body"], cfg, exclude_dates=(own,)))
    now_ts = datetime.now(timezone.utc).timestamp()

    # ---- output ------------------------------------------------------------
    if args.json:
        print(json.dumps(
            {"generated": datetime.now().isoformat(timespec="seconds"),
             "window_hours": args.hours, "count": len(messages),
             "summary": summarize(messages),
             "messages": [{**{k: v for k, v in m.items()
                              if k not in ("sort_key", "attachments")},
                            "attachments": [n for n, _ in m["attachments"]]}
                          for m in messages]},
            ensure_ascii=False, indent=2))
    elif args.summary:
        print(summarize(messages))
    elif not (args.notify or args.urgent or args.calendar or args.daily_summary):
        if not messages:
            print(f"No unread UoA emails in the last {args.hours}h")
        for m in messages:
            print(f"{U.BUCKET_ICON[m['bucket']]} From:    {m['from']}")
            print(f"   Subject: {m['subject']}")
            print(f"   Date:    {m['date']}")
            if m["due_date"]:
                print(f"   Due:     {U.due_str(m)}")
            print("-" * 60)

    # ---- notify + forward every message through the hub --------------------
    # dispatch() is idempotent: it forwards once, tags the subject so Gmail
    # read state can be synced back, and re-fires the desktop alert on every
    # run while the item is still unread.
    if args.notify:
        from notify import dispatch
        state.setdefault("items", {})       # same file as STATE: share the dict
        for m in messages:
            body = m["subject"]
            if m["due_date"]:
                body += f"\ndue {U.due_str(m)}"
            dispatch("webmail", m["id"], m["from"][:80],
                     f"{body}\n{m['date']}", U.category_for(m),
                     url="", due=U.due_str(m), subject=m["subject"],
                     dry_run=args.dry_run, state=state)
            state["notified"][m["id"]] = now_ts
        U.ledger_prune(state)

    # ---- Google Calendar events (+ .ics fallback) --------------------------
    state.setdefault("calendared", {})
    if args.calendar:
        for m in messages:
            if not m["due_date"] or (not args.resend and m["id"] in state["calendared"]):
                continue
            path = ""
            try:                                   # local .ics always, as a fallback
                os.makedirs(ICS_DIR, exist_ok=True)
                slug = re.sub(r"[^A-Za-z0-9]+", "-", m["subject"])[:50].strip("-")
                path = os.path.join(ICS_DIR, f"{m['due_date']}-{slug or 'deadline'}.ics")
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(U.build_ics(m["id"], f"UoA: {m['subject']}",
                                         m["due_date"], m["due_time"],
                                         f"From: {m['from']}\n\n{m['body'][:600]}"))
            except OSError as exc:
                U.log(TOOL, "warn", f"cannot write .ics: {exc}")

            created = U.calendar_add_deadline(
                f"{m['subject']}", m["due_date"], m["due_time"],
                f"From: {m['from']}\nReceived: {m['date']}", "UoA email",
                dry_run=args.dry_run, tool=TOOL)
            if created:
                state["calendared"][m["id"]] = now_ts
            _mark_calendar(state, m["id"], created)
            state["deadlines"].append({
                "message_id": m["id"], "subject": m["subject"], "from": m["from"],
                "due_date": m["due_date"], "due_time": m["due_time"],
                "source": "uoa-mail", "ics_path": path,
                "calendar_synced": bool(created),
                "queued_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
            if not args.json:
                print(f"{'calendar event created' if created else 'queued (calendar offline)'}"
                      f": {m['subject']} → {U.due_str(m)}")

    # ---- attachments into the Obsidian vault -------------------------------
    state.setdefault("attached", {})
    if args.save_attachments:
        for m in messages:
            if not m["attachments"] or (not args.resend and m["id"] in state["attached"]):
                continue
            folder = U.find_course_folder(m["subject"], m["from"])
            saved = []
            for fname, blob in m["attachments"]:
                target = os.path.join(folder, "attachments", U.safe_filename(fname))
                try:
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    if not os.path.exists(target):
                        with open(target, "wb") as fh:
                            fh.write(blob)
                    saved.append(target)
                except OSError as exc:
                    U.log(TOOL, "warn", f"cannot save attachment {fname}: {exc}")
            if not saved:
                continue
            note = os.path.join(folder, U.safe_filename(
                f"{m['date'][:10]} {m['subject'][:60]}") + ".md")
            links = "\n".join(f"- [[{os.path.basename(t)}]]" for t in saved)
            U.write_note(note, m["subject"],
                         {"type": "uoa-email", "course": f'"{os.path.basename(folder)}"',
                          "tags": "[uoa, email, attachment]", "status": "inbox"},
                         f"# {m['subject']}\n\n"
                         f"**From:** {m['from']}  \n**Received:** {m['date']}  \n"
                         + (f"**Deadline:** {U.due_str(m)}  \n" if m["due_date"] else "")
                         + f"\n## Attachments\n{links}\n\n## Message\n\n"
                           f"{m['body'][:2000].strip()}\n")
            state["attached"][m["id"]] = now_ts
            if not args.json:
                print(f"saved {len(saved)} attachment(s) → {folder}")

    # ---- urgent alerts -----------------------------------------------------
    if args.urgent:
        for m in messages:
            if m["bucket"] != "urgent":
                continue
            if not args.resend and m["id"] in state["alerted"]:
                continue
            if urgent_email(cfg, user, smtp_pw, m, args.dry_run):
                state["alerted"][m["id"]] = now_ts
                if not args.json:
                    print(f"urgent alert sent: {m['subject']}")

    # ---- daily digest ------------------------------------------------------
    if args.daily_summary:
        if not summary_email(cfg, user, smtp_pw, messages, args.hours, args.dry_run):
            return 4
        if not args.json:
            print(f"daily summary sent ({len(messages)} unread)")

    for k in ("notified", "alerted", "calendared", "attached"):
        state[k] = U.prune_seen(state.get(k, {}))
    if getattr(args, "dry_run", False):
        U.log(TOOL, "info", "dry run: state not persisted")
    else:
        U.save_state(STATE, state)
    # ---- Part 8: anything still unread comes back next cycle --------------
    if args.notify and not getattr(args, "dry_run", False):
        try:
            from notify import renotify_unread
            renotify_unread('webmail')
        except Exception as exc:                      # never break the run
            U.log(TOOL, "warn", f"re-notify pass failed: {exc}")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
