#!/usr/bin/env python3
"""Watch di.uoa.gr (and any extra URLs) for new announcements.

Rather than only hashing the page, this extracts the individual link items
and diffs them against what was seen before, so a change is reported as
"these 2 announcements are new" instead of an opaque "page changed".

Watched by default:
  https://www.di.uoa.gr
  https://www.di.uoa.gr/studies/undergraduate
Extra URLs: one per line in ~/.watch-urls (optional; # comments allowed).
State:      ~/.local/state/web-hashes.json
"""

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import uoa_common as U  # noqa: E402

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError as exc:
    print(f"check-department: missing dependency: {exc}. "
          f"Install with: pip3 install --user requests beautifulsoup4",
          file=sys.stderr)
    sys.exit(5)

TOOL = "check-department"
STATE = "web-hashes"
WATCH_FILE = os.path.expanduser("~/.watch-urls")
DEFAULT_URLS = ["https://www.di.uoa.gr",
                "https://www.di.uoa.gr/studies/undergraduate"]
UA = "Mozilla/5.0 (X11; Linux x86_64) check-department/1.0"
NOISE = re.compile(r"(cookie|σύνδεση|login|menu|μενού|search|αναζήτηση)", re.I)


def watch_urls():
    urls = list(DEFAULT_URLS)
    try:
        with open(WATCH_FILE, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and line not in urls:
                    urls.append(line)
    except FileNotFoundError:
        pass
    except OSError as exc:
        U.log(TOOL, "warn", f"cannot read {WATCH_FILE}: {exc}")
    return urls


def fetch(url, timeout=30):
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": UA})
        r.raise_for_status()
        return r.text
    except requests.exceptions.SSLError as exc:
        U.log(TOOL, "warn", f"TLS error for {url}: {exc}")
    except requests.exceptions.ConnectionError:
        U.log(TOOL, "warn", f"cannot reach {url} — offline or DNS failure")
    except requests.exceptions.Timeout:
        U.log(TOOL, "warn", f"timed out fetching {url}")
    except requests.exceptions.RequestException as exc:
        U.log(TOOL, "warn", f"failed fetching {url}: {exc}")
    return None


def extract_items(html, base_url):
    """Meaningful link items on the page, plus a hash of the whole text."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()

    items = {}
    for a in soup.find_all("a", href=True):
        text = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
        if len(text) < 25 or NOISE.search(text):
            continue
        href = a["href"]
        if href.startswith("/"):
            href = base_url.rstrip("/") + href
        elif not href.startswith("http"):
            continue
        key = hashlib.sha1(f"{text}|{href}".encode("utf-8")).hexdigest()[:16]
        items[key] = {"text": text[:300], "url": href}

    body = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    return items, hashlib.sha256(body.encode("utf-8")).hexdigest()



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
    p = argparse.ArgumentParser(description="Watch di.uoa.gr for new announcements.")
    p.add_argument("--notify", action="store_true")
    p.add_argument("--email", action="store_true", help="email the changes")
    p.add_argument("--json", action="store_true")
    p.add_argument("--calendar", action="store_true", default=True)
    p.add_argument("--no-calendar", dest="calendar", action="store_false")
    p.add_argument("--reset", action="store_true",
                   help="re-baseline every page without reporting changes")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    cfg = U.load_config()
    state = U.load_state(STATE)
    state.setdefault("pages", {})
    state.setdefault("calendared", {})

    findings, unreachable = [], []
    for url in watch_urls():
        html = fetch(url, cfg.getint("eclass", "timeout"))
        if html is None:
            unreachable.append(url)
            continue
        items, page_hash = extract_items(html, url)
        prev = state["pages"].get(url, {})
        known = set(prev.get("items", {}))
        first_run = not prev

        new_keys = [k for k in items if k not in known]
        state["pages"][url] = {
            "hash": page_hash,
            "items": {k: v["text"][:120] for k, v in items.items()},
            "checked": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        if first_run or args.reset:
            U.log(TOOL, "info", f"baselined {url} ({len(items)} items)")
            continue
        if prev.get("hash") == page_hash and not new_keys:
            continue
        for k in new_keys:
            it = items[k]
            info = U.classify(it["text"], "", cfg)
            findings.append({"id": k, "source": "di.uoa.gr", "page": url,
                             "title": it["text"], "url": it["url"], **info})

    now_ts = datetime.now(timezone.utc).timestamp()

    if args.json:
        print(json.dumps({"generated": datetime.now().isoformat(timespec="seconds"),
                          "unreachable": unreachable, "count": len(findings),
                          "changes": findings}, ensure_ascii=False, indent=2))
    elif not (args.notify or args.email):
        if not findings:
            print("No new department announcements"
                  + (f" ({len(unreachable)} page(s) unreachable)" if unreachable else ""))
        for f in findings:
            print(f"{U.BUCKET_ICON[f['bucket']]} {f['title']}")
            print(f"   {f['url']}")
            if f["due_date"]:
                print(f"   Due: {U.due_str(f)}")
            print("-" * 60)

    # Every change goes through the hub: desktop alert that opens the exact
    # page that changed, plus a tagged forward for read-tracking.
    if args.notify:
        from notify import dispatch
        state.setdefault("items", {})       # same file as STATE: share the dict
        for f in findings:
            dispatch("department", f["id"], f["title"][:120],
                     f"{f['title']}\n{f['url']}", U.category_for(f),
                     url=f["url"], due=U.due_str(f), subject=f["title"],
                     dry_run=args.dry_run, state=state)
        U.ledger_prune(state)

    if args.email and findings:
        user, _, smtp_pw = U.read_credentials(TOOL)
        lines, html = [], ['<div style="font-family:system-ui,sans-serif">'
                           '<h2>🏛 Department updates</h2><ul>']
        for f in findings:
            lines.append(f"• {f['title']}\n  {f['url']}"
                         + (f"\n  ⏰ due {U.due_str(f)}" if f["due_date"] else ""))
            html.append(f'<li><b>{U.esc_html(f["title"])}</b><br>'
                        f'<a href="{U.esc_html(f["url"])}">{U.esc_html(f["url"])}</a>'
                        + (f'<br>⏰ due {U.esc_html(U.due_str(f))}' if f["due_date"] else "")
                        + '</li>')
        html.append("</ul></div>")
        U.send_mail(cfg, user, smtp_pw,
                    f"🏛 di.uoa.gr — {len(findings)} new announcement(s)",
                    "\n\n".join(lines), "".join(html),
                    dry_run=args.dry_run, tool=TOOL)

    if args.calendar:
        for f in findings:
            if not f["due_date"] or f["id"] in state["calendared"]:
                continue
            if U.calendar_add_deadline(f"🏛 {f['title'][:80]}", f["due_date"],
                                       f["due_time"], f"Department announcement",
                                       f["url"], dry_run=args.dry_run, tool=TOOL):
                state["calendared"][f["id"]] = now_ts
                _mark_calendar(state, f["id"], True)

    state["calendared"] = U.prune_seen(state["calendared"])
    if getattr(args, "dry_run", False):
        U.log(TOOL, "info", "dry run: state not persisted")
    else:
        U.save_state(STATE, state)
    if unreachable:
        U.log(TOOL, "warn", f"unreachable: {', '.join(unreachable)}")
    U.log(TOOL, "info", f"{len(findings)} new item(s)")
    # ---- Part 8: anything still unread comes back next cycle --------------
    if args.notify and not getattr(args, "dry_run", False):
        try:
            from notify import renotify_unread
            renotify_unread('department')
        except Exception as exc:                      # never break the run
            U.log(TOOL, "warn", f"re-notify pass failed: {exc}")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
