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

### Clicking a notification

Activating a desktop notification opens the source page and marks the item
read. Two details of the freedesktop notification specification decide whether
that works at all, and both were originally wrong.

Clicking the *body* of a notification does not fire an arbitrary named action.
It fires the action registered under the reserved key `default`, and nothing
else. A notification declaring only `--action=open=Open` therefore looks
clickable and does nothing when clicked, because no handler is bound to the
body — and GNOME Shell hides named action buttons until the banner is
expanded, so there is frequently nothing visible to click either.
Both `notify-open.sh` and the daemon register `default` first, with `open`
alongside it for the servers that render a button.

`--action` also implies `--wait`: `notify-send` blocks until the notification
is activated or closed. GNOME Shell ignores `--expire-time` and keeps the
notification in its tray indefinitely, so that wait has no natural end. Left
unbounded, one helper process per unread item survives forever, holding the
per-item lock that exists to prevent duplicate banners and thereby silencing
that item permanently. Measured before the fix: 97 live helpers, the oldest
fourteen hours old.

Bounding the wait fixes the leak and uncovers the real problem, which is that
**a notification outlives the process that posted it**. GNOME keeps it in the
tray after the helper has exited, so the banner is still there, still looks
clickable, and nothing is listening any more. Clicking does nothing, silently,
for as long as the notification sits there.

`uoa-notifyd.py` removes that failure mode. `ActionInvoked` is broadcast on
the session bus and carries the notification's own id, so one long-lived
process can subscribe once, keep a map of id to target, and act on a click
regardless of which process posted the notification or how long ago. Requests
arrive on a Unix socket in `$XDG_RUNTIME_DIR/uoa-notify/`; a repeat for the
same tag reuses the previous notification id, which replaces the banner in
place and makes the old per-item lock file unnecessary.

The daemon starts on demand from the first notification of a cycle and exits
after six idle hours. Without `python3-gi` it cannot run, and `notify_send`
falls back to `notify-open.sh` and then to a plain notification: the click
degrades, the alert does not.

Then the read-back, which has to satisfy both directions of the sync:

    click → xdg-open the source page
          → ledger read = true          stops the desktop alert immediately
          → Gmail UNREAD label removed  makes it stick on every other device

The local flag alone would not survive. Gmail is the declared source of truth,
so the next `sync-read-status.py` run would see the message still unread, flip
the ledger back, reset `notify_count` and start alerting again. The push is
therefore attempted straight away and recorded as `read_push_pending` when it
cannot be made, and `apply_state` treats Gmail's verdict as stale rather than
authoritative while a push is owed — retrying it instead of undoing the click.

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

The scripts reach Gmail, Calendar, Trello and Drive over MCP, by handing a
single-shot prompt to an assistant CLI that already holds the OAuth grants for
those accounts (`U.mcp_ask` / `U.mcp_json`, and `mcp_ask` in `uoa-lib.sh`).
Each call is restricted to an explicit tool list, so a connector is never
reachable beyond what that step needs.

Which CLI, and the prefix its connector tool ids carry, are configuration and
not constants:

    [agent]
    cli        = <command on PATH>
    mcp_prefix = <tool-id prefix>

read from `~/.config/check-uoa-mail/config.ini` or from `UOA_AGENT_CLI` and
`UOA_MCP_PREFIX`. Both are blank in the repository, so no vendor is baked into
the source and the backend can be swapped without touching a script.

Every MCP call is best-effort. On a missing or unconfigured CLI, a timeout or
no network it returns `None`, and the caller carries on with local data; the
notifications, deadline parsing, ledger and local briefs never depend on it.

`notify-send` needs a desktop session, which cron does not have;
`U.desktop_env()` fills in `DISPLAY` and `DBUS_SESSION_BUS_ADDRESS`.

## Credentials

Credentials live in `~/.local/share/uoa-notify/credentials`, a directory
created mode 700 so that neither the secrets nor their names are readable by
another account. `bin/uoa-credentials.sh` manages it.

Four backends are tried in order, strongest first, and each may fail quietly
so that a locked store degrades to the next rather than taking a cron cycle
down with it:

| Backend | Storage | Unattended |
|---|---|---|
| keepass | KDBX4 database, AES-256 + Argon2, read with `keepassxc-cli` | yes, unlocked by a key file rather than a passphrase |
| keyring | login keyring via `secret-tool` | yes, while the session keyring is unlocked |
| gpg | GPG-encrypted file, decrypted by `gpg-agent` | only while the agent holds the passphrase |
| file | plain file, mode 600 | yes |

    uoa-credentials.sh status     where each secret currently lives
    uoa-credentials.sh store      store one, prompting without echo
    uoa-credentials.sh migrate    move the legacy dotfiles in and shred them

Legacy paths are still read when nothing is stored: `~/.uoa-mail-creds` (line 1
username, line 2 password, optional line 3 SMTP password) and `~/.eudoxus-creds`
in the same format; absent → Eudoxus checks public pages only. `~/.watch-urls`
holds optional extra pages for `check-department.py`.

Nothing is hardcoded, and a file still holding placeholder values is rejected.

What the encryption does and does not buy is worth stating plainly, because it
invites the wrong assumption: whatever an unattended cron job can decrypt with
nobody present, an attacker who already controls this account can decrypt too.
The protection is against the realistic accidents — a dotfile swept into a
backup, a synchronised home directory, an accidental commit, a stray `chmod`
that widens the mode — not against a local attacker who is already you.

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
- **Eudoxus** currently checks public pages only — run
  `uoa-credentials.sh store eudoxus` to enable the logged-in checks.
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
