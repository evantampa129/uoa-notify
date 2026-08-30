"""Shared helpers for the UoA notification scripts.

Holds config, credentials, logging, SMTP delivery, Greek/English deadline
parsing and urgency bucketing. Imported by check-uoa-mail.py and
check-eclass.py so the two agree on what "urgent" means.

No credentials are hardcoded; everything comes from ~/.uoa-mail-creds.
"""

import configparser
import email.utils
import html as html_mod
import json
import os
import re
import smtplib
import socket
import ssl
import subprocess
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage

CREDS_PATH = os.path.expanduser("~/.uoa-mail-creds")
EUDOXUS_CREDS_PATH = os.path.expanduser("~/.eudoxus-creds")

# Preferred home for credentials: one directory, mode 700, holding either
# GPG-encrypted or plain files. The legacy dotfiles above are still read when
# nothing is stored here, so an existing install keeps working untouched.
CREDS_DIR = os.path.expanduser("~/.local/share/uoa-notify/credentials")
CONFIG_PATH = os.path.expanduser("~/.config/check-uoa-mail/config.ini")
STATE_DIR = os.path.expanduser("~/.local/state")
LOG_PATH = os.path.expanduser("~/.local/log/notifications.log")

DEFAULTS = {
    "imap": {"host": "mail.uoa.gr", "port": "993", "mailbox": "INBOX", "timeout": "20"},
    # mail.uoa.gr refuses submission; smtp.uoa.gr accepts it (465 SSL / 587 STARTTLS).
    "smtp": {"host": "smtp.uoa.gr", "port": "465", "mode": "ssl", "from": "",
             "timeout": "25"},
    # Set your own address in ~/.config/check-uoa-mail/config.ini, or export
    # UOA_RECIPIENT. Deliberately empty here so no address ships in the source.
    "notify": {"recipient": ""},
    "deadlines": {"urgent_days": "3", "week_days": "7", "horizon_days": "365"},
    "eclass": {"base": "https://eclass.uoa.gr", "sso": "https://sso.uoa.gr",
               "timeout": "30"},
    # Which assistant CLI drives the MCP connectors, and how that vendor
    # namespaces its tool ids. Deliberately empty so no vendor name ships in
    # the source: set them in the local config or export UOA_AGENT_CLI /
    # UOA_MCP_PREFIX. With both unset every MCP-backed step is skipped and
    # the rest of the system runs unchanged.
    "agent": {"cli": "", "mcp_prefix": "", "timeout": "180"},
}

SECRETARY_HINTS = ("secretar", "γραμματ", "grammat")

DEADLINE_KEYWORDS = [
    # "προθεσμιες" is listed separately: the plural shifts the stem vowel
    # (προθεσμι-α -> προθεσμι-ες), so suffix-tolerant matching cannot reach it.
    "προθεσμια", "προθεσμιες", "εξεταστικη", "παραδοση", "ημερομηνια", "εργασια",
    "υποβολη", "ληξη", "εξεταση", "εξετασεις", "διαγωνισμα", "απαλλακτικη",
    "καταληκτικη", "καταληκτικο", "εγγραφη", "διεξαχθει", "θα γινει",
    "deadline", "due", "assignment", "exam", "submission", "submit",
    "closes", "expires",
]
# Words strong enough to mark a nearby date as a real deadline. Deliberately
# excludes bare "ημερομηνία", which appears in eClass mail headers.
PROXIMITY_KEYWORDS = [
    "προθεσμια", "προθεσμιες", "καταληκτικη", "καταληκτικο", "παραδοση", "εξεταση",
    "εξετασεις", "εξεταστικη", "διαγωνισμα", "υποβολη", "ληξη", "εγγραφη",
    "διεξαχθει", "απαλλακτικη", "deadline", "due", "exam", "submission",
    "submit", "closes", "expires",
]
HARD_KEYWORDS = ["προθεσμια", "εξεταστικη", "παραδοση", "deadline", "exam", "due"]
GRADE_KEYWORDS = ["βαθμολογια", "βαθμος", "βαθμοι", "βαθμολογηση", "grade", "grades",
                  "marks", "results", "αποτελεσματα"]

# Phrases that merely *contain* a deadline word without being one. Blanked out
# before matching so e.g. "σταθμό εργασίας" (workstation) in an automated NOC
# notice cannot turn a routine message into an urgent deadline.
NOISE_PHRASES = [
    "σταθμο εργασιας", "σταθμος εργασιας", "σταθμου εργασιας",
    "σταθμων εργασιας", "περιβαλλον εργασιας", "χωρο εργασιας",
    "ωρες εργασιας", "ωραριο εργασιας", "workstation", "workstations",
    "submit an electronic form", "εργασιας απο τον σταθμο",
]


_KW_CACHE = {}


def kw_hits(norm_text, keywords):
    """Keywords present in already-normalised text, matched at a word start.

    The leading boundary stops "εργασία" from matching inside "επεξεργασία"
    (Edit Profile). There is deliberately NO trailing boundary, so Greek
    inflection still matches: "προθεσμία" also finds "προθεσμίας", and
    "παράδοση" also finds "παραδόσης".
    """
    out = []
    for kw in keywords:
        rx = _KW_CACHE.get(kw)
        if rx is None:
            rx = _KW_CACHE[kw] = re.compile(
                r"(?<!\w)" + r"\s+".join(re.escape(w) for w in kw.split()))
        if rx.search(norm_text):
            out.append(kw)
    return out


def kw_present(norm_text, keywords):
    for kw in keywords:
        rx = _KW_CACHE.get(kw)
        if rx is None:
            rx = _KW_CACHE[kw] = re.compile(
                r"(?<!\w)" + r"\s+".join(re.escape(w) for w in kw.split()))
        if rx.search(norm_text):
            return True
    return False


_NOISE_RE = None


def strip_noise(norm_text):
    """Blank out noise phrases, preserving offsets so date positions hold.

    Matching is whitespace-tolerant: mail is hard-wrapped, so "σταθμό
    εργασίας" frequently arrives split across a line break.
    """
    global _NOISE_RE
    if _NOISE_RE is None:
        _NOISE_RE = re.compile(
            "|".join(r"\s+".join(re.escape(w) for w in phrase.split())
                     for phrase in sorted(NOISE_PHRASES, key=len, reverse=True)))
    return _NOISE_RE.sub(lambda m: " " * len(m.group(0)), norm_text)

GREEK_MONTHS = {
    "ιανουαριου": 1, "ιανουαριος": 1, "φεβρουαριου": 2, "φεβρουαριος": 2,
    "μαρτιου": 3, "μαρτιος": 3, "απριλιου": 4, "απριλιος": 4,
    "μαιου": 5, "μαιος": 5, "ιουνιου": 6, "ιουνιος": 6,
    "ιουλιου": 7, "ιουλιος": 7, "αυγουστου": 8, "αυγουστος": 8,
    "σεπτεμβριου": 9, "σεπτεμβριος": 9, "οκτωβριου": 10, "οκτωβριος": 10,
    "νοεμβριου": 11, "νοεμβριος": 11, "δεκεμβριου": 12, "δεκεμβριος": 12,
}
ENGLISH_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}

# Kept deliberately: these three are the priority signal itself, not
# decoration. They survive a subject line truncated on a phone screen,
# where the words after them do not.
BUCKET_ICON = {"urgent": "🔴", "week": "🟡", "info": "🟢"}
BUCKET_LABEL = {
    "urgent": "Urgent (deadlines within 3 days)",
    "week": "This week (deadlines within 7 days)",
    "info": "Informational (no deadline)",
}


# ------------------------------------------------------------------ logging

def log(tool, level, message):
    """Append one line to ~/.local/log/notifications.log; never raise."""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{stamp} [{tool}] {level.upper()}: {message}\n"
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass
    if level.lower() in ("error", "warn", "warning"):
        sys.stderr.write(line)


def die(tool, msg, code=1):
    log(tool, "error", msg)
    sys.exit(code)


# ------------------------------------------------------------ config/creds

def load_config():
    cfg = configparser.ConfigParser()
    cfg.read_dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        try:
            cfg.read(CONFIG_PATH, encoding="utf-8")
        except (configparser.Error, OSError) as exc:
            log("config", "warn", f"ignoring bad config {CONFIG_PATH}: {exc}")
    return cfg


# ------------------------------------------------------------- credentials
#
# Credentials are read through three backends, tried in this order. All of
# them are optional: with none set up the system behaves exactly as it always
# has, reading the plain mode-600 dotfile.
#
#   1. a KeePassXC database (KDBX4, AES-256 + Argon2) under CREDS_DIR, read
#      with keepassxc-cli. Unlocked by a key file rather than a passphrase so
#      cron can open it unattended, and the same database can be opened in
#      the KeePassXC desktop app. Preferred when keepassxc-cli is installed.
#   2. the login keyring, via `secret-tool` (package: libsecret-tools).
#      Encrypted at rest, unlocked once by the login password, and readable
#      by cron jobs running in the same desktop session.
#   3. a GPG-encrypted file under CREDS_DIR, decrypted through gpg-agent.
#      Unattended runs succeed while the agent still holds the passphrase; an
#      expired cache degrades to a warning and the next backend, never to a
#      crash in the middle of a cron cycle.
#   4. a plain file with mode 600, looked for in CREDS_DIR first and then at
#      the legacy path in the home directory.
#
# Worth stating the limit plainly, because encryption invites the wrong
# assumption: whatever an unattended cron job can decrypt without a human
# present, an attacker who already controls this account can decrypt too.
# What these backends actually buy is protection against the realistic
# accidents -- a dotfile swept into a backup, a synchronised home directory,
# a repository committed by mistake, a stray chmod that widens the mode --
# and not against a local attacker who is already you.

# Legacy path -> short name used for the keyring entry and the file in
# CREDS_DIR, so both backends address the same secret by the same name.
CRED_NAMES = {CREDS_PATH: "uoa-mail", EUDOXUS_CREDS_PATH: "eudoxus"}
KEYRING_SERVICE = "uoa-notify"


def credentials_dir():
    """CREDS_DIR, created mode 700 on first use. Returns None if unusable."""
    try:
        os.makedirs(CREDS_DIR, mode=0o700, exist_ok=True)
        os.chmod(CREDS_DIR, 0o700)
        return CREDS_DIR
    except OSError:
        return None


def _run_quiet(cmd, timeout=20):
    """Run a helper binary, returning stdout or None. Never raises."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


KDBX_PATH = os.path.join(CREDS_DIR, "uoa-notify.kdbx")
KDBX_KEYFILE = os.path.join(CREDS_DIR, "uoa-notify.keyfile")


def _from_keepassxc(name, tool):
    """Read one entry's UserName and Password out of the KDBX database.

    -a UserName -a Password prints exactly those two attributes, one per
    line, in that order -- which is already the two-line format every caller
    expects, so no parsing is needed. --no-password with a key file is what
    makes this work from cron with nobody at the keyboard.
    """
    from shutil import which
    if not which("keepassxc-cli"):
        return None
    if not (os.path.exists(KDBX_PATH) and os.path.exists(KDBX_KEYFILE)):
        return None
    out = _run_quiet(["keepassxc-cli", "show", "--quiet", "--show-protected",
                      "--key-file", KDBX_KEYFILE, "--no-password",
                      "-a", "UserName", "-a", "Password",
                      KDBX_PATH, name], timeout=30)
    if out and len(out.strip().splitlines()) >= 2:
        log(tool, "info", f"credentials for {name} read from {KDBX_PATH}")
        return out
    if out is None:
        log(tool, "warn", f"keepassxc-cli could not open {KDBX_PATH}")
    return None


def _from_keyring(name, tool):
    """Secret stored as one blob under service=uoa-notify, account=<name>."""
    from shutil import which
    if not which("secret-tool"):
        return None
    out = _run_quiet(["secret-tool", "lookup",
                      "service", KEYRING_SERVICE, "account", name])
    if out:
        log(tool, "info", f"credentials for {name} read from the login keyring")
        return out
    return None


def _from_gpg(name, tool):
    """Decrypt CREDS_DIR/<name>.gpg with the user's gpg-agent."""
    from shutil import which
    path = os.path.join(CREDS_DIR, name + ".gpg")
    if not os.path.exists(path):
        return None
    if not which("gpg"):
        log(tool, "warn", f"{path} exists but gpg is not installed")
        return None
    # --batch keeps gpg from trying to prompt on a terminal cron does not
    # have; if the agent has no cached passphrase this fails cleanly and the
    # caller falls through to the next backend.
    out = _run_quiet(["gpg", "--quiet", "--batch", "--decrypt", path], timeout=30)
    if out:
        log(tool, "info", f"credentials for {name} decrypted from {path}")
        return out
    log(tool, "warn", f"cannot decrypt {path} (locked agent or wrong key)")
    return None


def _from_file(name, legacy_path, tool):
    """Plain file: CREDS_DIR/<name> first, then the legacy dotfile."""
    for path in (os.path.join(CREDS_DIR, name), legacy_path):
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            log(tool, "warn", f"cannot read {path}: {exc}")
            continue
        try:
            mode = os.stat(path).st_mode & 0o777
            if mode & 0o077:
                log(tool, "warn",
                    f"{path} is mode {mode:o} and readable by others; "
                    f"run 'chmod 600 {path}'")
        except OSError:
            pass
        return text
    return None


def credential_lines(name, legacy_path, tool="creds"):
    """Return the secret's non-comment lines, or None if it is not stored.

    Backends are tried strongest first and every one of them is allowed to
    fail quietly, so a locked keyring or an expired gpg-agent falls back
    instead of taking the whole cron cycle down with it.
    """
    for backend in (_from_keepassxc, _from_keyring, _from_gpg):
        text = backend(name, tool)
        if text:
            break
    else:
        text = _from_file(name, legacy_path, tool)
    if not text:
        return None
    lines = [ln.strip() for ln in text.splitlines()]
    return [ln for ln in lines if ln and not ln.startswith("#")]


def read_credentials(tool="creds", path=CREDS_PATH):
    """Return (username, password, smtp_password).

    Line 1 is the username, line 2 the password and line 3 an optional
    separate SMTP password. Fatal when nothing is stored: every caller needs
    these to do anything at all.
    """
    name = CRED_NAMES.get(path, os.path.basename(path).lstrip("."))
    lines = credential_lines(name, path, tool)
    if lines is None:
        die(tool, f"no credentials for '{name}'. Store them with "
                  f"bin/uoa-credentials.sh store {name}, or write {path} "
                  f"with the username on line 1 and the password on line 2 "
                  f"and chmod 600 it")
    if len(lines) < 2:
        die(tool, f"credentials for '{name}' need a username on line 1 "
                  f"and a password on line 2")
    user, password = lines[0], lines[1]
    if user.startswith("YOUR_") or password.startswith("YOUR_"):
        die(tool, f"credentials for '{name}' still hold placeholder values")
    return user, password, (lines[2] if len(lines) > 2 else password)


def read_optional_credentials(path, tool="creds"):
    """Same as read_credentials but returns None instead of exiting.

    Used for Eudoxus, which the specification requires to be skipped
    gracefully rather than to be a hard dependency.
    """
    name = CRED_NAMES.get(path, os.path.basename(path).lstrip("."))
    lines = credential_lines(name, path, tool)
    if not lines or len(lines) < 2:
        return None
    if lines[0].startswith("YOUR_") or lines[1].startswith("YOUR_"):
        return None
    return lines[0], lines[1]


# -------------------------------------------------------------------- state

def load_state(name):
    path = os.path.join(STATE_DIR, f"{name}.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    except OSError as exc:
        log(name, "warn", f"cannot read state: {exc}")
        return {}


def save_state(name, state):
    path = os.path.join(STATE_DIR, f"{name}.json")
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError as exc:
        log(name, "warn", f"cannot write state: {exc}")


def prune_seen(seen, days=30):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
    return {k: v for k, v in seen.items() if isinstance(v, (int, float)) and v >= cutoff}


# --------------------------------------------------------- deadline parsing

def normalize(text):
    """Lowercase + strip accents so ΠΡΟΘΕΣΜΙΑ == προθεσμία."""
    text = unicodedata.normalize("NFD", (text or "").lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def _valid(y, m, d):
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _date_candidates(text, horizon_days=365):
    """Every plausible date in text, with the offset where it was found."""
    norm = strip_noise(normalize(text))
    today = date.today()
    floor = today - timedelta(days=1)
    ceiling = today + timedelta(days=horizon_days)
    out = []

    def add(d, pos):
        if d and floor <= d <= ceiling:
            out.append({"date": d, "pos": pos})

    for m in re.finditer(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", norm):
        add(_valid(int(m.group(1)), int(m.group(2)), int(m.group(3))), m.start())

    for m in re.finditer(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})\b", norm):
        d_, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        add(_valid(y, mo, d_), m.start())

    for m in re.finditer(r"\b(\d{1,2})[/\-.](\d{1,2})\b(?![/\-.]\d)", norm):
        d_, mo = int(m.group(1)), int(m.group(2))
        cand = _valid(today.year, mo, d_)
        if cand and cand < floor:
            cand = _valid(today.year + 1, mo, d_)
        add(cand, m.start())

    months = {**GREEK_MONTHS, **ENGLISH_MONTHS}
    name_re = "|".join(sorted(months, key=len, reverse=True))
    for m in re.finditer(rf"\b(\d{{1,2}})\s+({name_re})\b(?:\s+(\d{{4}}))?", norm):
        d_, mo = int(m.group(1)), months[m.group(2)]
        y = int(m.group(3)) if m.group(3) else today.year
        cand = _valid(y, mo, d_)
        if cand and not m.group(3) and cand < floor:
            cand = _valid(y + 1, mo, d_)
        add(cand, m.start())

    return norm, out


def _time_near(norm, pos, window=90):
    """A clock time close to a date mention, e.g. '1/9/2026 και ωρα 12:00'."""
    seg = norm[pos:pos + window]
    m = re.search(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b", seg)
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else None


def find_dates(text, horizon_days=365, exclude=()):
    """Pick the most likely *deadline* date in text.

    Dates sitting next to deadline words beat bare dates, which keeps the
    message's own 'Ημερομηνία: 24/8/26' header from being read as a due date.
    """
    norm, cands = _date_candidates(text, horizon_days)
    if not cands:
        return None, None

    exclude = {d for d in exclude if d}
    for c in cands:
        ctx = norm[max(0, c["pos"] - 160):c["pos"] + 160]
        c["kw"] = kw_present(ctx, PROXIMITY_KEYWORDS)
        c["time"] = _time_near(norm, c["pos"])

    # Drop the message's own timestamp unless it carries deadline language.
    usable = [c for c in cands if c["date"] not in exclude] or cands
    keyworded = [c for c in usable if c["kw"]]
    pool = keyworded or usable

    today = date.today()
    future = [c for c in pool if c["date"] >= today]
    best = min(future or pool, key=lambda c: c["date"])
    return best["date"], best["time"]


def classify(title, body, cfg=None, exclude_dates=()):
    """Return dict with keywords, due_date, due_time, days_left, bucket."""
    cfg = cfg or load_config()
    horizon = cfg.getint("deadlines", "horizon_days")
    urgent_days = cfg.getint("deadlines", "urgent_days")
    week_days = cfg.getint("deadlines", "week_days")

    blob = f"{title}\n{body}"
    hay = strip_noise(normalize(blob))
    hits = kw_hits(hay, DEADLINE_KEYWORDS)
    grades = kw_hits(hay, GRADE_KEYWORDS)

    due, clock = (find_dates(blob, horizon, exclude_dates) if hits
                  else (None, None))
    days_left = (due - date.today()).days if due else None

    if days_left is not None:
        bucket = ("urgent" if days_left <= urgent_days
                  else "week" if days_left <= week_days else "info")
    elif any(k in hits for k in HARD_KEYWORDS):
        bucket = "week"   # deadline language, unparseable date
    else:
        bucket = "info"

    return {"keywords": hits, "grade_keywords": grades,
            "due_date": due.isoformat() if due else None, "due_time": clock,
            "days_left": days_left, "bucket": bucket}


def due_str(item):
    if not item.get("due_date"):
        return ""
    s = item["due_date"]
    if item.get("due_time"):
        s += f" {item['due_time']}"
    n = item.get("days_left")
    if n == 0:
        s += " (today!)"
    elif n == 1:
        s += " (tomorrow)"
    elif isinstance(n, int) and n > 0:
        s += f" (in {n} days)"
    return s


# ------------------------------------------------------------------- notify

def desktop_env():
    """Environment able to reach the logged-in desktop.

    cron starts with no DISPLAY and no DBUS_SESSION_BUS_ADDRESS, so
    notify-send would fail silently. Fill both in from the running session
    when they are missing.
    """
    env = dict(os.environ)
    env.setdefault("DISPLAY", ":0")
    if "DBUS_SESSION_BUS_ADDRESS" not in env:
        runtime = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        bus = os.path.join(runtime, "bus")
        if os.path.exists(bus):
            env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
        env.setdefault("XDG_RUNTIME_DIR", runtime)
    return env


NOTIFY_OPEN = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "notify-open.sh")


# ------------------------------------------------------- notification daemon
#
# Clickable notifications are handed to uoa-notifyd.py rather than posted
# directly. The reason is that a notification outlives the process that posts
# it: GNOME keeps it in its tray indefinitely, so a short-lived helper leaves
# a banner behind that still looks clickable and has nobody left listening.
# One long-lived daemon subscribes to ActionInvoked once and stays reachable
# for every notification it has ever posted.
#
# Every failure here falls back to notify-open.sh and then to a plain
# notify-send, so a missing daemon degrades the click, never the alert.

NOTIFYD = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "uoa-notifyd.py")


def notifyd_socket():
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return os.path.join(base, "uoa-notify", "notifyd.sock")


def notifyd_request(payload, timeout=5):
    """Send one request to the daemon. Returns the reply, or None."""
    import socket as _socket
    path = notifyd_socket()
    if not os.path.exists(path):
        return None
    try:
        with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(path)
            sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            data = sock.recv(4096)
        return json.loads(data.decode("utf-8") or "{}")
    except (OSError, ValueError):
        return None


def notifyd_start(tool="notify"):
    """Start the daemon if it is not already answering. Returns True if up."""
    if notifyd_request({"command": "ping"}):
        return True
    if not os.access(NOTIFYD, os.X_OK):
        return False
    try:
        subprocess.Popen([NOTIFYD], env=desktop_env(),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        log(tool, "warn", f"cannot start the notification daemon: {exc}")
        return False
    # Give it a moment to bind the socket before the first request.
    for _ in range(20):
        time.sleep(0.1)
        if notifyd_request({"command": "ping"}):
            return True
    log(tool, "warn", "notification daemon did not come up")
    return False


def notifyd_send(title, body, urgency, url, tag, source, message_id,
                 tool="notify"):
    """Post through the daemon. False means "fall back to the old path"."""
    if not notifyd_start(tool):
        return False
    reply = notifyd_request({"title": title, "body": body, "urgency": urgency,
                             "url": url, "tag": tag, "source": source,
                             "message_id": message_id})
    return bool(reply and reply.get("ok"))


def notify_send(title, body, urgency="normal", tool="notify", url="",
                icon="mail-unread", tag="", source="", message_id=""):
    """Desktop notification.

    With a `url` or an identified item, hand off to notify-open.sh: that
    helper is what makes the notification body clickable, opens the source
    page and marks the item read afterwards. It blocks waiting for the click,
    so it is detached and never joined — this function returns as soon as the
    helper is running, not when the user answers.
    """
    env = desktop_env()
    if url or tag or message_id:
        if notifyd_send(title, body, urgency, url, tag, source, message_id,
                        tool=tool):
            return True
    if (url or tag or message_id) and os.access(NOTIFY_OPEN, os.X_OK):
        try:
            subprocess.Popen([NOTIFY_OPEN, title, body, urgency, url,
                              tag or "", source or "", message_id or ""],
                             env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             start_new_session=True)
            return True
        except OSError as exc:
            log(tool, "warn", f"notify-open.sh failed ({exc}); "
                              f"falling back to plain notify-send")
    try:
        subprocess.run(["notify-send", "--app-name=UoA", f"--icon={icon}",
                        f"--urgency={urgency}", title, body],
                       check=False, timeout=10, env=env)
        return True
    except FileNotFoundError:
        log(tool, "warn", "notify-send not found; skipping desktop notification")
    except subprocess.TimeoutExpired:
        log(tool, "warn", "notify-send timed out")
    except OSError as exc:
        log(tool, "warn", f"notify-send failed: {exc}")
    return False


# --------------------------------------------------------------------- smtp

def notify_recipient(cfg, tool="mail"):
    """Where notifications are forwarded: $UOA_RECIPIENT, else the config file.

    Returns "" and logs if neither is set — nothing is hardcoded, so a fresh
    clone must be configured before it can mail anyone.
    """
    to = (os.environ.get("UOA_RECIPIENT") or "").strip()
    if not to:
        to = cfg["notify"]["recipient"].strip()
    if not to:
        log(tool, "error",
            "no notification recipient configured — set [notify] recipient in "
            f"{CONFIG_PATH} or export UOA_RECIPIENT")
    return to


def send_mail(cfg, smtp_user, smtp_password, subject, text, html=None,
              attachments=None, dry_run=False, tool="mail", recipient=None):
    """Send one message. attachments: list of (bytes, maintype, subtype, filename).
    Returns True on success; logs and returns False on any failure."""
    sender = cfg["smtp"]["from"].strip() or f"{smtp_user}@uoa.gr"
    to = recipient or notify_recipient(cfg, tool)
    if not to:
        return False

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid(domain="uoa.gr")
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    for blob, maintype, subtype, fname in (attachments or []):
        params = {"charset": "UTF-8"} if maintype == "text" else {}
        if subtype == "calendar":
            params["method"] = "PUBLISH"
        msg.add_attachment(blob, maintype=maintype, subtype=subtype,
                           filename=fname, params=params)

    if dry_run:
        print(f"--- DRY RUN: would send to {to} ---")
        print(f"From:    {sender}\nSubject: {subject}\n")
        print(text[:4000])
        for _, _, _, fname in (attachments or []):
            print(f"[attachment: {fname}]")
        print("--- end dry run ---")
        return True

    host = cfg["smtp"]["host"]
    port = cfg.getint("smtp", "port")
    mode = cfg["smtp"]["mode"].strip().lower()
    timeout = cfg.getint("smtp", "timeout")
    ctx = ssl.create_default_context()
    server = None
    try:
        if mode == "ssl":
            server = smtplib.SMTP_SSL(host, port, timeout=timeout, context=ctx)
        else:
            server = smtplib.SMTP(host, port, timeout=timeout)
            server.ehlo(); server.starttls(context=ctx); server.ehlo()
        server.login(smtp_user, smtp_password)
        server.send_message(msg)
        log(tool, "info", f"sent '{subject}' to {to}")
        return True
    except socket.gaierror:
        log(tool, "error", f"cannot resolve {host} — no internet or DNS down")
    except (socket.timeout, TimeoutError):
        log(tool, "error", f"timed out talking to {host}:{port}")
    except smtplib.SMTPAuthenticationError as exc:
        log(tool, "error", f"SMTP auth failed for {smtp_user}: {exc}")
    except smtplib.SMTPRecipientsRefused:
        log(tool, "error", f"SMTP refused recipient {to}")
    except smtplib.SMTPSenderRefused:
        log(tool, "error", f"SMTP refused sender {sender}; set [smtp] from in {CONFIG_PATH}")
    except (smtplib.SMTPException, ssl.SSLError, OSError) as exc:
        log(tool, "error", f"send failed: {type(exc).__name__}: {exc}")
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:
                pass
    return False


# ---------------------------------------------------------------------- ics

def build_ics(uid_seed, summary, due_date, due_time=None, description=""):
    """METHOD:PUBLISH VEVENT — Gmail shows an 'Add to Calendar' button."""
    due = date.fromisoformat(due_date)
    uid = re.sub(r"[^A-Za-z0-9]", "", uid_seed)[:40] or "uoa"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if due_time:
        hh, mm = (int(x) for x in due_time.split(":"))
        start = datetime(due.year, due.month, due.day, hh, mm)
        end = start + timedelta(hours=1)
        dt = [f"DTSTART;TZID=Europe/Athens:{start.strftime('%Y%m%dT%H%M%S')}",
              f"DTEND;TZID=Europe/Athens:{end.strftime('%Y%m%dT%H%M%S')}"]
    else:
        dt = [f"DTSTART;VALUE=DATE:{due.strftime('%Y%m%d')}",
              f"DTEND;VALUE=DATE:{(due + timedelta(days=1)).strftime('%Y%m%d')}"]

    def esc(s):
        return (str(s).replace("\\", "\\\\").replace(";", r"\;")
                .replace(",", r"\,").replace("\n", r"\n"))

    su, de = esc(summary[:200]), esc(description[:800])
    return "\r\n".join(["BEGIN:VCALENDAR", "VERSION:2.0",
        "PRODID:-//uoa-notify//EN", "METHOD:PUBLISH", "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT", f"UID:uoa-{uid}-{due.strftime('%Y%m%d')}@uoa-notify",
        f"DTSTAMP:{stamp}", *dt, f"SUMMARY:{su}", f"DESCRIPTION:{de}",
        "STATUS:CONFIRMED", "TRANSP:TRANSPARENT",
        "BEGIN:VALARM", "TRIGGER:-P1D", "ACTION:DISPLAY", f"DESCRIPTION:{su}", "END:VALARM",
        "BEGIN:VALARM", "TRIGGER:-PT2H", "ACTION:DISPLAY", f"DESCRIPTION:{su}", "END:VALARM",
        "END:VEVENT", "END:VCALENDAR", ""])


def esc_html(s):
    return html_mod.escape(str(s or ""))


# ------------------------------------------------- greek relative timestamps

GREEK_AMPM = {"π.μ.": 0, "πμ": 0, "μ.μ.": 12, "μμ": 12}
_DMY = re.compile(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})")


def parse_greek_stamp(text, today=None):
    """Parse eClass timestamps: 'σήμερα - 2:40 π.μ.', 'χθες - 18:05',
    '22-08-2026 - 9:15 μ.μ.'. Returns a datetime or None."""
    if not text:
        return None
    today = today or date.today()
    # eClass uses narrow/no-break spaces around the am/pm marker.
    norm = normalize(text.replace("\u202f", " ").replace("\xa0", " "))
    day = None
    if "σημερα" in norm:
        day = today
    elif "χθες" in norm:
        day = today - timedelta(days=1)
    elif "προχθες" in norm:
        day = today - timedelta(days=2)
    else:
        m = _DMY.search(norm)
        if m:
            y = int(m.group(3))
            if y < 100:
                y += 2000
            day = _valid(y, int(m.group(2)), int(m.group(1)))
        else:
            # "Τετάρτη 19 Αυγούστου 2026" — weekday, day, genitive month, year
            names = "|".join(sorted(GREEK_MONTHS, key=len, reverse=True))
            gm = re.search(rf"\b(\d{{1,2}})\s+({names})\s+(\d{{4}})\b", norm)
            if gm:
                day = _valid(int(gm.group(3)), GREEK_MONTHS[gm.group(2)],
                             int(gm.group(1)))
    if not day:
        return None

    hh = mm = 0
    t = re.search(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b", norm)
    if t:
        hh, mm = int(t.group(1)), int(t.group(2))
        # Look only after the clock, and drop the dots in "μ.μ." / "π.μ."
        suffix = norm[t.end():t.end() + 12].replace(".", "").replace(" ", "")
        if suffix.startswith("μμ") and hh < 12:
            hh += 12
        elif suffix.startswith("πμ") and hh == 12:
            hh = 0
    return datetime(day.year, day.month, day.day, hh, mm)


# -------------------------------------------------------------- mcp bridge
#
# Gmail, Google Calendar, Google Drive and Trello are not reached with their
# own API clients and their own OAuth dance. They are reached over MCP, by
# handing a single-shot prompt to an assistant CLI that already holds the
# grants for those accounts. That keeps this repository free of a second set
# of tokens to store and refresh.
#
# Which CLI, and the prefix its connector tool ids carry, are configuration
# rather than constants -- see the [agent] section in DEFAULTS above. Nothing
# here depends on a particular vendor, and nothing here fails when the CLI is
# absent: every call returns None and every caller treats None as "skip this
# section", which is also what happens offline.

MCP_TIMEOUT = 180


def agent_cli(cfg=None):
    """Name of the assistant CLI, or "" when none is configured."""
    env = os.environ.get("UOA_AGENT_CLI", "").strip()
    if env:
        return env
    cfg = cfg or load_config()
    return cfg.get("agent", "cli", fallback="").strip()


def mcp_prefix(cfg=None):
    """Prefix shared by this vendor's MCP tool ids, e.g. "mcp_vendor_"."""
    env = os.environ.get("UOA_MCP_PREFIX", "").strip()
    if env:
        return env
    cfg = cfg or load_config()
    return cfg.get("agent", "mcp_prefix", fallback="").strip()


def mcp_tools(service, names, cfg=None):
    """Fully-qualified tool ids for one connector.

    `service` is the connector name as the vendor spells it (Gmail,
    Trello, ...) and `names` the bare tool names. Returns [] when no prefix
    is configured, which mcp_ask reads as "no MCP available".
    """
    prefix = mcp_prefix(cfg)
    return [f"{prefix}{service}__{n}" for n in names] if prefix else []


def mcp_available(cfg=None):
    """True if a CLI is configured and actually on PATH.

    Checked on every call rather than cached: cron starts with a minimal PATH
    and the CLI usually lives in ~/.local/bin.
    """
    from shutil import which
    cli = agent_cli(cfg)
    return bool(cli) and which(cli) is not None


def mcp_ask(prompt, tools, timeout=MCP_TIMEOUT, tool="mcp"):
    """Run one headless assistant turn restricted to the given MCP tools.

    Returns stdout text, or None if no CLI is configured, the CLI is missing,
    the tool list is empty, or the call fails or times out. Every caller must
    treat None as "skip this section" so the scripts keep working offline.
    """
    cfg = load_config()
    cli = agent_cli(cfg)
    if not cli:
        log(tool, "warn", "no assistant CLI configured ([agent] cli) — "
                          "skipping MCP step")
        return None
    if not tools:
        log(tool, "warn", "no MCP tool prefix configured ([agent] mcp_prefix) "
                          "— skipping MCP step")
        return None
    if not mcp_available(cfg):
        log(tool, "warn", f"assistant CLI '{cli}' not on PATH — skipping MCP step")
        return None
    cmd = [cli, "-p", prompt, "--allowedTools", ",".join(tools)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log(tool, "warn", f"MCP call timed out after {timeout}s")
        return None
    except OSError as exc:
        log(tool, "warn", f"cannot run assistant CLI '{cli}': {exc}")
        return None
    if r.returncode != 0:
        log(tool, "warn", f"MCP call failed (rc={r.returncode}): "
                          f"{(r.stderr or '').strip()[:300]}")
        return None
    return (r.stdout or "").strip()


# Connector tool names, kept bare so the vendor prefix stays configuration.
CAL_TOOL_NAMES = ["create_event", "list_events", "list_calendars"]


def cal_tools():
    return mcp_tools("Google_Calendar", CAL_TOOL_NAMES)


def calendar_add_deadline(title, due_date, due_time=None, description="",
                          source_url="", dry_run=False, tool="calendar"):
    """Create the deadline event plus 3-day and 1-day reminder events.

    Returns True if the MCP call reported success. Degrades to False (and
    logs) when offline or when the CLI is unavailable."""
    due = date.fromisoformat(due_date)
    r3 = (due - timedelta(days=3)).isoformat()
    r1 = (due - timedelta(days=1)).isoformat()
    when = (f"on {due_date} from {due_time} for one hour (Europe/Athens)"
            if due_time else f"all day on {due_date}")
    detail = (description or "").strip().replace("\n", " ")[:400]

    prompt = (
        "Create Google Calendar events on the user's primary calendar. "
        "Do not ask questions; just create them and reply with the single "
        "word DONE followed by the event titles you created.\n\n"
        f"1) Title: \"{title}\" — {when}.\n"
        f"2) Title: \"⏰ 3 days: {title}\" — all day on {r3}.\n"
        f"3) Title: \"⏰ Tomorrow: {title}\" — all day on {r1}.\n\n"
        f"Give every event this description:\n{detail}\n"
        f"Source: {source_url or 'UoA notification system'}\n"
    )
    if dry_run:
        print(f"--- DRY RUN calendar: {title} on {due_date} "
              f"(+reminders {r3}, {r1}) ---")
        return True
    out = mcp_ask(prompt, cal_tools(), tool=tool)
    if out is None:
        return False
    ok = "DONE" in out.upper() or "created" in out.lower()
    log(tool, "info" if ok else "warn",
        f"calendar '{title}' {due_date}: {out[:200]}")
    return ok


# ------------------------------------------------------------ obsidian vault

VAULT = os.path.expanduser("~/Documents/ObsidianVault")
COURSEWORK = os.path.join(VAULT, "Coursework")
ATTACH_FALLBACK = os.path.join(COURSEWORK, "UoA Inbox")

_STOPWORDS = {"και", "της", "του", "των", "στη", "στο", "the", "and", "for",
              "ι", "ιι", "iii", "i", "ii"}


def _tokens(text):
    norm = normalize(text)
    return {w for w in re.split(r"[^a-zα-ω0-9]+", norm)
            if len(w) > 3 and w not in _STOPWORDS}


def find_course_folder(*hints):
    """Best-matching Coursework/<term>/<course> folder for a subject/sender.

    Returns a path, or the shared UoA Inbox folder when nothing matches well.
    """
    want = set()
    for h in hints:
        want |= _tokens(h or "")
    if not want or not os.path.isdir(COURSEWORK):
        return ATTACH_FALLBACK

    best, best_score = None, 0
    try:
        for term in os.listdir(COURSEWORK):
            term_dir = os.path.join(COURSEWORK, term)
            if not os.path.isdir(term_dir):
                continue
            for course in os.listdir(term_dir):
                cdir = os.path.join(term_dir, course)
                if not os.path.isdir(cdir):
                    continue
                score = len(want & _tokens(course))
                if score > best_score:
                    best, best_score = cdir, score
    except OSError as exc:
        log("vault", "warn", f"cannot scan Coursework: {exc}")
        return ATTACH_FALLBACK
    return best if best_score >= 1 else ATTACH_FALLBACK


def safe_filename(name, default="file"):
    name = re.sub(r"[/\x00-\x1f]", "-", (name or "").strip())
    name = re.sub(r"\s+", " ", name).strip(". ")
    return (name or default)[:120]


def write_note(path, title, frontmatter_extra, body):
    """Write an Obsidian note with the vault's frontmatter conventions."""
    fm = {"title": f'"{title}"',
          "date": date.today().isoformat(),
          **frontmatter_extra}
    lines = ["---"] + [f"{k}: {v}" for k, v in fm.items()] + ["---", "", body]
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
        return True
    except OSError as exc:
        log("vault", "warn", f"cannot write note {path}: {exc}")
        return False


# ------------------------------------------------- forwarding subject lines

SOURCE_LABEL = {"webmail": "UoA", "eclass": "eClass", "eudoxus": "Eudoxus",
                "department": "DI.UoA", "gmail": "Gmail UNI"}

# (source, category) -> subject prefix used when forwarding to the phone.
PREFIXES = {
    ("webmail", "urgent"):    "🔴 UoA URGENT:",
    ("webmail", "deadline"):  "🟡 UoA DEADLINE:",
    ("webmail", "grade"):     "UoA GRADE:",
    ("webmail", "file"):      "UoA FILE:",
    ("webmail", "info"):      "UoA:",
    ("eclass", "grade"):      "🔴 eClass GRADE:",
    ("eclass", "urgent"):     "🔴 eClass DEADLINE:",
    ("eclass", "deadline"):   "🔴 eClass DEADLINE:",
    ("eclass", "file"):       "eClass FILE:",
    ("eclass", "info"):       "eClass:",
    ("eudoxus", "urgent"):    "🔴 Eudoxus DEADLINE:",
    ("eudoxus", "deadline"):  "🔴 Eudoxus DEADLINE:",
    ("eudoxus", "info"):      "Eudoxus:",
    ("department", "urgent"): "🔴 DI.UoA:",
    ("department", "deadline"): "🟡 DI.UoA:",
    ("department", "info"):   "DI.UoA:",
    ("gmail", "urgent"):      "🔴 Gmail UNI URGENT:",
    ("gmail", "deadline"):    "🟡 Gmail UNI:",
    ("gmail", "info"):        "Gmail UNI:",
}


def forward_prefix(source, category):
    return (PREFIXES.get((source, category))
            or PREFIXES.get((source, "info"))
            or f"{SOURCE_LABEL.get(source, source)}:")


def category_for(item):
    """Map a classified item onto a forwarding category."""
    if item.get("grade_keywords") or item.get("is_grade"):
        return "grade"
    if item.get("is_file"):
        return "file"
    bucket = item.get("bucket", "info")
    if bucket == "urgent":
        return "urgent"
    if bucket == "week" and item.get("due_date"):
        return "deadline"
    return "info"


# ------------------------------------------------------------- item ledger

LEDGER_FILES = {
    "webmail": "uoa-mail-seen",
    "eclass": "eclass-seen",
    "eudoxus": "eudoxus-seen",
    "department": "web-hashes",
    "gmail": "gmail-uni-seen",
}


def ledger_load(source):
    state = load_state(LEDGER_FILES.get(source, f"{source}-seen"))
    state.setdefault("items", {})
    return state


def ledger_save(source, state):
    save_state(LEDGER_FILES.get(source, f"{source}-seen"), state)


def ledger_record(state, message_id, **fields):
    """Create or update one tracked notification. Returns the entry."""
    entry = state["items"].setdefault(message_id, {
        "message_id": message_id,
        "first_seen": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "notified": False, "forwarded": False, "read": False,
        "calendar_event_created": False,
    })
    entry.update({k: v for k, v in fields.items() if v is not None})
    return entry


def ledger_prune(state, days=45):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    keep = {}
    for k, v in state["items"].items():
        try:
            seen = datetime.fromisoformat(v.get("first_seen", ""))
        except ValueError:
            keep[k] = v
            continue
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        if seen >= cutoff:
            keep[k] = v
    state["items"] = keep


# ------------------------------------------------------------- web watching

WEB_UA = "Mozilla/5.0 (X11; Linux x86_64) uoa-notify/1.0"
_WEB_NOISE = re.compile(r"(cookie|σύνδεση|login|menu|μενού|search|αναζήτηση|"
                        r"accessibility|facebook|twitter|instagram)", re.I)


def web_fetch(url, timeout=30, tool="web"):
    """GET a page. Returns HTML or None (logging the reason)."""
    try:
        import requests
    except ImportError:
        log(tool, "error", "python3-requests is not installed")
        return None
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": WEB_UA})
        r.raise_for_status()
        if not r.encoding or r.encoding.lower() == "iso-8859-1":
            r.encoding = r.apparent_encoding or "utf-8"
        return r.text
    except requests.exceptions.SSLError as exc:
        log(tool, "warn", f"TLS error for {url}: {exc}")
    except requests.exceptions.ConnectionError:
        log(tool, "warn", f"cannot reach {url} — offline or DNS failure")
    except requests.exceptions.Timeout:
        log(tool, "warn", f"timed out fetching {url}")
    except requests.exceptions.RequestException as exc:
        log(tool, "warn", f"failed fetching {url}: {exc}")
    return None


def web_items(html, base_url, min_len=25):
    """Link items on a page plus a hash of its whole text."""
    import hashlib
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()

    items = {}
    for a in soup.find_all("a", href=True):
        text = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
        if len(text) < min_len or _WEB_NOISE.search(text):
            continue
        href = a["href"]
        if href.startswith("/"):
            href = re.match(r"(https?://[^/]+)", base_url).group(1) + href
        elif not href.startswith("http"):
            continue
        key = hashlib.sha1(f"{text}|{href}".encode("utf-8")).hexdigest()[:16]
        items[key] = {"text": text[:300], "url": href}

    body = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    return items, hashlib.sha256(body.encode("utf-8")).hexdigest()


def read_watch_urls(path="~/.watch-urls", tool="web"):
    """Extra URLs to watch, one per line; '#' comments allowed."""
    out = []
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    out.append(line)
    except FileNotFoundError:
        pass
    except OSError as exc:
        log(tool, "warn", f"cannot read {path}: {exc}")
    return out


def mcp_json(prompt, tools, timeout=MCP_TIMEOUT, tool="mcp", default=None):
    """Run an MCP query that must answer with JSON. Returns parsed data.

    Tolerates ``` fences and stray prose around the payload. Returns
    `default` (None unless given) when anything at all goes wrong, so callers
    degrade instead of crashing.
    """
    raw = mcp_ask(prompt + "\n\nReply with ONLY the JSON. No markdown fences, "
                           "no commentary before or after.", tools, timeout, tool)
    if not raw:
        return default
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start = min([i for i in (text.find("["), text.find("{")) if i != -1],
                default=-1)
    if start == -1:
        log(tool, "warn", f"MCP reply had no JSON: {text[:160]}")
        return default
    end = max(text.rfind("]"), text.rfind("}"))
    try:
        return json.loads(text[start:end + 1])
    except ValueError as exc:
        log(tool, "warn", f"MCP reply was not valid JSON ({exc}): {text[:160]}")
        return default


GMAIL_READ_NAMES = ["search_threads", "get_thread", "get_message"]
DRIVE_NAMES = ["create_file", "search_files", "update_file"]
TRELLO_NAMES = ["trelloSearch", "trelloReadBoard", "trelloReadList",
                "trelloWriteCard", "trelloReadMember"]


def gmail_read_tools():
    return mcp_tools("Gmail", GMAIL_READ_NAMES)


def drive_tools():
    return mcp_tools("Google_Drive", DRIVE_NAMES)


def trello_tools():
    return mcp_tools("Trello", TRELLO_NAMES)


# ------------------------------------------------- message tags & gmail ids
#
# Every forwarded notification carries a stable tag in its subject, e.g.
#     UoA DEADLINE: Εργασία 3 [UOA-MSG-3f8a1c9d]
# The tag is a pure function of (source, message_id), so the same item always
# produces the same tag — re-runs, re-sends and sync all agree on it.
# sync-read-status.py searches Gmail for these tags to learn what has been
# opened, on any device.

TAG_CODES = {"webmail": "UOA", "eclass": "ECL", "eudoxus": "EDX",
             "department": "DPT", "gmail": "GML"}
TAG_RE = re.compile(r"\[((?:UOA|ECL|EDX|DPT|GML)-MSG-[0-9a-f]{8})\]")


def make_tag(source, message_id):
    """Stable unique tag for one item. Same input -> same tag, always."""
    import hashlib
    code = TAG_CODES.get(source, "GEN")
    digest = hashlib.sha1(f"{source}|{message_id}".encode("utf-8")).hexdigest()[:8]
    return f"{code}-MSG-{digest}"


def tag_in(text):
    """Extract the first [XXX-MSG-xxxxxxxx] tag from a subject, or ''."""
    m = TAG_RE.search(text or "")
    return m.group(1) if m else ""


GMAIL_TAG_PROMPT = """Search Gmail for the automated university notifications this \
system forwards. Run these searches and combine the results:
  subject:"MSG-" newer_than:{days}d
Only keep messages whose subject contains a tag in square brackets that looks \
like [UOA-MSG-xxxxxxxx], [ECL-MSG-xxxxxxxx], [EDX-MSG-xxxxxxxx], \
[DPT-MSG-xxxxxxxx] or [GML-MSG-xxxxxxxx].

For every such message return one JSON object with exactly these keys:
  "tag"      - the tag WITHOUT the square brackets, e.g. "UOA-MSG-3f8a1c9d"
  "id"       - the Gmail message id
  "subject"  - the full subject line, verbatim
  "read"     - true if the message has been read, false if it is still unread
             (a message is unread when it carries the UNREAD label)

Return a JSON array of those objects. If there are none, return exactly: []"""


def gmail_scan_tags(days=45, tool="gmail-sync"):
    """Ask Gmail about every tagged notification in the last `days`.

    Returns {tag: {"id": gmail_msg_id, "subject": str, "read": bool}}, or
    None when Gmail could not be reached at all — callers must treat None as
    'leave read state untouched' rather than 'nothing is read'.
    """
    rows = mcp_json(GMAIL_TAG_PROMPT.format(days=days), gmail_read_tools(),
                    tool=tool, default=None)
    if rows is None:
        return None
    if not isinstance(rows, list):
        log(tool, "warn", f"unexpected Gmail reply shape: {type(rows).__name__}")
        return None
    out = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        tag = str(r.get("tag") or "").strip().strip("[]")
        if not tag:
            tag = tag_in(str(r.get("subject") or ""))
        if not tag:
            continue
        out[tag] = {"id": str(r.get("id") or "") or None,
                    "subject": str(r.get("subject") or ""),
                    "read": bool(r.get("read"))}
    return out


# --------------------------------------------------------- unread reporting

def is_real(entry):
    """True for genuine notifications, False for baselined history.

    The first run of a web checker records everything already on the page so
    it will not fire hundreds of alerts. Those entries are marked
    `baselined` and must never be counted as activity or as 'read'.
    """
    return not entry.get("baselined")


def ledger_unread(source):
    """Items forwarded but not yet read, newest first."""
    state = ledger_load(source)
    items = [e for e in state["items"].values()
             if e.get("forwarded") and not e.get("read") and is_real(e)]
    items.sort(key=lambda e: e.get("first_seen", ""), reverse=True)
    return items


def ledger_activity(source, day=None):
    """Real notifications recorded on `day` (YYYY-MM-DD), newest last."""
    items = [e for e in ledger_load(source)["items"].values()
             if is_real(e) and e.get("forwarded")
             and (day is None or (e.get("first_seen") or "")[:10] == day)]
    items.sort(key=lambda e: e.get("first_seen", ""))
    return items


def unread_counts():
    """{source: unread_count} across every ledger, plus 'total'."""
    counts, total = {}, 0
    for source in LEDGER_FILES:
        n = len(ledger_unread(source))
        counts[source] = n
        total += n
    counts["total"] = total
    return counts


SOURCE_HOME = {
    "webmail": "https://webmail.noc.uoa.gr",
    "eclass": "https://eclass.uoa.gr",
    "eudoxus": "https://eudoxus.gr",
    "department": "https://www.di.uoa.gr",
    "gmail": "https://mail.google.com",
}


def source_link(source, url=""):
    """Best link for an item: its own URL, else the source's front page."""
    return url or SOURCE_HOME.get(source, "")


# ------------------------------------------------------- forcing UNREAD
#
# Gmail marks incoming mail as already-read when the sender address is one of
# the account's own "send mail as" aliases — which is exactly our case, since
# notifications are forwarded from the @uoa.gr address to the Gmail address.
# Left alone, every notification would arrive pre-read, the unread count would
# sit at zero and nothing would ever be re-notified.
#
# So the first time sync links a freshly forwarded message, it puts the UNREAD
# label back on. From then on Gmail's own read state is authoritative and
# genuinely reflects opening the mail on the phone or the laptop.

def gmail_write_tools():
    """Read tools plus the one that changes labels — nothing wider."""
    return mcp_tools("Gmail", GMAIL_READ_NAMES + ["update_message_labels"])

# Only force UNREAD on a message we forwarded this recently, so a mail the
# user has genuinely already opened is never dragged back to unread.
FORCE_UNREAD_WINDOW_MIN = 90


def recently_forwarded(entry, minutes=FORCE_UNREAD_WINDOW_MIN):
    stamp = entry.get("forwarded_at")
    if not stamp:
        return False
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - when).total_seconds() / 60.0
    return 0 <= age <= minutes


def gmail_mark_read(msg_ids, tool="gmail-sync"):
    """Remove the UNREAD label — the counterpart of gmail_force_unread.

    Called when the item is opened from the desktop notification. Gmail is
    the declared source of truth for read state, so a local "read" that is
    never pushed here would simply be overwritten by the next sync run; this
    is what makes clicking a notification stick across every device.

    Returns the number reported updated; 0 when Gmail is unreachable, which
    the caller records as a pending push and retries later. Never raises.
    """
    ids = [m for m in dict.fromkeys(msg_ids) if m]
    if not ids:
        return 0
    listing = "\n".join(f"  - {m}" for m in ids)
    prompt = (
        "For each of these Gmail message ids, remove the UNREAD label so the "
        "message shows as read. Do not add or remove any other label and do "
        "not change anything else.\n"
        f"{listing}\n\n"
        "Reply with the single word DONE followed by how many you updated."
    )
    out = mcp_ask(prompt, gmail_write_tools(), tool=tool)
    if out is None:
        log(tool, "warn", f"could not mark {len(ids)} message(s) read")
        return 0
    ok = "DONE" in out.upper()
    log(tool, "info" if ok else "warn",
        f"marked {len(ids)} message(s) read: {out[:150]}")
    return len(ids) if ok else 0


def ledger_find_by_tag(tag):
    """Locate one entry across every source ledger by its [XXX-MSG-...] tag.

    notify-open.sh only has the tag to hand — it is the one identifier that
    appears in the notification, the forwarded subject and the ledger alike.
    Returns (source, state, entry) or (None, None, None).
    """
    for source in SOURCE_LABEL:
        state = ledger_load(source)
        for entry in state["items"].values():
            if entry.get("tag") == tag:
                return source, state, entry
    return None, None, None


def gmail_force_unread(msg_ids, tool="gmail-sync"):
    """Add the UNREAD label back to freshly delivered notifications.

    Returns the number the model reported updating; 0 when Gmail is
    unreachable. Never raises.
    """
    ids = [m for m in dict.fromkeys(msg_ids) if m]
    if not ids:
        return 0
    listing = "\n".join(f"  - {m}" for m in ids)
    prompt = (
        "For each of these Gmail message ids, add the UNREAD label so the "
        "message shows as unread. Do not remove any other label and do not "
        "change anything else.\n"
        f"{listing}\n\n"
        "Reply with the single word DONE followed by how many you updated."
    )
    out = mcp_ask(prompt, gmail_write_tools(), tool=tool)
    if out is None:
        log(tool, "warn", f"could not mark {len(ids)} message(s) unread")
        return 0
    ok = "DONE" in out.upper()
    log(tool, "info" if ok else "warn",
        f"forced UNREAD on {len(ids)} message(s): {out[:150]}")
    return len(ids) if ok else 0
