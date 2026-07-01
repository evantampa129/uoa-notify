#!/usr/bin/env bash
# Shared helpers for the UoA shell scripts. Source this, don't run it.
# Everything here degrades gracefully: a missing tool or a dead network
# must never abort the calling script.

BIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$HOME/.local/log"
STATE_DIR="$HOME/.local/state"
LOG_FILE="$LOG_DIR/notifications.log"
VAULT="$HOME/Documents/ObsidianVault"
# Recipient comes from the environment or ~/.config/check-uoa-mail/config.ini;
# no address is hardcoded here.
RECIPIENT="${UOA_RECIPIENT:-}"

mkdir -p "$LOG_DIR" "$STATE_DIR" 2>/dev/null || true

# cron gives a minimal PATH; claude lives in ~/.local/bin
export PATH="$HOME/.local/bin:$HOME/bin:/usr/local/bin:/usr/bin:/bin"

log() {  # log LEVEL MESSAGE
    local level="$1"; shift
    printf '%s [%s] %s: %s\n' "$(date '+%F %T')" "${TOOL_NAME:-shell}" \
        "$level" "$*" >> "$LOG_FILE" 2>/dev/null
    [ "$level" = ERROR ] || [ "$level" = WARN ] && printf '%s: %s\n' "$level" "$*" >&2
    return 0
}

# mcp_ask TIMEOUT TOOLS PROMPT -> stdout (empty on any failure)
mcp_ask() {
    local timeout_s="$1" tools="$2" prompt="$3" out=""
    if ! command -v claude >/dev/null 2>&1; then
        log WARN "claude CLI not on PATH; skipping MCP query"
        return 1
    fi
    if ! out=$(timeout "$timeout_s" claude -p "$prompt" --allowedTools "$tools" 2>/dev/null); then
        log WARN "MCP query failed or timed out (${timeout_s}s)"
        return 1
    fi
    printf '%s' "$out"
}

# run_json TOOL ARGS... -> writes JSON to stdout, {} on failure
run_json() {
    local script="$1"; shift
    local out
    if [ ! -x "$BIN_DIR/$script" ]; then
        log WARN "$script not found or not executable"
        printf '{}'; return 1
    fi
    if ! out=$("$BIN_DIR/$script" "$@" 2>>"$LOG_FILE"); then
        log WARN "$script exited non-zero; continuing without its data"
        printf '{}'; return 1
    fi
    printf '%s' "$out"
}

have_network() { timeout 6 getent hosts mail.uoa.gr >/dev/null 2>&1; }
