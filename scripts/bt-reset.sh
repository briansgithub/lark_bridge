#!/usr/bin/env bash
# Bluetooth controller recovery ladder for the Pi 3B's BCM43438.
#
# Climbs rungs in order, stopping as soon as the controller responds. Each rung is more
# disruptive than the last. It never reboots -- that is the operator's call.
#
# WHY THIS EXISTS (observed 2026-08-16, after ~7 minutes of continuous mSBC SCO):
# the controller stopped answering HCI commands entirely.
#
#     Bluetooth: hci0: command 0x0406 tx timeout      (0x0406 = HCI_Disconnect)
#     Can't init device hci0: Connection timed out (110)
#
# RX byte counters froze while TX kept climbing -- the host was talking to something that
# had stopped listening. Rungs 1-5 (disconnect, HCI disconnect, adapter down/up, service
# restart) ALL FAILED. Only rung 6, unbinding and rebinding the serdev driver, recovered
# it -- that forces a full firmware reload:
#
#     Bluetooth: hci0: BCM43430A1 'brcm/BCM43430A1.raspberrypi,3-model-b.hcd' Patch
#
# The production Device Tree property is reapplied when the serdev driver reloads the
# controller. Recovery still runs bridge-btfw.service, but that unit is deliberately
# read-only: it refuses to continue unless both Device Tree and controller readback agree.

set -euo pipefail

HCI="${BRIDGE_HCI:-hci0}"
SERDEV="${BRIDGE_SERDEV:-serial0-0}"
DRIVER="/sys/bus/serial/drivers/hci_uart_bcm"

log()  { printf '[bt-reset] %s\n' "$*"; }
warn() { printf '[bt-reset] WARN: %s\n' "$*" >&2; }

# The only trustworthy liveness test is whether the controller ANSWERS. Counters alone
# lie: TX keeps incrementing into a dead controller.
alive() {
  local before after
  before="$(hciconfig "$HCI" 2>/dev/null | grep -oE 'RX bytes:[0-9]+' | tr -dc 0-9)" || return 1
  hciconfig "$HCI" 2>/dev/null | grep -q 'UP RUNNING' || return 1
  timeout 5 hcitool -i "$HCI" cmd 0x03 0x0014 >/dev/null 2>&1 || return 1   # Read Local Name
  sleep 1
  after="$(hciconfig "$HCI" 2>/dev/null | grep -oE 'RX bytes:[0-9]+' | tr -dc 0-9)"
  [ "${after:-0}" -gt "${before:-0}" ]
}

verify_sco_routing() {
  if [ -x /usr/local/lib/rpi-lark-bridge/set-sco-routing.sh ]; then
    log "verifying Device Tree-native SCO routing after controller recovery"
    systemctl restart bridge-btfw.service 2>/dev/null \
      || /usr/local/lib/rpi-lark-bridge/set-sco-routing.sh || warn "could not verify SCO routing"
  else
    warn "set-sco-routing.sh not installed — SCO routing cannot be verified"
  fi
}

finish() {
  verify_sco_routing
  log "restarting the audio session so bluez endpoints re-register"
  # `su -` starts a login shell that may not see the user's systemd manager, so this
  # silently did nothing: the adapter came back UP but with NO bluez endpoints
  # registered, which looks like a half-successful recovery and is worse than a clean
  # failure. Address the user instance explicitly instead.
  local u uid
  u="${BRIDGE_USER:-admin}"
  uid="$(id -u "$u" 2>/dev/null || echo 1000)"
  runuser -u "$u" -- env XDG_RUNTIME_DIR="/run/user/$uid" \
      systemctl --user restart wireplumber 2>/dev/null \
    || warn "could not restart wireplumber; run: systemctl --user restart wireplumber"

  # Verify the endpoints actually came back. Without them the radio is up but the
  # bridge is deaf, and nothing else in the system will tell you.
  sleep 4
  if bluetoothctl show 2>/dev/null | grep -qE '0000110a|0000111e'; then
    log "bluez endpoints re-registered"
  else
    warn "adapter is up but NO A2DP/HFP endpoints are registered — audio will not work"
    warn "  run: systemctl --user restart wireplumber   (as $u)"
  fi
  log "RECOVERED at rung $1"
  exit 0
}

[ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }

log "checking controller"
if alive; then log "controller is responding — nothing to do"; exit 0; fi

# --- rung 1: disconnect everything ----------------------------------------------------
log "rung 1: disconnecting all devices"
for mac in $(hcitool con 2>/dev/null | awk '/ACL|eSCO/{print $3}' | sort -u); do
  bluetoothctl disconnect "$mac" >/dev/null 2>&1 || true
done
sleep 3
alive && finish 1

# --- rung 2: force-drop links ---------------------------------------------------------
log "rung 2: forcing HCI disconnect"
for mac in $(hcitool con 2>/dev/null | awk '/ACL|eSCO/{print $3}' | sort -u); do
  hcitool dc "$mac" >/dev/null 2>&1 || true
done
sleep 3
alive && finish 2

# --- rung 3: adapter down/up ----------------------------------------------------------
log "rung 3: adapter down/up"
hciconfig "$HCI" down >/dev/null 2>&1 || true
sleep 2
hciconfig "$HCI" up   >/dev/null 2>&1 || true
sleep 3
alive && finish 3

# --- rung 4: rfkill cycle -------------------------------------------------------------
if command -v rfkill >/dev/null 2>&1; then
  log "rung 4: rfkill cycle"
  rfkill block bluetooth  >/dev/null 2>&1 || true
  sleep 2
  rfkill unblock bluetooth >/dev/null 2>&1 || true
  sleep 4
  alive && finish 4
else
  log "rung 4: skipped (rfkill not installed)"
fi

# --- rung 5: restart bluetoothd -------------------------------------------------------
log "rung 5: restarting bluetooth.service"
systemctl restart bluetooth >/dev/null 2>&1 || true
sleep 6
alive && finish 5

# --- rung 6: reload the driver, forcing a firmware reload -----------------------------
# This is the one that actually worked. It is not a bigger hammer than a service restart;
# it is a DIFFERENT hammer -- it re-runs the firmware patch load, which is what a wedged
# BCM43438 needs.
log "rung 6: unbind/rebind $DRIVER ($SERDEV) — forces firmware reload"
if [ -d "$DRIVER" ]; then
  echo "$SERDEV" > "$DRIVER/unbind" 2>/dev/null || warn "unbind failed"
  sleep 3
  echo "$SERDEV" > "$DRIVER/bind"   2>/dev/null || warn "bind failed"
  sleep 8
  alive && finish 6
else
  warn "$DRIVER not present — is the chip attached via serdev?"
fi

log "ALL RUNGS EXHAUSTED — the controller is still not responding."
log "A reboot is the remaining option. Deliberately not doing that automatically."
exit 1
