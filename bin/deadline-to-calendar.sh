#!/usr/bin/env bash
# deadline-to-calendar.sh — add one deadline by hand, everywhere at once.
#
#   deadline-to-calendar.sh --title "Εργασία X" --date 2026-09-15 \
#                           --course "Μαθηματικά" [--time 23:59] [--notes "..."]
#
# Creates:
#   1. a Google Calendar event, plus 3-day and 1-day reminder events
#   2. a Trello card with the same due date
#   3. an Obsidian note in the matching course folder
#
# Each step is independent: if Trello is down the calendar event and the note
# are still created, and the failure is logged rather than aborting the run.

set -uo pipefail
TOOL_NAME="deadline-to-calendar"
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/uoa-lib.sh"

TITLE=""; DATE=""; COURSE=""; TIME=""; NOTES=""; DRY=""
usage() {
    cat <<USAGE
usage: deadline-to-calendar.sh --title TITLE --date YYYY-MM-DD [options]

  --title   TEXT        what is due            (required)
  --date    YYYY-MM-DD  when it is due         (required)
  --course  TEXT        course name, used to pick the vault folder
  --time    HH:MM       time of day (default: all-day event)
  --notes   TEXT        extra detail for the event, card and note
  --dry-run             show what would happen, change nothing
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --title)   TITLE="${2:-}"; shift 2 ;;
        --date)    DATE="${2:-}"; shift 2 ;;
        --course)  COURSE="${2:-}"; shift 2 ;;
        --time)    TIME="${2:-}"; shift 2 ;;
        --notes)   NOTES="${2:-}"; shift 2 ;;
        --dry-run) DRY="1"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage; exit 2 ;;
    esac
done

[ -n "$TITLE" ] && [ -n "$DATE" ] || { echo "error: --title and --date are required" >&2; usage; exit 2; }
if ! printf '%s' "$DATE" | grep -Eq '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'; then
    echo "error: --date must be YYYY-MM-DD (got '$DATE')" >&2; exit 2
fi
if [ -n "$TIME" ] && ! printf '%s' "$TIME" | grep -Eq '^[0-9]{2}:[0-9]{2}$'; then
    echo "error: --time must be HH:MM (got '$TIME')" >&2; exit 2
fi
if ! date -d "$DATE" >/dev/null 2>&1; then
    echo "error: '$DATE' is not a real date" >&2; exit 2
fi

log INFO "manual deadline: '$TITLE' due $DATE${COURSE:+ (course: $COURSE)}"
echo "📌 $TITLE — due $DATE${TIME:+ at $TIME}${COURSE:+  ·  $COURSE}"

export DL_TITLE="$TITLE" DL_DATE="$DATE" DL_COURSE="$COURSE" \
       DL_TIME="$TIME" DL_NOTES="$NOTES" DL_DRY="$DRY"

python3 - <<'PYEOF'
import os, sys, subprocess
from datetime import date, datetime, timedelta
sys.path.insert(0, os.path.expanduser("~/bin"))
import uoa_common as U

TOOL   = "deadline-to-calendar"
title  = os.environ["DL_TITLE"]
due    = os.environ["DL_DATE"]
course = os.environ.get("DL_COURSE", "")
tm     = os.environ.get("DL_TIME") or None
notes  = os.environ.get("DL_NOTES", "")
dry    = bool(os.environ.get("DL_DRY"))

label = f"{course} — {title}" if course else title
days_left = (date.fromisoformat(due) - date.today()).days
detail = "\n".join(filter(None, [
    notes, f"Course: {course}" if course else "",
    "Added by hand with deadline-to-calendar.sh"]))

# ---- 1. calendar -----------------------------------------------------------
ok_cal = U.calendar_add_deadline(label, due, tm, detail,
                                 source_url="manual entry",
                                 dry_run=dry, tool=TOOL)
print(f"  {'✅' if ok_cal else '❌'} Calendar event + reminders (3 days, 1 day)")
if not ok_cal:
    U.log(TOOL, "warn", f"calendar event not created for '{label}'")

# ---- 2. trello -------------------------------------------------------------
due_iso = f"{due}T{tm or '09:00'}:00"
prompt = (
    "Create ONE Trello card on my most appropriate board for university "
    "coursework (prefer a board about studies/university/σχολή; otherwise my "
    "first board), in a list for to-do or upcoming work.\n"
    f"Card name: {label}\n"
    f"Due date: {due_iso} in the user's local timezone Europe/Athens — "
    "convert it to UTC before setting it.\n"
    f"Description: {detail or 'University deadline'}\n"
    "Do not ask questions. Reply with the single word DONE and the card URL."
)
if dry:
    print(f"  ✅ Trello card (dry run): {label} due {due_iso}")
    ok_trello = True
else:
    out = U.mcp_ask(prompt, U.TRELLO_TOOLS, tool=TOOL)
    ok_trello = bool(out) and ("DONE" in (out or "").upper()
                               or "trello.com" in (out or "").lower())
    print(f"  {'✅' if ok_trello else '❌'} Trello card"
          + ("" if ok_trello else " (unavailable — logged, calendar/note still done)"))
    U.log(TOOL, "info" if ok_trello else "warn",
          f"trello card '{label}': {(out or 'no reply')[:200]}")

# ---- 3. obsidian note ------------------------------------------------------
folder = U.find_course_folder(course, title)
fname  = U.safe_filename(f"{due} {title}", "deadline") + ".md"
path   = os.path.join(folder, fname)
body = "\n".join(filter(None, [
    f"**Deadline:** {due}" + (f" at {tm}" if tm else ""),
    f"**Course:** {course}" if course else "",
    f"**Days left:** {days_left}",
    "", "## Notes", "", notes or "- ",
    "", "## Checklist", "", "- [ ] Started", "- [ ] Draft done",
    "- [ ] Submitted",
]))
if dry:
    print(f"  ✅ Obsidian note (dry run): {path}")
else:
    try:
        os.makedirs(folder, exist_ok=True)
        U.write_note(path, title,
                     {"due": due, "course": course or "unknown",
                      "tags": "[deadline, uoa]", "status": "status/todo",
                      "source": "manual"},
                     body)
        print(f"  ✅ Obsidian note: {path.replace(os.path.expanduser('~'), '~')}")
        U.log(TOOL, "info", f"wrote note {path}")
    except OSError as exc:
        print(f"  ❌ Obsidian note failed: {exc}")
        U.log(TOOL, "error", f"cannot write note {path}: {exc}")

when = ("today" if days_left == 0 else
        "tomorrow" if days_left == 1 else f"in {days_left} days")
print(f"\n⏰ {label} — {when}")
PYEOF
