#!/usr/bin/env bash
# notify-open.sh TITLE BODY URGENCY URL [TAG] [SOURCE] [MESSAGE_ID]
#
# Shows one desktop notification whose *body* is clickable: activating it runs
# xdg-open on URL — the eClass announcement, the changed department page,
# webmail, Eudoxus or the Gmail thread — and then marks the item read so it
# stops being re-notified on the next cron cycle.
#
# Two details of the freedesktop notification spec drive the whole design:
#
#   1. Clicking the *body* of a notification does not fire an arbitrary named
#      action. It fires the action registered under the reserved key
#      "default", and nothing else. A notification that only declares, say,
#      --action=open=Open therefore looks clickable and does nothing at all
#      when clicked, because no handler is bound to the body. GNOME Shell also
#      hides named action buttons until the banner is expanded, so in practice
#      there is often nothing visible to click either. Registering "default"
#      first is what makes the obvious gesture work.
#
#   2. --action implies --wait: notify-send blocks until the notification is
#      activated or closed. GNOME Shell ignores --expire-time and keeps the
#      notification in its tray indefinitely, so that wait has no natural end.
#      Left unbounded, one helper per unread item survives forever, pinning
#      the per-item lock below and silently stopping all further alerts for
#      that item. Every blocking call is therefore wrapped in `timeout`.
#
# Callers must run this detached; it is expected to outlive them.

set -uo pipefail

TITLE="${1:-UoA}"
BODY="${2:-}"
URGENCY="${3:-normal}"
URL="${4:-}"
TAG="${5:-}"
SOURCE="${6:-}"
MESSAGE_ID="${7:-}"

BIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="$HOME/.local/log/notifications.log"
mkdir -p "$HOME/.local/log" 2>/dev/null || true
log() { printf '%s [notify-open] %s: %s\n' "$(date '+%F %T')" "$1" "$2" \
        >> "$LOG_FILE" 2>/dev/null || true; }

# cron has no desktop session of its own; point at the logged-in one.
export DISPLAY="${DISPLAY:-:0}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
if [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ] && [ -S "$XDG_RUNTIME_DIR/bus" ]; then
    export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
fi

# How long to keep waiting for a click before giving up and letting the next
# cron cycle put the notification back. Critical items get the longer window.
WAIT_SECS=330
[ "$URGENCY" = critical ] && WAIT_SECS=960

# --------------------------------------------------------------- activation

open_url() {
    [ -n "$URL" ] || return 0
    if ! command -v xdg-open >/dev/null 2>&1; then
        log WARN "xdg-open missing; cannot open $URL"
        return 1
    fi
    # setsid so the browser is not killed when this helper exits, and a
    # discarded exit status so a broken handler cannot abort the read-back.
    setsid xdg-open "$URL" >/dev/null 2>&1 &
    log INFO "opened $URL"
    return 0
}

mark_read() {
    # Opening the item *is* reading it, so record that immediately: the ledger
    # flag stops the desktop alert on the very next cycle, and notify.py pushes
    # the same verdict to Gmail so every other device agrees. Backgrounded,
    # because the push is a network round trip and the browser must not wait
    # for it.
    [ -n "$TAG$MESSAGE_ID" ] || return 0
    [ -x "$BIN_DIR/notify.py" ] || { log WARN "notify.py not executable"; return 1; }
    local args=(--mark-read)
    [ -n "$SOURCE" ]     && args+=(--source "$SOURCE")
    [ -n "$MESSAGE_ID" ] && args+=(--message-id "$MESSAGE_ID")
    [ -n "$TAG" ]        && args+=(--tag "$TAG")
    setsid "$BIN_DIR/notify.py" "${args[@]}" >/dev/null 2>>"$LOG_FILE" &
    log INFO "marking read: ${TAG:-$SOURCE/$MESSAGE_ID}"
}

activate() { open_url; mark_read; }

# ------------------------------------------------------------ plain fallback

# Nothing to open and nothing to mark: a plain notification is the whole job.
if [ -z "$URL" ] && [ -z "$TAG$MESSAGE_ID" ]; then
    notify-send --app-name=UoA --icon=mail-unread --urgency="$URGENCY" \
                "$TITLE" "$BODY" 2>/dev/null
    exit 0
fi

# ------------------------------------------------------------------ one live
#
# One live notification per item. Each branch below blocks until the click or
# the timeout, and unread items are re-notified every cycle, so without this
# the same alert would stack up copy after copy.

LOCK_DIR="${XDG_RUNTIME_DIR:-/tmp}/uoa-notify"
mkdir -p "$LOCK_DIR" 2>/dev/null || true
KEY="$(printf '%s|%s' "${TAG:-$TITLE}" "$URL" | cksum | tr -d ' ')"
LOCK="$LOCK_DIR/$KEY.pid"
if [ -f "$LOCK" ]; then
    HOLDER="$(cat "$LOCK" 2>/dev/null)"
    # Only honour a lock still held by a live helper. A stale pid — from a
    # crash, a reboot, or a pid the kernel has since reused for something
    # else — must not silence this item forever.
    if [ -n "$HOLDER" ] && kill -0 "$HOLDER" 2>/dev/null &&
       grep -qa 'notify-open' "/proc/$HOLDER/cmdline" 2>/dev/null; then
        log INFO "already showing: ${TITLE:0:60}"
        exit 0
    fi
    log INFO "clearing stale lock from pid ${HOLDER:-?}"
    rm -f "$LOCK" 2>/dev/null || true
fi
echo $$ > "$LOCK" 2>/dev/null || true
trap 'rm -f "$LOCK" 2>/dev/null' EXIT

# 1. notify-send with actions (preferred: native and clickable).
if command -v notify-send >/dev/null 2>&1 && \
   notify-send --help 2>&1 | grep -q -- '--action'; then
    # "default" binds the body click; "open" adds a visible button for the
    # daemons that render one. Both lead to the same place.
    CHOICE="$(timeout "$WAIT_SECS" \
              notify-send --app-name=UoA --icon=mail-unread \
                  --urgency="$URGENCY" \
                  --action=default="Open" \
                  --action=open="Open" \
                  --action=dismiss="Dismiss" \
                  "$TITLE" "$BODY" 2>/dev/null)" || CHOICE=""
    case "$CHOICE" in
        # Named keys, plus the positional indices notify-send falls back to
        # when a daemon reports the action by number instead of by name.
        default|open|0|1) activate ;;
        "") log INFO "no response within ${WAIT_SECS}s: ${TITLE:0:60}" ;;
        *)  log INFO "dismissed: ${TITLE:0:60}" ;;
    esac
    exit 0
fi

# 2. zenity fallback for daemons without action support.
if command -v zenity >/dev/null 2>&1; then
    log INFO "notify-send lacks --action; using zenity"
    if timeout "$WAIT_SECS" zenity --question --title="$TITLE" \
              --text="$BODY"$'\n\n'"$URL" \
              --ok-label="Open" --cancel-label="Dismiss" \
              --width=460 2>/dev/null; then
        activate
    fi
    exit 0
fi

# 3. Last resort: show something rather than losing the alert entirely.
log WARN "no notify-send action support and no zenity; plain notification"
notify-send --app-name=UoA --urgency="$URGENCY" "$TITLE" "$BODY" 2>/dev/null \
    || log ERROR "no desktop notification mechanism available"
exit 0
