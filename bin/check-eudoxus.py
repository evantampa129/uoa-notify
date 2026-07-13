#!/usr/bin/env python3
"""Watch Eudoxus (eudoxus.gr) for announcements, deadlines and period openings.

Announcements come from the site's own JSON feed, https://eudoxus.gr/api/News/Get,
which carries the full announcement list (title, date, HTML body) without a
login. That covers what matters to every student: declaration periods opening
and closing, distribution deadlines and extensions.

SCOPE NOTE — what this cannot do:
Eudoxus authenticates through Shibboleth/SAML (wayf.grnet.gr → idp.uoa.gr) and
its student portal is a JavaScript application, so there is no server-rendered
login form for an unattended script to post to. *Personal* data — your own
textbook declarations and pickup PINs — is therefore out of reach unless you
export browser cookies to ~/.eudoxus-cookies (Netscape format); if that file
exists the authenticated pages in EUDOXUS_PRIVATE are read too.

Everything routes through notify.py for desktop + phone + read-tracking.
"""

import argparse
import http.cookiejar
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import uoa_common as U      # noqa: E402
from notify import dispatch  # noqa: E402

TOOL = "check-eudoxus"
SOURCE = "eudoxus"
COOKIE_FILE = os.path.expanduser("~/.eudoxus-cookies")

NEWS_API = "https://eudoxus.gr/api/News/Get"
EUDOXUS_PUBLIC = ["https://eudoxus.gr/"]
EUDOXUS_PRIVATE = ["https://service.eudoxus.gr/student/"]

# Words that make a Eudoxus item worth surfacing at all.
RELEVANT = ("δηλωσ", "συγγραμ", "παραλαβ", "διανομ", "προθεσμ", "παραταση",
            "περιοδ", "ανακοιν", "βιβλι", "eudoxus", "ευδοξ")


def load_cookies(session):
    """Reuse an exported browser session if the user provided one."""
    if not os.path.exists(COOKIE_FILE):
        return False
    try:
        jar = http.cookiejar.MozillaCookieJar(COOKIE_FILE)
        jar.load(ignore_discard=True, ignore_expires=True)
        session.cookies.update(jar)
        U.log(TOOL, "info", f"loaded browser cookies from {COOKIE_FILE}")
        return True
    except (OSError, http.cookiejar.LoadError) as exc:
        U.log(TOOL, "warn", f"cannot load {COOKIE_FILE}: {exc}")
        return False


def relevant(text):
    norm = U.normalize(text)
    return any(k in norm for k in RELEVANT)


def fetch_news(cfg, limit=40):
    """Read the public announcements feed. Returns a list of dicts."""
    import hashlib
    try:
        import requests
    except ImportError:
        U.log(TOOL, "error", "python3-requests is not installed")
        return []
    try:
        r = requests.get(NEWS_API, timeout=cfg.getint("eclass", "timeout"),
                         headers={"User-Agent": U.WEB_UA,
                                  "Accept": "application/json"})
        r.raise_for_status()
        rows = r.json().get("data") or []
    except requests.exceptions.ConnectionError:
        U.log(TOOL, "warn", "cannot reach eudoxus.gr — offline or DNS failure")
        return []
    except requests.exceptions.Timeout:
        U.log(TOOL, "warn", "timed out reading the Eudoxus news feed")
        return []
    except (requests.exceptions.RequestException, ValueError) as exc:
        U.log(TOOL, "warn", f"bad response from the Eudoxus news feed: {exc}")
        return []

    from bs4 import BeautifulSoup
    out = []
    rows.sort(key=lambda r: r.get("PostDate") or "", reverse=True)
    for row in rows[:limit]:
        html = row.get("PostContent") or ""
        soup = BeautifulSoup(html, "html.parser")
        head = soup.find(["h3", "h2", "h4"])
        title = (row.get("PostTitle")
                 or (head.get_text(" ", strip=True) if head else "")
                 or soup.get_text(" ", strip=True)[:90] or "(untitled)")
        if head:
            head.extract()
        body = " ".join(soup.get_text(" ", strip=True).split())
        posted = (row.get("PostDate") or "")[:10]
        ident = str(row.get("Id") or hashlib.sha1(
            title.encode("utf-8")).hexdigest()[:16])
        out.append({"id": f"news-{ident}", "title": title.strip(),
                    "body": body, "posted": posted,
                    "url": "https://eudoxus.gr/news"})
    return out


def main():
    p = argparse.ArgumentParser(description="Watch Eudoxus for new notifications.")
    p.add_argument("--notify", action="store_true",
                   help="desktop + forwarded notification for new items")
    p.add_argument("--json", action="store_true")
    p.add_argument("--calendar", action="store_true", default=True)
    p.add_argument("--no-calendar", dest="calendar", action="store_false")
    p.add_argument("--no-forward", dest="forward", action="store_false", default=True)
    p.add_argument("--reset", action="store_true", help="re-baseline silently")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    cfg = U.load_config()
    state = U.ledger_load(SOURCE)
    state.setdefault("pages", {})

    urls = list(EUDOXUS_PUBLIC)
    private_ok = False
    try:
        import requests
        session = requests.Session()
        private_ok = load_cookies(session)
    except ImportError:
        session = None
    if private_ok:
        urls += EUDOXUS_PRIVATE

    findings, unreachable = [], []
    for url in urls:
        html = U.web_fetch(url, cfg.getint("eclass", "timeout"), TOOL)
        if html is None:
            unreachable.append(url)
            continue
        items, page_hash = U.web_items(html, url)
        prev = state["pages"].get(url, {})
        first_run = not prev
        known = set(prev.get("items", {}))

        state["pages"][url] = {
            "hash": page_hash,
            "items": {k: v["text"][:120] for k, v in items.items()},
            "checked": datetime.now(timezone.utc).isoformat(timespec="seconds")}

        if first_run or args.reset:
            U.log(TOOL, "info", f"baselined {url} ({len(items)} items)")
            continue

        for key, it in items.items():
            if key in known or not relevant(it["text"]):
                continue
            info = U.classify(it["text"], "", cfg)
            findings.append({"id": key, "source": SOURCE, "page": url,
                             "title": it["text"], "url": it["url"], **info})

    # ---- the public announcements feed ------------------------------------
    for n in fetch_news(cfg):
        if n["id"] in state["items"] and state["items"][n["id"]].get("notified"):
            continue
        if n["id"] in state["items"]:
            continue
        info = U.classify(n["title"], n["body"], cfg)
        findings.append({"id": n["id"], "source": SOURCE, "page": NEWS_API,
                         "title": n["title"], "url": n["url"],
                         "posted": n["posted"], "body": n["body"], **info})

    # First ever run: adopt the existing backlog silently instead of firing
    # forty historical announcements at the phone.
    baseline = not state["items"]
    if baseline and findings:
        for f in findings:
            U.ledger_record(state, f["id"], source=SOURCE, subject=f["title"],
                            url=f["url"], category="info",
                            notified=True, forwarded=True, read=True,
                            baselined=True)
        U.log(TOOL, "info", f"baselined {len(findings)} existing announcement(s)")
        if not args.json:
            print(f"Baselined {len(findings)} existing Eudoxus announcement(s); "
                  f"only newer ones will notify from now on.")
        findings = []

    # ---- notify + forward + calendar --------------------------------------
    for f in findings:
        category = U.category_for(f)
        entry = state["items"].get(f["id"], {})
        already = entry.get("notified") and entry.get("forwarded")
        if args.notify and not already:
            dispatch(SOURCE, f["id"], f["title"][:120],
                     body=(f.get("body") or f"Seen on {f['page']}")[:1200],
                     category=category,
                     url=f["url"], due=U.due_str(f) if f["due_date"] else "",
                     forward=args.forward, dry_run=args.dry_run, state=state)
        # Record every finding, notified or not, so it is not re-reported.
        U.ledger_record(state, f["id"], source=SOURCE, subject=f["title"],
                        url=f["url"], category=category,
                        due=f["due_date"] or "")
        if args.calendar and f["due_date"]:
            e = state["items"].get(f["id"], {})
            if not e.get("calendar_event_created"):
                ok = U.calendar_add_deadline(f"📚 {f['title'][:80]}", f["due_date"],
                                             f["due_time"], "Eudoxus announcement",
                                             f["url"], dry_run=args.dry_run, tool=TOOL)
                U.ledger_record(state, f["id"], calendar_event_created=ok)

    if args.json:
        print(json.dumps({
            "generated": datetime.now().isoformat(timespec="seconds"),
            "authenticated": private_ok, "unreachable": unreachable,
            "count": len(findings), "items": findings,
            "unread": sum(1 for v in state["items"].values() if not v.get("read"))},
            ensure_ascii=False, indent=2))
    elif not args.notify:
        if not findings:
            print("No new Eudoxus notifications"
                  + (" (public pages only — see script header)"
                     if not private_ok else ""))
        for f in findings:
            print(f"{U.BUCKET_ICON[f['bucket']]} {f['title'][:100]}")
            print(f"   {f['url']}")
            if f["due_date"]:
                print(f"   Due: {U.due_str(f)}")
            print("-" * 60)

    U.ledger_prune(state)
    if getattr(args, "dry_run", False):
        U.log(TOOL, "info", "dry run: state not persisted")
    else:
        U.ledger_save(SOURCE, state)
    if unreachable:
        U.log(TOOL, "warn", f"unreachable: {', '.join(unreachable)}")
    U.log(TOOL, "info", f"{len(findings)} new item(s); authenticated={private_ok}")
    # ---- Part 8: anything still unread comes back next cycle --------------
    if args.notify and not getattr(args, "dry_run", False):
        try:
            from notify import renotify_unread
            renotify_unread('eudoxus')
        except Exception as exc:                      # never break the run
            U.log(TOOL, "warn", f"re-notify pass failed: {exc}")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
