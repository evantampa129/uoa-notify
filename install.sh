#!/usr/bin/env bash
# install.sh — set up the UoA notification system on this machine.
#
#   ./install.sh              copy scripts to ~/bin, create dirs, seed config
#   ./install.sh --cron       the above, then install the crontab
#   ./install.sh --check      verify an existing install, change nothing
#   ./install.sh --uninstall  remove the crontab and the installed scripts
#
# Never overwrites your credentials or your existing config.ini.

set -uo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$HOME/bin"
CFG_DIR="$HOME/.config/check-uoa-mail"
CFG="$CFG_DIR/config.ini"
LOG_DIR="$HOME/.local/log"
STATE_DIR="$HOME/.local/state"
CRED_DIR="$HOME/.local/share/uoa-notify/credentials"

# Plain status markers rather than emoji: this output is read in a terminal,
# often over ssh, where a colour word beats a glyph that may not render.
ok()   { printf '  \033[32m[ ok ]\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m[warn]\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31m[fail]\033[0m %s\n' "$*"; }

MODE="install"
case "${1:-}" in
    --cron)      MODE="cron" ;;
    --check)     MODE="check" ;;
    --uninstall) MODE="uninstall" ;;
    -h|--help)
        sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    "") ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
esac

# ---------------------------------------------------------------- uninstall
if [ "$MODE" = uninstall ]; then
    echo "Removing the UoA notification system…"
    if crontab -l 2>/dev/null | grep -q "check-uoa-mail.py"; then
        crontab -l 2>/dev/null | grep -v "UoA academic notification\|check-uoa-mail.py\|check-eclass.py\|check-eudoxus.py\|check-department.py\|check-gmail-university.py\|sync-read-status.py\|morning-brief.sh\|daily-log.sh\|weekly-review.sh\|UOA_LOG=" | crontab -
        ok "crontab entries removed"
    else
        warn "no crontab entries found"
    fi
    "$BIN/uoa-notifyd.py" --stop >/dev/null 2>&1 && ok "daemon stopped"
    for f in "$SRC"/bin/*; do rm -f "$BIN/$(basename "$f")"; done
    rm -f "$HOME/.local/share/applications/uoa-notify.desktop"
    ok "scripts removed from $BIN"
    warn "left alone: $CFG_DIR, $STATE_DIR, $LOG_DIR and your credential files"
    exit 0
fi

# -------------------------------------------------------------------- check
echo "Checking prerequisites…"
MISSING=0
for c in python3 curl; do
    command -v "$c" >/dev/null 2>&1 && ok "$c" || { bad "$c is required"; MISSING=1; }
done
for c in notify-send xdg-open zenity crontab; do
    command -v "$c" >/dev/null 2>&1 && ok "$c" \
        || warn "$c not found — desktop notifications or cron may be limited"
done
python3 -c "import requests, bs4" 2>/dev/null \
    && ok "python3 requests + beautifulsoup4" \
    || { bad "pip install requests beautifulsoup4"; MISSING=1; }
# The assistant CLI that drives the MCP connectors is named in the config,
# never here, so this check reports what is configured rather than assuming.
AGENT_CLI="${UOA_AGENT_CLI:-$(sed -n '/^\[agent\]/,/^\[/p' "$CFG" 2>/dev/null \
    | sed -n 's/^[[:space:]]*cli[[:space:]]*=[[:space:]]*//p' | head -1)}"
if [ -z "$AGENT_CLI" ]; then
    warn "no assistant CLI configured ([agent] cli in $CFG)"
    warn "  the system still runs, but Gmail read-sync, Calendar events,"
    warn "  Trello cards and Drive filing will be skipped"
elif command -v "$AGENT_CLI" >/dev/null 2>&1; then
    ok "assistant CLI '$AGENT_CLI' (Gmail/Calendar/Trello/Drive access)"
else
    warn "assistant CLI '$AGENT_CLI' is configured but not on PATH"
fi

for c in keepassxc-cli secret-tool gpg; do
    command -v "$c" >/dev/null 2>&1 && ok "$c (encrypted credential storage)" \
        && break
done
[ "$MISSING" -eq 1 ] && { echo; bad "install the missing requirements first"; exit 1; }

if [ "$MODE" = check ]; then
    echo; echo "Checking installation…"
    if [ -x "$BIN/uoa-credentials.sh" ] &&
       "$BIN/uoa-credentials.sh" test uoa-mail >/dev/null 2>&1; then
        ok "credentials readable"
    else
        bad "no readable credentials (./bin/uoa-credentials.sh status)"
    fi
    [ -f "$CFG" ] && ok "config present" || bad "missing $CFG"
    [ -x "$BIN/check-uoa-mail.py" ] && ok "scripts installed" || bad "scripts not in $BIN"
    crontab -l 2>/dev/null | grep -q "check-uoa-mail.py" \
        && ok "crontab installed" || warn "crontab not installed (./install.sh --cron)"
    [ -x "$BIN/unread-count.sh" ] && echo "  unread now: $("$BIN/unread-count.sh")"
    exit 0
fi

# ------------------------------------------------------------------ install
echo; echo "Installing…"
mkdir -p "$BIN" "$CFG_DIR" "$LOG_DIR" "$STATE_DIR" || { bad "cannot create directories"; exit 1; }
ok "directories ready"

install -m 755 "$SRC"/bin/*.py "$SRC"/bin/*.sh "$BIN"/ && ok "scripts installed to $BIN"
chmod 644 "$BIN/uoa_common.py"          # imported, not executed

# A desktop entry lets GNOME group every notification under one application
# instead of showing each as an unrelated stray, and gives the daemon an
# identity to put in the desktop-entry hint.
APPS="$HOME/.local/share/applications"
mkdir -p "$APPS"
cat > "$APPS/uoa-notify.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=UoA Notify
Comment=Academic notifications for the University of Athens
Exec=xdg-open %u
Icon=mail-unread
Terminal=false
NoDisplay=true
StartupNotify=false
DESKTOP
update-desktop-database "$APPS" >/dev/null 2>&1 || true
ok "desktop entry installed"

# The daemon is what keeps notifications clickable after the process that
# posted them has exited; without pygobject it cannot run and clicks fall
# back to the older, more fragile helper.
if python3 -c "import gi" 2>/dev/null ||
   PYTHONPATH=/usr/lib/python3/dist-packages python3 -c "import gi" 2>/dev/null; then
    ok "python3-gi (clickable notifications)"
else
    warn "python3-gi missing — clicking a notification will often do nothing"
    warn "  install it with: sudo apt install python3-gi"
fi

if [ ! -f "$CFG" ]; then
    install -m 600 "$SRC/examples/config.ini.example" "$CFG"
    warn "seeded $CFG — set [notify] recipient to your email"
else
    ok "existing config left untouched"
fi

# The credential store is mode 700 and holds mode-600 files, so that neither
# the secrets nor even their names are readable by another account.
mkdir -p "$CRED_DIR" 2>/dev/null && chmod 700 "$CRED_DIR" 2>/dev/null \
    && ok "credential store ready ($(printf '%s' "$CRED_DIR" | sed "s|$HOME|~|"), mode 700)"

if "$SRC/bin/uoa-credentials.sh" test uoa-mail >/dev/null 2>&1; then
    ok "credentials readable"
    if [ -f "$HOME/.uoa-mail-creds" ]; then
        chmod 600 "$HOME/.uoa-mail-creds"
        warn "credentials are still a plain file; encrypt them with:"
        echo "        ./bin/uoa-credentials.sh migrate"
    fi
else
    warn "no credentials stored yet:"
    echo "        ./bin/uoa-credentials.sh store uoa-mail"
fi

case ":$PATH:" in
    *":$BIN:"*) ok "$BIN is on PATH" ;;
    *) warn "$BIN is not on PATH — add: export PATH=\"\$HOME/bin:\$PATH\"" ;;
esac

# --------------------------------------------------------------------- cron
if [ "$MODE" = cron ]; then
    echo; echo "Installing crontab…"
    TMP="$(mktemp)"
    # cron does not expand variables in assignment lines: PATH must be literal.
    sed "s|/home/YOUR_USER|$HOME|g" "$SRC/examples/crontab.example" > "$TMP"
    if crontab -l 2>/dev/null | grep -q "check-uoa-mail.py"; then
        warn "existing UoA entries found — replacing them"
        { crontab -l 2>/dev/null | grep -v "UoA academic notification\|bin/check-\|bin/sync-read-status\|bin/morning-brief\|bin/daily-log\|bin/weekly-review\|^UOA_LOG=\|^MAILTO=\|^SHELL=\|^PATH="; cat "$TMP"; } | crontab -
    else
        crontab "$TMP"
    fi
    rm -f "$TMP"
    crontab -l 2>/dev/null | grep -q "check-uoa-mail.py" \
        && ok "crontab installed ($(crontab -l | grep -c '\$HOME/bin/') jobs)" \
        || bad "crontab installation failed"
fi

echo
echo "Next steps:"
echo "  1. put your email in $CFG   ([notify] recipient)"
echo "  2. creds:  ./bin/uoa-credentials.sh store uoa-mail"
echo "  3. test:   $BIN/check-uoa-mail.py --test-email"
echo "  4. cron:   ./install.sh --cron"
echo "  5. verify: ./install.sh --check"
