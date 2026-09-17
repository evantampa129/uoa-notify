#!/usr/bin/env python3
"""Draws the ASCII figure in the Architecture section of README.md.

Generated rather than hand-aligned, because a hand-aligned diagram rots: one
edit shifts a border by a column, nobody notices, and fixing it later means
re-counting several hundred characters. Here the widths are declared, text
that will not fit raises, and the connectors are computed from the box
centres.

One convention throughout: everything that is a component gets a box,
everything that flows between components is a line.

    python tools/diagram.py            print it
    python tools/diagram.py --write    replace the block in README.md
    python tools/diagram.py --check    exit 1 if README.md has drifted
"""

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
README = os.path.join(REPO_ROOT, "README.md")

MARGIN = 2
GAP = 1


def box(lines, width, pad=2):
    """Frame a block of text. Raises if a line will not fit."""
    area = width - 2 - pad
    for line in lines:
        if len(line) > area:
            raise ValueError(f"{line!r} needs {len(line)} columns, box holds {area}")
    border = "+" + "-" * (width - 2) + "+"
    body = ["|" + (" " * pad + line).ljust(width - 2) + "|" for line in lines]
    return [border] + body + [border]


def row(boxes, gap=GAP, margin=MARGIN):
    """Place boxes side by side. Returns the lines and each box's centre column."""
    height = max(len(b) for b in boxes)
    padded = []
    for b in boxes:
        filler = "|" + " " * (len(b[0]) - 2) + "|"
        padded.append(b[:-1] + [filler] * (height - len(b)) + [b[-1]])
    lines = [" " * margin + (" " * gap).join(parts) for parts in zip(*padded)]

    centres, cursor = [], margin
    for b in boxes:
        centres.append(cursor + len(b[0]) // 2)
        cursor += len(b[0]) + gap
    return lines, centres


def at(centres, char="|", note=""):
    """A line carrying `char` at each column, with an optional trailing note."""
    line = [" "] * (max(centres) + 1)
    for c in centres:
        line[c] = char
    return ("".join(line) + ("   " + note if note else "")).rstrip()


def gather(centres, target):
    """A bracket collecting several columns into one."""
    line = [" "] * (max(max(centres), target) + 1)
    for i in range(min(centres), max(centres) + 1):
        line[i] = "-"
    for c in list(centres) + [target]:
        line[c] = "+"
    return "".join(line)


def spread(source, targets):
    """A bracket fanning one column out to several."""
    line = [" "] * (max(max(targets), source) + 1)
    for i in range(min(targets), max(targets) + 1):
        line[i] = "-"
    for t in list(targets) + [source]:
        line[t] = "+"
    return "".join(line)


def build():
    """Assemble the figure."""
    out = []

    # --- the five sources, each with how it is reached --------------------
    sources, source_centres = row([
        box(["mail.uoa.gr", "IMAP"], 14, pad=1),
        box(["eclass.uoa.gr", "CAS / SSO"], 16, pad=1),
        box(["eudoxus.gr", "Shibboleth"], 13, pad=1),
        box(["di.uoa.gr", "page hash"], 12, pad=1),
        box(["Gmail", "MCP connector"], 16, pad=1),
    ])
    out += sources
    out += [at(source_centres), at(source_centres, "v")]

    # --- the checkers, one per source ------------------------------------
    # Cadence is on each line: the schedule is the thing people forget, and it
    # explains why two checkers can raise the same item minutes apart.
    checkers, spine = row([box([
        "CHECKERS - one per source, each with its own seen-set",
        "",
        "check-uoa-mail.py           every 15 min, offset by 5",
        "check-eclass.py             twice an hour",
        "check-gmail-university.py   twice an hour",
        "check-eudoxus.py            every 2 hours",
        "check-department.py         every 2 hours",
    ], 66)])
    out += checkers
    out += [at(spine, note="new items only, never re-announced"), at(spine, "v")]

    # --- the shared core --------------------------------------------------
    core, core_centre = row([box([
        "uoa_common.py - shared core",
        "",
        "Greek parser    normalise -> strip noise -> match keyword",
        "                -> date near keyword -> urgency bucket",
        "Ledger          per-source JSON: notified / forwarded / read",
        "                gmail_msg_id / calendar_created",
        "Credentials     keyring -> keepass -> gpg -> file",
        "MCP bridge      configured CLI, explicit tool allow-list",
    ], 66)])
    out += core
    out += [at(core_centre), at(core_centre, "v")]

    # --- the hub ----------------------------------------------------------
    hub_lines, hub_centre = row([box([
        "notify.py",
        "one hub, every path",
    ], 25)], margin=25)
    out += hub_lines

    # --- where a notification goes ---------------------------------------
    destinations, dest_centres = row([
        box(["uoa-notifyd.py", "desktop banner", "click -> open page",
             "click -> mark read"], 22),
        box(["forward once", "to the phone address", "[XXX-MSG-xxxxxxxx]", ""], 24),
        box(["~/.local/log/", "notifications.log", "", ""], 21),
    ])
    out += [at(hub_centre), spread(hub_centre[0], dest_centres),
            at(dest_centres, "v")] + destinations

    # --- Gmail decides what counts as read -------------------------------
    gmail_lines, gmail_centre = row([box([
        "Gmail",
        "UNREAD label",
        "",
        "the source of truth for read/unread,",
        "whichever device did the reading",
    ], 42)], margin=8)
    out += [at([dest_centres[0]]), at([dest_centres[0]], "v")] + gmail_lines

    # --- and the sync puts that verdict back ------------------------------
    sync_lines, sync_centre = row([box([
        "sync-read-status.py",
        "every 15 min, on the quarter hour",
        "",
        "Gmail verdict -> ledger -> re-notify only",
        "what is genuinely still unread",
    ], 46)], margin=8)
    out += [at(gmail_centre), at(gmail_centre, "v")] + sync_lines
    loop = " " * sync_centre[0] + "+--> re-notify goes back through notify.py"
    out += [at(sync_centre), loop]

    # --- side effects, raised once per item ------------------------------
    out += ["", "  Side effects, all deduplicated through the ledger:"]
    effects, _ = row([
        box(["Google Calendar", "deadline event,", "3-day + 1-day"], 19),
        box(["Obsidian vault", "attachment filed", "with a note"], 20),
        box(["Google Drive", "University/", "<sender>/"], 17),
        box(["Trello", "card per", "deadline"], 14),
    ])
    out += effects

    return "\n".join(line.rstrip() for line in out)


def readme_block(text):
    """Find the fenced block under the Architecture heading."""
    lines = text.split("\n")
    try:
        heading = next(i for i, l in enumerate(lines) if l.startswith("## Architecture"))
        opening = next(i for i in range(heading, len(lines)) if lines[i].strip() == "```")
        closing = next(i for i in range(opening + 1, len(lines)) if lines[i].strip() == "```")
    except StopIteration:
        raise ValueError("No fenced block found under '## Architecture' in README.md")
    return opening, closing


def main():
    figure = build()

    if "--write" in sys.argv or "--check" in sys.argv:
        with open(README) as f:
            text = f.read()
        lines = text.split("\n")
        opening, closing = readme_block(text)
        current = "\n".join(lines[opening + 1:closing])

        if "--check" in sys.argv:
            if current == figure:
                print("README.md architecture figure is up to date")
                return
            print("README.md architecture figure differs from the generator")
            sys.exit(1)

        with open(README, "w") as f:
            f.write("\n".join(lines[:opening + 1] + figure.split("\n") + lines[closing:]))
        print(f"README.md updated: {closing - opening - 1} lines replaced by "
              f"{len(figure.splitlines())}")
        return

    print(figure)


if __name__ == "__main__":
    main()
