#!/usr/bin/env bash
# uoa-credentials.sh — store, migrate and inspect the credentials the
# checkers need, without ever leaving them lying around in plain text.
#
#   uoa-credentials.sh status              show where each secret lives
#   uoa-credentials.sh store NAME          store NAME (prompts, never echoes)
#   uoa-credentials.sh migrate             move the legacy dotfiles into the
#                                          encrypted store and shred them
#   uoa-credentials.sh test NAME           prove NAME can be read back
#
# NAME is "uoa-mail" (webmail + eClass) or "eudoxus".
#
# Backends, strongest first — the reader in uoa_common.py tries them in the
# same order:
#
#   keyring  login keyring via secret-tool. Encrypted at rest, unlocked by
#            the login password, readable by cron in the same session.
#            Install with: sudo apt install libsecret-tools
#   keepass  a KeePassXC database (KDBX4, AES-256 + Argon2) unlocked by a key
#            file rather than a passphrase, so cron can open it unattended.
#            The same database opens in the KeePassXC desktop app.
#   gpg      GPG-encrypted file in the store, decrypted by gpg-agent. Works
#            unattended only while the agent still holds the passphrase.
#   file     plain file, mode 600, in a mode-700 directory. The fallback.
#
# Note on what this does and does not protect: whatever an unattended cron
# job can decrypt with nobody present, an attacker who already controls this
# account can decrypt too. The value here is against backups, synced home
# directories, accidental commits and widened file modes.

set -uo pipefail

STORE="$HOME/.local/share/uoa-notify/credentials"
SERVICE="uoa-notify"
KDBX="$STORE/uoa-notify.kdbx"
KEYFILE="$STORE/uoa-notify.keyfile"
declare -A LEGACY=([uoa-mail]="$HOME/.uoa-mail-creds" [eudoxus]="$HOME/.eudoxus-creds")

die() { printf 'error: %s\n' "$*" >&2; exit 1; }

valid_name() {
    case "$1" in uoa-mail|eudoxus) return 0 ;; *) return 1 ;; esac
}

have() { command -v "$1" >/dev/null 2>&1; }

# Pick the best backend available on this machine, unless one is forced with
# UOA_CRED_BACKEND. Reported so the user always knows what they just used.
choose_backend() {
    if [ -n "${UOA_CRED_BACKEND:-}" ]; then printf '%s' "$UOA_CRED_BACKEND"; return; fi
    if have secret-tool; then printf 'keyring'
    elif have keepassxc-cli; then printf 'keepass'
    elif have gpg && [ -n "$(gpg --list-secret-keys 2>/dev/null)" ]; then
        printf 'gpg'
    else printf 'file'; fi
}

ensure_store() {
    mkdir -p "$STORE" || die "cannot create $STORE"
    chmod 700 "$STORE" 2>/dev/null || true
}

# Create the database and its key file on first use. A key file rather than a
# passphrase is what lets cron open the database with nobody at the keyboard;
# the mode-700 directory and mode-600 key file are what keep it private.
ensure_kdbx() {
    [ -f "$KDBX" ] && [ -f "$KEYFILE" ] && return 0
    ensure_store
    if [ ! -f "$KEYFILE" ]; then
        # 256 bits of kernel entropy, written with a private umask from the
        # start so it is never briefly world-readable.
        (umask 077; head -c 32 /dev/urandom > "$KEYFILE") \
            || die "cannot create $KEYFILE"
        chmod 600 "$KEYFILE"
        printf 'created key file %s\n' "$(printf '%s' "$KEYFILE" | sed "s|$HOME|~|")"
    fi
    if [ ! -f "$KDBX" ]; then
        if ! keepassxc-cli db-create --quiet --set-key-file "$KEYFILE" \
                "$KDBX" >/dev/null 2>&1; then
            die "could not create $KDBX"
        fi
        chmod 600 "$KDBX"
        printf 'created database %s\n' "$(printf '%s' "$KDBX" | sed "s|$HOME|~|")"
    fi
    return 0
}

# ------------------------------------------------------------------- status

cmd_status() {
    printf 'Credential store: %s\n\n' "$STORE"
    printf 'Available backends:\n'
    have keepassxc-cli && printf '  keepass  yes\n' \
                       || printf '  keepass  no   (sudo apt install keepassxc)\n'
    have secret-tool && printf '  keyring  yes\n' \
                     || printf '  keyring  no   (sudo apt install libsecret-tools)\n'
    have gpg         && printf '  gpg      yes\n' \
                     || printf '  gpg      no\n'
    printf '  file     yes\n\n'
    printf 'Secrets:\n'
    for name in uoa-mail eudoxus; do
        local where="not stored"
        if have keepassxc-cli && [ -f "$KDBX" ] && [ -f "$KEYFILE" ] &&
           keepassxc-cli show --quiet --key-file "$KEYFILE" --no-password \
               -a UserName "$KDBX" "$name" >/dev/null 2>&1; then
            where="keepass $(printf '%s' "$KDBX" | sed "s|$HOME|~|") (encrypted)"
        elif have secret-tool &&
           secret-tool lookup service "$SERVICE" account "$name" >/dev/null 2>&1; then
            where="keyring (encrypted)"
        elif [ -f "$STORE/$name.gpg" ]; then
            where="gpg  $STORE/$name.gpg (encrypted)"
        elif [ -f "$STORE/$name" ]; then
            where="file $STORE/$name (plain, mode $(stat -c %a "$STORE/$name"))"
        elif [ -f "${LEGACY[$name]}" ]; then
            where="legacy $(printf '%s' "${LEGACY[$name]}" | sed "s|$HOME|~|") (plain, mode $(stat -c %a "${LEGACY[$name]}"))"
        fi
        printf '  %-10s %s\n' "$name" "$where"
    done
}

# -------------------------------------------------------------------- store

# write_secret NAME PAYLOAD — payload is username\npassword[\nsmtp-password]
write_secret() {
    local name="$1" payload="$2" backend
    backend="$(choose_backend)"
    case "$backend" in
        keepass)
            have keepassxc-cli || die "keepassxc-cli not installed"
            ensure_store
            ensure_kdbx || return 1
            # Replace rather than duplicate: keepassxc-cli has no upsert, and
            # a second entry with the same title would shadow the first.
            keepassxc-cli rm --quiet --key-file "$KEYFILE" --no-password \
                "$KDBX" "$name" >/dev/null 2>&1 || true
            local user pass
            user="$(printf '%s' "$payload" | sed -n 1p)"
            pass="$(printf '%s' "$payload" | sed -n 2p)"
            # --password-prompt reads from stdin, which keeps the password out
            # of the process table where a command-line argument would sit.
            if ! printf '%s\n' "$pass" | keepassxc-cli add --quiet \
                    --key-file "$KEYFILE" --no-password \
                    --username "$user" --password-prompt \
                    "$KDBX" "$name" >/dev/null 2>&1; then
                die "could not write $name into $KDBX"
            fi
            printf 'stored %s in %s (KDBX4, encrypted)\n' "$name" "$KDBX"
            ;;
        keyring)
            have secret-tool || die "secret-tool not installed"
            printf '%s' "$payload" | secret-tool store --label="UoA notify: $name" \
                service "$SERVICE" account "$name" \
                || die "secret-tool refused to store $name"
            printf 'stored %s in the login keyring (encrypted)\n' "$name"
            ;;
        gpg)
            have gpg || die "gpg not installed"
            ensure_store
            local out="$STORE/$name.gpg"
            # --default-recipient-self so this works with the user's own key
            # and no extra flags; the umask keeps the ciphertext private even
            # though it is already encrypted.
            if ! printf '%s' "$payload" | (umask 077; gpg --quiet --yes --encrypt \
                    --default-recipient-self --output "$out" 2>/dev/null); then
                die "gpg encryption failed — is there a secret key? (gpg --gen-key)"
            fi
            chmod 600 "$out" 2>/dev/null || true
            printf 'stored %s at %s (gpg-encrypted)\n' "$name" "$out"
            ;;
        file)
            ensure_store
            local out="$STORE/$name"
            (umask 077; printf '%s' "$payload" > "$out") || die "cannot write $out"
            chmod 600 "$out" 2>/dev/null || true
            printf 'stored %s at %s (plain, mode 600)\n' "$name" "$out"
            printf 'note: no encryption backend available — install libsecret-tools\n'
            printf '      or create a gpg key to encrypt this at rest.\n'
            ;;
        *) die "unknown backend: $backend" ;;
    esac
}

cmd_store() {
    local name="${1:-}"
    valid_name "$name" || die "usage: $0 store uoa-mail|eudoxus"
    local user pass pass2
    read -r -p "username for $name: " user
    [ -n "$user" ] || die "username cannot be empty"
    # -s keeps the password off the screen and out of the scrollback.
    read -r -s -p "password for $name: " pass; echo
    [ -n "$pass" ] || die "password cannot be empty"
    read -r -s -p "repeat password:    " pass2; echo
    [ "$pass" = "$pass2" ] || die "passwords do not match"
    write_secret "$name" "$user"$'\n'"$pass"$'\n'
    unset pass pass2
}

# ------------------------------------------------------------------ migrate

# Overwrite before unlinking so the plaintext does not survive in free blocks
# on a filesystem that would otherwise just drop the inode.
shred_file() {
    if have shred; then shred -u "$1" 2>/dev/null && return 0; fi
    dd if=/dev/urandom of="$1" bs=1 count="$(stat -c %s "$1")" conv=notrunc \
        >/dev/null 2>&1 || true
    rm -f "$1"
}

cmd_migrate() {
    local moved=0
    for name in uoa-mail eudoxus; do
        local src="${LEGACY[$name]}"
        [ -f "$src" ] || continue
        printf 'migrating %s …\n' "$(printf '%s' "$src" | sed "s|$HOME|~|")"
        write_secret "$name" "$(cat "$src")" || continue
        # Only destroy the original once the new copy reads back correctly.
        if cmd_test "$name" >/dev/null 2>&1; then
            shred_file "$src"
            printf '  original removed\n'
            moved=$((moved + 1))
        else
            printf '  read-back failed — original left in place\n'
        fi
    done
    [ "$moved" -gt 0 ] || printf 'nothing to migrate\n'
}

# --------------------------------------------------------------------- test

cmd_test() {
    local name="${1:-}"
    valid_name "$name" || die "usage: $0 test uoa-mail|eudoxus"
    BIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" \
    NAME="$name" python3 - <<'PY'
import os, sys
sys.path.insert(0, os.environ["BIN_DIR"])
import uoa_common as U
name = os.environ["NAME"]
legacy = {"uoa-mail": U.CREDS_PATH, "eudoxus": U.EUDOXUS_CREDS_PATH}[name]
lines = U.credential_lines(name, legacy, tool="uoa-credentials")
if not lines or len(lines) < 2:
    print(f"{name}: NOT readable")
    sys.exit(1)
# Never print the password itself, only enough to confirm the right entry.
print(f"{name}: readable — username {lines[0]}, password {len(lines[1])} chars")
PY
}

case "${1:-status}" in
    status)  cmd_status ;;
    store)   cmd_store "${2:-}" ;;
    migrate) cmd_migrate ;;
    test)    cmd_test "${2:-}" ;;
    -h|--help) sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//' ;;
    *) die "unknown command: $1 (try status, store, migrate, test)" ;;
esac
