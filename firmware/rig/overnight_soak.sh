#!/usr/bin/env bash
# Launch the overnight soak from WSL at the repo root:
#   bash firmware/rig/overnight_soak.sh OUT_DIR GOOD_DIR [overnight_soak.py args...]
# GOOD_DIR holds s31.bin and output.bin: the images flashed at launch, restored app-only if
# the soak reports a reset loop (exit 4). Never touches eFuses, secure boot or encryption.
set -uo pipefail
out=$(realpath -m "$1"); good=$(realpath "$2"); shift 2
py=/mnt/c/Windows/py.exe
adb=/mnt/c/Users/user1/AppData/Local/Android/Sdk/platform-tools/adb.exe
mkdir -p "$out"
for f in s31.bin output.bin; do [[ -f $good/$f ]] || { echo "missing $good/$f"; exit 3; }; done
sha256sum "$good/s31.bin" "$good/output.bin" > "$out/images.sha256"
"$py" -3 -m serial.tools.list_ports -v 2>/dev/null | tr -d '\r' > "$out/usb-start.log"
"$adb" devices -l | tr -d '\r' > "$out/adb-start.log"
grep -q '3C081JEHN14930 *device' "$out/adb-start.log" || { echo "Pixel not on adb"; exit 3; }
/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe -NoProfile -ExecutionPolicy Bypass \
    -File "$(wslpath -w firmware/rig/shelly_power.ps1)" status > "$out/plug-start.json" || exit 3
echo "soak start $(date -Is)" | tee "$out/run.log"
"$py" -3 firmware/rig/overnight_soak.py "$(wslpath -w "$out")" "$@" >> "$out/run.log" 2>&1
rc=$?
echo "soak end $(date -Is) rc=$rc" | tee -a "$out/run.log"
if [[ $rc == 4 ]]; then
    echo "reset loop: restoring launch images" | tee -a "$out/run.log"
    "$py" -3 -m esptool --chip esp32s31 --port COM3 --baud 460800 --no-stub --after hard-reset \
        write-flash 0x10000 "$(wslpath -w "$good/s31.bin")" > "$out/restore-s31.log" 2>&1
    "$py" -3 -m esptool --chip esp32 --port COM4 --baud 460800 --after hard-reset \
        write-flash 0x10000 "$(wslpath -w "$good/output.bin")" > "$out/restore-output.log" 2>&1
    grep -c 'Hash of data verified' "$out"/restore-*.log | tee -a "$out/run.log"
fi
tail -1 "$out/run.log"
exit $rc
