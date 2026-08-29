#!/usr/bin/env python3
"""Check eclass.uoa.gr for new announcements in enrolled courses.

eClass at UoA authenticates through CAS SSO (sso.uoa.gr), so this performs
the CAS flow with the same institutional credentials in ~/.uoa-mail-creds.
Announcements come from the aggregated 'my announcements' DataTables
endpoint — one request covers every enrolled course.

  --notify   desktop notification for each new announcement
  --urgent   email grade postings / near deadlines to the phone address
  --json     machine-readable output on stdout
  --hours N  window, default 24
"""

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import uoa_common as U  # noqa: E402

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError as exc:
    print(f"check-eclass: missing dependency: {exc}. "
          f"Install with: pip3 install --user requests beautifulsoup4",
          file=sys.stderr)
    sys.exit(5)

TOOL = "check-eclass"
STATE = "eclass-seen"
ANN_PATH = "/modules/announcements/myannouncements.php"
CAS_SERVICE = "/modules/auth/cas.php"
UA = "Mozilla/5.0 (X11; Linux x86_64) check-eclass/1.0"


# ------------------------------------------------------------------- login

def eclass_session(cfg, user, password):
    """Authenticate through CAS and return a logged-in requests.Session."""
    base = cfg["eclass"]["base"].rstrip("/")
    sso = cfg["eclass"]["sso"].rstrip("/")
    timeout = cfg.getint("eclass", "timeout")

    s = requests.Session()
    s.headers["User-Agent"] = UA
    try:
        r = s.get(f"{sso}/login", params={"service": base + CAS_SERVICE},
                  timeout=timeout)
        r.raise_for_status()
    except requests.exceptions.SSLError as exc:
        U.die(TOOL, f"TLS error contacting {sso}: {exc}", 2)
    except requests.exceptions.ConnectionError:
        U.die(TOOL, f"cannot reach {sso} — no internet or DNS is down", 2)
    except requests.exceptions.Timeout:
        U.die(TOOL, f"timed out contacting {sso}", 2)
    except requests.exceptions.RequestException as exc:
        U.die(TOOL, f"CAS request failed: {exc}", 2)

    soup = BeautifulSoup(r.text, "html.parser")
    form = {i.get("name"): (i.get("value") or "")
            for i in soup.find_all("input") if i.get("name")}
    if "execution" not in form:
        U.die(TOOL, "CAS login form changed — no 'execution' token found", 6)
    form.update(username=user, password=password, _eventId="submit")

    try:
        post = s.post(r.url, data=form, timeout=timeout)
        post.raise_for_status()
    except requests.exceptions.Timeout:
        U.die(TOOL, "timed out submitting CAS credentials", 2)
    except requests.exceptions.RequestException as exc:
        U.die(TOOL, f"CAS login failed: {exc}", 2)

    low = post.text.lower()
    if any(k in low for k in ("invalid credentials", "λανθασμ",
                              "authentication failed", "δεν είναι έγκυρα")):
        U.die(TOOL, f"eClass/CAS login rejected the credentials for {user}", 3)
    if "sso.uoa.gr" in post.url and "login" in post.url:
        U.die(TOOL, "still at the CAS login page — credentials likely wrong", 3)
    return s, base


# ------------------------------------------------------------- fetch/parse

def fetch_announcements(s, base, cfg, limit=100):
    """Query the DataTables endpoint that backs 'My announcements'."""
    url = base + ANN_PATH
    timeout = cfg.getint("eclass", "timeout")
    try:
        s.get(url, timeout=timeout)          # establish module session
        payload = {"draw": "1", "start": "0", "length": str(limit),
                   "search[value]": "", "search[regex]": "false"}
        for i in range(2):
            payload[f"columns[{i}][data]"] = str(i)
            payload[f"columns[{i}][searchable]"] = "true"
            payload[f"columns[{i}][orderable]"] = "false"
            payload[f"columns[{i}][search][value]"] = ""
            payload[f"columns[{i}][search][regex]"] = "false"
        r = s.post(url, data=payload, timeout=timeout,
                   headers={"X-Requested-With": "XMLHttpRequest", "Referer": url})
        r.raise_for_status()
    except requests.exceptions.Timeout:
        U.die(TOOL, "timed out fetching announcements", 2)
    except requests.exceptions.RequestException as exc:
        U.die(TOOL, f"cannot fetch announcements: {exc}", 2)

    try:
        data = r.json()
    except ValueError:
        U.die(TOOL, "announcements endpoint did not return JSON "
                    "(session expired or page layout changed)", 6)
    return data.get("aaData") or data.get("data") or []


GREEK_DATE = re.compile(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})")


def parse_row(row):
    """Turn one DataTables row into a dict."""
    cells = [str(c) for c in row] if isinstance(row, (list, tuple)) else [str(row)]
    soup = BeautifulSoup(cells[0], "html.parser")

    link_el = soup.find("a", href=True)
    title = link_el.get_text(" ", strip=True) if link_el else "(untitled)"
    href = link_el["href"] if link_el else ""
    course_code = ""
    an_id = ""
    if href:
        m = re.search(r"course=([A-Za-z0-9_]+)", href)
        course_code = m.group(1) if m else ""
        m = re.search(r"an_id=(\d+)", href)
        an_id = m.group(1) if m else ""

    small = soup.find("small")
    course_name = small.get_text(" ", strip=True) if small else ""

    if link_el:
        link_el.extract()
    if small:
        small.extract()
    body = soup.get_text(" ", strip=True)

    # Column 1 carries the post time as Greek relative text ("σήμερα - 2:40 π.μ.").
    stamp = None
    for cell in cells[1:]:
        text = BeautifulSoup(cell, "html.parser").get_text(" ", strip=True)
        stamp = U.parse_greek_stamp(text)
        if stamp:
            break
    if stamp is None and len(cells) > 1:
        U.log(TOOL, "warn",
              f"unparsed post time: {BeautifulSoup(cells[1], 'html.parser').get_text(strip=True)[:60]!r}")

    return {"id": an_id or f"{course_code}:{title[:40]}",
            "source": "eclass",
            "course_code": course_code,
            "course": course_name,
            "title": title,
            "body": body,
            "url": ("https://eclass.uoa.gr" + href) if href.startswith("/") else href,
            "posted": stamp.strftime("%Y-%m-%d %H:%M") if stamp else "",
            "posted_ts": stamp.timestamp() if stamp else 0}



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
    p = argparse.ArgumentParser(
        description="Check eclass.uoa.gr for new course announcements.")
    p.add_argument("--notify", action="store_true")
    p.add_argument("--urgent", action="store_true",
                   help="email grade postings and near deadlines")
    p.add_argument("--calendar", action="store_true",
                   help="create Google Calendar events for parsed deadlines")
    p.add_argument("--no-calendar", dest="calendar", action="store_false")
    p.set_defaults(calendar=True)
    p.add_argument("--json", action="store_true")
    p.add_argument("--hours", type=int, default=24)
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--all", action="store_true",
                   help="ignore the time window; list everything returned")
    p.add_argument("--resend", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    cfg = U.load_config()
    state = U.load_state(STATE)
    state.setdefault("notified", {})
    state.setdefault("alerted", {})

    user, password, smtp_pw = U.read_credentials(TOOL)
    s, base = eclass_session(cfg, user, password)
    rows = fetch_announcements(s, base, cfg, args.limit)

    cutoff = datetime.now() - timedelta(hours=args.hours)
    items = []
    for row in rows:
        try:
            item = parse_row(row)
        except Exception as exc:                      # one bad row must not kill the run
            U.log(TOOL, "warn", f"could not parse a row: {exc}")
            continue
        if not args.all and item["posted_ts"]:
            if datetime.fromtimestamp(item["posted_ts"]) < cutoff:
                continue
        item.update(U.classify(item["title"], item["body"], cfg))
        item["is_grade"] = bool(item["grade_keywords"])
        items.append(item)

    items.sort(key=lambda i: i["posted_ts"], reverse=True)
    now_ts = datetime.now(timezone.utc).timestamp()

    # ---- output ------------------------------------------------------------
    if args.json:
        print(json.dumps({
            "generated": datetime.now().isoformat(timespec="seconds"),
            "window_hours": None if args.all else args.hours,
            "count": len(items), "announcements": items},
            ensure_ascii=False, indent=2))
    elif not (args.notify or args.urgent):
        if not items:
            print(f"No new eClass announcements in the last {args.hours}h")
        for i in items:
            flag = "" if i["is_grade"] else U.BUCKET_ICON[i["bucket"]]
            print(f"{flag} {i['title']}")
            print(f"   Course: {i['course'] or i['course_code']}")
            print(f"   Posted: {i['posted'] or '(unknown)'}")
            if i["due_date"]:
                print(f"   Due:    {U.due_str(i)}")
            if i["url"]:
                print(f"   Link:   {i['url']}")
            print("-" * 60)

    # ---- notify + forward every item through the hub -----------------------
    if args.notify:
        from notify import dispatch
        state.setdefault("items", {})       # same file as STATE: share the dict
        for i in items:
            course = i["course"] or i["course_code"] or "eClass"
            entry = dispatch("eclass", i["id"],
                             f"{course} · {i['title']}"[:120],
                             i["body"][:400] or i["title"],
                             U.category_for(i), url=i["url"],
                             due=U.due_str(i),
                             subject=f"{course} - {i['title']}",
                             dry_run=args.dry_run, state=state)
            entry["course"] = course
            state["notified"][i["id"]] = now_ts
        U.ledger_prune(state)

    # ---- urgent email (grades, imminent deadlines) -------------------------
    if args.urgent:
        for i in items:
            if not (i["is_grade"] or i["bucket"] == "urgent"):
                continue
            if not args.resend and i["id"] in state["alerted"]:
                continue
            tag = "GRADE" if i["is_grade"] else "🔴 UoA URGENT"
            subject = f"{tag}: {i['title']}"
            text = "\n".join([
                f"Course:  {i['course'] or i['course_code']}",
                f"Posted:  {i['posted'] or '(unknown)'}",
                *( [f"Deadline: {U.due_str(i)}"] if i["due_date"] else [] ),
                f"Link:    {i['url']}", "", i["body"][:1500] or "(no text)"])
            html = (f'<div style="font-family:system-ui,sans-serif">'
                    f'<h2>{U.esc_html(tag)}: {U.esc_html(i["title"])}</h2>'
                    f'<p><b>Course:</b> {U.esc_html(i["course"] or i["course_code"])}<br>'
                    f'<b>Posted:</b> {U.esc_html(i["posted"])}<br>'
                    + (f'<b>Deadline:</b> {U.esc_html(U.due_str(i))}<br>'
                       if i["due_date"] else "")
                    + (f'<a href="{U.esc_html(i["url"])}">Open in eClass</a>'
                       if i["url"] else "")
                    + f'</p><hr><pre style="white-space:pre-wrap;font-family:inherit">'
                      f'{U.esc_html(i["body"][:1500])}</pre></div>')
            att = []
            if i["due_date"]:
                ics = U.build_ics(i["id"], f"eClass: {i['title']}", i["due_date"],
                                  i["due_time"], f"{i['course']}\n{i['url']}")
                att.append((ics.encode("utf-8"), "text", "calendar", "eclass-deadline.ics"))
            if U.send_mail(cfg, user, smtp_pw, subject, text, html,
                           attachments=att, dry_run=args.dry_run, tool=TOOL):
                state["alerted"][i["id"]] = now_ts
                if not args.json:
                    print(f"urgent alert sent: {i['title']}")

    # ---- Google Calendar (via MCP bridge; skipped cleanly when offline) ----
    state.setdefault("calendared", {})
    if args.calendar:
        for i in items:
            if not i["due_date"] or (not args.resend and i["id"] in state["calendared"]):
                continue
            if U.calendar_add_deadline(
                    f"{i['title']}", i["due_date"], i["due_time"],
                    f"{i['course'] or i['course_code']} — eClass announcement",
                    i["url"], dry_run=args.dry_run, tool=TOOL):
                state["calendared"][i["id"]] = now_ts
                _mark_calendar(state, i["id"], True)
                if not args.json:
                    print(f"calendar event created: {i['title']} → {i['due_date']}")

    state["notified"] = U.prune_seen(state["notified"])
    state["alerted"] = U.prune_seen(state["alerted"])
    state["calendared"] = U.prune_seen(state["calendared"])
    if getattr(args, "dry_run", False):
        U.log(TOOL, "info", "dry run: state not persisted")
    else:
        U.save_state(STATE, state)
    U.log(TOOL, "info", f"checked eClass: {len(items)} item(s) in window")
    # ---- Part 8: anything still unread comes back next cycle --------------
    if args.notify and not getattr(args, "dry_run", False):
        try:
            from notify import renotify_unread
            renotify_unread('eclass')
        except Exception as exc:                      # never break the run
            U.log(TOOL, "warn", f"re-notify pass failed: {exc}")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
