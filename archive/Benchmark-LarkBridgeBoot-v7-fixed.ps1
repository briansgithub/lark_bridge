param(
    [string]$PiHost = "192.168.0.251",
    [string]$PiUser = "admin",
    [ValidateRange(3, 20)]
    [int]$Boots = 7,
    [string]$OutputDirectory = ""
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $OutputDirectory = Join-Path (Get-Location) "LarkBridge-v7-Benchmark-$stamp"
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

echo "BOOT_ID=$(cat /proc/sys/kernel/random/boot_id)"
echo "BOOT_TIME=$(systemd-analyze 2>/dev/null | head -1)"
echo "TUNING_US=$(unit_us bridge-tuning.service)"
echo "BLUETOOTH_US=$(unit_us bluetooth.service)"
echo "SCO_US=$(unit_us bridge-btfw.service)"
echo "USER_US=$(unit_us user@$(id -u).service)"
echo "PIPEWIRE_US=$(user_unit_us pipewire.service)"
echo "WIREPLUMBER_US=$(user_unit_us wireplumber.service)"
echo "SUPERVISOR_US=$(user_unit_us bridge-supervisor.service)"
echo "BT_MGMT_SEC=$(journal_ts 'Bluetooth management interface')"
echo "SCO_VERIFY_SEC=$(journal_ts 'verified: SCO routed to the HCI transport')"
echo "HFP_WATCH_SEC=$(journal_ts 'watching for HFP nodes')"
echo "A2DP_SEC=$(journal_ts 'Endpoint registered: sender=')"

FAILED_COUNT="$(
    systemctl --failed --no-legend --plain 2>/dev/null |
        sed '/^[[:space:]]*$/d' |
        wc -l
)"
echo "FAILED_UNITS=$FAILED_COUNT"

GOVERNOR="$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || true)"
echo "GOVERNOR=$GOVERNOR"

SCO_PARAMS="$(
    hcitool -i hci0 cmd 0x3f 0x1d 2>/dev/null |
        awk '/^[[:space:]]*01 1D FC / {print $5" "$6" "$7" "$8" "$9; exit}'
)"
echo "SCO_PARAMS=$SCO_PARAMS"

echo
echo "=== FAILED UNITS DETAIL ==="
systemctl --failed --no-pager 2>&1 || true

echo
echo "=== TARGETED JOURNAL ==="
journalctl -b --no-pager -o short-monotonic 2>/dev/null |
    grep -E 'bridge-tuning|bridge-btfw|Bluetooth management interface|Started bluetooth.service|Endpoint registered: sender=|Started pipewire.service|Started wireplumber.service|Started bridge-supervisor.service|watching for HFP nodes|INFO call DOWN' |
    head -200 || true
'@

function Test-SshPort {
    try {
        return Test-NetConnection -ComputerName $PiHost -Port 22 -InformationLevel Quiet -WarningAction SilentlyContinue
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

    throw "SSH port did not return within $UpTimeoutSec seconds."
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
    $min = ($vals | Measure-Object -Minimum).Minimum
    $max = ($vals | Measure-Object -Maximum).Maximum

    $sumSq = 0.0
    foreach ($v in $vals) {
        $sumSq += [math]::Pow($v - $mean, 2)
    }
    $std = [math]::Sqrt($sumSq / $vals.Count)

    return ("{0,-18} mean={1,8:N3}s  min={2,8:N3}s  max={3,8:N3}s  stddev={4,7:N3}s" -f `
        $Property, $mean, $min, $max, $std)
}

$rows = @()

for ($i = 1; $i -le $Boots; $i++) {
    Write-Host ""
    Write-Host "============================================================"
    Write-Host (" LarkBridge benchmark boot {0}/{1}" -f $i, $Boots)
    Write-Host "============================================================"

    Write-Host "Rebooting Pi..."
    & ssh -tt "$PiUser@$PiHost" "sudo reboot"
    # A disconnect/nonzero status is normal during reboot.
    $global:LASTEXITCODE = 0

    Write-Host "Waiting for SSH to go down and return..."
    Wait-ForSshCycle

    Write-Host "Collecting boot metrics..."
    $raw = $RemoteProbe | & ssh "$PiUser@$PiHost" "bash -s" 2>&1

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
        ScoServiceSec    = Convert-UsToSeconds $kv["SCO_US"]
        ScoVerifySec     = Convert-ToDoubleOrNull $kv["SCO_VERIFY_SEC"]
        UserManagerSec   = Convert-UsToSeconds $kv["USER_US"]
        PipeWireSec      = Convert-UsToSeconds $kv["PIPEWIRE_US"]
        WirePlumberSec   = Convert-UsToSeconds $kv["WIREPLUMBER_US"]
        SupervisorSec    = Convert-UsToSeconds $kv["SUPERVISOR_US"]
        HfpWatchSec      = Convert-ToDoubleOrNull $kv["HFP_WATCH_SEC"]
        A2dpEndpointSec  = Convert-ToDoubleOrNull $kv["A2DP_SEC"]
        FailedUnits      = $kv["FAILED_UNITS"]
        Governor         = $kv["GOVERNOR"]
        ScoParams        = $kv["SCO_PARAMS"]
        BootTimeRaw      = $kv["BOOT_TIME"]
    }

    $rows += $row

    Write-Host (
        "Boot {0}: tuning={1}s BT={2}s SCO={3}s PW={4}s WP={5}s supervisor={6}s HFPwatch={7}s A2DP={8}s failed={9}" -f `
        $i, $row.TuningSec, $row.BluetoothSec, $row.ScoVerifySec,
        $row.PipeWireSec, $row.WirePlumberSec, $row.SupervisorSec,
        $row.HfpWatchSec, $row.A2dpEndpointSec, $row.FailedUnits
    )

    # Persist the CSV after every boot so partial runs remain useful.
    $csvPath = Join-Path $OutputDirectory "boot-metrics.csv"
    $rows | Export-Csv -Path $csvPath -NoTypeInformation -Encoding utf8
}

$summaryPath = Join-Path $OutputDirectory "summary.txt"
$summary = @()
$summary += "LarkBridge v7 multi-boot benchmark"
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
$summary += "Boots with failed units: " + (@($rows | Where-Object { [int]$_.FailedUnits -ne 0 }).Count)
$summary += "Boots not using performance governor: " + (@($rows | Where-Object { $_.Governor -ne "performance" }).Count)
$summary += "Boots without verified SCO params 01 02 00 01 01: " + (@($rows | Where-Object { $_.ScoParams -ne "01 02 00 01 01" }).Count)
$summary += ""
$summary += "Per-boot data"
$summary += "-------------"
$summary += ($rows | Format-Table -AutoSize | Out-String -Width 240)

$summary | Set-Content -Path $summaryPath -Encoding utf8

$zipPath = "$OutputDirectory.zip"
if (Test-Path $zipPath) {
    Remove-Item $zipPath -Force
}
Compress-Archive -Path (Join-Path $OutputDirectory "*") -DestinationPath $zipPath -Force

Write-Host ""
Write-Host "Benchmark complete."
Write-Host "CSV:     $(Join-Path $OutputDirectory 'boot-metrics.csv')"
Write-Host "Summary: $summaryPath"
Write-Host "Archive: $zipPath"
Write-Host ""
Write-Host "Attach the ZIP archive for analysis."
