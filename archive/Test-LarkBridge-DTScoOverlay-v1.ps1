param(
    [ValidateSet("Audit", "Apply", "Rollback")]
    [string]$Mode = "Audit",

    [string]$PiHost = "192.168.0.251",
    [string]$PiUser = "admin"
)

$ErrorActionPreference = "Stop"

$RemoteScript = @'
set -euo pipefail

MODE="__MODE__"

EXPECTED_MODEL="Raspberry Pi 3 Model B Rev 1.2"
DT_ROOT="/sys/firmware/devicetree/base"
BT_PATH="/soc/serial@7e201000/bluetooth"
BT_NODE="$DT_ROOT$BT_PATH"

BOOT_CONFIG="/boot/firmware/config.txt"
OVERLAY_DIR="/boot/firmware/overlays"
OVERLAY_NAME="larkbridge-bt-sco"
OVERLAY_DTBO="$OVERLAY_DIR/$OVERLAY_NAME.dtbo"

STATE="/var/lib/larkbridge-dt-sco-test"
MARK_BEGIN="# BEGIN LARKBRIDGE DT SCO TEST V1"
MARK_END="# END LARKBRIDGE DT SCO TEST V1"

DESIRED_HEX="0102000101"

say()  { printf '%s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

model() {
    tr '\0' '\n' < "$DT_ROOT/model" 2>/dev/null | head -1
}

prop_hex() {
    local f="$1"
    [ -f "$f" ] || return 1
    od -An -tx1 -v "$f" | tr -d ' \n'
}

prop_string() {
    local f="$1"
    [ -f "$f" ] || return 1
    tr '\0' '\n' < "$f"
}

remove_config_block() {
    local cfg="$1"
    local tmp
    tmp="$(mktemp)"

    awk -v b="$MARK_BEGIN" -v e="$MARK_END" '
        $0 == b {skip=1; next}
        $0 == e {skip=0; next}
        !skip {print}
    ' "$cfg" > "$tmp"

    cat "$tmp" > "$cfg"
    rm -f "$tmp"
}

check_identity() {
    local m alias compatible status speed existing

    m="$(model || true)"
    [ "$m" = "$EXPECTED_MODEL" ] ||
        die "Model mismatch: expected '$EXPECTED_MODEL', found '${m:-<unknown>}'"

    [ -d "$BT_NODE" ] ||
        die "Verified Bluetooth DT node is absent: $BT_PATH"

    alias="$(prop_string "$DT_ROOT/aliases/bluetooth" 2>/dev/null || true)"
    [ "$alias" = "$BT_PATH" ] ||
        die "Bluetooth alias mismatch: expected '$BT_PATH', found '${alias:-<absent>}'"

    compatible="$(prop_string "$BT_NODE/compatible" 2>/dev/null || true)"
    printf '%s\n' "$compatible" | grep -Fqx 'brcm,bcm43438-bt' ||
        die "Active node is not compatible with brcm,bcm43438-bt"

    status="$(prop_string "$BT_NODE/status" 2>/dev/null || true)"
    [ -z "$status" ] || [ "$status" = "okay" ] ||
        die "Active node status is '$status', not okay"

    speed="$(prop_hex "$BT_NODE/max-speed" 2>/dev/null || true)"
    [ "$speed" = "000e1000" ] ||
        die "Unexpected Bluetooth max-speed bytes: '${speed:-<absent>}' (expected 000e1000 = 921600)"

    existing="$(prop_hex "$BT_NODE/brcm,bt-pcm-int-params" 2>/dev/null || true)"
    if [ -n "$existing" ] && [ "$existing" != "$DESIRED_HEX" ]; then
        die "Existing brcm,bt-pcm-int-params has unexpected value '$existing'"
    fi

    [ -f "$BOOT_CONFIG" ] ||
        die "$BOOT_CONFIG is missing"

    [ -d "$OVERLAY_DIR" ] ||
        die "$OVERLAY_DIR is missing"
}

audit() {
    local current alias status speed cfg_marker overlay_present dtc_path svc_state svc_failed

    say "============================================================"
    say " LarkBridge DT-native SCO audit"
    say "============================================================"
    say
    say "mode=$MODE"
    say "model=$(model || true)"

    alias="$(prop_string "$DT_ROOT/aliases/bluetooth" 2>/dev/null || true)"
    say "bluetooth_alias=${alias:-<absent>}"
    say "expected_bt_path=$BT_PATH"
    say "bt_node_present=$([ -d "$BT_NODE" ] && echo yes || echo no)"

    if [ -d "$BT_NODE" ]; then
        say "compatible=$(prop_string "$BT_NODE/compatible" 2>/dev/null | tr '\n' ' ' || true)"
        status="$(prop_string "$BT_NODE/status" 2>/dev/null || true)"
        say "status=${status:-<absent=enabled>}"
        speed="$(prop_hex "$BT_NODE/max-speed" 2>/dev/null || true)"
        say "max_speed_hex=${speed:-<absent>}"

        current="$(prop_hex "$BT_NODE/brcm,bt-pcm-int-params" 2>/dev/null || true)"
        say "dt_pcm_params_hex=${current:-ABSENT}"
    fi

    if grep -Fq "$MARK_BEGIN" "$BOOT_CONFIG" 2>/dev/null; then
        cfg_marker=yes
    else
        cfg_marker=no
    fi
    say "config_marker=$cfg_marker"

    if [ -f "$OVERLAY_DTBO" ]; then
        overlay_present=yes
    else
        overlay_present=no
    fi
    say "overlay_dtbo_present=$overlay_present"

    dtc_path="$(command -v dtc 2>/dev/null || true)"
    say "dtc=${dtc_path:-not-installed}"

    if command -v hcitool >/dev/null 2>&1; then
        say
        say "=== current controller SCO params ==="
        hcitool -i hci0 cmd 0x3f 0x1d 2>&1 || true
    fi

    say
    say "=== bridge-btfw current-boot journal ==="
    journalctl -b -u bridge-btfw.service --no-pager -o short-monotonic 2>&1 || true

    svc_state="$(systemctl is-active bridge-btfw.service 2>/dev/null || true)"
    svc_failed=no
    if systemctl is-failed --quiet bridge-btfw.service 2>/dev/null; then
        svc_failed=yes
    fi
    say
    say "bridge_btfw_active=$svc_state"
    say "bridge_btfw_failed=$svc_failed"

    say
    say "=== failed units ==="
    systemctl --failed --no-pager 2>&1 || true
}

apply_overlay() {
    check_identity

    local existing
    existing="$(prop_hex "$BT_NODE/brcm,bt-pcm-int-params" 2>/dev/null || true)"

    if [ "$existing" = "$DESIRED_HEX" ] &&
       ! grep -Fq "$MARK_BEGIN" "$BOOT_CONFIG" 2>/dev/null
    then
        die "Desired property is already present but not managed by this test; refusing to claim or overwrite external configuration"
    fi

    if ! command -v dtc >/dev/null 2>&1; then
        die "dtc is not installed. Install Debian package 'device-tree-compiler', then rerun Apply."
    fi

    if findmnt -no OPTIONS --target "$BOOT_CONFIG" 2>/dev/null |
       tr ',' '\n' | grep -qx ro
    then
        die "Boot filesystem containing $BOOT_CONFIG is read-only"
    fi

    mkdir -p "$STATE"

    if [ ! -f "$STATE/config.before" ]; then
        cp -a "$BOOT_CONFIG" "$STATE/config.before"
    fi

    # Refuse to overwrite an unrelated pre-existing file.
    if [ -f "$OVERLAY_DTBO" ] && [ ! -f "$STATE/overlay_installed" ]; then
        die "$OVERLAY_DTBO already exists but is not recorded as ours"
    fi

    local tmpd src compiled decompiled
    tmpd="$(mktemp -d)"
    trap 'rm -rf "$tmpd"' RETURN

    src="$tmpd/$OVERLAY_NAME-overlay.dts"
    compiled="$tmpd/$OVERLAY_NAME.dtbo"
    decompiled="$tmpd/$OVERLAY_NAME.check.dts"

    cat > "$src" <<'DTS'
/dts-v1/;
/plugin/;

/ {
    fragment@0 {
        target-path = "/soc/serial@7e201000/bluetooth";

        __overlay__ {
            brcm,bt-pcm-int-params = [01 02 00 01 01];
        };
    };
};
DTS

    dtc -@ -I dts -O dtb -o "$compiled" "$src"
    dtc -I dtb -O dts -o "$decompiled" "$compiled"

    grep -Fq 'target-path = "/soc/serial@7e201000/bluetooth";' "$decompiled" ||
        die "Compiled overlay does not contain the verified target path"

    # Decompiler whitespace is not part of the contract; normalize before testing bytes.
    tr -s '[:space:]' ' ' < "$decompiled" |
        grep -Fq 'brcm,bt-pcm-int-params = [01 02 00 01 01];' ||
        die "Compiled overlay does not contain desired 5-byte SCO PCM parameters"

    install -m 0644 "$compiled" "$OVERLAY_DTBO"
    sha256sum "$OVERLAY_DTBO" | awk '{print $1}' > "$STATE/overlay.sha256"
    : > "$STATE/overlay_installed"

    remove_config_block "$BOOT_CONFIG"

    {
        echo
        echo "$MARK_BEGIN"
        echo "# Route BCM43438 SCO over the HCI transport during kernel controller setup."
        echo "# Active node verified on this Pi: $BT_PATH"
        echo "[all]"
        echo "dtoverlay=$OVERLAY_NAME"
        echo "$MARK_END"
    } >> "$BOOT_CONFIG"

    sync

    say "============================================================"
    say " DT SCO overlay staged successfully"
    say "============================================================"
    say "overlay=$OVERLAY_DTBO"
    say "config=$BOOT_CONFIG"
    say "target=$BT_PATH"
    say "params=[01 02 00 01 01]"
    say
    say "No running Bluetooth service was restarted."
    say "Reboot once, then run this script with -Mode Audit."
    say "The existing bridge-btfw.service remains enabled as a fallback/verifier."
}

rollback_overlay() {
    [ -d "$STATE" ] ||
        die "No DT SCO test state exists at $STATE"

    if [ -f "$BOOT_CONFIG" ]; then
        remove_config_block "$BOOT_CONFIG"
    fi

    if [ -f "$STATE/overlay_installed" ] && [ -f "$OVERLAY_DTBO" ]; then
        local expected actual
        expected="$(cat "$STATE/overlay.sha256" 2>/dev/null || true)"
        actual="$(sha256sum "$OVERLAY_DTBO" 2>/dev/null | awk '{print $1}')"

        if [ -n "$expected" ] && [ "$actual" = "$expected" ]; then
            rm -f "$OVERLAY_DTBO"
            say "Removed managed overlay: $OVERLAY_DTBO"
        else
            warn "Overlay file changed since installation; refusing to delete it"
        fi
    fi

    sync

    say "Removed managed config block."
    say "Reboot to restore the pre-test live device tree."
}

case "$MODE" in
    audit)
        check_identity
        audit
        ;;
    apply)
        apply_overlay
        ;;
    rollback)
        rollback_overlay
        ;;
    *)
        die "Unknown mode: $MODE"
        ;;
esac
'@

$modeMap = @{
    "Audit"    = "audit"
    "Apply"    = "apply"
    "Rollback" = "rollback"
}

$RemoteScript = $RemoteScript.Replace("__MODE__", $modeMap[$Mode])

$Target = "$PiUser@$PiHost"
$RemoteFile = "/tmp/larkbridge-dt-sco-test-$PID.sh"
$TempFile = Join-Path ([System.IO.Path]::GetTempPath()) "larkbridge-dt-sco-test-$PID.sh"

$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($TempFile, $RemoteScript, $Utf8NoBom)

Write-Host ""
Write-Host "============================================================"
Write-Host " LarkBridge DT-native SCO test"
Write-Host "============================================================"
Write-Host "Target: $Target"
Write-Host "Mode:   $Mode"
Write-Host ""

try {
    & scp.exe -q $TempFile "${Target}:$RemoteFile"
    if ($LASTEXITCODE -ne 0) {
        throw "SCP failed with exit code $LASTEXITCODE"
    }

    Write-Host "sudo may request the Raspberry Pi password."
    Write-Host ""

    & ssh.exe -tt $Target "sudo bash '$RemoteFile'"
    $SshExit = $LASTEXITCODE

    & ssh.exe $Target "rm -f '$RemoteFile'" 2>$null

    if ($SshExit -ne 0) {
        throw "Remote DT SCO test failed with exit code $SshExit"
    }
}
finally {
    if (Test-Path $TempFile) {
        Remove-Item -Force $TempFile
    }
}
