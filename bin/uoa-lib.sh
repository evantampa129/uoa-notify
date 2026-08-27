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

# cron gives a minimal PATH, and the assistant CLI normally lives in
# ~/.local/bin, so put the user's own bin directories back on it.
export PATH="$HOME/.local/bin:$HOME/bin:/usr/local/bin:/usr/bin:/bin"

CONFIG_FILE="$HOME/.config/check-uoa-mail/config.ini"

# config_get SECTION KEY -> value, empty if absent. A small ini reader keeps
# the shell scripts from paying for a python round trip just to look up two
# settings, and it is tolerant: no file, no section or no key all print
# nothing rather than failing.
config_get() {
    [ -f "$CONFIG_FILE" ] || return 0
    awk -v want="[$1]" -v key="$2" '
        /^[[:space:]]*\[/ { section = $0; sub(/[[:space:]]+$/, "", section); next }
        section == want {
            line = $0
            sub(/[;#].*$/, "", line)
            if (line ~ "^[[:space:]]*" key "[[:space:]]*=") {
                sub(/^[^=]*=[[:space:]]*/, "", line)
                sub(/[[:space:]]+$/, "", line)
                print line
                exit
            }
        }' "$CONFIG_FILE" 2>/dev/null
}

# Which assistant CLI reaches the MCP connectors, and the prefix its tool ids
# carry. Both are configuration and never hardcoded: set [agent] cli and
# [agent] mcp_prefix in the file above, or export UOA_AGENT_CLI and
# UOA_MCP_PREFIX. Unset simply means MCP-backed sections are skipped.
AGENT_CLI="${UOA_AGENT_CLI:-$(config_get agent cli)}"
MCP_PREFIX="${UOA_MCP_PREFIX:-$(config_get agent mcp_prefix)}"

# mcp_tools SERVICE NAME[,NAME...] -> comma-joined fully-qualified tool ids.
# Prints nothing when no prefix is set, which mcp_ask reads as "no MCP here".
mcp_tools() {
    local service="$1" names="$2" out="" name
    [ -n "$MCP_PREFIX" ] || return 0
    local IFS=,
    for name in $names; do
        out="${out:+$out,}${MCP_PREFIX}${service}__${name}"
    done
    printf '%s' "$out"
}

log() {  # log LEVEL MESSAGE
    local level="$1"; shift
    printf '%s [%s] %s: %s\n' "$(date '+%F %T')" "${TOOL_NAME:-shell}" \
        "$level" "$*" >> "$LOG_FILE" 2>/dev/null
    [ "$level" = ERROR ] || [ "$level" = WARN ] && printf '%s: %s\n' "$level" "$*" >&2
    return 0
}

# mcp_ask TIMEOUT TOOLS PROMPT -> stdout (empty on any failure)
#
# Every failure mode here is a warning and a non-zero return, never an abort:
# a brief that cannot reach Calendar should still go out with the sections it
# does have.
mcp_ask() {
    local timeout_s="$1" tools="$2" prompt="$3" out=""
    if [ -z "$AGENT_CLI" ]; then
        log WARN "no assistant CLI configured ([agent] cli); skipping MCP query"
        return 1
    fi
    if [ -z "$tools" ]; then
        log WARN "no MCP tool prefix configured ([agent] mcp_prefix); skipping"
        return 1
    fi
    if ! command -v "$AGENT_CLI" >/dev/null 2>&1; then
        log WARN "assistant CLI '$AGENT_CLI' not on PATH; skipping MCP query"
        return 1
    fi
    if ! out=$(timeout "$timeout_s" "$AGENT_CLI" -p "$prompt" \
                   --allowedTools "$tools" 2>/dev/null); then
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
