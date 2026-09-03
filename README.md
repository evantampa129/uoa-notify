# UoA Notify

**Cross-device academic notification system for the University of Athens**

UoA Notify watches every channel a Greek university student is expected to
check by hand — the institutional mailbox, eClass, Eudoxus, the department
site and university mail already sitting in Gmail — parses Greek deadline
language out of what it finds, and delivers one notification per item with a
single honest read state behind it. Read it on the phone and the laptop stops
asking; read it on the laptop and the phone stops asking.

Every deadline it recognises becomes a calendar event with reminders. Every
attachment lands in the vault next to a note. Nothing is ever announced twice.

Author: Evangelos Tampachaniotis
Version: 1.0.0

---

## Architecture

```
   mail.uoa.gr      eclass.uoa.gr    eudoxus.gr     di.uoa.gr      Gmail
       IMAP          CAS / SSO       Shibboleth     page hash    MCP connector
         |                |               |              |            |
         v                v               v              v            v
   +---------------------------------------------------------------------+
   |  CHECKERS - one per source, each with its own seen-set              |
   |                                                                     |
   |  check-uoa-mail.py   check-eclass.py    check-eudoxus.py            |
   |  check-department.py check-gmail-university.py                      |
   +---------------------------------------------------------------------+
                                  |
                     new items only, never re-announced
                                  v
   +---------------------------------------------------------------------+
   |  uoa_common.py - shared core                                        |
   |                                                                     |
   |   Greek deadline parser   normalise -> strip noise -> match keyword  |
   |                           -> date near keyword -> urgency bucket     |
   |   Ledger                  per-source JSON: notified / forwarded /    |
   |                           read / gmail_msg_id / calendar_created     |
   |   Credentials             keyring -> keepass -> gpg -> file          |
   |   MCP bridge              configured CLI, explicit tool allow-list   |
   +---------------------------------------------------------------------+
                                  |
                                  v
                        +--------------------+
                        |     notify.py      |  one hub, every path
                        +--------------------+
                          |       |        |
            +-------------+       |        +-------------+
            v                     v                      v
     uoa-notifyd.py        forward once to        ~/.local/log/
     desktop banner        the phone address      notifications.log
     click -> open page    [XXX-MSG-xxxxxxxx]
     click -> mark read           |
            |                     v
            |            +------------------+
            +----------->|   Gmail          |  the source of truth
                         |   UNREAD label   |  for read/unread
                         +------------------+
                                  |
                                  v
                        sync-read-status.py        every 15 minutes
                        Gmail verdict -> ledger -> re-notify what is
                                                   genuinely still unread

   Side effects, all deduplicated:
     Google Calendar    deadline event + 3-day and 1-day reminders
     Obsidian vault     attachments filed with a companion note
     Google Drive       University/<sender>/
     Trello             card per deadline
```

---

## Features

### Sources

| Source | Access | What it watches |
|---|---|---|
| UoA webmail | IMAP over TLS, `mail.uoa.gr` | New mail, deadlines in the body, PDF and DOCX attachments |
| eClass | CAS single sign-on | Announcements, assignments, files and grades, per course |
| Eudoxus | Shibboleth/SAML, public JSON feed | Textbook declaration and distribution periods |
| Department | Page-hash diffing of `di.uoa.gr` | Announcements, plus any extra URL in `~/.watch-urls` |
| Gmail | MCP connector | University mail already delivered to Gmail |

Each source keeps its own seen-set, so an announcement is announced once and
never again — including across restarts, and including when the same item is
edited upstream.

### Greek deadline parsing

Announcements are written in inflected Greek, so keywords are matched at a
word start with no trailing boundary: `προθεσμία` still finds `προθεσμίας`,
while `εργασία` cannot match inside `επεξεργασία`. Noise phrases such as
`σταθμό εργασίας` (workstation) are blanked out before matching, whitespace-
tolerantly, so a line-wrapped IT notice is not read as coursework.

A date adjacent to a deadline word beats a bare date elsewhere in the message,
which keeps a mail's own `Ημερομηνία:` header from becoming its due date.
What survives is bucketed by how close it is: urgent within 3 days, this week
within 7, otherwise informational.

### One read state across every device

Notifications are forwarded to the phone with a stable tag in the subject:

    🟡 UoA DEADLINE: Εργασία 3 — Δομές Δεδομένων [UOA-MSG-3f8a1c9d]

The tag is `sha1(source|message_id)` truncated to eight hex characters, and it
is the only identifier that survives Gmail's threading, the subject-length
clamp and any client rewriting the display. `sync-read-status.py` matches on
it every 15 minutes and copies Gmail's verdict onto the local ledger, which is
what makes reading on one device silence the other.

Gmail marks incoming mail already-read when the sender is one of the account's
own send-as aliases — which is exactly the case here, since notifications are
forwarded from the `@uoa.gr` address to the Gmail address. Left alone, every
notification would arrive pre-read and nothing would ever be notified. The
first time sync links a freshly forwarded message it puts the `UNREAD` label
back, within a 90-minute window so a mail genuinely already opened is never
dragged back to unread.

### Clickable notifications

Activating a notification opens the source page and marks the item read, in
that order and without the browser waiting on the network.

Getting that to work took three fixes, because a notification is harder to
make clickable than it looks.

Clicking the *body* of a notification fires the action registered under the
reserved key `default` and nothing else, so a notification declaring only a
named action looks clickable and does nothing at all. GNOME Shell compounds
this by hiding named action buttons until the banner is expanded, so there is
often nothing visible to click either.

`notify-send --action` also implies `--wait`, and GNOME Shell ignores
`--expire-time`, so the process blocks forever. Measured in practice: 97 live
helper processes, the oldest fourteen hours old, each holding the per-item
lock that then silenced its own item permanently.

Bounding that wait fixes the leak and exposes the real problem. **A
notification outlives the process that posted it.** GNOME keeps it in the tray
after the helper exits, so the banner is still there, still looks clickable,
and the connection that would have received the click is gone. That is why
`uoa-notifyd.py` exists: `ActionInvoked` is broadcast on the session bus with
the notification's own id, so one long-lived daemon subscribes once and stays
reachable for every notification it has ever posted, however long ago.

    checker -> notify.py -> notifyd (unix socket) -> Notify()  ->  GNOME
                                    ^                                |
                                    +---------- ActionInvoked -------+
                                    |
                                    +-> xdg-open the page
                                    +-> notify.py --mark-read (out of process)

The daemon starts on demand from the first notification of a cron cycle and
exits after six idle hours, so there is no service file to manage. If it
cannot start — no `python3-gi` — the older `notify-open.sh` helper is used
instead, and a plain notification after that. The click degrades; the alert
never does.

### Deadlines become things you can act on

| Destination | What is created | Deduplicated by |
|---|---|---|
| Google Calendar | Deadline event plus 3-day and 1-day reminder events | `calendar_created` in the ledger |
| Obsidian vault | Attachment filed under `Coursework/UoA Inbox` with a companion note | Path already present |
| Google Drive | Attachment under `University/<sender>/` | Existing file search |
| Trello | One card per deadline, with the due date set | Card search before create |

### Briefs

`morning-brief.sh` at 07:30 leads with what is still unread, then urgent
deadlines, then the week. `weekly-review.sh` on Sunday evening reports
read-versus-unread by source and looks three weeks ahead. `daily-log.sh`
writes the day into the vault at 22:00. `unread-count.sh` prints a status-bar
count for polybar, waybar or i3status without touching the network.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Language | Python 3.8+, Bash |
| Mail | `imaplib`, `smtplib`, `email` (standard library) |
| Scraping | requests 2.x, beautifulsoup4 4.x |
| Desktop | `notify-send` (libnotify), `xdg-open`, zenity fallback |
| Credentials | KeePassXC (KDBX4), libsecret, GPG, or mode-600 files |
| Connectors | Gmail, Google Calendar, Google Drive and Trello over MCP |
| Scheduling | cron |
| Tests | `unittest` (standard library), GitHub Actions on 3.8 and 3.12 |

`uoa_common.py` imports only the standard library at module level; requests
and beautifulsoup4 are imported lazily by the scrapers that need them.

---

## Requirements

- Linux with a desktop session for notifications; the checkers themselves run
  headless
- Python 3.8 or newer
- `requests` and `beautifulsoup4`
- `notify-send`, `xdg-open`; `zenity` optional as a fallback
- `python3-gi` for clickable notifications; without it clicks are unreliable
- A UoA account, used for webmail and eClass alike
- Optional: `keepassxc-cli` for encrypted credential storage
- Optional: an assistant CLI on `PATH` for the Gmail, Calendar, Drive and
  Trello connectors. Without one, everything local still works and those
  steps are skipped.

---

## Installation

```bash
git clone git@github.com:evantampa129/uoa-notify.git
cd uoa-notify

pip install -r requirements.txt

./install.sh              # copy to ~/bin, create directories, seed config
./install.sh --cron       # the above, then install the crontab
./install.sh --check      # verify an existing install, change nothing
./install.sh --uninstall  # remove the crontab and the installed scripts
```

`install.sh` never overwrites an existing config or existing credentials.

---

## Configuration

### Credentials

Credentials live in `~/.local/share/uoa-notify/credentials`, a directory
created mode 700 so that neither the secrets nor their names are readable by
any other account.

```bash
./bin/uoa-credentials.sh store uoa-mail   # webmail + eClass, prompts silently
./bin/uoa-credentials.sh store eudoxus    # optional
./bin/uoa-credentials.sh migrate          # move legacy dotfiles in and shred them
./bin/uoa-credentials.sh status           # show where each secret lives
```

Four backends are tried in order, strongest first, and any of them may fail
quietly so a locked store falls through rather than taking a cron cycle down:

| Backend | Storage | Unattended |
|---|---|---|
| keyring | Login keyring via `secret-tool` | Yes, while the session keyring is unlocked |
| keepass | KDBX4 database, AES-256 + Argon2, via `keepassxc-cli` | Yes, unlocked by a key file rather than a passphrase |
| gpg | GPG-encrypted file, decrypted by `gpg-agent` | Only while the agent holds the passphrase |
| file | Plain file, mode 600 | Yes |

The legacy `~/.uoa-mail-creds` and `~/.eudoxus-creds` are still read when
nothing is stored, so an existing install keeps working untouched.

Worth stating plainly, because encryption invites the wrong assumption:
whatever an unattended cron job can decrypt with nobody present, an attacker
who already controls the account can decrypt too. The protection is against
the realistic accidents — a dotfile swept into a backup, a synchronised home
directory, an accidental commit, a `chmod` that widens the mode.

### Settings

`~/.config/check-uoa-mail/config.ini`, seeded from
`examples/config.ini.example`. Only `[notify] recipient` is required.

```ini
[notify]
recipient = you@gmail.com      ; or export UOA_RECIPIENT

[deadlines]
urgent_days = 3
week_days   = 7

[agent]
cli        =                   ; assistant CLI on PATH, for the connectors
mcp_prefix =                   ; prefix its connector tool ids share
```

`[agent]` is blank in the repository: no vendor is baked into the source, and
the backend can be swapped without touching a script. Leave it blank and the
MCP-backed steps are skipped while everything local keeps working. It can
also be supplied as `UOA_AGENT_CLI` and `UOA_MCP_PREFIX`.

---

## Usage

Every checker takes the same three flags.

```bash
check-uoa-mail.py              # report what it finds, change nothing
check-uoa-mail.py --notify     # notify, forward and record (what cron runs)
check-uoa-mail.py --dry-run    # rehearse everything, leave no trace
```

```bash
sync-read-status.py --json     # per-source unread counts
unread-count.sh --waybar       # status-bar JSON
unread-count.sh --by-source    # one line per source

morning-brief.sh               # the 07:30 brief, on demand
deadline-to-calendar.sh --title "Εργασία 3" --date 2026-09-15 \
                        --course "Δομές Δεδομένων"

uoa-credentials.sh status      # where each secret lives
notify.py --renotify-unread --source eclass
```

---

## Scripts

| Script | What it does |
|---|---|
| `uoa_common.py` | Shared core: config, credentials, logging, SMTP, Greek parsing, ledger, MCP bridge |
| `notify.py` | Central hub. Desktop notification, forward, log, ledger, read-back |
| `uoa-notifyd.py` | Notification daemon. Keeps every notification clickable, opens the page and records the read |
| `notify-open.sh` | Fallback notification helper, used when the daemon cannot run |
| `check-uoa-mail.py` | UoA IMAP mailbox |
| `check-eclass.py` | eClass announcements, assignments, files and grades |
| `check-eudoxus.py` | Textbook periods and deadlines |
| `check-department.py` | `di.uoa.gr` change detection |
| `check-gmail-university.py` | University mail already in Gmail |
| `sync-read-status.py` | Cross-device read synchronisation |
| `morning-brief.sh` | Daily brief, unread first |
| `weekly-review.sh` | Weekly summary and three-week horizon |
| `daily-log.sh` | Writes the day into the Obsidian vault |
| `deadline-to-calendar.sh` | Manual deadline: Calendar, Trello and vault note in one command |
| `unread-count.sh` | Status-bar widget output |
| `uoa-credentials.sh` | Credential store management |
| `uoa-notify-ctl.sh` | One switch: pause or resume every scheduled job and the daemon |

---

## Schedule

Installed by `./install.sh --cron` from `examples/crontab.example`.

| When | Job |
|---|---|
| Every 15 minutes, on the quarter hour | `sync-read-status.py` |
| Every 15 minutes, offset by 5 | `check-uoa-mail.py` |
| Twice an hour | `check-eclass.py`, `check-gmail-university.py` |
| Every 2 hours | `check-eudoxus.py`, `check-department.py` |
| 07:30 daily | `morning-brief.sh` |
| 22:00 daily | `daily-log.sh` |
| Sunday 18:00 | `weekly-review.sh` |

Sync runs first on each quarter hour so the checkers that follow re-notify
only what is genuinely still unread.

Cron is the only supported scheduler. If an earlier install left systemd user
timers behind, they fire the same checkers independently: two copies race for
the same mailbox and the same lock, one loses, and the loser shows up as a
process that went nowhere. `uoa-notify-ctl.sh status` reports them and `stop`
disables them.

---

## Control

```bash
uoa-notify-ctl.sh status    # what is scheduled, what is running, what failed
uoa-notify-ctl.sh stop      # pause every job, stop the daemon, disable stray timers
uoa-notify-ctl.sh start     # resume
```

Pausing rewrites the crontab in place, prefixing each job with `#DISABLED `,
and backs the previous table up to `~/.local/state/crontab.uoa-notify.bak`.
Nothing is deleted, so `start` is exactly reversible.

Pausing stops new notifications and new calendar events. It does not remove
what has already been created: events already on the calendar stay there, and
the ledgers keep their `calendar_event_created` flags, so a later `start` does
not recreate anything it created before.

---

## Development

```bash
python3 -m unittest discover -s tests -t tests
```

72 tests, no credentials, no network and no third-party packages. They cover
the Greek classifier, subject-tag generation, the configurable MCP backend,
the notification daemon and the click-to-read chain. Continuous integration runs them on Python 3.8 and
3.12 and parses every shell script.

Two historical bugs are pinned by name, because both were silent: `εργασία`
matching inside `επεξεργασία`, and the workstation noise phrase escaping
across a hard line wrap. Test dates are computed relative to today so they do
not rot.

`docs/design.md` covers the parts that are not obvious from the code: why read
state is shaped the way it is, the two feedback loops that had to be broken,
and what the notification click actually has to do.

---

## Limitations

- **Cross-source duplicate deadlines.** The same exam can arrive by email and
  by eClass. Each source deduplicates itself, so two calendar events can
  appear for one deadline under different titles. Deduplicating across sources
  would need fuzzy title matching, which risks silently dropping genuinely
  different deadlines that fall on the same day.
- **Eudoxus** checks public pages unless credentials are stored.
- **The morning brief takes two to four minutes**, because it runs every
  checker and several connector queries. Fine at 07:30, slow by hand.
- **Notifications need a desktop session.** The checkers run headless and
  still forward, log and record; only the desktop banner is lost.

---

## Security

- No credential, address or token appears anywhere in the source. The
  recipient address and the assistant CLI are blank by default and read from
  local configuration or the environment.
- The credential directory is mode 700 and its contents mode 600, with
  encrypted storage preferred over plain files and a warning logged whenever a
  credential file is readable by anyone else.
- Migration overwrites the plaintext original before unlinking it, and only
  after the encrypted copy has been read back successfully.
- Every connector call carries an explicit tool allow-list, so no connector is
  ever reachable beyond what that step needs. The only write access granted to
  Gmail is the label change that read synchronisation depends on.
- `.gitignore` covers the credential files, the local config, the state
  directory and the logs.
