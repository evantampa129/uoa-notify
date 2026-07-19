#!/usr/bin/env bash
# daily-log.sh — write today's activity into the Obsidian vault (runs 10 PM).
#
# Creates DailyNotes/YYYY-MM-DD.md summarising every notification the system
# handled today, with read/unread status for each, plus Trello activity and
# links to the course MOCs involved. Purely local except for the optional
# Trello lookup, so it still works offline.
#
#   --date YYYY-MM-DD   write the log for another day
#   --dry-run           print the note instead of writing it

set -uo pipefail
TOOL_NAME="daily-log"
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/uoa-lib.sh"

DAY="$(date +%F)"; DRY=""
while [ $# -gt 0 ]; do
    case "$1" in
        --date)    DAY="${2:-}"; shift 2 ;;
        --dry-run) DRY="1"; shift ;;
        -h|--help) echo "usage: daily-log.sh [--date YYYY-MM-DD] [--dry-run]"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done
date -d "$DAY" >/dev/null 2>&1 || { echo "error: bad --date '$DAY'" >&2; exit 2; }

log INFO "building daily log for $DAY"

# Refresh read state first so the note reflects what was actually opened.
if have_network; then
    "$BIN_DIR/sync-read-status.py" --quiet >/dev/null 2>&1 \
        || log WARN "read-status sync failed; using last known state"
fi

# Trello activity is a nice-to-have; never let it block the note.
TRELLO_FILE="$(mktemp)"; trap 'rm -f "$TRELLO_FILE"' EXIT
: > "$TRELLO_FILE"
if have_network; then
    mcp_ask 150 "mcp__claude_ai_Trello__trelloSearch,mcp__claude_ai_Trello__trelloReadBoard,mcp__claude_ai_Trello__trelloReadList,mcp__claude_ai_Trello__trelloReadCard" \
"List my Trello cards that are due within the next 7 days or already overdue. \
One per line as: - DUEDATE | CARD NAME | BOARD
If none, output exactly: NONE
No preamble." > "$TRELLO_FILE" 2>/dev/null || true
fi

export DL_DAY="$DAY" DL_DRY="$DRY" DL_TRELLO="$TRELLO_FILE"

python3 - <<'PYEOF'
import os, sys
from datetime import date, datetime, timedelta
sys.path.insert(0, os.path.expanduser("~/bin"))
import uoa_common as U

TOOL = "daily-log"
day  = os.environ["DL_DAY"]
dry  = bool(os.environ.get("DL_DRY"))

def trello_lines():
    try:
        txt = open(os.environ["DL_TRELLO"], encoding="utf-8").read().strip()
    except OSError:
        return []
    if not txt or txt.upper().startswith("NONE"):
        return []
    return [l.strip(" -\t") for l in txt.splitlines()
            if l.strip() and not l.strip().upper().startswith("NONE")][:15]

# ---- gather everything this system touched today ---------------------------
today_items, all_unread = [], []
for source in U.LEDGER_FILES:
    for entry in U.ledger_activity(source, day):
        today_items.append(dict(entry, _source=source))
    for entry in U.ledger_unread(source):
        all_unread.append(dict(entry, _source=source))

today_items.sort(key=lambda e: e.get("first_seen", ""))
read_n   = sum(1 for e in today_items if e.get("read"))
total_n  = len(today_items)
unread_n = total_n - read_n

def mark(e):
    return "✅" if e.get("read") else "❌"

def line(e):
    label = U.SOURCE_LABEL.get(e["_source"], e["_source"])
    subj  = (e.get("subject") or "(no subject)").replace("\n", " ")[:110]
    url   = U.source_link(e["_source"], e.get("url", ""))
    txt   = f"- {mark(e)} **{label}** · {subj}"
    if e.get("due"):
        txt += f" — ⏰ `{e['due']}`"
    if url:
        txt += f" · [open]({url})"
    return txt

# ---- course MOCs mentioned today ------------------------------------------
mocs = []
for e in today_items:
    folder = U.find_course_folder(e.get("course", ""), e.get("subject", ""))
    if folder and folder != U.ATTACH_FALLBACK:
        name = os.path.basename(folder)
        if name not in mocs:
            mocs.append(name)

# ---- deadlines still ahead -------------------------------------------------
upcoming = []
for source in U.LEDGER_FILES:
    for e in U.ledger_load(source)["items"].values():
        due = e.get("due")
        if not due or not U.is_real(e):
            continue
        try:
            d = date.fromisoformat(str(due)[:10])
        except ValueError:
            continue
        if d >= date.fromisoformat(day):
            upcoming.append((d, dict(e, _source=source)))
upcoming.sort(key=lambda t: t[0])

# ---- compose ---------------------------------------------------------------
pretty = datetime.strptime(day, "%Y-%m-%d").strftime("%A %d %B %Y")
B = [f"# 📓 {pretty}", ""]
B.append(f"**Summary:** Read: {read_n}/{total_n} notifications "
         f"({unread_n} unread today, {len(all_unread)} unread overall)")
B.append("")

if today_items:
    B += ["## 📥 Today's notifications", ""]
    by_source = {}
    for e in today_items:
        by_source.setdefault(e["_source"], []).append(e)
    for source, group in by_source.items():
        label = U.SOURCE_LABEL.get(source, source)
        n_un = sum(1 for e in group if not e.get("read"))
        B.append(f"### {label} — {len(group)}"
                 + (f" ({n_un} unread)" if n_un else " (all read)"))
        B += [line(e) for e in group]
        B.append("")
else:
    B += ["## 📥 Today's notifications", "", "_Nothing arrived today._", ""]

if all_unread:
    B += ["## ⚠️ Still unread", ""]
    for e in all_unread[:25]:
        B.append(line(e))
    B.append("")

if upcoming:
    B += ["## ⏰ Upcoming deadlines", ""]
    for d, e in upcoming[:15]:
        left = (d - date.fromisoformat(day)).days
        word = "today" if left == 0 else ("tomorrow" if left == 1 else f"{left} days")
        B.append(f"- `{d.isoformat()}` ({word}) — "
                 f"{(e.get('subject') or '')[:90]} {mark(e)}")
    B.append("")

tl = trello_lines()
B += ["## 📋 Trello", ""]
B += ([f"- {t}" for t in tl] if tl else ["_Nothing due, or Trello unavailable._"])
B.append("")

if mocs:
    B += ["## 📚 Courses touched today", ""]
    B += [f"- [[{m}]]" for m in mocs]
    B.append("")

B += ["---", "", "#daily-log"]
body = "\n".join(B)

path = os.path.join(U.VAULT, "DailyNotes", f"{day}.md")
if dry:
    print(f"--- DRY RUN: would write {path} ---")
    print(body)
else:
    ok = U.write_note(path, day,
                      {"tags": "[daily-log, uoa]",
                       "notifications": total_n, "read": read_n,
                       "unread": unread_n},
                      body)
    if ok:
        print(f"✅ {path.replace(os.path.expanduser('~'), '~')}")
        print(f"   Read: {read_n}/{total_n} notifications ({unread_n} unread)")
        U.log(TOOL, "info", f"wrote daily log {day}: "
                            f"{read_n}/{total_n} read, {unread_n} unread")
    else:
        print(f"❌ could not write {path}")
        U.log(TOOL, "error", f"failed writing daily log {path}")
        sys.exit(1)
PYEOF
