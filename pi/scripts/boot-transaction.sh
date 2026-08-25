#!/usr/bin/env bash
# Restore a boot provisioning transaction without requiring network access.
set -euo pipefail

STATE_ROOT="${BRIDGE_BOOT_STATE_ROOT:-/var/lib/rpi-lark-bridge/boot-transactions}"

die() { printf '[boot-transaction] ERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '[boot-transaction] %s\n' "$*" >&2; }

safe_target() {
    case "$1" in
        /etc/systemd/system/bridge-*.service|\
        /etc/systemd/system/bridge-*.timer|\
        /etc/systemd/system/NetworkManager.service.d/10-larkbridge-netplan-startup.conf|\
        /etc/systemd/system/user@1000.service.d/10-login-barrier.conf|\
        /usr/local/lib/rpi-lark-bridge/*|\
        /home/admin/.config/pipewire/pipewire.conf.d/20-bridge-endpoints.conf|\
        /home/admin/.config/pipewire/pipewire.conf.d/20-bridge-endpoints.notes.txt|\
        /home/admin/.config/wireplumber/wireplumber.conf.d/65-bridge-hfp-no-autolink.conf|\
        /boot/firmware/config.txt|\
        /etc/cloud/cloud-init.disabled|\
        /etc/NetworkManager/system-connections/*.nmconnection)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

resolve_transaction() {
    local value="$1"
    case "$value" in
        */*) transaction="$value" ;;
        *) transaction="$STATE_ROOT/$value" ;;
    esac
    [ -d "$transaction" ] || die "transaction not found: $transaction"
    [ -f "$transaction/paths.tsv" ] || die "transaction manifest missing: $transaction/paths.tsv"
}

restore_transaction() {
    local value="$1" transaction index disposition target backup current previous
    resolve_transaction "$value"
    log "restoring $transaction"

    while IFS=$'\t' read -r index disposition target; do
        [ -n "$index" ] || continue
        safe_target "$target" || die "unsafe path in transaction: $target"
        backup="$transaction/backups/$index"
        mkdir -p "$(dirname "$target")"
        rm -f -- "$target"
        if [ "$disposition" = "present" ]; then
            [ -e "$backup" ] || [ -L "$backup" ] || die "backup missing for $target"
            cp -a -- "$backup" "$target"
        elif [ "$disposition" != "absent" ]; then
            die "invalid disposition '$disposition' for $target"
        fi
    done < <(tac "$transaction/paths.tsv")

    systemctl daemon-reload
    if [ -f "$transaction/units.tsv" ]; then
        while IFS=$'\t' read -r unit enabled active; do
            case "$unit" in
                bridge-tuning.service|\
                bridge-btfw.service|\
                bridge-btwatchdog.service|\
                bridge-btwatchdog@call.service|\
                bridge-btwatchdog@output.service|\
                bridge-boot-trial-rollback.timer|\
                hciuart.service) ;;
                *) die "unsafe unit in transaction: $unit" ;;
            esac
            if [ "$enabled" = "enabled" ]; then
                systemctl enable "$unit" >/dev/null
            else
                systemctl disable "$unit" >/dev/null 2>&1 || true
            fi
            # Older manifests intentionally omit runtime state. New transactions
            # restore it so watchdog migration is reversible on install failure.
            if [ "$active" = active ]; then
                systemctl start "$unit" >/dev/null
            elif [ "$active" = inactive ]; then
                systemctl stop "$unit" >/dev/null 2>&1 || true
            fi
        done < "$transaction/units.tsv"
    fi

    printf '%s\n' "$(basename "$transaction")" > "$transaction/rolled-back"
    if [ -f "$STATE_ROOT/pending" ] &&
       [ "$(cat "$STATE_ROOT/pending")" = "$(basename "$transaction")" ]; then
        rm -f "$STATE_ROOT/pending"
    fi
    if [ -L "$STATE_ROOT/current" ]; then
        current="$(readlink "$STATE_ROOT/current")"
        if [ "$current" = "$(basename "$transaction")" ]; then
            previous=""
            [ ! -f "$transaction/previous" ] || previous="$(cat "$transaction/previous")"
            case "$previous" in
                */*|.|..) die "unsafe previous transaction pointer: $previous" ;;
            esac
            if [ -n "$previous" ]; then
                [ -d "$STATE_ROOT/$previous" ] || die "previous transaction is missing: $previous"
                ln -sfn "$previous" "$STATE_ROOT/current"
            else
                rm -f "$STATE_ROOT/current"
            fi
        fi
    fi
    log "rollback complete"
}

case "${1:-}" in
    rollback)
        [ "$#" -eq 2 ] || die "usage: $0 rollback TRANSACTION"
        restore_transaction "$2"
        ;;
    *)
        die "usage: $0 rollback TRANSACTION"
        ;;
esac
