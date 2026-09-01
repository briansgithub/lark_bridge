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
SOURCE_ROOT="${BRIDGE_SOURCE_ROOT:-/home/admin/rpi-lark-bridge}"
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

call_controller="$(cat "$transaction/call-controller" 2>/dev/null || printf onboard)"
onboard_bluetooth="$(cat "$transaction/onboard-bluetooth" 2>/dev/null || printf keep)"
if [ "$call_controller" = onboard ]; then
    verifier=/usr/local/lib/rpi-lark-bridge/set-sco-routing.sh
    grep -Fq BRIDGE_BTFW_VERIFY_ONLY_V2 "$verifier" || bad "Bluetooth verifier marker missing"
    if grep -Eq 'Write_SCO_PCM_Int_Param|0x3f[[:space:]]+0x1c' "$verifier"; then
        bad "legacy SCO controller write returned"
    else
        ok "Bluetooth verifier is read-only"
    fi
fi

if [ "$onboard_bluetooth" = disable-qualified ]; then
    if python3 /usr/local/lib/rpi-lark-bridge/onboard_bluetooth_config.py \
        --path /boot/firmware/config.txt --check >/dev/null; then
        ok "qualified onboard-Bluetooth disablement is present"
    else
        bad "qualified onboard-Bluetooth disablement is absent or ambiguous"
    fi
    if systemctl is-active --quiet hciuart.service; then
        bad "hciuart.service remains active"
    else
        ok "hciuart.service inactive"
    fi
    if systemctl is-enabled --quiet hciuart.service; then
        bad "hciuart.service remains enabled"
    else
        ok "hciuart.service disabled"
    fi
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
show_has bridge-tuning.service Before bridge-btwatchdog@call.service
show_lacks bridge-tuning.service After multi-user.target
if systemctl is-active --quiet bridge-tuning.service; then ok "bridge-tuning.service active"; else bad "bridge-tuning.service inactive"; fi

if [ "$call_controller" = usb-bt500 ]; then
    controller_report="$({
        python3 "$SOURCE_ROOT/pi/bridged/controller_roles.py" --policy final-usb status
    } 2>&1)" && {
        ok "strict USB call-controller identity is ready"
        printf '[boot-verify] controller roles: %s\n' "$controller_report"
    } || {
        bad "strict USB call-controller identity failed: $controller_report"
    }
    for unit in bridge-btwatchdog@call.service; do
        if systemctl is-active --quiet "$unit"; then ok "$unit active"; else bad "$unit inactive"; fi
        if systemctl is-enabled --quiet "$unit"; then ok "$unit enabled"; else bad "$unit disabled"; fi
    done
    call_address="$(python3 "$SOURCE_ROOT/pi/bridged/controller_roles.py" \
        --policy final-usb resolve call --field address 2>/dev/null || true)"
    call_radio="$(bluetoothctl show "$call_address" 2>/dev/null || true)"
    if grep -Fq 'Powered: yes' <<<"$call_radio"; then ok "call adapter powered"; else bad "call adapter is not powered"; fi
    if grep -Fq 'Pairable: no' <<<"$call_radio"; then ok "call adapter pairability closed"; else bad "call adapter remains pairable"; fi
    if grep -Fq 'Discoverable: no' <<<"$call_radio"; then ok "call adapter discovery closed"; else bad "call adapter remains discoverable"; fi
    if grep -Fq '0000110b-0000-1000-8000-00805f9b34fb' <<<"$call_radio"; then ok "call adapter A2DP Sink role ready"; else bad "call adapter A2DP Sink role missing"; fi
    if grep -Fq '0000111e-0000-1000-8000-00805f9b34fb' <<<"$call_radio"; then ok "call adapter Handsfree role ready"; else bad "call adapter Handsfree role missing"; fi
    watchdog_state=/run/larkbridge/bt-watchdog/call.json
    if python3 - "$watchdog_state" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    state = json.load(handle)
required = {
    "bond_state",
    "repair_state",
    "repair_trigger",
    "repair_deadline_monotonic",
    "reconnect_attempts",
    "reconnect_next_monotonic",
    "startup_phase",
    "startup_connect_attempts",
    "startup_missing_local_uuids",
}
if required - state.keys():
    raise SystemExit(1)
if state["repair_state"] in {"requested", "preparing", "pairing_window"}:
    raise SystemExit(1)
if state["bond_state"] not in {"trusted", "connected"}:
    raise SystemExit(1)
PY
    then
        ok "call watchdog bond and repair state ready"
    else
        bad "call watchdog bond or repair state is not ready"
    fi
    for unit in bridge-btfw.service bridge-btwatchdog.service bridge-btwatchdog@output.service; do
        if systemctl is-active --quiet "$unit"; then bad "$unit unexpectedly active"; else ok "$unit inactive"; fi
        if systemctl is-enabled --quiet "$unit"; then bad "$unit unexpectedly enabled"; else ok "$unit disabled"; fi
    done
else
    show_has bridge-tuning.service Before bridge-btfw.service
    show_has bridge-btfw.service After bluetooth.service
    show_has bridge-btfw.service Requires bluetooth.service
    show_has bridge-btfw.service PartOf bluetooth.service
    show_lacks bridge-btfw.service Before bridge.target
    for unit in bridge-btfw.service bridge-btwatchdog.service; do
        if systemctl is-active --quiet "$unit"; then ok "$unit active"; else bad "$unit inactive"; fi
    done
fi

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
    /etc/systemd/system/bridge-btwatchdog@.service \
    /etc/systemd/system/bridge-btwatchdog.service \
    /etc/systemd/system/bridge-storage-guard.service \
    /etc/systemd/system/bridge-pairing-seal.service \
    /etc/systemd/system/bridge-pairing-seal.timer \
    /etc/systemd/system/bridge-boot-trial-rollback.service \
    /etc/systemd/system/bridge-boot-trial-rollback.timer || bad "systemd unit verification"

[ "$fail" -eq 0 ] || exit 1
ok "boot provisioning verification complete"
