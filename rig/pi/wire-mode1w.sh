#!/usr/bin/env bash
# Wire the Mode 1W signal path. Runs ON THE PI.
#
#   Lark  -> bridge.mic     -> HFP sink   (uplink: our microphone to the phone)
#   HFP source -> bridge.callout -> dongle A   (downlink: call audio to the wired output)
#
# Bring-up scaffolding, not the product. The shipped design declares these loopbacks in
# pi/pipewire/pipewire.conf.d/20-bridge-endpoints.conf and lets bridged set target.object;
# this script does the same thing imperatively so the path can be proven before the
# declarative version is trusted.
#
# PREREQUISITE: SCO must be ACTIVE. The HFP nodes (bluez_input/bluez_output) only exist
# while an SCO transport is up -- i.e. during a call. Check with:
#     rig phone-state --summary        -> SCO state must be SCO_STATE_ACTIVE_*
#
# CAUTION, learned the hard way: the SCO transport is refcounted on node consumers.
# Removing the last link from bluez_input causes PipeWire to close the transport, and
# Android then drops SCO entirely. Always attach the new consumer BEFORE detaching the
# old one.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
FIND="$REPO/rig/analysis/find_node.py"

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

node() { pw-dump 2>/dev/null | python3 "$FIND" "$@"; }

LARK="$(node --prefix alsa_input  --contains Hollyland || true)"
HFP_SINK="$(node --prefix bluez_output || true)"
HFP_SRC="$(node --prefix bluez_input  || true)"
OUT="$(node --prefix alsa_output --contains AB13X || true)"

printf 'lark       : %s\n' "${LARK:-<missing>}"
printf 'hfp sink   : %s  (uplink)\n'   "${HFP_SINK:-<missing>}"
printf 'hfp source : %s  (downlink)\n' "${HFP_SRC:-<missing>}"
printf 'wired out  : %s\n' "${OUT:-<missing>}"

[ -n "$LARK" ] || { echo "ERROR: Lark not found" >&2; exit 78; }
[ -n "$OUT" ]  || { echo "ERROR: wired output not found" >&2; exit 78; }
if [ -z "$HFP_SINK" ] || [ -z "$HFP_SRC" ]; then
  echo "ERROR: HFP nodes absent -- SCO is not active." >&2
  echo "       Start a call on the phone and select the bridge, then re-run." >&2
  exit 78
fi

systemctl --user stop bridge-mic bridge-callout 2>/dev/null || true
sleep 1

# systemd-run rather than a bare background job: an ssh session will not close while a
# backgrounded child still holds its file descriptors, which returns exit 255.
systemd-run --user --unit=bridge-mic --collect \
  pw-loopback --name bridge.mic --capture "$LARK" --playback "$HFP_SINK" >/dev/null
systemd-run --user --unit=bridge-callout --collect \
  pw-loopback --name bridge.callout --capture "$HFP_SRC" --playback "$OUT" >/dev/null
sleep 3

# WirePlumber's default policy also auto-links the default source/sink, which duplicates
# the signal path -- the microphone gets summed into the HFP sink twice. Remove those,
# but only AFTER the loopbacks are attached (see the refcount caution above).
for ch in FL FR; do
  pw-link -d "$LARK:capture_$ch" "$HFP_SINK:input_$ch" 2>/dev/null \
    && echo "removed duplicate auto-link: Lark -> HFP sink ($ch)" || true
done

echo
echo "signal path:"
pw-link -l 2>/dev/null | grep -A1 -E "^(output\.bridge|alsa_input.*Hollyland.*capture_FL|bluez_input.*output_FL)"
