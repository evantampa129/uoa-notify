#!/usr/bin/env bash
# unread-count.sh — total unread notifications across every source.
#
# Prints "📧 5" (or "📧 0"). Safe for polybar/i3status/waybar: it only reads
# local ledger files, never touches the network, and always exits 0 with
# something printable even if the state files are missing or corrupt.
#
#   --plain     just the number
#   --waybar    JSON for waybar's custom module
#   --by-source one line per source

set -uo pipefail
export PATH="$HOME/.local/bin:$HOME/bin:/usr/local/bin:/usr/bin:/bin"

MODE="${1:---icon}"

python3 - "$MODE" <<'PYEOF' 2>/dev/null || echo "📧 ?"
import json, os, sys
sys.path.insert(0, os.path.expanduser("~/bin"))
mode = sys.argv[1] if len(sys.argv) > 1 else "--icon"
try:
    import uoa_common as U
    counts = U.unread_counts()
    urgent = 0
    for source in U.LEDGER_FILES:
        for e in U.ledger_unread(source):
            if e.get("category") in ("urgent", "grade"):
                urgent += 1
except Exception:
    counts, urgent = {"total": 0}, 0

total = counts.get("total", 0)

if mode == "--plain":
    print(total)
elif mode == "--waybar":
    cls = "urgent" if urgent else ("unread" if total else "clear")
    tip = ", ".join(f"{k}: {v}" for k, v in counts.items()
                    if k != "total" and v) or "nothing unread"
    print(json.dumps({"text": f"📧 {total}", "class": cls,
                      "tooltip": tip, "alt": cls}, ensure_ascii=False))
elif mode == "--by-source":
    for k, v in counts.items():
        if k != "total":
            print(f"{k:11} {v}")
    print(f"{'total':11} {total}")
else:
    print(f"📧 {total}" + (f" 🔴{urgent}" if urgent else ""))
PYEOF
