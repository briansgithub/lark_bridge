param(
    [string]$PiHost = "192.168.0.251",
    [string]$PiUser = "admin",
    [ValidateRange(3, 20)]
    [int]$Boots = 7,
    [string]$OutputDirectory = ""
)

$ErrorActionPreference = "Stop"
$Target = "$PiUser@$PiHost"

if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $OutputDirectory = Join-Path (Get-Location) "LarkBridge-v8.2-Benchmark-$stamp"
}

$OutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null

$RemoteProbe = @'
#!/usr/bin/env bash
set -u
export LANG=C
export LC_ALL=C
export SYSTEMD_COLORS=0
export SYSTEMD_PAGER=cat

unit_us() {
    systemctl show "$1" -p ActiveEnterTimestampMonotonic --value 2>/dev/null || true
}

user_unit_us() {
    XDG_RUNTIME_DIR="/run/user/$(id -u)" \
        systemctl --user show "$1" -p ActiveEnterTimestampMonotonic --value 2>/dev/null || true
}

journal_ts() {
    local pattern="$1"
    journalctl -b --no-pager -o short-monotonic 2>/dev/null |
        grep -F -m1 "$pattern" |
        sed -n 's/^\[[[:space:]]*\([0-9.][0-9.]*\)\].*/\1/p'
}

journal_count() {
    local pattern="$1"
    journalctl -b --no-pager -o cat 2>/dev/null |
        grep -F -c "$pattern" || true
}

DT=/sys/firmware/devicetree/base
BT="$DT/soc/serial@7e201000/bluetooth"
BTFW_SCRIPT=/usr/local/lib/rpi-lark-bridge/set-sco-routing.sh

echo "BOOT_ID=$(cat /proc/sys/kernel/random/boot_id)"
echo "BOOT_TIME=$(systemd-analyze 2>/dev/null | head -1)"

echo "TUNING_US=$(unit_us bridge-tuning.service)"
echo "BLUETOOTH_US=$(unit_us bluetooth.service)"
echo "SCO_SERVICE_US=$(unit_us bridge-btfw.service)"
echo "USER_US=$(unit_us user@$(id -u).service)"
echo "NETWORKMANAGER_US=$(unit_us NetworkManager.service)"
echo "SSH_US=$(unit_us ssh.service)"

echo "PIPEWIRE_US=$(user_unit_us pipewire.service)"
echo "WIREPLUMBER_US=$(user_unit_us wireplumber.service)"
echo "SUPERVISOR_US=$(user_unit_us bridge-supervisor.service)"

echo "BT_MGMT_SEC=$(journal_ts 'Bluetooth management interface')"
echo "SCO_READ_SEC=$(journal_ts '[bridge-btfw] SCO PCM params:')"
echo "SCO_VERIFY_SEC=$(journal_ts 'verified: DT-native SCO routing is active; no userspace write performed')"
echo "HFP_WATCH_SEC=$(journal_ts 'watching for HFP nodes')"
echo "A2DP_SEC=$(journal_ts 'Endpoint registered: sender=')"

echo "SCO_WRITE_REQUESTS=$(journal_count 'requesting 0x01')"
echo "SCO_WRITE_COMMAND_TEXT=$(grep -F -c 'Write_SCO_PCM_Int_Param' "$BTFW_SCRIPT" 2>/dev/null || true)"
echo "VERIFY_MARKER=$(grep -F -c 'BRIDGE_BTFW_VERIFY_ONLY_V2' "$BTFW_SCRIPT" 2>/dev/null || true)"

PARTOF="$(systemctl show bridge-btfw.service -p PartOf --value 2>/dev/null || true)"
BEFORE="$(systemctl show bridge-btfw.service -p Before --value 2>/dev/null || true)"
echo "BTFW_PARTOF=$PARTOF"
echo "BTFW_BEFORE=$BEFORE"

FAILED_COUNT="$(
    systemctl --failed --no-legend --plain --no-pager 2>/dev/null |
        sed '/^[[:space:]]*$/d' |
        wc -l
)"
echo "FAILED_UNITS=$FAILED_COUNT"

if systemctl is-failed --quiet bridge-btfw.service 2>/dev/null; then
    echo "BTFW_FAILED=yes"
else
    echo "BTFW_FAILED=no"
fi

echo "GOVERNOR=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || true)"

if [ -f "$BT/brcm,bt-pcm-int-params" ]; then
    DT_PCM="$(od -An -tx1 -v "$BT/brcm,bt-pcm-int-params" 2>/dev/null | tr -d ' \n')"
else
    DT_PCM="ABSENT"
fi
echo "DT_PCM_HEX=$DT_PCM"

SCO_PARAMS="$(
    sudo -n hcitool -i hci0 cmd 0x3f 0x1d 2>/dev/null |
        awk '/^[[:space:]]*01 1D FC / {
            if ($4 == "00") print $5" "$6" "$7" "$8" "$9
            exit
        }'
)"
echo "SCO_PARAMS=$SCO_PARAMS"

echo
echo "=== BTFW JOURNAL ==="
journalctl -b -u bridge-btfw.service --no-pager -o short-monotonic 2>&1 || true

echo
echo "=== TARGETED JOURNAL ==="
journalctl -b --no-pager -o short-monotonic 2>/dev/null |
    grep -E \
    'bridge-tuning|bridge-btfw|Bluetooth management interface|Endpoint registered: sender=|Started pipewire.service|Started wireplumber.service|Started bridge-supervisor.service|watching for HFP nodes|INFO call DOWN' |
    head -250 || true

echo
echo "=== FAILED UNITS DETAIL ==="
systemctl --failed --plain --no-legend --no-pager 2>&1 || true
'@

$RemoteProbe = $RemoteProbe -replace "`r`n", "`n"

function Test-SshPort {
    try {
        return Test-NetConnection `
            -ComputerName $PiHost `
            -Port 22 `
            -InformationLevel Quiet `
            -WarningAction SilentlyContinue
    }
    catch {
        return $false
    }
}

function Wait-ForSshCycle {
    param(
        [int]$DownTimeoutSec = 60,
        [int]$UpTimeoutSec = 180
    )

    $sw = [System.Diagnostics.Stopwatch]::StartNew()

    while ($sw.Elapsed.TotalSeconds -lt $DownTimeoutSec) {
        if (-not (Test-SshPort)) {
            break
        }
        Start-Sleep -Milliseconds 500
    }

    $sw.Restart()

    while ($sw.Elapsed.TotalSeconds -lt $UpTimeoutSec) {
        if (Test-SshPort) {
            Start-Sleep -Seconds 1
            return
        }
        Start-Sleep -Seconds 1
    }

    throw "SSH did not return within $UpTimeoutSec seconds."
}

function Convert-UsToSeconds {
    param([string]$Value)

    [long]$n = 0
    if ([long]::TryParse($Value, [ref]$n) -and $n -gt 0) {
        return [math]::Round($n / 1000000.0, 6)
    }

    return $null
}

function Convert-ToDoubleOrNull {
    param([string]$Value)

    [double]$n = 0
    if ([double]::TryParse(
        $Value,
        [System.Globalization.NumberStyles]::Float,
        [System.Globalization.CultureInfo]::InvariantCulture,
        [ref]$n
    )) {
        return $n
    }

    return $null
}

function Get-StatsLine {
    param(
        [object[]]$Rows,
        [string]$Property
    )

    $vals = @(
        foreach ($row in $Rows) {
            $v = $row.$Property
            if ($null -ne $v -and "$v" -ne "") {
                [double]$v
            }
        }
    )

    if ($vals.Count -eq 0) {
        return "${Property}: no data"
    }

    $mean = ($vals | Measure-Object -Average).Average
    $min  = ($vals | Measure-Object -Minimum).Minimum
    $max  = ($vals | Measure-Object -Maximum).Maximum

    $sumSq = 0.0
    foreach ($v in $vals) {
        $sumSq += [math]::Pow($v - $mean, 2)
    }

    $std = [math]::Sqrt($sumSq / $vals.Count)

    return (
        "{0,-20} mean={1,8:N3}s  min={2,8:N3}s  max={3,8:N3}s  stddev={4,7:N3}s" -f `
        $Property, $mean, $min, $max, $std
    )
}

$rows = @()

for ($i = 1; $i -le $Boots; $i++) {
    Write-Host ""
    Write-Host "============================================================"
    Write-Host (" LarkBridge v8.2 benchmark boot {0}/{1}" -f $i, $Boots)
    Write-Host "============================================================"
    Write-Host "Rebooting Pi..."

    $SavedErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"

    try {
        & ssh.exe -tt $Target "sudo reboot" 2>&1 |
            ForEach-Object {
                if ($_ -is [System.Management.Automation.ErrorRecord]) {
                    $_.Exception.Message
                }
                else {
                    $_
                }
            }
    }
    finally {
        $ErrorActionPreference = $SavedErrorActionPreference
    }

    Write-Host "Waiting for SSH to cycle..."
    Wait-ForSshCycle

    Write-Host "Collecting metrics..."

    $SavedErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"

    try {
        $raw = $RemoteProbe |
            & ssh.exe $Target "bash -s" 2>&1 |
            ForEach-Object {
                if ($_ -is [System.Management.Automation.ErrorRecord]) {
                    $_.Exception.Message
                }
                else {
                    $_
                }
            }
        $probeExit = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $SavedErrorActionPreference
    }

    $rawPath = Join-Path $OutputDirectory ("boot-{0:D2}.txt" -f $i)
    $raw | Tee-Object -FilePath $rawPath

    if ($probeExit -ne 0) {
        throw "Probe failed on boot $i with exit code $probeExit. Output saved to $rawPath"
    }

    $kv = @{}

    foreach ($line in $raw) {
        if ($line -match '^([A-Z0-9_]+)=(.*)$') {
            $kv[$matches[1]] = $matches[2].Trim()
        }
    }

    $row = [pscustomobject]@{
        Boot              = $i
        BootId            = $kv["BOOT_ID"]

        TuningSec         = Convert-UsToSeconds $kv["TUNING_US"]
        BluetoothSec      = Convert-UsToSeconds $kv["BLUETOOTH_US"]
        BluetoothMgmtSec  = Convert-ToDoubleOrNull $kv["BT_MGMT_SEC"]

        ScoServiceSec     = Convert-UsToSeconds $kv["SCO_SERVICE_US"]
        ScoReadSec        = Convert-ToDoubleOrNull $kv["SCO_READ_SEC"]
        ScoVerifySec      = Convert-ToDoubleOrNull $kv["SCO_VERIFY_SEC"]

        UserManagerSec    = Convert-UsToSeconds $kv["USER_US"]
        PipeWireSec       = Convert-UsToSeconds $kv["PIPEWIRE_US"]
        WirePlumberSec    = Convert-UsToSeconds $kv["WIREPLUMBER_US"]
        SupervisorSec     = Convert-UsToSeconds $kv["SUPERVISOR_US"]

        HfpWatchSec       = Convert-ToDoubleOrNull $kv["HFP_WATCH_SEC"]
        A2dpEndpointSec   = Convert-ToDoubleOrNull $kv["A2DP_SEC"]

        NetworkManagerSec = Convert-UsToSeconds $kv["NETWORKMANAGER_US"]
        SshSec            = Convert-UsToSeconds $kv["SSH_US"]

        ScoWriteRequests  = $kv["SCO_WRITE_REQUESTS"]
        ScoWriteText      = $kv["SCO_WRITE_COMMAND_TEXT"]
        VerifyMarker      = $kv["VERIFY_MARKER"]
        BtfwPartOf        = $kv["BTFW_PARTOF"]
        BtfwBefore        = $kv["BTFW_BEFORE"]

        FailedUnits       = $kv["FAILED_UNITS"]
        BtfwFailed        = $kv["BTFW_FAILED"]
        Governor          = $kv["GOVERNOR"]
        DtPcmHex          = $kv["DT_PCM_HEX"]
        ScoParams         = $kv["SCO_PARAMS"]

        BootTimeRaw       = $kv["BOOT_TIME"]
    }

    $rows += $row

    Write-Host (
        "Boot {0}: BT={1}s SCOverify={2}s PW={3}s WP={4}s HFP={5}s A2DP={6}s NM={7}s SSH={8}s writes={9} failed={10}" -f `
        $i,
        $row.BluetoothSec,
        $row.ScoVerifySec,
        $row.PipeWireSec,
        $row.WirePlumberSec,
        $row.HfpWatchSec,
        $row.A2dpEndpointSec,
        $row.NetworkManagerSec,
        $row.SshSec,
        $row.ScoWriteRequests,
        $row.FailedUnits
    )

    $rows |
        Export-Csv `
            -Path (Join-Path $OutputDirectory "boot-metrics.csv") `
            -NoTypeInformation `
            -Encoding utf8
}

$summary = @()
$summary += "LarkBridge v8.2 multi-boot benchmark"
$summary += "Generated: $(Get-Date -Format o)"
$summary += "Host: $Target"
$summary += "Boots: $Boots"
$summary += ""

$summary += "Timing statistics"
$summary += "-----------------"

foreach ($property in @(
    "TuningSec",
    "BluetoothSec",
    "BluetoothMgmtSec",
    "ScoReadSec",
    "ScoVerifySec",
    "UserManagerSec",
    "PipeWireSec",
    "WirePlumberSec",
    "SupervisorSec",
    "HfpWatchSec",
    "A2dpEndpointSec",
    "NetworkManagerSec",
    "SshSec"
)) {
    $summary += Get-StatsLine -Rows $rows -Property $property
}

$summary += ""
$summary += "Reliability / invariants"
$summary += "------------------------"

$summary += "Boots with any failed systemd units: " +
    (@($rows | Where-Object { [int]$_.FailedUnits -ne 0 }).Count)

$summary += "Boots where bridge-btfw failed: " +
    (@($rows | Where-Object { $_.BtfwFailed -ne "no" }).Count)

$summary += "Boots where DT property != 0102000101: " +
    (@($rows | Where-Object { $_.DtPcmHex -ne "0102000101" }).Count)

$summary += "Boots where controller SCO params != 01 02 00 01 01: " +
    (@($rows | Where-Object { $_.ScoParams -ne "01 02 00 01 01" }).Count)

$summary += "Boots with userspace SCO write requests: " +
    (@($rows | Where-Object { [int]$_.ScoWriteRequests -gt 0 }).Count)

$summary += "Boots where verifier script still contains legacy write text: " +
    (@($rows | Where-Object { [int]$_.ScoWriteText -ne 0 }).Count)

$summary += "Boots missing verifier marker: " +
    (@($rows | Where-Object { [int]$_.VerifyMarker -lt 1 }).Count)

$summary += "Boots missing PartOf=bluetooth.service: " +
    (@($rows | Where-Object { $_.BtfwPartOf -notmatch '(^| )bluetooth\.service($| )' }).Count)

$summary += "Boots still ordered Before=bridge.target: " +
    (@($rows | Where-Object { $_.BtfwBefore -match '(^| )bridge\.target($| )' }).Count)

$summary += "Boots not using performance governor: " +
    (@($rows | Where-Object { $_.Governor -ne "performance" }).Count)

$summary += "Boots with no A2DP endpoint timestamp: " +
    (@($rows | Where-Object { $null -eq $_.A2dpEndpointSec }).Count)

$summary += ""
$summary += "Per-boot data"
$summary += "-------------"
$summary += ($rows | Format-Table -AutoSize | Out-String -Width 320)

$summaryPath = Join-Path $OutputDirectory "summary.txt"
$summary | Set-Content -Path $summaryPath -Encoding utf8

$zipPath = "$OutputDirectory.zip"
if (Test-Path $zipPath) {
    Remove-Item -Force $zipPath
}

Compress-Archive `
    -Path (Join-Path $OutputDirectory "*") `
    -DestinationPath $zipPath `
    -Force

Write-Host ""
Write-Host "Benchmark complete."
Write-Host "CSV:     $(Join-Path $OutputDirectory 'boot-metrics.csv')"
Write-Host "Summary: $summaryPath"
Write-Host "Archive: $zipPath"
Write-Host ""
Write-Host "Attach the ZIP archive for analysis."
