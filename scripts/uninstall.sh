#!/usr/bin/env bash
# Roll back a LarkBridge boot provisioning transaction.
set -euo pipefail

STATE_ROOT="${BRIDGE_BOOT_STATE_ROOT:-/var/lib/rpi-lark-bridge/boot-transactions}"
BOOT_ONLY=0
TRANSACTION=""

while [ "$#" -gt 0 ]; do
    case "$1" in
        --boot-only) BOOT_ONLY=1 ;;
        --rollback) shift; TRANSACTION="${1:?--rollback requires a transaction ID}" ;;
        -h|--help)
            printf 'usage: sudo %s --boot-only [--rollback TRANSACTION_ID]\n' "$0"; exit 0 ;;
        *) exit 2 ;;
    esac
    shift
done

[ "$BOOT_ONLY" -eq 1 ] || { printf 'ERROR: --boot-only is required\n' >&2; exit 2; }
[ "$(id -u)" -eq 0 ] || { printf 'ERROR: must run as root\n' >&2; exit 1; }
if [ -z "$TRANSACTION" ]; then
    [ -L "$STATE_ROOT/current" ] || { printf 'ERROR: no current transaction\n' >&2; exit 1; }
    TRANSACTION="$(readlink "$STATE_ROOT/current")"
fi
exec /usr/local/lib/rpi-lark-bridge/boot-transaction.sh rollback "$TRANSACTION"
