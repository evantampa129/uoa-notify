#!/usr/bin/env python3
"""Sync read/unread status of forwarded notifications back from Gmail.

Gmail is the single source of truth. Every notification this system forwards
carries a stable tag in its subject — [UOA-MSG-xxxxxxxx], [ECL-MSG-...],
[EDX-MSG-...], [DPT-MSG-...], [GML-MSG-...]. This asks Gmail which of those
messages have been opened and writes the answer back into every ledger:

    uoa-mail-seen.json  eclass-seen.json  eudoxus-seen.json
    web-hashes.json     gmail-uni-seen.json

Read it on the phone → the laptop stops notifying. Read it on the laptop →
the phone stops too. Matching is by tag first, then by the stored
gmail_msg_id for an exact match, then by exact subject for legacy entries
forwarded before tagging existed.

If Gmail cannot be reached, read state is left exactly as it was — never
guessed — so an outage can't silence a real notification.

  --json   per-source unread counts on stdout
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import uoa_common as U  # noqa: E402

TOOL = "sync-read-status"
SOURCES = ["webmail", "eclass", "eudoxus", "department", "gmail"]


def apply_state(entry, info):
    """Copy Gmail's verdict onto one ledger entry. Returns what changed.

    Gmail is the source of truth, with one exception. An item the user just
    opened from a desktop notification is read *here* before Gmail has been
    told, and that local verdict is held as `read_push_pending`. Until the
    push lands, Gmail's "still unread" is stale rather than authoritative:
    copying it back would undo the click, reset notify_count and start the
    alerts all over again. So the pending push is retried instead.
    """
    changed = []
    if info.get("id") and entry.get("gmail_msg_id") != info["id"]:
        entry["gmail_msg_id"] = info["id"]
        changed.append("id")

    if entry.get("read_push_pending") and not info.get("read"):
        msg_id = entry.get("gmail_msg_id")
        if msg_id and U.gmail_mark_read([msg_id], tool=TOOL):
            entry["read_push_pending"] = False
            entry["read_pushed_at"] = datetime.now(timezone.utc).isoformat(
                timespec="seconds")
            changed.append("pushed")
        # Read either way: the user opened it, and a failed push is a
        # transport problem to retry, not a reason to un-read the item.
        entry["read"] = True
        return changed

    was = bool(entry.get("read"))
    now = bool(info.get("read"))
    if was != now:
        entry["read"] = now
        entry["read_synced_at"] = datetime.now().isoformat(timespec="seconds")
        if now:
            changed.append("read")
        else:
            # Marked unread again on some device — let it notify once more.
            entry["notify_count"] = 0
            entry.pop("read_push_pending", None)
            changed.append("unread")
    return changed


def main():
    p = argparse.ArgumentParser(
        description="Sync read/unread state of forwarded notifications.")
    p.add_argument("--json", action="store_true")
    p.add_argument("--days", type=int, default=45)
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()

    gmail_state = U.gmail_scan_tags(days=args.days, tool=TOOL)
    if gmail_state is None:
        U.log(TOOL, "warn", "Gmail unavailable; read status left unchanged")

    # Secondary indexes for entries that predate tagging.
    by_id, by_subject = {}, {}
    for tag, info in (gmail_state or {}).items():
        if info.get("id"):
            by_id[info["id"]] = info
        subj = (info.get("subject") or "").strip()
        if subj:
            by_subject[subj] = info

    report = {}
    marked_read = marked_unread = linked = 0

    forced_unread = 0
    for source in SOURCES:
        state = U.ledger_load(source)
        changes = {"read": 0, "unread": 0, "id": 0, "pushed": 0}
        to_unread = []

        if gmail_state is not None:
            for entry in state["items"].values():
                if not entry.get("forwarded"):
                    continue
                tag = entry.get("tag") or U.tag_in(
                    entry.get("forward_subject", ""))
                info = None
                if tag:
                    info = gmail_state.get(tag)          # 1. by unique tag
                    if info and not entry.get("tag"):
                        entry["tag"] = tag
                if info is None and entry.get("gmail_msg_id"):
                    info = by_id.get(entry["gmail_msg_id"])   # 2. exact id
                if info is None:                              # 3. legacy
                    info = by_subject.get(
                        (entry.get("forward_subject") or "").strip())
                if info is None:
                    continue

                # First sight of a message we just forwarded: Gmail will have
                # auto-marked it read because it came from our own alias, so
                # put UNREAD back and let real read-state take over from here.
                if not entry.get("unread_forced"):
                    entry["unread_forced"] = True
                    if info.get("id"):
                        entry["gmail_msg_id"] = info["id"]
                        changes["id"] += 1
                    if U.recently_forwarded(entry) and info.get("read"):
                        to_unread.append(info["id"])
                        entry["read"] = False
                        continue
                for what in apply_state(entry, info):
                    changes[what] += 1
            if any(changes.values()) or to_unread:
                U.ledger_save(source, state)

        if to_unread:
            forced_unread += U.gmail_force_unread(to_unread, tool=TOOL)

        marked_read += changes["read"]
        marked_unread += changes["unread"]
        linked += changes["id"]

        items = [e for e in state["items"].values() if e.get("forwarded")]
        unread = [e for e in items if not e.get("read")]
        report[source] = {
            "tracked": len(state["items"]),
            "forwarded": len(items),
            "unread": len(unread),
            "unread_urgent": sum(1 for e in unread
                                 if e.get("category") in ("urgent", "grade")),
            "linked_to_gmail": sum(1 for e in items if e.get("gmail_msg_id")),
            "marked_read_now": changes["read"],
        }

    report["_meta"] = {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "gmail_available": gmail_state is not None,
        "tagged_messages_in_gmail": len(gmail_state or {}),
        "total_marked_read": marked_read,
        "total_marked_unread": marked_unread,
        "gmail_ids_linked": linked,
        "forced_unread_in_gmail": forced_unread,
        "total_unread": sum(report[s]["unread"] for s in SOURCES),
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif not args.quiet:
        if gmail_state is None:
            print("Gmail unavailable — showing last known state")
        for source in SOURCES:
            r = report[source]
            print(f"{source:11} tracked={r['tracked']:3}  unread={r['unread']:3}"
                  f"  urgent-unread={r['unread_urgent']:2}"
                  f"  linked={r['linked_to_gmail']:3}"
                  + (f"  (+{r['marked_read_now']} newly read)"
                     if r["marked_read_now"] else ""))
        print(f"{'TOTAL':11} unread={report['_meta']['total_unread']}")

    U.log(TOOL, "info",
          f"read+{marked_read} unread+{marked_unread} ids+{linked} "
          f"forced_unread={forced_unread}; "
          f"gmail_available={gmail_state is not None}; "
          f"unread_total={report['_meta']['total_unread']}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
