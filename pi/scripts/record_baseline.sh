#!/usr/bin/env bash
# Record audio for echo analysis.
# Captures DURATION seconds of Mic and HFP downlink.

DURATION=${1:-10}
OUTPUT_DIR=${2:-/tmp/larkbridge_baseline}
mkdir -p "$OUTPUT_DIR"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)

LARK_RECORD="$OUTPUT_DIR/lark_$TIMESTAMP.wav"
HFP_RECORD="$OUTPUT_DIR/hfp_$TIMESTAMP.wav"

echo "Recording for $DURATION seconds..."

# If AEC source exists, record THAT instead of raw Lark
AEC_SOURCE=$(pw-cli ls Node | grep -o 'node.name = "echo-cancel-source"' | cut -d'"' -f2 | head -n 1)

if [ -n "$AEC_SOURCE" ]; then
    echo "Discovery: Found AEC Source: $AEC_SOURCE. Recording CLEANED audio."
    TARGET_MIC="$AEC_SOURCE"
else
    echo "Discovery: No AEC Source. Recording RAW Lark audio."
    TARGET_MIC="alsa_input.usb-Shenzhen_Hollyland_Technology_Co._Ltd_Wireless_Microphone_Wireless_Microphone-01.analog-stereo"
fi

# Record Mic (Cleaned or Raw)
pw-record --target "$TARGET_MIC" --format s16 --rate 48000 --channels 1 "$LARK_RECORD" &
PID_LARK=$!

# Record HFP Source (far-end reference)
HFP_SOURCE=$(pw-cli ls Node | grep -o 'node.name = "bluez_input[^"]*"' | cut -d'"' -f2 | head -n 1)

if [ -n "$HFP_SOURCE" ]; then
    echo "Discovery: Found HFP Source: $HFP_SOURCE"
    pw-record --target "$HFP_SOURCE" --format s16 --rate 48000 --channels 1 "$HFP_RECORD" &
    PID_HFP=$!
else
    echo "Warning: HFP Source not found."
fi

sleep "$DURATION"

kill $PID_LARK
if [ -n "$PID_HFP" ]; then
    kill $PID_HFP
fi

echo "Done. Samples saved to $OUTPUT_DIR"
