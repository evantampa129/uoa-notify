#!/usr/bin/env bash
# notify-open.sh TITLE BODY URGENCY URL
#
# Shows a desktop notification with an "Open" action. Clicking it launches
# xdg-open on URL — the actual eClass announcement, the changed department
# page, webmail, Eudoxus or Gmail.
#
# notify-send --action implies --wait, so this script blocks until the user
# clicks or the notification expires. Callers must run it detached.
# If notify-send has no action support, fall back to zenity; if that is also
# missing, degrade to a plain notification so the alert is never lost.

set -uo pipefail

TITLE="${1:-UoA}"
BODY="${2:-}"
URGENCY="${3:-normal}"
URL="${4:-}"

LOG_FILE="$HOME/.local/log/notifications.log"
mkdir -p "$HOME/.local/log" 2>/dev/null || true
log() { printf '%s [notify-open] %s: %s\n' "$(date '+%F %T')" "$1" "$2" \
        >> "$LOG_FILE" 2>/dev/null || true; }

# cron has no desktop session; point at the logged-in one.
export DISPLAY="${DISPLAY:-:0}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
if [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ] && [ -S "$XDG_RUNTIME_DIR/bus" ]; then
    export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
fi

open_url() {
    [ -n "$URL" ] || return 0
    if command -v xdg-open >/dev/null 2>&1; then
        setsid xdg-open "$URL" >/dev/null 2>&1 &
        log INFO "opened $URL"
    else
        log WARN "xdg-open missing; cannot open $URL"
    fi
}

# No URL to open: plain notification is all that is needed.
if [ -z "$URL" ]; then
    notify-send --app-name=UoA --icon=mail-unread --urgency="$URGENCY" \
                "$TITLE" "$BODY" 2>/dev/null
    exit 0
fi

# One live notification per item. Each helper blocks until the click or the
# timeout, and unread items are re-notified every cycle, so without this the
# same alert would stack up copy after copy.
LOCK_DIR="${XDG_RUNTIME_DIR:-/tmp}/uoa-notify"
mkdir -p "$LOCK_DIR" 2>/dev/null || true
KEY="$(printf '%s|%s' "$TITLE" "$URL" | cksum | tr -d ' ')"
LOCK="$LOCK_DIR/$KEY.pid"
if [ -f "$LOCK" ] && kill -0 "$(cat "$LOCK" 2>/dev/null)" 2>/dev/null; then
    log INFO "already showing: ${TITLE:0:60}"
    exit 0
fi
echo $$ > "$LOCK" 2>/dev/null || true
trap 'rm -f "$LOCK" 2>/dev/null' EXIT

# 1. notify-send with an action (preferred: native, clickable).
if command -v notify-send >/dev/null 2>&1 && \
   notify-send --help 2>&1 | grep -q -- '--action'; then
    # Bounded on purpose: --urgency=critical would otherwise never expire, and
    # the next cron cycle re-notifies anything still unread anyway. 15 minutes
    # matches the fastest cycle, so exactly one alert per item is ever live.
    EXPIRE=300000
    [ "$URGENCY" = critical ] && EXPIRE=900000
    CHOICE="$(notify-send --app-name=UoA --icon=mail-unread \
                  --urgency="$URGENCY" --expire-time="$EXPIRE" \
                  --action=open="🔗 Open" --action=dismiss="Dismiss" \
                  "$TITLE" "$BODY" 2>/dev/null)" || CHOICE=""
    case "$CHOICE" in
        open|0) open_url ;;
        *)      log INFO "notification dismissed or expired: ${TITLE:0:60}" ;;
    esac
    exit 0
fi

# 2. zenity fallback.
if command -v zenity >/dev/null 2>&1; then
    log INFO "notify-send lacks --action; using zenity"
    if zenity --question --title="$TITLE" \
              --text="$BODY"$'\n\n'"$URL" \
              --ok-label="🔗 Open" --cancel-label="Dismiss" \
              --width=460 2>/dev/null; then
        open_url
    fi
    exit 0
fi

# 3. Last resort: at least show something.
log WARN "no notify-send action support and no zenity; plain notification"
notify-send --app-name=UoA --urgency="$URGENCY" "$TITLE" "$BODY" 2>/dev/null \
    || log ERROR "no desktop notification mechanism available"
exit 0
