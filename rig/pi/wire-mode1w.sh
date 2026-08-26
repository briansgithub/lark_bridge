#!/usr/bin/env bash
# Retired: this bring-up helper created a raw physical-microphone -> HFP path.
set -euo pipefail

echo "ERROR: wire-mode1w.sh is retired because it can bypass AEC and route ownership." >&2
echo "       Use bridge-supervisor and inspect it with bridgectl microphone status." >&2
exit 64
