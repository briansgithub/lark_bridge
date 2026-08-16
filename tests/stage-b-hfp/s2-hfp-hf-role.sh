#!/usr/bin/env bash
# Spike S2 — Can this Pi act as an HFP *Hands-Free* unit for the Pixel (not an Audio Gateway)?
#
# bluez5.roles names the role WE play. Measured 2026-08-16 by setting each value and reading
# back what the adapter advertises in `bluetoothctl show`:
#
#     a2dp_source => adapter advertises Audio Source 0000110a => we stream to headphones <-- want
#     a2dp_sink   => adapter advertises Audio Sink   0000110b => we receive from a phone <-- not this
#     hfp_hf      => adapter advertises Handsfree    0000111e => we are the HF unit      <-- want
#     hfp_ag      => adapter advertises Handsfree AG 0000111f => we are the gateway      <-- not this
#     hsp_hs      => adapter advertises Headset      00001108 => HSP fallback
#
# An earlier version of this script claimed the opposite (remote-perspective) convention,
# inferred from spa_bt_profile_from_uuid(). That function maps a REMOTE device's UUIDs and does
# not govern this config key. The table above is measured.
#
# This spike verifies (a) that we register UUID 0000111e, (b) that an Android AG completes a
# service level connection with us, and (c) that WirePlumber survives it. Point (c) is not
# paranoia: there is a field report of WirePlumber segfaulting specifically when the HFP path
# is exercised as a system service, which is why ADR-0006 keeps us in a user session.
#
# Usage:
#   ./tests/stage-b-hfp/s2-hfp-hf-role.sh                 # 30-minute soak (the real test)
#   ./tests/stage-b-hfp/s2-hfp-hf-role.sh --soak 300      # quick 5-minute check
#   ./tests/stage-b-hfp/s2-hfp-hf-role.sh --no-install    # use existing config, do not touch it

set -euo pipefail
# shellcheck source=../../scripts/lib/common.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/scripts/lib/common.sh"

SOAK=1800
INSTALL_CONFIG=1

while [ $# -gt 0 ]; do
  case "$1" in
    --soak)       SOAK="${2:?--soak needs seconds}"; shift 2 ;;
    --no-install) INSTALL_CONFIG=0; shift ;;
    -h|--help)    sed -n '2,30p' "$0"; exit 0 ;;
    *)            die "unknown argument: $1" ;;
  esac
done

require_linux
require_not_root
require_cmd wpctl systemctl python3
require_bt_adapter

DIR="$(artifact_dir "s2-hfp-hf")"
info "artifacts: $DIR"
info "running as user: $(id -un) (PipeWire is a user service — see ADR-0006)"

# ---------------------------------------------------------------- 1. apply the role config

WP_CONF_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/wireplumber/wireplumber.conf.d"
WP_CONF="$WP_CONF_DIR/50-bridge-bluez.conf"
SRC_CONF="$BRIDGE_REPO_ROOT/pi/wireplumber/wireplumber.conf.d/50-bridge-bluez.conf"

if [ "$INSTALL_CONFIG" -eq 1 ]; then
  [ -f "$SRC_CONF" ] || die "missing $SRC_CONF"
  mkdir -p "$WP_CONF_DIR"
  backup_file "$WP_CONF"
  cp "$SRC_CONF" "$WP_CONF"
  ok "installed $WP_CONF"
  info "restarting wireplumber"
  systemctl --user restart wireplumber || die "could not restart wireplumber"
  sleep 3
else
  info "--no-install: leaving WirePlumber config alone"
fi

cp "$WP_CONF" "$DIR/wireplumber-bluez.conf" 2>/dev/null || true

# ---------------------------------------------------------------- 2. what do we advertise?

info "checking which HFP UUID we register with BlueZ"
capture "$DIR" "sdp-browse-local" sdptool browse local

# 0000111e = Handsfree (HF). 0000111f = Handsfree Audio Gateway (AG).
# We want 111e present. 111f present as well is acceptable but noteworthy.
HAVE_HF=0; HAVE_AG=0
if grep -qiE '0x111e|"Handsfree" \(0x111e\)|Handsfree \(0x111e\)' "$DIR/sdp-browse-local.txt" 2>/dev/null; then HAVE_HF=1; fi
if grep -qiE '0x111f|Handsfree Audio Gateway' "$DIR/sdp-browse-local.txt" 2>/dev/null; then HAVE_AG=1; fi

if [ "$HAVE_HF" -eq 1 ]; then
  ok "we advertise Handsfree (0x111e) — correct role for talking to a phone"
else
  err "we do NOT advertise Handsfree (0x111e)"
  err "  Check bluez5.roles in $WP_CONF. It must contain 'hfp_ag' (remote-is-AG), NOT 'hfp_hf'."
fi
[ "$HAVE_AG" -eq 1 ] && warn "we also advertise Handsfree AG (0x111f) — harmless here, but not needed"

# ---------------------------------------------------------------- 3. connect the phone

prompt_user "On the Pixel 7a:
  1. Pair with this Pi if not already paired (bluetoothctl on the Pi may need 'discoverable on').
  2. In Bluetooth settings, open the Pi's entry and confirm 'Phone calls' is enabled.
  3. Connect it.

Press Enter once the Pixel reports the Pi as Connected."
read -r _

capture "$DIR" "bluetoothctl-devices"  bluetoothctl devices
capture "$DIR" "bluetoothctl-info"     sh -c "for m in \$(bluetoothctl devices | awk '{print \$2}'); do bluetoothctl info \"\$m\"; echo; done"
capture "$DIR" "wpctl-status-initial"  wpctl status

# The presence of a handsfree-head-unit node pair is the real proof that PipeWire has
# taken the HF role and built a transport, independent of what SDP claims.
if grep -qE 'handsfree|hfp' "$DIR/wpctl-status-initial.txt" 2>/dev/null; then
  ok "PipeWire shows an HFP node — service level connection reached"
  grep -E 'handsfree|hfp' "$DIR/wpctl-status-initial.txt" | sed 's/^/      /' >&2
  SLC_OK=1
else
  err "no HFP node in wpctl status — the service level connection did not complete"
  err "  Look at: journalctl --user -u wireplumber -n 100"
  SLC_OK=0
fi

# ---------------------------------------------------------------- 4. stability soak

RESTARTS_BEFORE="$(systemctl --user show wireplumber -p NRestarts --value 2>/dev/null || echo 0)"
PW_RESTARTS_BEFORE="$(systemctl --user show pipewire -p NRestarts --value 2>/dev/null || echo 0)"
info "wireplumber restart counter at start: $RESTARTS_BEFORE"

prompt_user "Now exercise the HFP path for the next $((SOAK / 60)) minutes:
  - place and end several calls (this is what forces SCO setup/teardown)
  - toggle Bluetooth off and on once or twice
  - leave a call running for a few minutes

The point is to make WirePlumber handle repeated HFP transitions, which is where
instability has been reported. Leave this running."

countdown "$SOAK" "soaking the HFP path"

RESTARTS_AFTER="$(systemctl --user show wireplumber -p NRestarts --value 2>/dev/null || echo 0)"
PW_RESTARTS_AFTER="$(systemctl --user show pipewire -p NRestarts --value 2>/dev/null || echo 0)"

capture "$DIR" "wpctl-status-final"   wpctl status
capture "$DIR" "journal-wireplumber"  journalctl --user -u wireplumber --no-pager -n 500
capture "$DIR" "journal-pipewire"     journalctl --user -u pipewire --no-pager -n 300
capture "$DIR" "journal-bluetooth"    journalctl -u bluetooth --no-pager -n 300
capture "$DIR" "coredumps"            coredumpctl list --no-pager

WP_CRASHES=$(( RESTARTS_AFTER - RESTARTS_BEFORE ))
PW_CRASHES=$(( PW_RESTARTS_AFTER - PW_RESTARTS_BEFORE ))

# ---------------------------------------------------------------- 5. verdict

{
  printf 'S2 HFP Hands-Free role analysis\n'
  printf '===============================\n'
  printf 'advertises_hfp_hf_111e : %s\n' "$([ "$HAVE_HF" -eq 1 ] && echo yes || echo no)"
  printf 'advertises_hfp_ag_111f : %s\n' "$([ "$HAVE_AG" -eq 1 ] && echo yes || echo no)"
  printf 'slc_established        : %s\n' "$([ "$SLC_OK" -eq 1 ] && echo yes || echo no)"
  printf 'soak_seconds           : %s\n' "$SOAK"
  printf 'wireplumber_restarts   : %s\n' "$WP_CRASHES"
  printf 'pipewire_restarts      : %s\n' "$PW_CRASHES"
} | tee "$DIR/analysis.txt"

echo
if [ "$HAVE_HF" -eq 1 ] && [ "$SLC_OK" -eq 1 ] && [ "$WP_CRASHES" -eq 0 ] && [ "$PW_CRASHES" -eq 0 ]; then
  ok "VERDICT: PASS — the Pi is a stable HFP Hands-Free unit for the Pixel."
  VERDICT="PASS"
elif [ "$HAVE_HF" -eq 1 ] && [ "$SLC_OK" -eq 1 ]; then
  warn "VERDICT: UNSTABLE — the role works but the stack restarted"
  warn "  wireplumber=$WP_CRASHES pipewire=$PW_CRASHES during the soak."
  warn "  Fallback per PLAN.md §8/S2: drop to HSP only (bluez5.roles = [ hsp_ag a2dp_sink ])."
  warn "  HSP is CVSD-only (8 kHz) so record the quality cost before accepting it."
  VERDICT="UNSTABLE"
else
  err "VERDICT: FAIL — the Pi is not functioning as an HFP HF unit."
  err "  Most likely cause, in order:"
  err "    1. bluez5.roles has 'hfp_hf' where it needs 'hfp_ag' (see the header of this script)"
  err "    2. ofono is installed but unconfigured — remove it, it hijacks the HFP backend"
  err "    3. bluez5.hfphsp-backend is not 'native'"
  VERDICT="FAIL"
fi

printf 'verdict: %s\n' "$VERDICT" >> "$DIR/analysis.txt"
info "Record this run in docs/experiments/E02-hfp-hf-role.md — verdict=$VERDICT dir=$DIR"

[ "$VERDICT" = "PASS" ] && exit 0 || exit 1
