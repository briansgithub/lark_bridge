#!/usr/bin/env bash
# bt-pin.sh - pin BlueZ to the ONBOARD controller by address (rig Pi only).
#
# Run at boot by larkbridge-rig-bt-pin.service, after bluetooth.service. The udev rule
# 70-larkbridge-rig-no-usb-bt.rules already keeps USB controllers (the USB-BT500) from
# binding; this is the second line: any controller whose address is not RIG_BT_ADDR is
# powered off and rfkill-blocked, and the unit FAILS if the pinned controller is missing,
# so selftest.sh and `systemctl --failed` show it instead of the fake car silently using
# the wrong radio.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib.sh
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/lib.sh"
rig_load_conf

ADDR="${RIG_BT_ADDR:-}"
ALIAS="${RIG_BT_ALIAS:-LarkRig Fake Car}"
is_bdaddr "$ADDR" || die "RIG_BT_ADDR is not set to a controller address in $RIG_CONF"

rfkill_ids() {   # rfkill switch numbers of one hci device
  local path
  for path in /sys/class/bluetooth/"$1"/rfkill*; do
    [ -e "$path" ] && printf '%s\n' "${path##*/rfkill}"
  done
}

deadline=$((SECONDS + 30))
pinned=""
while [ "$SECONDS" -lt "$deadline" ]; do
  pinned="$(bt_pinned_index "$ADDR" || true)"
  [ -n "$pinned" ] && break
  sleep 1
done
[ -n "$pinned" ] || die "onboard controller $ADDR not found (btmgmt info lists: $(bt_controllers | tr '\n' ' '))"

while read -r idx addr; do
  [ "$idx" = "$pinned" ] && continue
  warn "foreign controller $idx ($addr): powering off and blocking"
  btmgmt --index "${idx#hci}" power off >/dev/null 2>&1 || true
  for id in $(rfkill_ids "$idx"); do rfkill block "$id" || true; done
done < <(bt_controllers)

for id in $(rfkill_ids "$pinned"); do rfkill unblock "$id" || true; done
busctl set-property org.bluez "/org/bluez/$pinned" org.bluez.Adapter1 Alias s "$ALIAS" \
  >/dev/null 2>&1 || warn "could not set the adapter alias (bluetoothd not ready?)"
ok "pinned to $pinned ($ADDR)"
