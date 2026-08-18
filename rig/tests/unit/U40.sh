#!/usr/bin/env bash
# U40 — AEC module and WebRTC engine presence
set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/lib/common.sh"

unit_header "U40" "AEC module presence" \
  "the PipeWire echo-cancel module and WebRTC engine are available" \
  "verify that the system is ready for AEC implementation"

require_pi
DIR="$(artifact_dir U40-aec-audit)"

# Paths found during Phase 0 audit
AEC_MODULE="/usr/lib/aarch64-linux-gnu/pipewire-0.3/libpipewire-module-echo-cancel.so"
WEBRTC_SPA="/usr/lib/aarch64-linux-gnu/spa-0.2/aec/libspa-aec-webrtc.so"

pi "ls -l $AEC_MODULE $WEBRTC_SPA" > "$DIR/plugins.txt" 2>&1

fail=0

if pi "[ -f $AEC_MODULE ]"; then
    ok "found libpipewire-module-echo-cancel.so"
else
    err "libpipewire-module-echo-cancel.so MISSING"
    fail=1
fi

if pi "[ -f $WEBRTC_SPA ]"; then
    ok "found libspa-aec-webrtc.so"
else
    err "libspa-aec-webrtc.so MISSING"
    fail=1
fi

if [ "$fail" -eq 0 ]; then
    ok "U40 PASS"
    emit_result U40 PASS "$DIR" aec_module "$AEC_MODULE" webrtc_spa "$WEBRTC_SPA"
else
    err "U40 FAIL — PipeWire AEC dependencies missing"
    emit_result U40 FAIL "$DIR" aec_module "$AEC_MODULE" webrtc_spa "$WEBRTC_SPA"
fi
exit "$fail"
