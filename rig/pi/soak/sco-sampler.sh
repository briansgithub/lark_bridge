#!/usr/bin/env bash
# Overnight soak sampler. One line per minute, so the failure TIME is readable directly
# instead of by decoding a multi-hundred-MB btmon capture.
#
# WHY IT PROBES ACTIVELY
# ----------------------
# E07 occurrence 4 established that every passive signal lies. On a controller that was
# already dead, `hciconfig` still said UP RUNNING PSCAN ISCAN, HCI error counters read
# errors:0 both directions, SCO frames were still being transmitted at the nominal rate, and
# bridge-supervisor reported both legs verified. The ONLY signal that discriminated was
# issuing a command and seeing whether it came back.
#
# So each sample runs `hciconfig hci0 version` (Read Local Version) and records whether it
# answered. The command costs one HCI round trip per minute against SCO's 133/s, i.e. it is
# not a meaningful addition to UART load.
#
# RX bytes is recorded too: during a wedge it FREEZES while TX keeps climbing, which is the
# corroborating signature and needs no decode.
set -u

OUT="${1:?usage: sco-sampler.sh <logfile>}"
INTERVAL="${2:-60}"

while :; do
  TS="$(date '+%Y-%m-%d %H:%M:%S')"

  ESCO="$(timeout 10 hcitool con 2>/dev/null | grep -c eSCO || true)"
  [ -n "$ESCO" ] || ESCO='?'

  if timeout 15 hciconfig hci0 version >/dev/null 2>&1; then ALIVE=yes; else ALIVE=NO; fi

  # RX/TX byte counters straight off the adapter line.
  COUNTS="$(hciconfig hci0 2>/dev/null | tr -s ' ' | grep -E 'RX bytes|TX bytes' \
            | tr '\n' ' ' | sed 's/  */ /g')"

  # journalctl -k, not `dmesg | grep -c`: the kernel ring buffer wraps and UNDERCOUNTS.
  # Measured discrepancy: journalctl 2868 vs dmesg 2013 over the same hour (E07).
  ERR="$(journalctl -k --since '1 minute ago' 2>/dev/null | grep -c 'Frame reassembly failed' || true)"
  [ -n "$ERR" ] || ERR=0

  printf '%s esco=%s controller=%s reassembly_1min=%s %s\n' \
    "$TS" "$ESCO" "$ALIVE" "$ERR" "$COUNTS" >> "$OUT"

  sleep "$INTERVAL"
done
