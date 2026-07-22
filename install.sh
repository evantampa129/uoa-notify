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

ok()   { printf '  \033[32m✅\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m⚠️\033[0m  %s\n' "$*"; }
bad()  { printf '  \033[31m❌\033[0m %s\n' "$*"; }

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
    for f in "$SRC"/bin/*; do rm -f "$BIN/$(basename "$f")"; done
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
if command -v claude >/dev/null 2>&1; then
    ok "claude CLI (Gmail/Calendar/Trello/Drive access)"
else
    warn "claude CLI not on PATH — the system still runs, but Gmail read-sync,"
    warn "  Calendar events, Trello and Drive steps will be skipped"
fi
[ "$MISSING" -eq 1 ] && { echo; bad "install the missing requirements first"; exit 1; }

if [ "$MODE" = check ]; then
    echo; echo "Checking installation…"
    [ -f "$HOME/.uoa-mail-creds" ] && ok "credentials present" || bad "missing ~/.uoa-mail-creds"
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

if [ ! -f "$CFG" ]; then
    install -m 600 "$SRC/examples/config.ini.example" "$CFG"
    warn "seeded $CFG — set [notify] recipient to your email"
else
    ok "existing config left untouched"
fi

if [ ! -f "$HOME/.uoa-mail-creds" ]; then
    warn "no ~/.uoa-mail-creds yet:"
    echo "        printf '%s\\n%s\\n' USERNAME PASSWORD > ~/.uoa-mail-creds"
    echo "        chmod 600 ~/.uoa-mail-creds"
else
    chmod 600 "$HOME/.uoa-mail-creds"
    ok "credentials present (mode set to 600)"
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
echo "  1. put your email in $CFG      ([notify] recipient)"
echo "  2. create ~/.uoa-mail-creds     (chmod 600)"
echo "  3. test:   $BIN/check-uoa-mail.py --test-email"
echo "  4. cron:   ./install.sh --cron"
echo "  5. verify: ./install.sh --check"
