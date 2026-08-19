#!/usr/bin/env bash
# Verify deployed LarkBridge boot configuration and effective dependencies.
set -euo pipefail

BOOT_ONLY=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --boot-only) BOOT_ONLY=1 ;;
        -h|--help) printf 'usage: %s --boot-only\n' "$0"; exit 0 ;;
        *) exit 2 ;;
    esac
    shift
done
[ "$BOOT_ONLY" -eq 1 ] || { printf 'ERROR: --boot-only is required\n' >&2; exit 2; }

STATE_ROOT="${BRIDGE_BOOT_STATE_ROOT:-/var/lib/rpi-lark-bridge/boot-transactions}"
transaction="${BRIDGE_BOOT_TRANSACTION:-}"
if [ -z "$transaction" ] && [ -L "$STATE_ROOT/current" ]; then
    transaction="$STATE_ROOT/$(readlink "$STATE_ROOT/current")"
fi
[ -n "$transaction" ] && [ -f "$transaction/deployed-hashes.tsv" ] || {
    printf 'ERROR: deployed hash manifest is unavailable\n' >&2; exit 1; }

fail=0
ok() { printf '[boot-verify] PASS: %s\n' "$*"; }
bad() { printf '[boot-verify] FAIL: %s\n' "$*" >&2; fail=1; }

while IFS=$'\t' read -r expected source target; do
    [ -f "$target" ] || { bad "missing deployed file: $target"; continue; }
    actual="$(sha256sum "$target" | awk '{print $1}')"
    if [ "$actual" = "$expected" ]; then ok "hash $target"; else bad "hash mismatch $target ($source)"; fi
done < "$transaction/deployed-hashes.tsv"

verifier=/usr/local/lib/rpi-lark-bridge/set-sco-routing.sh
grep -Fq BRIDGE_BTFW_VERIFY_ONLY_V2 "$verifier" || bad "Bluetooth verifier marker missing"
if grep -Eq 'Write_SCO_PCM_Int_Param|0x3f[[:space:]]+0x1c' "$verifier"; then
    bad "legacy SCO controller write returned"
else
    ok "Bluetooth verifier is read-only"
fi

legacy=/home/admin/.config/pipewire/pipewire.conf.d/20-bridge-endpoints.conf
if [ -e "$legacy" ]; then bad "active static PipeWire endpoint file exists"; else ok "static PipeWire endpoints absent"; fi

fastpath="$(cat "$transaction/networkmanager-fastpath" 2>/dev/null || printf keep)"
fastpath_script=/usr/local/lib/rpi-lark-bridge/boot-path/netplan
fastpath_dropin=/etc/systemd/system/NetworkManager.service.d/10-larkbridge-netplan-startup.conf
case "$fastpath" in
    enable|skip)
        if [ -x "$fastpath_script" ] && [ -f "$fastpath_dropin" ]; then
            ok "NetworkManager boot-only Netplan fast path deployed"
        else
            bad "NetworkManager boot-only Netplan fast path incomplete"
        fi
        if systemctl show NetworkManager.service -p Environment --value | grep -Fq '/usr/local/lib/rpi-lark-bridge/boot-path'; then
            ok "NetworkManager boot-only path is effective for the next start"
        else
            bad "NetworkManager boot-only path is not effective"
        fi
        expected_mode=generate
        [ "$fastpath" != skip ] || expected_mode=skip-audited
        if systemctl show NetworkManager.service -p Environment --value | grep -Fq "BRIDGE_NETPLAN_STARTUP_MODE=$expected_mode"; then
            ok "NetworkManager Netplan startup mode is $expected_mode"
        else
            bad "NetworkManager Netplan startup mode is not $expected_mode"
        fi
        ;;
    disable)
        if [ ! -e "$fastpath_script" ] && [ ! -e "$fastpath_dropin" ]; then
            ok "NetworkManager boot-only Netplan fast path absent"
        else
            bad "NetworkManager boot-only Netplan fast path was not removed"
        fi
        ;;
esac

show_has() {
    local unit="$1" property="$2" value="$3" values
    values="$(systemctl show "$unit" -p "$property" --value 2>/dev/null || true)"
    if grep -qw "$value" <<<"$values"; then ok "$unit $property includes $value"; else bad "$unit $property lacks $value"; fi
}
show_lacks() {
    local unit="$1" property="$2" value="$3" values
    values="$(systemctl show "$unit" -p "$property" --value 2>/dev/null || true)"
    if grep -qw "$value" <<<"$values"; then bad "$unit $property unexpectedly includes $value"; else ok "$unit $property excludes $value"; fi
}

show_has bridge-tuning.service After systemd-modules-load.service
show_has bridge-tuning.service Before bluetooth.service
show_has bridge-tuning.service Before bridge-btfw.service
show_has bridge-tuning.service Before bridge-btwatchdog.service
show_lacks bridge-tuning.service After multi-user.target
show_has bridge-btfw.service After bluetooth.service
show_has bridge-btfw.service Requires bluetooth.service
show_has bridge-btfw.service PartOf bluetooth.service
show_lacks bridge-btfw.service Before bridge.target

for unit in bridge-tuning.service bridge-btfw.service; do
    if systemctl is-active --quiet "$unit"; then ok "$unit active"; else bad "$unit inactive"; fi
done

barrier=/etc/systemd/system/user@1000.service.d/10-login-barrier.conf
if [ -L "$barrier" ] && [ "$(readlink "$barrier")" = /dev/null ]; then
    show_lacks user@1000.service After systemd-user-sessions.service
fi

if [ -e /etc/cloud/cloud-init.disabled ]; then
    uuid="$(nmcli -t -f UUID,DEVICE connection show --active | awk -F: '$2 == "eth0" {print $1; exit}')"
    if [ -n "$uuid" ] && grep -RIl --include='*.nmconnection' -E "^uuid=${uuid}[[:space:]]*$" \
        /etc/NetworkManager/system-connections 2>/dev/null | grep -q .; then
        ok "active Ethernet profile is persistent"
    else
        bad "cloud-init disabled without a persistent active Ethernet profile"
    fi
    if systemctl is-enabled NetworkManager-wait-online.service 2>/dev/null | grep -Eq '^(enabled|disabled|static|indirect)$'; then
        ok "NetworkManager wait-online is not masked"
    else
        bad "NetworkManager wait-online is masked"
    fi
fi

systemd-analyze verify \
    /etc/systemd/system/bridge-tuning.service \
    /etc/systemd/system/bridge-btfw.service \
    /etc/systemd/system/bridge-boot-trial-rollback.service \
    /etc/systemd/system/bridge-boot-trial-rollback.timer || bad "systemd unit verification"

[ "$fail" -eq 0 ] || exit 1
ok "boot provisioning verification complete"
