#!/usr/bin/env bash
# uoa-notify-ctl.sh — one switch for the whole system.
#
#   uoa-notify-ctl.sh status    what is scheduled, what is running, what failed
#   uoa-notify-ctl.sh stop      pause every scheduled job and stop the daemon
#   uoa-notify-ctl.sh start     resume the jobs that stop paused
#
# Why this exists: the schedule lives in two places that do not know about
# each other. The supported one is cron, installed from
# examples/crontab.example. The other is whatever systemd user timers an
# earlier install left behind — those fire the same checker independently, so
# two copies race for the same mailbox and the same lock, and one of them
# always loses. "stop" turns off both, "status" shows both.
#
# Pausing rewrites the crontab in place, prefixing each job with "#DISABLED ".
# Nothing is deleted, so "start" is exactly reversible and the schedule keeps
# its comments and its ordering.

set -uo pipefail

MARK="#DISABLED "
JOBS='\$HOME/bin/(sync-read-status|check-|morning-brief|daily-log|weekly-review)'
DAEMON="$HOME/bin/uoa-notifyd.py"
LOG="$HOME/.local/log/notifications.log"
BACKUP="$HOME/.local/state/crontab.uoa-notify.bak"

ok()   { printf '[ ok ] %s\n' "$*"; }
warn() { printf '[warn] %s\n' "$*"; }

# The crontab is only rewritten through this, so a failed edit can never leave
# the user without a schedule: the backup is written first, from the live
# table, and awk output replaces it only if awk actually succeeded.
rewrite_crontab() {  # rewrite_crontab AWK_PROGRAM
    local current new
    current=$(crontab -l 2>/dev/null) || { warn "no crontab for $USER"; return 1; }
    printf '%s\n' "$current" > "$BACKUP"
    new=$(printf '%s\n' "$current" | awk "$1") || { warn "rewrite failed"; return 1; }
    printf '%s\n' "$new" | crontab - || { warn "crontab install failed"; return 1; }
}

cmd_stop() {
    rewrite_crontab '
        $0 ~ /^'"$MARK"'/ { print; next }          # already paused
        $0 ~ /^#/         { print; next }          # comments and headers
        $0 ~ /'"$JOBS"'/  { print "'"$MARK"'" $0; next }
        { print }
    ' && ok "cron jobs paused (backup: $BACKUP)"

    # The daemon holds the click-to-open socket; without it a banner is inert.
    if [ -x "$DAEMON" ]; then
        "$DAEMON" --stop >/dev/null 2>&1 && ok "daemon stopped" || ok "daemon was not running"
    fi

    # A checker mid-run keeps notifying after the schedule is off. The bracket
    # in each pattern keeps pkill from matching this script's own command line.
    pkill -f 'sync-read-statu[s].py'  2>/dev/null
    pkill -f 'chec[k]-uoa-mail.py'    2>/dev/null
    pkill -f 'chec[k]-eclass.py'      2>/dev/null
    pkill -f 'chec[k]-eudoxus.py'     2>/dev/null
    pkill -f 'chec[k]-department.py'  2>/dev/null
    pkill -f 'chec[k]-gmail-university.py' 2>/dev/null
    ok "in-flight checkers signalled"

    # Timers from an earlier install keep running the checkers on their own.
    local unit
    while read -r unit; do
        [ -n "$unit" ] || continue
        systemctl --user stop "$unit" >/dev/null 2>&1
        systemctl --user disable "$unit" >/dev/null 2>&1
        ok "disabled stray timer $unit"
    done < <(systemctl --user list-timers --all --no-legend 2>/dev/null \
             | awk '/uoa|eclass|eudoxus/ {print $NF}' | grep -o '[^ ]*\.service' \
             | sed 's/\.service$/.timer/' | sort -u)
}

cmd_start() {
    rewrite_crontab '{ sub(/^'"$MARK"'/, ""); print }' \
        && ok "cron jobs resumed"
    # The daemon runs in the foreground by design (it owns a GLib main loop),
    # so detach it here and wait for the socket rather than for the process.
    if [ -x "$DAEMON" ]; then
        if "$DAEMON" --status >/dev/null 2>&1; then
            ok "daemon already running"
        else
            setsid "$DAEMON" >/dev/null 2>&1 &
            for _ in 1 2 3 4 5 6 7 8 9 10; do
                sleep 0.2
                "$DAEMON" --status >/dev/null 2>&1 && break
            done
            "$DAEMON" --status >/dev/null 2>&1 \
                && ok "daemon started" \
                || warn "daemon did not come up — run $DAEMON --self-test"
        fi
    fi
}

cmd_status() {
    local active paused
    active=$(crontab -l 2>/dev/null | grep -Ec "^[^#].*$JOBS")
    paused=$(crontab -l 2>/dev/null | grep -c "^$MARK")
    printf 'cron:    %s active, %s paused\n' "$active" "$paused"

    if [ -x "$DAEMON" ]; then
        printf 'daemon:  %s\n' "$("$DAEMON" --status 2>&1 | head -1)"
    fi

    local running
    running=$(pgrep -fc 'chec[k]-|sync-read-statu[s]' 2>/dev/null || true)
    printf 'running: %s checker process(es)\n' "${running:-0}"

    local timers
    timers=$(systemctl --user list-timers --all --no-legend 2>/dev/null \
             | grep -c 'uoa\|eclass\|eudoxus')
    [ "$timers" -gt 0 ] && warn "$timers systemd timer(s) also scheduled — run stop"

    if [ -f "$LOG" ]; then
        printf '\nlast errors:\n'
        grep -aE 'ERROR|WARN' "$LOG" | tail -5 || printf '  none\n'
    fi
}

case "${1:-status}" in
    stop)   cmd_stop ;;
    start)  cmd_start ;;
    status) cmd_status ;;
    *)      printf 'usage: %s {status|stop|start}\n' "${0##*/}" >&2; exit 2 ;;
esac
