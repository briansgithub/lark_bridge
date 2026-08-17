param(
    [string]$PiHost = "192.168.0.251",
    [string]$PiUser = "admin",
    [ValidateRange(3, 20)]
    [int]$Boots = 10,
    [string]$OutputDirectory = ""
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $OutputDirectory = Join-Path (Get-Location) "LarkBridge-DTSco-Benchmark-$stamp"
}

New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null

$RemoteProbe = @'
set -u

unit_us() {
    systemctl show "$1" -p ActiveEnterTimestampMonotonic --value 2>/dev/null || true
}

user_unit_us() {
    XDG_RUNTIME_DIR="/run/user/$(id -u)" \
        systemctl --user show "$1" -p ActiveEnterTimestampMonotonic --value 2>/dev/null || true
}

journal_ts() {
    pattern="$1"
    journalctl -b --no-pager -o short-monotonic 2>/dev/null |
        grep -F -m1 "$pattern" |
        sed -n 's/^\[[[:space:]]*\([0-9.][0-9.]*\)\].*/\1/p'
}

journal_count() {
    pattern="$1"
    journalctl -b --no-pager -o cat 2>/dev/null |
        grep -F -c "$pattern" || true
}

DT=/sys/firmware/devicetree/base
BT="$DT/soc/serial@7e201000/bluetooth"

echo "BOOT_ID=$(cat /proc/sys/kernel/random/boot_id)"
echo "BOOT_TIME=$(systemd-analyze 2>/dev/null | head -1)"

echo "TUNING_US=$(unit_us bridge-tuning.service)"
echo "BLUETOOTH_US=$(unit_us bluetooth.service)"
echo "SCO_SERVICE_US=$(unit_us bridge-btfw.service)"
echo "USER_US=$(unit_us user@$(id -u).service)"
echo "PIPEWIRE_US=$(user_unit_us pipewire.service)"
echo "WIREPLUMBER_US=$(user_unit_us wireplumber.service)"
echo "SUPERVISOR_US=$(user_unit_us bridge-supervisor.service)"

echo "BT_MGMT_SEC=$(journal_ts 'Bluetooth management interface')"
echo "SCO_READ_SEC=$(journal_ts '[bridge-btfw] SCO PCM params:')"
echo "SCO_VERIFY_SEC=$(journal_ts 'verified: SCO routed to the HCI transport')"
echo "HFP_WATCH_SEC=$(journal_ts 'watching for HFP nodes')"
echo "A2DP_SEC=$(journal_ts 'Endpoint registered: sender=')"

echo "SCO_WRITE_REQUESTS=$(journal_count 'requesting 0x01')"
echo "SCO_READ_NOT_READY=$(journal_count 'controller not ready for SCO vendor read')"
echo "SCO_WRITE_REJECTS=$(journal_count 'controller rejected Write_SCO_PCM_Int_Param')"

FAILED_COUNT="$(
    systemctl --failed --no-legend --plain 2>/dev/null |
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
    'bridge-tuning|bridge-btfw|Bluetooth management interface|Started bluetooth.service|Endpoint registered: sender=|Started pipewire.service|Started wireplumber.service|Started bridge-supervisor.service|watching for HFP nodes|INFO call DOWN' |
    head -250 || true

echo
echo "=== FAILED UNITS DETAIL ==="
systemctl --failed --no-pager 2>&1 || true
'@

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
        "{0,-18} mean={1,8:N3}s  min={2,8:N3}s  max={3,8:N3}s  stddev={4,7:N3}s" -f `
        $Property, $mean, $min, $max, $std
    )
}

$rows = @()

for ($i = 1; $i -le $Boots; $i++) {
    Write-Host ""
    Write-Host "============================================================"
    Write-Host (" LarkBridge DT-SCO benchmark boot {0}/{1}" -f $i, $Boots)
    Write-Host "============================================================"

    Write-Host "Rebooting Pi..."
    & ssh.exe -tt "$PiUser@$PiHost" "sudo reboot"

    # SSH normally exits nonzero when the remote system drops the connection
    # during reboot; that is expected.
    $global:LASTEXITCODE = 0

    Write-Host "Waiting for SSH to cycle..."
    Wait-ForSshCycle

    Write-Host "Collecting metrics..."
    $raw = $RemoteProbe | & ssh.exe "$PiUser@$PiHost" "bash -s" 2>&1

    $rawPath = Join-Path $OutputDirectory ("boot-{0:D2}.txt" -f $i)
    $raw | Set-Content -Path $rawPath -Encoding utf8

    $kv = @{}

    foreach ($line in $raw) {
        if ($line -match '^([A-Z0-9_]+)=(.*)$') {
            $kv[$matches[1]] = $matches[2].Trim()
        }
    }

    $row = [pscustomobject]@{
        Boot             = $i
        BootId           = $kv["BOOT_ID"]

        TuningSec        = Convert-UsToSeconds $kv["TUNING_US"]
        BluetoothSec     = Convert-UsToSeconds $kv["BLUETOOTH_US"]
        BluetoothMgmtSec = Convert-ToDoubleOrNull $kv["BT_MGMT_SEC"]

        ScoServiceSec    = Convert-UsToSeconds $kv["SCO_SERVICE_US"]
        ScoReadSec       = Convert-ToDoubleOrNull $kv["SCO_READ_SEC"]
        ScoVerifySec     = Convert-ToDoubleOrNull $kv["SCO_VERIFY_SEC"]

        UserManagerSec   = Convert-UsToSeconds $kv["USER_US"]
        PipeWireSec      = Convert-UsToSeconds $kv["PIPEWIRE_US"]
        WirePlumberSec   = Convert-UsToSeconds $kv["WIREPLUMBER_US"]
        SupervisorSec    = Convert-UsToSeconds $kv["SUPERVISOR_US"]

        HfpWatchSec      = Convert-ToDoubleOrNull $kv["HFP_WATCH_SEC"]
        A2dpEndpointSec  = Convert-ToDoubleOrNull $kv["A2DP_SEC"]

        ScoWriteRequests = $kv["SCO_WRITE_REQUESTS"]
        ScoReadNotReady  = $kv["SCO_READ_NOT_READY"]
        ScoWriteRejects  = $kv["SCO_WRITE_REJECTS"]

        FailedUnits      = $kv["FAILED_UNITS"]
        BtfwFailed       = $kv["BTFW_FAILED"]
        Governor         = $kv["GOVERNOR"]
        DtPcmHex         = $kv["DT_PCM_HEX"]
        ScoParams        = $kv["SCO_PARAMS"]

        BootTimeRaw      = $kv["BOOT_TIME"]
    }

    $rows += $row

    Write-Host (
        "Boot {0}: BT={1}s SCOverify={2}s PW={3}s WP={4}s HFP={5}s A2DP={6}s writes={7} notReady={8} failed={9}" -f `
        $i,
        $row.BluetoothSec,
        $row.ScoVerifySec,
        $row.PipeWireSec,
        $row.WirePlumberSec,
        $row.HfpWatchSec,
        $row.A2dpEndpointSec,
        $row.ScoWriteRequests,
        $row.ScoReadNotReady,
        $row.FailedUnits
    )

    $rows |
        Export-Csv `
            -Path (Join-Path $OutputDirectory "boot-metrics.csv") `
            -NoTypeInformation `
            -Encoding utf8
}

$summary = @()

$summary += "LarkBridge DT-native SCO multi-boot benchmark"
$summary += "Generated: $(Get-Date -Format o)"
$summary += "Host: $PiUser@$PiHost"
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
    "PipeWireSec",
    "WirePlumberSec",
    "SupervisorSec",
    "HfpWatchSec",
    "A2dpEndpointSec"
)) {
    $summary += Get-StatsLine -Rows $rows -Property $property
}

$summary += ""
$summary += "Reliability"
$summary += "-----------"

$summary += "Boots with any failed systemd units: " +
    (@($rows | Where-Object { [int]$_.FailedUnits -ne 0 }).Count)

$summary += "Boots where bridge-btfw itself failed: " +
    (@($rows | Where-Object { $_.BtfwFailed -ne "no" }).Count)

$summary += "Boots where DT property != 0102000101: " +
    (@($rows | Where-Object { $_.DtPcmHex -ne "0102000101" }).Count)

$summary += "Boots where controller SCO params != 01 02 00 01 01: " +
    (@($rows | Where-Object { $_.ScoParams -ne "01 02 00 01 01" }).Count)

$summary += "Boots where bridge-btfw requested a userspace SCO write: " +
    (@($rows | Where-Object { [int]$_.ScoWriteRequests -gt 0 }).Count)

$summary += "Boots with at least one unreadable SCO vendor read: " +
    (@($rows | Where-Object { [int]$_.ScoReadNotReady -gt 0 }).Count)

$summary += "Boots with rejected SCO writes: " +
    (@($rows | Where-Object { [int]$_.ScoWriteRejects -gt 0 }).Count)

$summary += "Boots not using performance governor: " +
    (@($rows | Where-Object { $_.Governor -ne "performance" }).Count)

$summary += "Boots with no A2DP endpoint timestamp: " +
    (@($rows | Where-Object { $null -eq $_.A2dpEndpointSec }).Count)

$summary += ""
$summary += "Per-boot data"
$summary += "-------------"
$summary += ($rows | Format-Table -AutoSize | Out-String -Width 300)

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
