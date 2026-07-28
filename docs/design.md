# UoA academic notification system

Everything runs from `~/bin` (installed by `install.sh`), on a cron schedule,
every day of the week.
State lives in `~/.local/state`, logs in `~/.local/log/notifications.log`.

## How read/unread works (the important part)

Every notification is forwarded to `your configured address` with a stable
tag in the subject line:

    🟡 UoA DEADLINE: Εργασία 3 [UOA-MSG-3f8a1c9d]

The tag is a hash of `(source, message_id)` — the same item always produces the
same tag. `sync-read-status.py` searches Gmail for these tags every 15 minutes
and writes the answer back into the ledgers. **Gmail is the single source of
truth**: open it on the phone and the laptop stops notifying, and vice versa.

Tag prefixes: `UOA` webmail · `ECL` eClass · `EDX` Eudoxus · `DPT` department ·
`GML` university mail already in Gmail.

### The forced-UNREAD step

Gmail marks incoming mail as *already read* when the sender is one of the
account's own "send mail as" aliases — which is our case, since notifications
are forwarded from `@uoa.gr` to the Gmail address. Left alone every
notification would arrive pre-read and nothing would ever be flagged.

So the first time sync links a freshly forwarded message it puts the `UNREAD`
label back on (only within 90 minutes of forwarding, so a genuinely-read mail
is never dragged back). From then on Gmail's own read state is authoritative.

### Persistent notifications

Desktop alerts repeat every cron cycle for as long as an item is unread, with a
`(still unread ×N)` counter. The forwarded email is sent **once** — it stays
unread in Gmail, which is the cross-device signal; re-sending would spam the
inbox and break the one-tag-one-message mapping.

Clicking a notification opens the source page (`notify-open.sh`, falling back
to zenity). One live notification per item, deduplicated by a lockfile.

### Two loops that had to be broken

**The echo loop.** Notifications are forwarded into Gmail, and
`check-gmail-university.py` scans Gmail for university mail — so it used to
pick up this system's own forwards and notify about them again. Any subject
carrying an `[XXX-MSG-…]` tag is now excluded, both in the search query and as
a second check on the results.

**The double alert.** A checker dispatches the items in its current window and
then re-notifies everything still unread; items in both sets popped up twice
per run. `renotify_unread()` now skips anything alerted in the last 3 minutes.

### Dry runs change nothing

`--dry-run` used to mark items as forwarded without sending them, so the real
run then skipped those messages for good. Every checker now refuses to persist
state during a dry run, and `dispatch()` restores its ledger entry afterwards.

## Scripts

| Script | What it does |
|---|---|
| `check-uoa-mail.py` | UoA IMAP mailbox. Greek deadline parsing, Calendar events, PDF/DOCX into the vault with a companion note. |
| `check-eclass.py` | eClass announcements, assignments, files, grades, per course. |
| `check-eudoxus.py` | Textbook periods and deadlines. Skips cleanly with no creds. |
| `check-department.py` | di.uoa.gr page-hash change detection, plus `~/.watch-urls`. |
| `check-gmail-university.py` | University mail already in Gmail; files attachments to Drive under `University/<sender>`. |
| `sync-read-status.py` | Cross-device read sync. `--json` for per-source unread counts. |
| `notify.py` | Central hub: desktop + forward + log + ledger. All scripts use it. |
| `notify-open.sh` | Clickable notification helper. |
| `morning-brief.sh` | 07:30 daily. Leads with the UNREAD section. |
| `weekly-review.sh` | Sundays 18:00. Week summary, read/unread stats, 3-week deadlines. |
| `daily-log.sh` | 22:00 daily. Writes `DailyNotes/YYYY-MM-DD.md`. |
| `deadline-to-calendar.sh` | Manual: Calendar + Trello card + vault note in one command. |
| `unread-count.sh` | Widget output for polybar/waybar/i3status. |

## Schedule

    */15      sync-read-status.py       (on the quarter hour, first)
    :05/15    check-uoa-mail.py         every 15 min
    :08,:38   check-eclass.py           every 30 min
    :12,:42   check-gmail-university.py every 30 min
    :20 */2h  check-eudoxus.py          every 2 hours
    :35 */2h  check-department.py       every 2 hours
    07:30     morning-brief.sh          daily, Mon–Sun
    22:00     daily-log.sh              daily, Mon–Sun
    18:00 Sun weekly-review.sh          Sundays as well as the brief

Sync runs on the quarter hour so the checkers that follow re-notify only what
is genuinely still unread. Edit with `crontab -e`; the source lives in
`~/.config/uoa-crontab`.

## MCP access from cron

The scripts reach Gmail, Calendar, Trello and Drive by shelling out to
`claude -p` with a restricted `--allowedTools` list (`U.mcp_ask` /
`U.mcp_json`). This works from cron's minimal environment because the CLI reads
its own credentials from `~/.claude`. Every MCP call is best-effort: on
timeout, missing CLI or no network it returns `None` and the caller carries on
with local data, so nothing ever hard-fails offline.

`notify-send` needs a desktop session, which cron does not have;
`U.desktop_env()` fills in `DISPLAY` and `DBUS_SESSION_BUS_ADDRESS`.

## Credentials

    ~/.uoa-mail-creds    line 1 username, line 2 password, optional line 3 SMTP password
    ~/.eudoxus-creds     same format; absent → Eudoxus checks public pages only
    ~/.watch-urls        optional extra pages for check-department.py

All `chmod 600`. Nothing is hardcoded; a file with placeholder values is
rejected.

## Greek deadline parsing

Keywords are matched at a word start but with no trailing boundary, so
inflection still matches (`προθεσμία` also finds `προθεσμίας`) while
`εργασία` cannot match inside `επεξεργασία`. Phrases in `NOISE_PHRASES`
(e.g. `σταθμό εργασίας` — workstation) are blanked before matching, so routine
automated mail is not read as a deadline. Dates next to deadline words beat
bare dates, which keeps a message's own `Ημερομηνία:` header from becoming a
due date.

## Known limits

- **Cross-source duplicate deadlines.** The same exam can arrive by email and
  by eClass; each source dedupes itself, so two Calendar events can appear for
  one deadline with different titles. Deduping across sources would need
  fuzzy title matching, which risks silently dropping genuinely different
  deadlines that fall on the same day.
- **Eudoxus** currently checks public pages only — add `~/.eudoxus-creds` to
  enable the logged-in checks.
- The morning brief takes two to four minutes because it runs every checker
  and several MCP queries; that is fine at 07:30 but slow to run by hand.

## Troubleshooting

    tail -f ~/.local/log/notifications.log     # everything logs here
    ./sync-read-status.py                      # per-source unread counts
    ./unread-count.sh --by-source
    ./morning-brief.sh --dry-run               # compose without sending
    ./check-uoa-mail.py --test-email           # prove SMTP works

If unread counts look stuck, check that Gmail is reachable:
`sync-read-status.py --json | head -30` reports `gmail_available`.
