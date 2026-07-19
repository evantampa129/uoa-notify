#!/usr/bin/env bash
# weekly-review.sh — Sunday 6 PM summary of the whole week.
# Runs in addition to that morning's brief.
#
# Covers every source, read vs unread per source, all deadlines in the next
# three weeks, Trello, the vault, and any grades posted. Sources that are
# unavailable are reported as such; the review always goes out.

set -uo pipefail
TOOL_NAME="weekly-review"
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/uoa-lib.sh"

DRY=""; DAYS=7
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY="--dry-run"; shift ;;
        --days)    DAYS="${2:-7}"; shift 2 ;;
        -h|--help) echo "usage: weekly-review.sh [--days N] [--dry-run]"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
log INFO "starting weekly review (${DAYS}d)"

# Gmail decides what counts as read; refresh before reporting.
if have_network; then
    "$BIN_DIR/sync-read-status.py" --json > "$WORK/sync.json" 2>/dev/null \
        || { log WARN "read-status sync failed"; echo '{}' > "$WORK/sync.json"; }
else
    log WARN "offline; weekly review uses last known read state"
    echo '{}' > "$WORK/sync.json"
fi

# Vault review notes (local, always available).
: > "$WORK/review.txt"
if [ -d "$VAULT" ]; then
    grep -rl --include='*.md' '#status/review' "$VAULT" 2>/dev/null \
        | head -30 | sed "s|$VAULT/||" > "$WORK/review.txt" || true
else
    log WARN "vault not found at $VAULT"
fi

# New notes this week.
NEW_NOTES=0
[ -d "$VAULT" ] && NEW_NOTES="$(find "$VAULT" -name '*.md' -mtime "-$DAYS" 2>/dev/null | wc -l)"

: > "$WORK/trello.txt"
if have_network; then
    mcp_ask 200 "mcp__claude_ai_Trello__trelloSearch,mcp__claude_ai_Trello__trelloReadBoard,mcp__claude_ai_Trello__trelloReadList,mcp__claude_ai_Trello__trelloReadCard" \
"List my Trello cards that are overdue, or due within the next 21 days. \
One per line as: - DUEDATE | STATUS | CARD NAME | BOARD
where STATUS is OVERDUE or DUE. If none, output exactly: NONE
No preamble." > "$WORK/trello.txt" 2>/dev/null || true
fi

export WR_WORK="$WORK" WR_DAYS="$DAYS" WR_NEWNOTES="$NEW_NOTES"

python3 - <<'PYEOF'
import json, os, sys
from datetime import date, datetime, timedelta
sys.path.insert(0, os.path.expanduser("~/bin"))
import uoa_common as U

TOOL = "weekly-review"
work = os.environ["WR_WORK"]
days = int(os.environ.get("WR_DAYS", "7"))
new_notes = os.environ.get("WR_NEWNOTES", "0")

today = date.today()
start = today - timedelta(days=days)
rng = f"{start.strftime('%d/%m')}–{today.strftime('%d/%m/%Y')}"

def tload(name):
    try:
        txt = open(os.path.join(work, name), encoding="utf-8").read().strip()
    except OSError:
        return []
    if not txt or txt.upper().startswith("NONE"):
        return []
    return [l.strip(" -\t") for l in txt.splitlines()
            if l.strip() and not l.strip().upper().startswith("NONE")][:25]

try:
    review = [l for l in open(os.path.join(work, "review.txt"),
                              encoding="utf-8").read().splitlines() if l]
except OSError:
    review = []
trello = tload("trello.txt")

# ---- gather the week -------------------------------------------------------
per_source, week_items, unread_items, grades = {}, [], [], []
for source in U.LEDGER_FILES:
    got = []
    for e in U.ledger_activity(source):
        seen = (e.get("first_seen") or "")[:10]
        if seen and seen >= start.isoformat():
            got.append(dict(e, _source=source))
    read_n = sum(1 for e in got if e.get("read"))
    per_source[source] = {"total": len(got), "read": read_n,
                          "unread": len(got) - read_n}
    week_items += got
    grades += [e for e in got if e.get("category") == "grade"]
    unread_items += [dict(e, _source=source) for e in U.ledger_unread(source)]

week_items.sort(key=lambda e: e.get("first_seen", ""), reverse=True)
unread_items.sort(key=lambda e: e.get("first_seen", ""), reverse=True)
total_unread = len(unread_items)

# ---- deadlines: next three weeks ------------------------------------------
horizon = today + timedelta(days=21)
upcoming, done_past = [], 0
for source in U.LEDGER_FILES:
    for e in U.ledger_load(source)["items"].values():
        if not U.is_real(e) or not e.get("due"):
            continue
        try:
            d = date.fromisoformat(str(e["due"])[:10])
        except ValueError:
            continue
        if today <= d <= horizon:
            upcoming.append((d, dict(e, _source=source)))
        elif d < today and start <= d:
            done_past += 1
upcoming.sort(key=lambda t: t[0])

def mark(e):
    return "✅" if e.get("read") else "❌"

subject = f"📋 Weekly Review - {rng} - {total_unread} unread"

T, H = [], []
T.append(f"WEEKLY REVIEW — {rng}")
T.append("=" * 56)
H.append('<div style="font-family:system-ui,-apple-system,sans-serif;max-width:720px">')
H.append(f'<h1 style="margin:0 0 2px;font-size:22px">📋 Weekly Review</h1>'
         f'<p style="color:#666;margin:0 0 16px">{U.esc_html(rng)} · '
         f'{len(week_items)} notifications · <b>{total_unread} still unread</b></p>')

def head(text):
    T.extend(["", text, "-" * len(text)])
    H.append(f'<h2 style="font-size:16px;margin:18px 0 6px">{U.esc_html(text)}</h2>')

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

# ---- unread first: the thing that matters ---------------------------------
head(f"⚠️ STILL UNREAD — {total_unread}")
rows = []
for e in unread_items[:25]:
    label = U.SOURCE_LABEL.get(e["_source"], e["_source"])
    subj = (e.get("subject") or "(no subject)")[:100]
    url = U.source_link(e["_source"], e.get("url", ""))
    t = f"[{label}] {subj}" + (f" — ⏰ {e['due']}" if e.get("due") else "")
    h = (f'<b>{U.esc_html(label)}</b> · {U.esc_html(subj)}'
         + (f' · <span style="color:#c00">⏰ {U.esc_html(e["due"])}</span>' if e.get("due") else "")
         + (f' · <a href="{U.esc_html(url)}">open</a>' if url else ""))
    rows.append((t, h))
bullets(rows, "nothing unread — inbox zero 🎉")

# ---- read vs unread per source --------------------------------------------
head("📊 READ vs UNREAD, by source")
rows = []
for source, s in per_source.items():
    if not s["total"]:
        continue
    label = U.SOURCE_LABEL.get(source, source)
    pct = round(100 * s["read"] / s["total"]) if s["total"] else 0
    t = f"{label}: {s['read']}/{s['total']} read ({s['unread']} unread, {pct}%)"
    bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
    h = (f'<b>{U.esc_html(label)}</b>: {s["read"]}/{s["total"]} read '
         f'<code>{bar}</code> {pct}%'
         + (f' · <span style="color:#c00">{s["unread"]} unread</span>' if s["unread"] else ""))
    rows.append((t, h))
bullets(rows, "no notifications this week")

# ---- deadlines -------------------------------------------------------------
head("⏰ DEADLINES — next 3 weeks")
rows = []
for d, e in upcoming[:25]:
    left = (d - today).days
    word = "today" if left == 0 else ("tomorrow" if left == 1 else f"{left} days")
    subj = (e.get("subject") or "")[:90]
    t = f"{d.isoformat()} ({word}) — {subj} {mark(e)}"
    colour = "#c00" if left <= 3 else ("#b80" if left <= 7 else "#555")
    h = (f'<code>{d.isoformat()}</code> '
         f'<span style="color:{colour}">({U.esc_html(word)})</span> — '
         f'{U.esc_html(subj)} {mark(e)}')
    rows.append((t, h))
bullets(rows, "no deadlines in the next three weeks")

T.append(f"\n  Deadlines passed this week: {done_past} · still pending: {len(upcoming)}")
H.append(f'<p style="color:#666">Deadlines passed this week: <b>{done_past}</b> · '
         f'still pending: <b>{len(upcoming)}</b></p>')

# ---- grades ----------------------------------------------------------------
head("📝 GRADES posted this week")
bullets([((e.get("subject") or "")[:100],
          U.esc_html((e.get("subject") or "")[:100]) + " " + mark(e))
         for e in grades[:15]], "no grades posted")

# ---- everything that arrived ----------------------------------------------
head(f"📧 EVERYTHING THIS WEEK — {len(week_items)}")
rows = []
for e in week_items[:40]:
    label = U.SOURCE_LABEL.get(e["_source"], e["_source"])
    subj = (e.get("subject") or "")[:95]
    rows.append((f"{mark(e)} [{label}] {subj}",
                 f'{mark(e)} <b>{U.esc_html(label)}</b> · {U.esc_html(subj)}'))
bullets(rows, "nothing arrived this week")

head("📋 TRELLO — overdue and due soon")
bullets([(t, U.esc_html(t)) for t in trello],
        "nothing due, or Trello unavailable")

head("📖 VAULT")
T.append(f"  Notes created or edited this week: {new_notes}")
H.append(f'<p>Notes created or edited this week: <b>{U.esc_html(new_notes)}</b></p>')
bullets([(r, U.esc_html(r)) for r in review[:20]],
        "no notes tagged #status/review")

H.append('<hr style="margin-top:22px"><p style="color:#999;font-size:85%">'
         'Generated by weekly-review.sh</p></div>')

for name, data in (("body.txt", "\n".join(T) + "\n"),
                   ("body.html", "".join(H)),
                   ("subject.txt", subject)):
    with open(os.path.join(work, name), "w", encoding="utf-8") as fh:
        fh.write(data)
print(f"composed: {len(week_items)} items, {total_unread} unread, "
      f"{len(upcoming)} upcoming deadlines")
U.log(TOOL, "info", f"week {rng}: {len(week_items)} items, {total_unread} unread")
PYEOF

SUBJECT="$(cat "$WORK/subject.txt" 2>/dev/null || echo "📋 Weekly Review")"
if [ -s "$WORK/body.txt" ]; then
    "$BIN_DIR/uoa-sendmail.py" --subject "$SUBJECT" --text "$WORK/body.txt" \
        --html "$WORK/body.html" --tool "$TOOL_NAME" $DRY \
        && log INFO "weekly review sent" \
        || { log ERROR "could not send weekly review"; exit 4; }
else
    log ERROR "review composition produced no body; nothing sent"
    exit 1
fi
