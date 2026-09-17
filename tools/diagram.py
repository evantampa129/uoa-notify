#!/usr/bin/env python3
"""Draws the figure in the Architecture section of README.md.

Generated rather than hand-aligned, because a hand-aligned diagram rots: one
edit shifts a border by a column, nobody notices, and fixing it later means
re-counting several hundred characters. Widths are declared, text that will
not fit raises, and every connector is computed from the box centres.

Two conventions. Everything that is a component gets a box, and everything
that flows between components is a line. The lines are box-drawing characters
and they meet the borders they touch, so a border reads as a border rather
than as punctuation.

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

# Junction glyphs, indexed by the directions a column has to connect:
# (up, down, left, right). Deriving the character from the directions is what
# keeps a junction correct when a box moves or changes width.
GLYPH = {
    (0, 0, 1, 1): "─", (1, 1, 0, 0): "│",
    (0, 1, 0, 1): "┌", (0, 1, 1, 0): "┐",
    (1, 0, 0, 1): "└", (1, 0, 1, 0): "┘",
    (1, 1, 0, 1): "├", (1, 1, 1, 0): "┤",
    (0, 1, 1, 1): "┬", (1, 0, 1, 1): "┴",
    (1, 1, 1, 1): "┼",
    (1, 0, 0, 0): "│", (0, 1, 0, 0): "│",
}


def glyph(up=0, down=0, left=0, right=0):
    """The box-drawing character joining the given directions."""
    return GLYPH[(int(bool(up)), int(bool(down)), int(bool(left)), int(bool(right)))]


def box(lines, width, pad=2):
    """Frame a block of text. Raises if a line will not fit."""
    area = width - 2 - pad
    for line in lines:
        if len(line) > area:
            raise ValueError(f"{line!r} needs {len(line)} columns, box holds {area}")
    top = "┌" + "─" * (width - 2) + "┐"
    bottom = "└" + "─" * (width - 2) + "┘"
    body = ["│" + (" " * pad + line).ljust(width - 2) + "│" for line in lines]
    return [top] + body + [bottom]


def row(boxes, gap=GAP, margin=MARGIN):
    """Place boxes side by side. Returns the lines and each box's centre column."""
    height = max(len(b) for b in boxes)
    padded = []
    for b in boxes:
        filler = "│" + " " * (len(b[0]) - 2) + "│"
        padded.append(b[:-1] + [filler] * (height - len(b)) + [b[-1]])
    lines = [" " * margin + (" " * gap).join(parts) for parts in zip(*padded)]

    centres, cursor = [], margin
    for b in boxes:
        centres.append(cursor + len(b[0]) // 2)
        cursor += len(b[0]) + gap
    return lines, centres


def tap(lines, index, columns, downward):
    """Open a border where a connector meets it. Modifies `lines` in place."""
    chars = list(lines[index])
    for column in columns:
        chars[column] = glyph(up=not downward, down=downward, left=1, right=1)
    lines[index] = "".join(chars)


def stem(columns, note=""):
    """A row carrying a vertical stroke at each column, with an optional note."""
    line = [" "] * (max(columns) + 1)
    for column in columns:
        line[column] = "│"
    return ("".join(line) + ("   " + note if note else "")).rstrip()


def junction(up, down):
    """The horizontal run joining columns above to columns below.

    One primitive serves both fans: one column above and three below is a
    distribution, several above and one below is a collection.
    """
    columns = list(up) + list(down)
    lo, hi = min(columns), max(columns)
    marks = {c: {"left": c > lo, "right": c < hi} for c in range(lo, hi + 1)}
    for column in down:
        marks[column]["down"] = True
    for column in up:
        marks[column]["up"] = True
    line = [" "] * (hi + 1)
    for column, directions in marks.items():
        line[column] = glyph(**directions)
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
    tap(sources, -1, source_centres, downward=True)
    out += sources + [stem(source_centres)]

    # --- the checkers, one per source ------------------------------------
    # The cadence sits on each line: the schedule is what gets forgotten
    # first, and it explains why two checkers can raise the same item minutes
    # apart.
    checkers, spine = row([box([
        "CHECKERS - one per source, each with its own seen-set",
        "",
        "check-uoa-mail.py           every 15 min, offset by 5",
        "check-eclass.py             twice an hour",
        "check-gmail-university.py   twice an hour",
        "check-eudoxus.py            every 2 hours",
        "check-department.py         every 2 hours",
    ], 74)])
    tap(checkers, 0, source_centres, downward=False)
    tap(checkers, -1, spine, downward=True)
    out += checkers + [stem(spine, "new items only, never re-announced")]

    # --- the shared core --------------------------------------------------
    core, core_centre = row([box([
        "uoa_common.py - shared core",
        "",
        "Greek parser    normalise → strip noise → match keyword",
        "                → date near keyword → urgency bucket",
        "Ledger          per-source JSON: notified / forwarded / read",
        "                gmail_msg_id / calendar_created",
        "Credentials     keyring → keepass → gpg → file",
        "MCP bridge      configured CLI, explicit tool allow-list",
    ], 74)], margin=2)
    tap(core, 0, core_centre, downward=False)
    tap(core, -1, core_centre, downward=True)
    out += core

    # --- the hub ----------------------------------------------------------
    hub_lines, hub_centre = row([box([
        "notify.py",
        "one hub, every path",
    ], 25)], margin=26)
    tap(hub_lines, 0, hub_centre, downward=False)
    tap(hub_lines, -1, hub_centre, downward=True)
    out += hub_lines

    # --- where a notification goes ---------------------------------------
    destinations, dest_centres = row([
        box(["uoa-notifyd.py", "desktop banner", "click → open page",
             "click → mark read"], 23),
        box(["forward once", "to the phone address", "[XXX-MSG-xxxxxxxx]", ""], 25),
        box(["~/.local/log/", "notifications.log", "", ""], 22),
    ])
    tap(destinations, 0, dest_centres, downward=False)
    # Only the daemon's column continues: the forward and the log file are
    # where a notification stops.
    tap(destinations, -1, dest_centres[:1], downward=True)
    out += [junction(up=hub_centre, down=dest_centres)] + destinations

    # --- Gmail decides what counts as read -------------------------------
    gmail, _ = row([box([
        "Gmail - UNREAD label",
        "",
        "the source of truth for read/unread,",
        "whichever device did the reading",
    ], 44)], margin=2)
    tap(gmail, 0, dest_centres[:1], downward=False)
    tap(gmail, -1, dest_centres[:1], downward=True)
    out += gmail

    # --- and the sync puts that verdict back ------------------------------
    sync, sync_centre = row([box([
        "sync-read-status.py   every 15 min, on the quarter hour",
        "",
        "Gmail verdict → ledger → re-notify only what is",
        "genuinely still unread, back through notify.py",
    ], 60)], margin=2)
    tap(sync, 0, dest_centres[:1], downward=False)
    out += sync

    # --- side effects, raised once per item ------------------------------
    effects, _ = row([
        box(["Google Calendar", "deadline event,", "3-day + 1-day"], 20),
        box(["Obsidian vault", "attachment filed", "with a note"], 20),
        box(["Google Drive", "University/", "<sender>/"], 18),
        box(["Trello", "card per", "deadline"], 14),
    ])
    out += ["", "  Side effects, all deduplicated through the ledger:"] + effects

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
