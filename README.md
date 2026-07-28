# uoa-notify

Academic notification system for the University of Athens, with read state
shared across devices.

It polls the UoA mailbox, eClass, Eudoxus and the department website, extracts
Greek deadlines, creates Calendar events, files attachments into an Obsidian
vault, and forwards each notification to Gmail. Gmail then acts as the single
record of what has been read, so opening a notification on one device stops it
being raised on another.

Written for `di.uoa.gr`. It adapts to any institution offering IMAP, a
Moodle-style LMS and a public announcements page.

![CI](https://github.com/evantampa129/uoa-notify/actions/workflows/ci.yml/badge.svg)

## Features

- Greek deadline extraction, tolerant of inflection and of hard-wrapped mail
- Calendar events for detected deadlines, with 3-day and 1-day reminders
- Desktop notifications that repeat until the item is read, and open the
  source page when clicked
- Cross-device read state, using Gmail as the authority
- Attachment filing into the matching Obsidian course folder
- Daily brief, weekly review and daily log
- Status-bar widget reporting the unread count
- Degrades to a logged warning when any source is unreachable

## Requirements

- Python 3.8 or later
- `requests`, `beautifulsoup4` (see `requirements.txt`)
- `notify-send`, `xdg-open`, `cron`
- Optional: `zenity`, used as a notification fallback
- Optional: a `claude` CLI on `PATH`, which is how the scripts reach Gmail,
  Calendar, Trello and Drive. Without it the system still runs; those steps
  are skipped and logged.

## Installation

```bash
git clone https://github.com/evantampa129/uoa-notify.git
cd uoa-notify
./install.sh --cron
```

`install.sh` copies the scripts to `~/bin`, creates the state and log
directories, seeds the configuration file and installs the crontab. Run
`./install.sh --check` to verify the result, or `--uninstall` to reverse it.

## Configuration

Credentials are read from files outside the repository, one value per line,
and must be mode `600`:

```bash
printf '%s\n%s\n' 'USERNAME' 'PASSWORD' > ~/.uoa-mail-creds
chmod 600 ~/.uoa-mail-creds
```

`~/.eudoxus-creds` follows the same format and is optional; without it the
public Eudoxus pages are still monitored.

Remaining settings live in `~/.config/check-uoa-mail/config.ini`. The
forwarding address must be set there, or in the `UOA_RECIPIENT` environment
variable, before notifications can be delivered. Templates for every file are
in `examples/`.

Verify the mail path before relying on it:

```bash
~/bin/check-uoa-mail.py --test-email
```

## Usage

Each checker runs unattended from cron, and can also be run by hand:

```bash
check-uoa-mail.py --notify          # poll the mailbox and dispatch
check-eclass.py --json              # machine-readable output, no side effects
sync-read-status.py                 # reconcile read state against Gmail
unread-count.sh --by-source         # unread totals, local state only
morning-brief.sh --dry-run          # compose the brief without sending it
deadline-to-calendar.sh --title T --date 2026-09-01
```

`--dry-run` is available throughout and never writes state.

## Scheduling

`install.sh --cron` installs the following:

| Schedule | Command |
|---|---|
| every 15 min, on the quarter | `sync-read-status.py` |
| every 15 min, offset 5 | `check-uoa-mail.py` |
| every 30 min | `check-eclass.py`, `check-gmail-university.py` |
| every 2 hours | `check-eudoxus.py`, `check-department.py` |
| 07:30 daily | `morning-brief.sh` |
| 22:00 daily | `daily-log.sh` |
| 18:00 Sunday | `weekly-review.sh` |

Read state is synchronised before the checkers run, so that only genuinely
unread items are raised again.

## How it works

Every source is dispatched through a single hub, which forwards a copy to
Gmail carrying a stable tag in the subject line:

```
UoA DEADLINE: Εργασία 3 — Δομές Δεδομένων [UOA-MSG-3f8a1c9d]
```

The tag is derived from `(source, message_id)`, so a given item always
produces the same tag. `sync-read-status.py` asks Gmail which of those tags
remain unread and writes the result back to local state.

```
UoA IMAP ─┐
eClass ───┤
Eudoxus ──┼──▶ notify.py ──▶ desktop notification
di.uoa.gr─┤        │
Gmail ────┘        └──────▶ Gmail, subject-tagged ◀── read on any device
                                    │
                    sync-read-status.py reads it back
                                    │
                    ledgers ──▶ brief · review · log · widget
```

One detail is worth stating explicitly. Gmail marks incoming mail as already
read when the sender is one of the account's own send-as aliases, which is the
case here. Left alone, every forwarded notification would arrive pre-read and
the unread count would remain at zero. The sync job therefore re-applies the
`UNREAD` label the first time it links a message, and only within 90 minutes of
forwarding, so a message that was genuinely read is never reverted.

`docs/design.md` covers the architecture in full.

## Development

Tests cover the Greek classifier and the subject tags. They require no
credentials, network access or third-party packages:

```bash
python3 -m unittest discover -s tests -t tests -v
```

CI runs the same tests on Python 3.8 and 3.12, byte-compiles every Python
script and parses every shell script.

## Limitations

- A deadline announced through two sources may produce two Calendar events.
  Each source de-duplicates its own items; de-duplicating across sources needs
  fuzzy title matching, which risks discarding distinct deadlines that fall on
  the same day.
- `morning-brief.sh` takes two to four minutes, as it runs every checker.
- Eudoxus is limited to public pages unless credentials are supplied.

## Security

Credentials are stored outside the repository at mode `600` and are never
embedded in source. The forwarding address is read from configuration or the
environment. Runtime state is excluded from version control, as the ledgers
contain real message subjects, senders and identifiers.
