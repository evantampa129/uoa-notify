# uoa-notify

Notifications for University of Athens coursework, on every device, with one
read state.

As a student at DI/NKUA you are expected to check the webmail, eClass, Eudoxus
and the department site by hand, several times a day, forever. This watches all
of them for you. When something new shows up it works out whether there is a
deadline in it (the announcements are in Greek, which is most of the difficulty),
notifies you once, and puts the deadline on your calendar.

The part I care about most: read it on your phone and your laptop stops asking.
Read it on your laptop and your phone stops asking. Nothing gets announced twice.

## How it works

Five checker scripts, one per source, each keeping its own set of things it has
already seen:

| Source | How it gets in | What it looks at |
|---|---|---|
| UoA webmail | IMAP over TLS to `mail.uoa.gr` | New mail, deadlines in the body, PDF and DOCX attachments |
| eClass | CAS single sign-on | Announcements, assignments, files, grades, per course |
| Eudoxus | Shibboleth, plus the public JSON feed | Textbook declaration and distribution periods |
| Department | Hashing the `di.uoa.gr` page and diffing | Announcements, plus anything you add to `~/.watch-urls` |
| Gmail | MCP connector | University mail that already landed in Gmail |

They all call into `uoa_common.py`, which holds the Greek parser, the ledger, the
credential lookup and the connector bridge. Everything then funnels through
`notify.py`, which is the only thing that posts a notification, forwards a mail,
writes the log or touches the ledger.

### Parsing Greek deadlines

Greek is inflected, so you cannot match whole words. Keywords are matched at word
start with no trailing boundary: `προθεσμία` has to find `προθεσμίας`, but
`εργασία` must not match inside `επεξεργασία`. That second one shipped as a bug
and there is now a test named after it.

Noise phrases get blanked before matching, whitespace-tolerantly, because
`σταθμό εργασίας` (workstation) in an IT notice was being read as coursework -
and it was doing it across a hard line wrap, which took a while to see.

When there are several dates, one next to a deadline word beats a bare date
elsewhere. Otherwise a mail's own `Ημερομηνία:` header becomes its due date,
which is wrong every single time. Whatever survives gets bucketed: urgent inside
3 days, this week inside 7, informational after that.

### One read state

Every forwarded notification carries a tag in the subject:

    UoA DEADLINE: Εργασία 3, Δομές Δεδομένων [UOA-MSG-3f8a1c9d]

That is `sha1(source|message_id)` cut to eight hex characters. It is the only
identifier that survives Gmail threading, the subject length clamp and clients
rewriting the display. `sync-read-status.py` runs every 15 minutes, matches on the
tag, and copies Gmail's read/unread verdict onto the local ledger. That is the
whole trick - Gmail is the source of truth, and both devices are just reading it.

There is one ugly detail. Gmail marks mail as already-read when the sender is one
of the account's own send-as aliases, which is exactly what is happening here
(`@uoa.gr` forwarding to the Gmail address). Left alone, every notification
arrives pre-read and you are never told about anything. So the first time sync
links a freshly forwarded message it puts the `UNREAD` label back, but only
within a 90-minute window, so a mail you genuinely opened does not get dragged
back to unread.

### Clickable notifications

Clicking a notification opens the page and marks it read, in that order, without
waiting on the network. Getting this to work took three attempts.

Clicking the *body* of a notification fires the action registered under the
reserved key `default` and nothing else. So a notification with only a named
action looks clickable and does absolutely nothing. GNOME makes this worse by
hiding named action buttons until you expand the banner, so often there is
nothing to click either.

Then: `notify-send --action` implies `--wait`, and GNOME ignores `--expire-time`,
so the process just blocks. I found 97 live helper processes, the oldest fourteen
hours old, each one holding the per-item lock and therefore permanently silencing
its own item.

Bounding the wait fixes the leak and reveals the actual problem, which is that a
notification outlives the process that posted it. GNOME keeps it in the tray after
the helper exits, so the banner is still sitting there looking clickable while the
connection that would receive the click is gone.

That is what `uoa-notifyd.py` is for. `ActionInvoked` is broadcast on the session
bus with the notification's own id, so one long-lived daemon subscribes once and
stays reachable for anything it has ever posted:

    checker -> notify.py -> notifyd (unix socket) -> Notify() -> GNOME
                                  ^                               |
                                  +-------- ActionInvoked --------+
                                  |
                                  +-> xdg-open the page
                                  +-> notify.py --mark-read (out of process)

It starts on demand from the first notification of a cron cycle and exits after
six idle hours, so there is no service file to look after. If it cannot start (no
`python3-gi`) it falls back to `notify-open.sh`, and then to a plain notification.
The click degrades, the alert does not.

### What happens to a deadline

Everything below is deduplicated, so re-running a checker does not create
anything twice:

- Google Calendar gets the deadline plus 3-day and 1-day reminders
- Attachments get filed in the Obsidian vault under `Coursework/UoA Inbox` with a
  note next to them
- A copy goes to Google Drive under `University/<sender>/`
- Trello gets a card with the due date

### Briefs

`morning-brief.sh` runs at 07:30 and leads with what is still unread, then urgent
deadlines, then the rest of the week. `weekly-review.sh` on Sunday evening breaks
down read vs unread by source and looks three weeks out. `daily-log.sh` writes the
day into the vault at 22:00. `unread-count.sh` prints a count for polybar, waybar
or i3status without hitting the network.

## Setup

You need Linux with a desktop session for the banners (the checkers themselves
run fine headless), Python 3.8+, `requests` and `beautifulsoup4`, `notify-send`
and `xdg-open`, and a UoA account - the same one gets you webmail and eClass.

`python3-gi` is optional but you want it, otherwise clicks are unreliable.
`zenity` is an optional fallback. `keepassxc-cli` is optional, for encrypted
credentials. An assistant CLI on `PATH` is optional too; without one, the Gmail,
Calendar, Drive and Trello steps are skipped and everything local still works.

```bash
git clone git@github.com:evantampa129/uoa-notify.git
cd uoa-notify
pip install -r requirements.txt

./install.sh              # copy to ~/bin, make directories, seed config
./install.sh --cron       # the same, then install the crontab
./install.sh --check      # check an existing install, change nothing
./install.sh --uninstall  # remove the crontab and the scripts
```

`install.sh` will not overwrite a config or credentials you already have.

### Credentials

They live in `~/.local/share/uoa-notify/credentials`, a directory created mode
700 so nobody else can read either the secrets or their names.

```bash
./bin/uoa-credentials.sh store uoa-mail   # webmail and eClass, prompts silently
./bin/uoa-credentials.sh store eudoxus    # optional
./bin/uoa-credentials.sh migrate          # pull in legacy dotfiles and shred them
./bin/uoa-credentials.sh status           # where each secret currently lives
```

Four backends, tried strongest first. Any of them is allowed to fail quietly, so
a locked store falls through instead of taking down a cron cycle:

| Backend | Storage | Works unattended? |
|---|---|---|
| keyring | Login keyring via `secret-tool` | Yes, while the session keyring is unlocked |
| keepass | KDBX4, AES-256 + Argon2, via `keepassxc-cli` | Yes, if unlocked by a key file rather than a passphrase |
| gpg | GPG-encrypted file via `gpg-agent` | Only while the agent still holds the passphrase |
| file | Plain file, mode 600 | Yes |

The old `~/.uoa-mail-creds` and `~/.eudoxus-creds` are still read if nothing is
stored, so an existing install keeps working.

Worth saying plainly, because encryption invites the wrong assumption: whatever an
unattended cron job can decrypt with nobody present, an attacker who already owns
the account can decrypt too. What this actually protects against is the boring
stuff - a dotfile swept into a backup, a synced home directory, an accidental
commit, a `chmod` that went too wide.

### Config

`~/.config/check-uoa-mail/config.ini`, seeded from `examples/config.ini.example`.
Only `[notify] recipient` is required.

```ini
[notify]
recipient = you@gmail.com      ; or export UOA_RECIPIENT

[deadlines]
urgent_days = 3
week_days   = 7

[agent]
cli        =                   ; assistant CLI on PATH, for the connectors
mcp_prefix =                   ; the prefix its connector tool ids share
```

`[agent]` is deliberately blank in the repo. No vendor is baked into the source
and the backend can be swapped without editing a script. Leave it empty and the
connector steps are skipped. `UOA_AGENT_CLI` and `UOA_MCP_PREFIX` work too.

## Using it

Every checker takes the same three flags:

```bash
check-uoa-mail.py              # report what it finds, change nothing
check-uoa-mail.py --notify     # notify, forward, record. this is what cron runs
check-uoa-mail.py --dry-run    # rehearse the whole thing, leave no trace
```

```bash
sync-read-status.py --json     # unread counts per source
unread-count.sh --waybar       # JSON for a status bar
unread-count.sh --by-source    # one line per source

morning-brief.sh               # the 07:30 brief, now
deadline-to-calendar.sh --title "Εργασία 3" --date 2026-09-15 \
                        --course "Δομές Δεδομένων"

uoa-credentials.sh status
notify.py --renotify-unread --source eclass
```

### Scripts

| Script | What it does |
|---|---|
| `uoa_common.py` | The shared core: config, credentials, logging, SMTP, Greek parsing, ledger, connector bridge |
| `uoa-lib.sh` | The same idea for the shell scripts |
| `notify.py` | The hub. Desktop notification, forward, log, ledger, read-back |
| `uoa-notifyd.py` | The daemon that keeps notifications clickable |
| `notify-open.sh` | Fallback helper for when the daemon cannot run |
| `uoa-sendmail.py` | SMTP send, used for forwarding |
| `check-uoa-mail.py` | The UoA IMAP mailbox |
| `check-eclass.py` | eClass announcements, assignments, files, grades |
| `check-eudoxus.py` | Textbook periods |
| `check-department.py` | `di.uoa.gr` change detection |
| `check-gmail-university.py` | University mail already in Gmail |
| `sync-read-status.py` | Cross-device read sync |
| `morning-brief.sh` | Daily brief, unread first |
| `weekly-review.sh` | Weekly summary, three weeks ahead |
| `daily-log.sh` | Writes the day into the vault |
| `deadline-to-calendar.sh` | Add a deadline by hand: calendar, Trello and note in one go |
| `unread-count.sh` | Status bar output |
| `uoa-credentials.sh` | Credential store management |
| `uoa-notify-ctl.sh` | One switch for pausing and resuming everything |

### Schedule

Installed by `./install.sh --cron` from `examples/crontab.example`.

| When | What |
|---|---|
| Every 15 min, on the quarter hour | `sync-read-status.py` |
| Every 15 min, offset by 5 | `check-uoa-mail.py` |
| Twice an hour | `check-eclass.py`, `check-gmail-university.py` |
| Every 2 hours | `check-eudoxus.py`, `check-department.py` |
| 07:30 daily | `morning-brief.sh` |
| 22:00 daily | `daily-log.sh` |
| Sunday 18:00 | `weekly-review.sh` |

Sync goes first on the quarter hour so the checkers behind it only re-notify what
is genuinely still unread.

Cron is the only scheduler I support. If an old install left systemd user timers
lying around, they fire the same checkers independently, two copies race for the
same mailbox and the same lock, and the loser shows up as a process that did
nothing. `uoa-notify-ctl.sh status` will point them out and `stop` disables them.

### Pausing everything

```bash
uoa-notify-ctl.sh status    # what is scheduled, what is running, what failed
uoa-notify-ctl.sh stop      # pause every job, stop the daemon, kill stray timers
uoa-notify-ctl.sh start     # put it back
```

`stop` rewrites the crontab in place, prefixing each line with `#DISABLED `, and
backs the old one up to `~/.local/state/crontab.uoa-notify.bak`. Nothing is
deleted so `start` is exactly reversible.

It stops new notifications and new calendar events. It does not undo what already
happened: existing events stay on the calendar and the ledger keeps its
`calendar_event_created` flags, so starting again does not recreate them.

## Development

```bash
python3 -m unittest discover -s tests -t tests
```

72 tests. No credentials, no network, no third-party packages - they cover the
Greek classifier, subject tag generation, the configurable connector backend, the
daemon and the click-to-read chain. CI runs them on 3.8 and 3.12 and shellchecks
every script.

Two bugs are pinned by name because both of them were silent: `εργασία` matching
inside `επεξεργασία`, and the workstation noise phrase getting through a hard
line wrap. Test dates are computed relative to today so they do not rot.

`docs/design.md` covers the things that are not obvious from reading the code -
why read state is shaped this way, the two feedback loops that had to be broken,
and what a notification click actually has to do.

`uoa_common.py` only imports the standard library at module level; `requests` and
`beautifulsoup4` are imported lazily inside the scrapers that need them, which is
why the tests need no dependencies.

## Things it does not do well

- **The same deadline can arrive twice.** An exam announced by mail and on eClass
  produces two calendar events under different titles, because each source only
  deduplicates against itself. Matching across sources needs fuzzy title
  comparison, and I would rather have a duplicate event than silently drop two
  genuinely different deadlines that happen to fall on the same day.
- **Eudoxus** only reads public pages unless you store credentials.
- **The morning brief takes two to four minutes**, because it runs every checker
  and several connector queries. Fine at 07:30, annoying by hand.
- **Banners need a desktop session.** Headless still forwards, logs and records -
  you just lose the popup.

## Security notes

No credential, address or token is anywhere in the source. The recipient address
and the assistant CLI are blank by default and come from local config or the
environment. The credential directory is 700 and its contents 600, encrypted
storage is preferred over plain files, and a warning is logged if a credential
file turns out to be readable by anyone else. Migration overwrites the plaintext
original before unlinking, and only after reading the encrypted copy back.

Every connector call carries an explicit tool allow-list, so no connector is ever
reachable beyond what that one step needs. The only write access Gmail is granted
is the label change that read sync depends on.

`.gitignore` covers the credential files, the local config, the state directory
and the logs.
