#!/usr/bin/env bash
# Morning brief — every day, Monday to Sunday, 7:30 AM.
#
# The point of this email is the UNREAD section: anything forwarded that has
# not been opened on any device, so nothing is ever quietly missed. Gmail
# decides what counts as read, so it is synced first.
#
# Any source that fails is reported as unavailable; the brief still goes out.

set -uo pipefail
TOOL_NAME="morning-brief"
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/uoa-lib.sh"

DRY=""
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY="--dry-run"; shift ;;
        -h|--help)
            echo "usage: morning-brief.sh [--dry-run]"
            echo "  Emails the daily brief. --dry-run composes it and prints"
            echo "  it instead of sending."
            exit 0 ;;
        *) echo "unknown option: $1" >&2
           echo "usage: morning-brief.sh [--dry-run]" >&2; exit 2 ;;
    esac
done
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
log INFO "starting morning brief"

# ---- 1. read status first: Gmail is the source of truth -------------------
if have_network; then
    "$BIN_DIR/sync-read-status.py" --json > "$WORK/sync.json" 2>/dev/null \
        || { log WARN "read-status sync failed"; echo '{}' > "$WORK/sync.json"; }
else
    log WARN "offline; brief uses last known read state"
    echo '{}' > "$WORK/sync.json"
fi

# ---- 2. every checker, read-only (no calendar/attachment side effects) -----
run_json check-uoa-mail.py         --json --hours 24 --no-calendar --no-attachments > "$WORK/mail.json"
run_json check-eclass.py           --json --hours 24 --no-calendar                  > "$WORK/eclass.json"
run_json check-department.py       --json --no-calendar                             > "$WORK/dept.json"
run_json check-eudoxus.py          --json --no-calendar                             > "$WORK/eudoxus.json"
run_json check-gmail-university.py --json --no-calendar --no-drive                             > "$WORK/gmailuni.json"

# ---- 3. vault notes awaiting review (local, always available) --------------
: > "$WORK/review.txt"
if [ -d "$VAULT" ]; then
    grep -rl --include='*.md' '#status/review' "$VAULT" 2>/dev/null \
        | head -25 | sed "s|$VAULT/||" > "$WORK/review.txt" || true
else
    log WARN "vault not found at $VAULT"
fi

# ---- 4. MCP-backed sources ------------------------------------------------
: > "$WORK/calendar.txt"; : > "$WORK/trello.txt"
if have_network; then
    mcp_ask 200 "mcp__claude_ai_Google_Calendar__list_events,mcp__claude_ai_Google_Calendar__list_calendars" \
"List my Google Calendar events for today and tomorrow across all my calendars. \
One per line as: - DAY TIME | TITLE
where DAY is Today or Tomorrow. If none, output exactly: NONE
No preamble." > "$WORK/calendar.txt" 2>/dev/null || true

    mcp_ask 200 "mcp__claude_ai_Trello__trelloSearch,mcp__claude_ai_Trello__trelloReadBoard,mcp__claude_ai_Trello__trelloReadCard,mcp__claude_ai_Trello__trelloReadList" \
"List my Trello cards with a due date within the next 7 days, or already overdue. \
One per line as: - DUEDATE | CARD NAME | BOARD
If none, output exactly: NONE
No preamble." > "$WORK/trello.txt" 2>/dev/null || true
else
    log WARN "no network; skipping Calendar/Trello sections"
fi

export MB_WORK="$WORK"

python3 - <<'PYEOF'
import json, os, sys
from datetime import date, datetime, timedelta
sys.path.insert(0, os.path.expanduser("~/bin"))
import uoa_common as U

TOOL = "morning-brief"
work = os.environ["MB_WORK"]
today = date.today()

def jload(name):
    try:
        with open(os.path.join(work, name), encoding="utf-8") as fh:
            return json.load(fh) or {}
    except Exception:
        return {}

def tload(name):
    try:
        txt = open(os.path.join(work, name), encoding="utf-8").read().strip()
    except OSError:
        return []
    if not txt or txt.upper().startswith("NONE"):
        return []
    return [l.strip(" -\t") for l in txt.splitlines()
            if l.strip() and not l.strip().upper().startswith("NONE")][:20]

sync = jload("sync.json")
cal, trello = tload("calendar.txt"), tload("trello.txt")
try:
    review = [l for l in open(os.path.join(work, "review.txt"),
                              encoding="utf-8").read().splitlines() if l]
except OSError:
    review = []

def mark(e):
    return "✅" if e.get("read") else "❌"

def link(e, source):
    return U.source_link(source, e.get("url", ""))

# ---- unread, straight from the ledgers ------------------------------------
unread_by_source, total_unread = {}, 0
for source in U.LEDGER_FILES:
    items = U.ledger_unread(source)
    if items:
        unread_by_source[source] = items
        total_unread += len(items)

# ---- last 24h, and every known deadline -----------------------------------
cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
recent_by_source, deadlines = {}, []
for source in U.LEDGER_FILES:
    fresh = [e for e in U.ledger_activity(source)
             if (e.get("first_seen") or "") >= cutoff]
    if fresh:
        recent_by_source[source] = fresh
    for e in U.ledger_load(source)["items"].values():
        if not U.is_real(e) or not e.get("due"):
            continue
        try:
            d = date.fromisoformat(str(e["due"])[:10])
        except ValueError:
            continue
        if d >= today:
            deadlines.append((d, (d - today).days, dict(e, _source=source)))
deadlines.sort(key=lambda t: t[0])

urgent = [t for t in deadlines if t[1] <= 3]
this_week = [t for t in deadlines if 3 < t[1] <= 7]

# ---- exam mode -------------------------------------------------------------
EXAM = ("εξεταση", "εξετασεις", "εξεταστικη", "διαγωνισμα", "exam")
exams = [t for t in deadlines if t[1] <= 14
         and any(k in U.normalize(t[2].get("subject", "")) for k in EXAM)]
exam_mode = ""
if len(exams) >= 3:
    exam_mode = (f"⚠️ EXAM MODE: {len(exams)} εξετάσεις μέσα σε "
                 f"{max(e[1] for e in exams)} ημέρες")

subject = (f"🎓 Morning Brief - {today.strftime('%d/%m/%Y')} "
           f"- {total_unread} unread")

T, H = [], []
T.append(f"MORNING BRIEF — {today.strftime('%A %d %B %Y')}")
T.append("=" * 56)
H.append('<div style="font-family:system-ui,-apple-system,sans-serif;max-width:720px">')
H.append(f'<h1 style="margin:0 0 2px;font-size:22px">🎓 Morning Brief</h1>'
         f'<p style="color:#666;margin:0 0 16px">'
         f'{U.esc_html(today.strftime("%A %d %B %Y"))} · '
         f'<b style="color:{"#c00" if total_unread else "#080"}">'
         f'{total_unread} unread</b></p>')

if not sync.get("_meta", {}).get("gmail_available", True):
    T += ["", "(Gmail unreachable — read/unread shown from last sync)"]
    H.append('<p style="background:#ffd;padding:8px 12px;color:#660">'
             'Gmail unreachable — read/unread shown from the last sync.</p>')

if exam_mode:
    T += ["", exam_mode]
    H.append(f'<p style="background:#fee;border-left:4px solid #c00;'
             f'padding:10px 14px;font-weight:600">{U.esc_html(exam_mode)}</p>')

def head(text):
    T.extend(["", text, "-" * len(text)])
    H.append(f'<h2 style="font-size:16px;margin:20px 0 6px">{U.esc_html(text)}</h2>')

def bullets(rows, empty):
    if not rows:
        T.append(f"  {empty}")
        H.append(f'<p style="color:#888">{U.esc_html(empty)}</p>')
        return
    H.append("<ul>")
    for t, h in rows:
        T.append(f"  • {t}")
        H.append(f"<li>{h}</li>")
    H.append("</ul>")

# ---- 1. UNREAD — the most important section -------------------------------
head(f"⚠️ UNREAD — {total_unread} not opened yet")
if not unread_by_source:
    bullets([], "everything has been read 🎉")
else:
    for source, items in unread_by_source.items():
        label = U.SOURCE_LABEL.get(source, source)
        T.extend(["", f"  {label} — {len(items)}"])
        H.append(f'<p style="margin:10px 0 2px"><b>{U.esc_html(label)}</b> '
                 f'— {len(items)}</p><ul>')
        for e in items[:12]:
            subj = (e.get("subject") or "(no subject)")[:100]
            url = link(e, source)
            t = f"❌ {subj}" + (f" — ⏰ {e['due']}" if e.get("due") else "")
            T.append(f"    {t}")
            if url:
                T.append(f"       {url}")
            H.append(f'<li>❌ {U.esc_html(subj)}'
                     + (f' · <span style="color:#c00">⏰ {U.esc_html(e["due"])}</span>'
                        if e.get("due") else "")
                     + (f' · <a href="{U.esc_html(url)}">open</a>' if url else "")
                     + '</li>')
        H.append("</ul>")

# ---- 2. urgent / 3. this week ---------------------------------------------
def deadline_rows(group):
    rows = []
    for d, left, e in group[:15]:
        word = "σήμερα" if left == 0 else ("αύριο" if left == 1 else f"{left} ημέρες")
        subj = (e.get("subject") or "")[:95]
        label = U.SOURCE_LABEL.get(e["_source"], e["_source"])
        url = link(e, e["_source"])
        colour = "#c00" if left <= 3 else "#b80"
        rows.append((
            f"{mark(e)} {d.isoformat()} ({word}) [{label}] {subj}",
            f'{mark(e)} <code>{d.isoformat()}</code> '
            f'<span style="color:{colour}">({U.esc_html(word)})</span> '
            f'<b>{U.esc_html(label)}</b> · {U.esc_html(subj)}'
            + (f' · <a href="{U.esc_html(url)}">open</a>' if url else "")))
    return rows

head(f"🔴 URGENT — deadlines within 3 days ({len(urgent)})")
bullets(deadline_rows(urgent), "nothing due in the next three days")

head(f"🟡 THIS WEEK — deadlines within 7 days ({len(this_week)})")
bullets(deadline_rows(this_week), "nothing else due this week")

# ---- 4. calendar -----------------------------------------------------------
head("📅 TODAY & TOMORROW")
bullets([(c, U.esc_html(c)) for c in cal],
        "no events (or Calendar unavailable)")

# ---- 5. new in the last 24h ------------------------------------------------
n24 = sum(len(v) for v in recent_by_source.values())
head(f"📧 NEW LAST 24H — {n24}")
if not recent_by_source:
    bullets([], "nothing new since yesterday")
else:
    for source, items in recent_by_source.items():
        label = U.SOURCE_LABEL.get(source, source)
        T.extend(["", f"  {label} — {len(items)}"])
        H.append(f'<p style="margin:10px 0 2px"><b>{U.esc_html(label)}</b> '
                 f'— {len(items)}</p><ul>')
        for e in items[:12]:
            subj = (e.get("subject") or "")[:100]
            url = link(e, source)
            T.append(f"    {mark(e)} {subj}")
            H.append(f'<li>{mark(e)} {U.esc_html(subj)}'
                     + (f' · <a href="{U.esc_html(url)}">open</a>' if url else "")
                     + '</li>')
        H.append("</ul>")

# ---- 6. tasks / 7. review --------------------------------------------------
head("📋 TASKS — Trello due this week")
bullets([(t, U.esc_html(t)) for t in trello],
        "nothing due (or Trello unavailable)")

head("📖 TO REVIEW — vault notes tagged #status/review")
bullets([(r, U.esc_html(r)) for r in review[:20]], "none")

H.append('<hr style="margin-top:22px"><p style="color:#999;font-size:85%">'
         'Generated by morning-brief.sh</p></div>')

for name, data in (("body.txt", "\n".join(T) + "\n"),
                   ("body.html", "".join(H)),
                   ("subject.txt", subject)):
    with open(os.path.join(work, name), "w", encoding="utf-8") as fh:
        fh.write(data)
print(f"composed: {total_unread} unread, {len(urgent)} urgent, "
      f"{n24} new in 24h" + (f", {exam_mode}" if exam_mode else ""))
U.log(TOOL, "info", f"brief: {total_unread} unread, {len(urgent)} urgent")
PYEOF

SUBJECT="$(cat "$WORK/subject.txt" 2>/dev/null || echo "🎓 Morning Brief")"
if [ -s "$WORK/body.txt" ]; then
    "$BIN_DIR/uoa-sendmail.py" --subject "$SUBJECT" --text "$WORK/body.txt" \
        --html "$WORK/body.html" --tool "$TOOL_NAME" $DRY \
        && log INFO "morning brief sent" \
        || { log ERROR "could not send morning brief"; exit 4; }
else
    log ERROR "brief composition produced no body; nothing sent"
    exit 1
fi
