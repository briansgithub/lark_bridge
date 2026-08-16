#!/usr/bin/env bash
# Spike S1 — Does SCO audio actually reach the host over HCI on this controller?
#
# THIS IS THE FIRST THING TO RUN ON HARDWARE. It gates the entire Bluetooth track.
#
# Background (PLAN.md §2.1): the BCM43438's PCM/I2S audio pins are not routed anywhere on
# the Pi 3B PCB, so HFP audio has exactly one possible path — SCO packets multiplexed over
# the HCI UART transport. Broadcom parts default to routing SCO to the PCM pins, which on
# this board go nowhere. That is the most likely explanation for the decade-old
# "A2DP works, HSP/HFP is silent on Pi 3" reports (raspberrypi/linux#2229).
#
# This script measures whether SCO data packets cross the HCI transport, before and after
# applying the vendor SCO-routing command. It does not fix anything; it tells you what is
# true so the fix can be chosen deliberately.
#
# Usage:
#   sudo ./tests/stage-b-hfp/s1-sco-over-hci.sh                     # baseline, as shipped
#   sudo ./tests/stage-b-hfp/s1-sco-over-hci.sh --apply-vendor-cmd  # after routing SCO to HCI
#   sudo ./tests/stage-b-hfp/s1-sco-over-hci.sh --duration 90
#
# Run BOTH forms and compare. The difference between them is the finding.

set -euo pipefail
# shellcheck source=../../scripts/lib/common.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/scripts/lib/common.sh"

DURATION=60
APPLY_VENDOR=0
LABEL="baseline"

while [ $# -gt 0 ]; do
  case "$1" in
    --apply-vendor-cmd) APPLY_VENDOR=1; LABEL="vendor-cmd"; shift ;;
    --duration)         DURATION="${2:?--duration needs a value}"; shift 2 ;;
    -h|--help)          sed -n '2,30p' "$0"; exit 0 ;;
    *)                  die "unknown argument: $1" ;;
  esac
done

require_linux
require_root "$@"
require_pi_model "Raspberry Pi 3 Model B"
require_cmd btmon awk grep
require_bt_adapter

HCI="$(bt_adapter)"
DIR="$(artifact_dir "s1-sco-$LABEL")"
info "artifacts: $DIR"

# ---------------------------------------------------------------- 1. environment

info "capturing environment"
capture "$DIR" "model"          cat /proc/device-tree/model
capture "$DIR" "kernel"         uname -a
capture "$DIR" "bluez-version"  bluetoothd --version
capture "$DIR" "hciconfig-all"  hciconfig -a
capture "$DIR" "hci-features"   hciconfig "$HCI" features
capture "$DIR" "hci-voice"      hciconfig "$HCI" voice
capture "$DIR" "hci-revision"   hciconfig "$HCI" revision
capture "$DIR" "rfkill"         rfkill list
capture "$DIR" "dmesg-bt"       sh -c "dmesg | grep -iE 'blue|hci|bcm' | tail -n 100"

# Which attach mechanism is in play matters: a device-tree property such as
# brcm,bt-pcm-int-params is only honoured when the kernel drives the chip via serdev.
# If userspace hciattach is doing it, the DT route is a dead end and only the runtime
# vendor command can work. Record the answer rather than assuming it.
{
  printf '=== attach mechanism ===\n'
  printf -- '--- hciuart.service (userspace hciattach) ---\n'
  systemctl is-enabled hciuart.service 2>&1 || true
  systemctl status hciuart.service --no-pager 2>&1 | head -n 15 || true
  printf -- '\n--- serdev bluetooth node in device tree ---\n'
  find /proc/device-tree -maxdepth 4 -name 'bluetooth*' -print 2>/dev/null || true
  for n in /proc/device-tree/soc/serial@*/bluetooth /proc/device-tree/soc/*/bluetooth; do
    [ -d "$n" ] || continue
    printf 'node: %s\n' "$n"
    for p in compatible max-speed shutdown-gpios brcm,bt-pcm-int-params; do
      [ -e "$n/$p" ] && printf '  %s = %s\n' "$p" "$(tr -d '\0' <"$n/$p" | od -An -tx1 | tr -s ' ')"
    done
  done
  printf -- '\n--- loaded bluetooth modules ---\n'
  lsmod 2>/dev/null | grep -iE 'bcm|hci|blue' || printf '(none / builtin)\n'
} > "$DIR/attach-mechanism.txt" 2>&1
log "captured attach-mechanism"

# Cheap sanity check before spending an hour: does the controller claim (e)SCO at all?
if grep -qiE '(^|[^e])sco' "$DIR/hci-features.txt" 2>/dev/null; then
  ok "controller advertises SCO/eSCO in Local Supported Features"
else
  warn "could not confirm SCO in the feature mask — read $DIR/hci-features.txt yourself"
fi

# ---------------------------------------------------------------- 2. optional vendor command

if [ "$APPLY_VENDOR" -eq 1 ]; then
  info "applying Broadcom Write_SCO_PCM_Int_Param (sco-routing = Transport/HCI)"
  VENDOR_TOOL="$BRIDGE_REPO_ROOT/tools/bt/hci_vendor_cmd.py"
  if python3 "$VENDOR_TOOL" --dev "${HCI#hci}" --sco-routing-transport > "$DIR/vendor-cmd.txt" 2>&1; then
    ok "controller accepted the vendor command"
  else
    rc=$?
    warn "vendor command failed (exit $rc) — see $DIR/vendor-cmd.txt"
    if have_cmd hcitool; then
      info "retrying with legacy hcitool"
      capture "$DIR" "vendor-cmd-hcitool" hcitool -i "$HCI" cmd 0x3f 0x1c 0x01 0x02 0x00 0x01 0x01
    fi
    warn "continuing anyway — a rejection is itself a finding worth recording"
  fi
  # Some firmwares only latch PCM parameters across an adapter reset.
  info "cycling the adapter so the new routing takes effect"
  hciconfig "$HCI" down || true
  sleep 1
  hciconfig "$HCI" up   || true
  sleep 2
fi

# ---------------------------------------------------------------- 3. capture

BTSNOOP="$DIR/hci.btsnoop"
BTTEXT="$DIR/btmon.txt"

info "starting btmon capture"
btmon -w "$BTSNOOP" > "$BTTEXT" 2>&1 &
BTMON_PID=$!
cleanup_pid_on_exit "$BTMON_PID"
sleep 2
kill -0 "$BTMON_PID" 2>/dev/null || die "btmon died immediately — check that you are root"

prompt_user "On the Pixel 7a, right now:
  1. Make sure the Pi is paired AND connected (Bluetooth settings — it should say 'Phone calls').
  2. Place a call, or start a Discord/WhatsApp voice call.
  3. SPEAK CONTINUOUSLY for the whole capture, and have the far end speak too.
     Both directions matter — this test measures uplink and downlink separately.

Capturing for ${DURATION}s. Do not stop the call until this returns."

countdown "$DURATION" "capturing HCI traffic"

kill "$BTMON_PID" 2>/dev/null || true
wait "$BTMON_PID" 2>/dev/null || true
ok "capture complete"

# ---------------------------------------------------------------- 4. analysis

info "analysing"

count_matches() { grep -c -- "$1" "$BTTEXT" 2>/dev/null || true; }

SCO_TX="$(count_matches 'SCO Data TX')"
SCO_RX="$(count_matches 'SCO Data RX')"
SYNC_SETUP="$(count_matches 'Setup Synchronous Connection')"
SYNC_COMPLETE="$(count_matches 'Synchronous Connect Complete')"
SYNC_DISCONN="$(count_matches 'Disconnect Complete')"

# Air mode tells us which codec the link actually negotiated:
#   "Transparent" => mSBC wideband (16 kHz);  "CVSD" => narrowband (8 kHz).
AIR_MODE="$(grep -o 'Air mode: [A-Za-z-]*' "$BTTEXT" 2>/dev/null | sort -u | tr '\n' ' ' || true)"
[ -n "$AIR_MODE" ] || AIR_MODE="(none observed)"

{
  printf 'S1 SCO-over-HCI analysis (%s)\n' "$LABEL"
  printf '=====================================\n'
  printf 'duration_s            : %s\n' "$DURATION"
  printf 'vendor_cmd_applied    : %s\n' "$([ "$APPLY_VENDOR" -eq 1 ] && echo yes || echo no)"
  printf 'setup_sync_connection : %s\n' "$SYNC_SETUP"
  printf 'sync_connect_complete : %s\n' "$SYNC_COMPLETE"
  printf 'disconnect_complete   : %s\n' "$SYNC_DISCONN"
  printf 'sco_data_tx_packets   : %s\n' "$SCO_TX"
  printf 'sco_data_rx_packets   : %s\n' "$SCO_RX"
  printf 'air_mode              : %s\n' "$AIR_MODE"
} | tee "$DIR/analysis.txt"

# Expected order of magnitude: mSBC runs 7.5 ms frames => ~133 packets/s per direction.
# A 60 s capture with a live call should therefore show thousands, not tens.
EXPECT_MIN=$(( DURATION * 20 ))

echo
if [ "$SYNC_COMPLETE" -eq 0 ]; then
  err "VERDICT: no SCO link was ever established."
  err "  The phone never opened a voice channel. This is an HFP/profile problem, not a"
  err "  routing problem — run spike S2 before drawing conclusions about the controller."
  VERDICT="NO_SCO_LINK"
elif [ "$SCO_TX" -lt "$EXPECT_MIN" ] && [ "$SCO_RX" -lt "$EXPECT_MIN" ]; then
  err "VERDICT: SCO link established, but NO SCO DATA crossed the HCI transport."
  err "  This is the failure mode PLAN.md §2.1 predicts: the controller is routing SCO"
  err "  audio to PCM pins that are not connected on the Pi 3B."
  if [ "$APPLY_VENDOR" -eq 1 ]; then
    err "  The vendor command did NOT fix it. Next: try the device-tree overlay route"
    err "  (pi/boot/overlays/bridge-bt-sco-overlay.dts), then escalate to PLAN.md §15 Q1."
  else
    warn "  Now re-run with --apply-vendor-cmd. That is the whole point of this spike."
  fi
  VERDICT="SCO_LINK_NO_DATA"
elif [ "$SCO_TX" -lt "$EXPECT_MIN" ] || [ "$SCO_RX" -lt "$EXPECT_MIN" ]; then
  warn "VERDICT: SCO data flows in ONE direction only (tx=$SCO_TX rx=$SCO_RX)."
  warn "  Half-duplex SCO is unusual. Check the phone's mute state and re-run before"
  warn "  concluding this is a controller limitation."
  VERDICT="SCO_HALF_DUPLEX"
else
  ok "VERDICT: SCO data flows in BOTH directions over HCI (tx=$SCO_TX rx=$SCO_RX)."
  ok "  Risk R2 is resolved. Air mode: $AIR_MODE"
  case "$AIR_MODE" in
    *Transparent*) ok "  Transparent air mode => mSBC wideband (16 kHz). Best case." ;;
    *CVSD*)        warn "  CVSD air mode => narrowband 8 kHz. Usable, but quality is capped." ;;
  esac
  VERDICT="SCO_OK"
fi

printf 'verdict: %s\n' "$VERDICT" >> "$DIR/analysis.txt"

echo
info "Record this run in docs/experiments/E01-sco-over-hci.md:"
info "  verdict   = $VERDICT"
info "  artifacts = $DIR"
info "Then re-run the other variant (with/without --apply-vendor-cmd) and compare."

[ "$VERDICT" = "SCO_OK" ] && exit 0 || exit 1
