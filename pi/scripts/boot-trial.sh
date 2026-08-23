#!/usr/bin/env bash
# Arm, confirm, or automatically roll back a trial boot configuration.
set -euo pipefail

STATE_ROOT="${BRIDGE_BOOT_STATE_ROOT:-/var/lib/rpi-lark-bridge/boot-transactions}"
PENDING="$STATE_ROOT/pending"
ROLLBACK="/usr/local/lib/rpi-lark-bridge/boot-transaction.sh"

die() { printf '[boot-trial] ERROR: %s\n' "$*" >&2; exit 1; }

case "${1:-}" in
    arm)
        [ "$#" -eq 2 ] || die "usage: $0 arm TRANSACTION_ID"
        [ -d "$STATE_ROOT/$2" ] || die "transaction does not exist: $2"
        printf '%s\n' "$2" > "$PENDING"
        sync -f "$PENDING"
        systemctl enable bridge-boot-trial-rollback.timer >/dev/null
        printf '[boot-trial] armed %s\n' "$2"
        ;;
    confirm)
        if [ -f "$PENDING" ]; then
            transaction="$(cat "$PENDING")"
            rm -f "$PENDING"
            systemctl disable --now bridge-boot-trial-rollback.timer >/dev/null 2>&1 || true
            printf '[boot-trial] confirmed %s\n' "$transaction"
        else
            printf '[boot-trial] no pending transaction\n'
        fi
        ;;
    rollback-pending)
        [ -f "$PENDING" ] || { printf '[boot-trial] no pending transaction\n'; exit 0; }
        transaction="$(cat "$PENDING")"
        "$ROLLBACK" rollback "$transaction"
        systemctl disable bridge-boot-trial-rollback.timer >/dev/null 2>&1 || true
        systemctl reboot
        ;;
    status)
        if [ -f "$PENDING" ]; then cat "$PENDING"; else printf 'none\n'; fi
        ;;
    *)
        die "usage: $0 arm TRANSACTION_ID | confirm | rollback-pending | status"
        ;;
esac
