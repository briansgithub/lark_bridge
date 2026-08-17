<#
.SYNOPSIS
    Safely analyzes and optimizes Raspberry Pi boot for LarkBridge (v7).

.DESCRIPTION
    This script intentionally avoids assumptions about:
      - Raspberry Pi OS version
      - network manager
      - LarkBridge systemd service name
      - graphical/headless configuration
      - PipeWire/WirePlumber service scope
      - watchdog availability
      - Wi-Fi requirements
      - whether NetworkManager profiles are runtime or persistent
      - unrelated installed services
      - whether the lingering user login barrier is safe to bypass
      - whether the BCM43438 SCO vendor-command script needs bounded readiness retries
      - whether the existing CPU-governor tuning service can safely run before Bluetooth

    Modes:
      Audit      = inspect only
      ApplySafe  = inspect, then apply only changes whose preconditions are verified
      Rollback   = remove changes previously made by this optimizer

    Audit mode makes no persistent configuration changes on the Pi.

    The Linux portion is copied to /tmp using SCP, then executed via sudo.
    This avoids placing a large Base64-encoded script on the SSH command line.

    If a .local hostname cannot be resolved, the script checks only the
    existing Windows neighbor cache for SSH-capable candidates and requires
    explicit confirmation. It does not scan or silently choose another host.

.EXAMPLE
    # First run I recommend:
    .\Optimize-LarkBridgeBoot.ps1 `
        -PiHost 192.168.0.251 `
        -PiUser admin `
        -Mode Audit

.EXAMPLE
    # Apply safe, automatically verifiable optimizations:
    .\Optimize-LarkBridgeBoot.ps1 `
        -PiHost 192.168.0.251 `
        -PiUser admin `
        -Mode ApplySafe

.EXAMPLE
    # Once larkbridge.service exists:
    .\Optimize-LarkBridgeBoot.ps1 `
        -PiHost 192.168.0.251 `
        -PiUser admin `
        -ServiceName larkbridge.service `
        -Mode ApplySafe `
        -EnableServiceAtBoot `
        -TuneServiceRestart `
        -EnableWatchdog

.EXAMPLE
    # Undo changes:
    .\Optimize-LarkBridgeBoot.ps1 `
        -PiHost 192.168.0.251 `
        -PiUser admin `
        -Mode Rollback
#>

[CmdletBinding()]
param(
    # Defaults match the current LarkBridge setup, but either can be overridden.
    [string]$PiHost = "192.168.0.251",

    [string]$PiUser = "admin",

    # Safety identity checks. ApplySafe/Rollback refuse to touch a different host.
    # Audit also reports and verifies these values.
    [string]$ExpectedHostname = "larkbridge",

    [string]$ExpectedMac = "B8-27-EB-E9-27-FB",

    [string]$ExpectedModel = "Raspberry Pi 3 Model B Rev 1.2",

    # Optional. If omitted, the Pi is searched for a unique
    # plausible LarkBridge SYSTEM service.
    [string]$ServiceName = "",

    [ValidateSet("Audit", "ApplySafe", "Rollback")]
    [string]$Mode = "Audit",

    # Explicit opt-in: enable the selected application service.
    [switch]$EnableServiceAtBoot,

    # Explicit opt-in: if the selected service is a normal long-running
    # service AND has Restart=no, add Restart=on-failure.
    [switch]$TuneServiceRestart,

    # Explicit opt-in. Only applied if a real hardware watchdog exists
    # and no competing watchdog daemon/configuration is detected.
    [switch]$EnableWatchdog,

    # Reboot after ApplySafe.
    [switch]$Reboot,

    # If set, never prompt to select a discovered SSH host.
    [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"


# =====================================================================
# Validate local inputs instead of trying to shell-escape arbitrary text
# =====================================================================

if ($PiHost -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]*$') {
    throw "PiHost contains unsupported characters. Use a hostname or IPv4 address."
}

if ($PiUser -notmatch '^[A-Za-z_][A-Za-z0-9_.-]*$') {
    throw "PiUser is not a valid Linux username for this script."
}

if ($ExpectedHostname -ne "" -and $ExpectedHostname -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]*$') {
    throw "ExpectedHostname contains unsupported characters."
}

if ($ExpectedMac -ne "" -and $ExpectedMac -notmatch '^(?i:[0-9a-f]{2}([:-][0-9a-f]{2}){5})$') {
    throw "ExpectedMac must look like B8-27-EB-E9-27-FB or B8:27:EB:E9:27:FB."
}

if ($ExpectedModel -match "['`r`n]") {
    throw "ExpectedModel contains unsupported characters."
}

if (
    $ServiceName -ne "" -and
    $ServiceName -notmatch '^[A-Za-z0-9_.@-]+\.service$'
) {
    throw "ServiceName must be a normal systemd .service unit name."
}

foreach ($Command in @("ssh.exe", "scp.exe")) {
    if (-not (Get-Command $Command -ErrorAction SilentlyContinue)) {
        throw "$Command was not found. Install/enable the Windows OpenSSH Client."
    }
}


# =====================================================================
# Translate switches into simple, validated remote arguments
# =====================================================================

$RemoteMode = switch ($Mode) {
    "Audit"     { "audit" }
    "ApplySafe" { "apply" }
    "Rollback"  { "rollback" }
}

$WatchdogArg = if ($EnableWatchdog)       { "1" } else { "0" }
$RestartArg  = if ($TuneServiceRestart)   { "1" } else { "0" }
$EnableArg   = if ($EnableServiceAtBoot)  { "1" } else { "0" }
$RebootArg   = if ($Reboot)               { "1" } else { "0" }


# =====================================================================
# Remote Linux script
# =====================================================================

$RemoteScript = @'
#!/usr/bin/env bash

set -Eeuo pipefail


# =====================================================================
# Arguments
# =====================================================================

MODE="${1:-audit}"
SERVICE_REQUEST="${2:-}"
ENABLE_WATCHDOG="${3:-0}"
TUNE_RESTART="${4:-0}"
ENABLE_AT_BOOT="${5:-0}"
LOGIN_USER="${6:-}"
DO_REBOOT="${7:-0}"
EXPECTED_HOSTNAME="${8:-}"
EXPECTED_MAC="${9:-}"
EXPECTED_MODEL="${10:-}"
TARGET_IPV4="${11:-}"


# =====================================================================
# Constants
# =====================================================================

STATE_ROOT="/var/lib/larkbridge-boot-optimizer"

MARK_BEGIN="# BEGIN PLARKBRIDGE SAFE BOOT OPTIMIZATION"
MARK_END="# END PLARKBRIDGE SAFE BOOT OPTIMIZATION"


# =====================================================================
# Helpers
# =====================================================================

say() {
    printf '%s\n' "$*"
}

warn() {
    printf 'WARNING: %s\n' "$*" >&2
}

have_unit() {
    systemctl cat "$1" >/dev/null 2>&1
}

unit_enabled() {
    systemctl is-enabled --quiet "$1" 2>/dev/null
}


# Remove only the exact config.txt section owned by this script.
remove_managed_block() {

    local file="$1"
    local tmp

    tmp="$(mktemp)"

    awk -v b="$MARK_BEGIN" -v e="$MARK_END" '
        $0 == b {
            skip=1
            next
        }

        $0 == e {
            skip=0
            next
        }

        !skip {
            print
        }
    ' "$file" > "$tmp"

    cat "$tmp" > "$file"
    rm -f "$tmp"
}


# Find config.txt only if its location is unambiguous.
find_boot_config() {

    local arr=()
    local p
    local rp
    local seen=""

    for p in \
        /boot/firmware/config.txt \
        /boot/config.txt
    do
        [ -f "$p" ] || continue

        rp="$(readlink -f "$p" 2>/dev/null || printf '%s' "$p")"

        case " $seen " in
            *" $rp "*)
                continue
                ;;
        esac

        seen="$seen $rp"
        arr+=("$rp")
    done

    if [ "${#arr[@]}" -eq 1 ]; then
        printf '%s\n' "${arr[0]}"
        return 0
    fi

    return 1
}


# Report user-level PipeWire/WirePlumber/LarkBridge units.
#
# This is important because making the OS "headless" without understanding
# user-session audio can break the application while making boot appear faster.
user_unit_inventory() {

    local home
    local uid

    home="$(getent passwd "$LOGIN_USER" | cut -d: -f6 || true)"
    uid="$(id -u "$LOGIN_USER")"

    say "User audio/service inventory for $LOGIN_USER (uid=$uid):"

    for d in \
        /usr/lib/systemd/user \
        /lib/systemd/user \
        /etc/systemd/user \
        "$home/.config/systemd/user"
    do
        [ -d "$d" ] || continue

        find "$d" \
            -maxdepth 1 \
            -type f \
            \( \
                -name 'pipewire*.service' \
                -o -name 'pipewire*.socket' \
                -o -name 'wireplumber.service' \
                -o -iname '*lark*bridge*.service' \
                -o -iname '*larkbridge*.service' \
            \) \
            -print 2>/dev/null || true
    done

    # Query the real user manager only if its D-Bus actually exists.
    # Do not infer "enabled" or "running" merely from files being installed.
    if (
        [ -S "/run/user/$uid/bus" ] &&
        command -v runuser >/dev/null 2>&1
    ); then

        say
        say "Current user-manager state:"

        runuser -u "$LOGIN_USER" -- \
            env \
                XDG_RUNTIME_DIR="/run/user/$uid" \
                DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
            systemctl --user \
                --no-pager \
                --plain \
                status \
                pipewire.service \
                pipewire.socket \
                wireplumber.service \
                2>/dev/null |
            head -80 || true

    else

        say
        say "No live user D-Bus was available."
        say "User-service runtime state was therefore NOT guessed."

    fi
}



# Collect a reason without aborting. Used by conservative eligibility checks.
add_reason() {
    if [ -z "${CHECK_REASONS:-}" ]; then
        CHECK_REASONS="$1"
    else
        CHECK_REASONS="${CHECK_REASONS}; $1"
    fi
}


# Persist the exact active NetworkManager profile that carries TARGET_IPV4,
# but only when it is an autoconnecting Ethernet profile currently stored as
# an in-memory /run keyfile. We intentionally keep the same UUID and settings.
#
# `nmcli connection modify` without --temporary requests a persistent profile.
# We make only the already-true autoconnect=yes assignment, then PROVE that a
# same-UUID keyfile now exists under /etc before cloud-init can be disabled.
# If NetworkManager decides there is nothing to save, the proof fails and the
# optimizer leaves cloud-init enabled.
persist_active_runtime_nm_profile_if_safe() {

    local target_iface active_uuid active_type autoconnect before_file after_file
    local persistent_file owner_mode

    if ! command -v nmcli >/dev/null 2>&1; then
        say "Network persistence:"
        say "  nmcli unavailable; no change"
        return 1
    fi

    if ! systemctl is-active --quiet NetworkManager.service 2>/dev/null; then
        say "Network persistence:"
        say "  NetworkManager is not active; no change"
        return 1
    fi

    target_iface="$(
        ip -o -4 addr show 2>/dev/null |
        awk -v ip="$TARGET_IPV4" '$4 ~ ("^" ip "/") {print $2; exit}'
    )"

    if [ -z "$target_iface" ]; then
        say "Network persistence:"
        say "  cannot map $TARGET_IPV4 to a local interface; no change"
        return 1
    fi

    active_uuid="$(
        nmcli -t -f UUID,DEVICE connection show --active 2>/dev/null |
        awk -F: -v dev="$target_iface" '$2 == dev {print $1; exit}'
    )"

    if [ -z "$active_uuid" ]; then
        say "Network persistence:"
        say "  no active NetworkManager profile for $target_iface; no change"
        return 1
    fi

    active_type="$(
        nmcli -g connection.type connection show "$active_uuid" 2>/dev/null |
        head -1 || true
    )"

    if [ "$active_type" != "802-3-ethernet" ] && [ "$active_type" != "ethernet" ]; then
        say "Network persistence:"
        say "  target profile type is '$active_type', not Ethernet; no automatic persistence"
        return 1
    fi

    autoconnect="$(
        nmcli -g connection.autoconnect connection show "$active_uuid" 2>/dev/null |
        head -1 || true
    )"

    if [ "$autoconnect" != "yes" ]; then
        say "Network persistence:"
        say "  target profile is not already autoconnect=yes; refusing to infer policy"
        return 1
    fi

    before_file="$(
        nmcli -t -f UUID,FILENAME connection show 2>/dev/null |
        awk -F: -v u="$active_uuid" '$1 == u {sub(/^[^:]*:/, ""); print; exit}'
    )"

    # If a persistent same-UUID file already exists, nothing needs to be written.
    persistent_file="$(
        grep -RIl \
            --include='*.nmconnection' \
            -E "^uuid=${active_uuid}[[:space:]]*$" \
            /etc/NetworkManager/system-connections \
            2>/dev/null |
        head -1 || true
    )"

    if [ -n "$persistent_file" ]; then
        say "Network persistence:"
        say "  same-UUID persistent profile already exists:"
        say "    $persistent_file"
        return 0
    fi

    case "$before_file" in
        /run/NetworkManager/system-connections/*.nmconnection)
            ;;
        /etc/NetworkManager/system-connections/*.nmconnection)
            say "Network persistence:"
            say "  active profile is already persistent: $before_file"
            return 0
            ;;
        *)
            say "Network persistence:"
            say "  active profile storage is '$before_file'"
            say "  only a proven /run NetworkManager keyfile is eligible for automatic persistence"
            return 1
            ;;
    esac

    # Save the runtime keyfile for diagnostics/rollback evidence before touching
    # the NetworkManager profile. This does not alter the active connection.
    if [ -r "$before_file" ] && [ ! -f "$STATE/network_profile_runtime_before.nmconnection" ]; then
        cp -a -- "$before_file" "$STATE/network_profile_runtime_before.nmconnection"
    fi

    printf '%s\n' "$active_uuid" > "$STATE/network_profile_uuid"
    printf '%s\n' "$target_iface" > "$STATE/network_profile_iface"
    printf '%s\n' "$before_file" > "$STATE/network_profile_runtime_path"

    say "Network persistence:"
    say "  active profile is runtime-only: $before_file"
    say "  requesting persistent storage of the SAME UUID/profile through nmcli"

    # Do not bounce/reapply/down/up the device. This is a profile-storage update
    # only, so the active SSH connection should remain untouched.
    if ! nmcli connection modify uuid "$active_uuid" connection.autoconnect yes; then
        warn "NetworkManager refused to persist the active profile."
        return 1
    fi

    # Verify the address and active profile survived the operation.
    if ! ip -o -4 addr show dev "$target_iface" 2>/dev/null |
         awk -v ip="$TARGET_IPV4" '$4 ~ ("^" ip "/") {found=1} END {exit(found ? 0 : 1)}'
    then
        warn "Target IPv4 disappeared after NetworkManager profile update."
        return 1
    fi

    if ! nmcli -t -f UUID,DEVICE connection show --active 2>/dev/null |
         awk -F: -v u="$active_uuid" -v dev="$target_iface" '$1 == u && $2 == dev {found=1} END {exit(found ? 0 : 1)}'
    then
        warn "The same NetworkManager UUID is no longer active on $target_iface."
        return 1
    fi

    persistent_file="$(
        grep -RIl \
            --include='*.nmconnection' \
            -E "^uuid=${active_uuid}[[:space:]]*$" \
            /etc/NetworkManager/system-connections \
            2>/dev/null |
        head -1 || true
    )"

    if [ -z "$persistent_file" ]; then
        # Some NetworkManager versions may treat a modify-to-the-same-value as
        # a no-op and keep the profile in /run. In that case, use nmcli's
        # documented --offline keyfile writer to materialize the SAME profile
        # (same UUID, same settings) under /etc. We still do not activate,
        # reload, bounce, or reapply the interface in this SSH session.
        local tmp_keyfile dest_keyfile parsed_uuid
        tmp_keyfile="$(mktemp)"
        dest_keyfile="/etc/NetworkManager/system-connections/larkbridge-${target_iface}-${active_uuid}.nmconnection"

        if [ -e "$dest_keyfile" ]; then
            rm -f -- "$tmp_keyfile"
            warn "$dest_keyfile already exists but was not matched as the active UUID; refusing to overwrite it."
            return 1
        fi

        if ! nmcli --offline connection modify connection.autoconnect yes \
                < "$before_file" > "$tmp_keyfile"; then
            rm -f -- "$tmp_keyfile"
            warn "nmcli --offline could not parse/materialize the runtime keyfile."
            return 1
        fi

        parsed_uuid="$(
            awk -F= '
                /^\[connection\]$/ {in_connection=1; next}
                /^\[/ {in_connection=0}
                in_connection && $1 == "uuid" {print $2; exit}
            ' "$tmp_keyfile" 2>/dev/null || true
        )"

        if [ "$parsed_uuid" != "$active_uuid" ]; then
            rm -f -- "$tmp_keyfile"
            warn "Offline materialization changed/lost the profile UUID; refusing to install it."
            return 1
        fi

        install -o root -g root -m 600 -- "$tmp_keyfile" "$dest_keyfile"
        rm -f -- "$tmp_keyfile"

        persistent_file="$dest_keyfile"
        say "  nmcli modify did not materialize /etc storage directly"
        say "  created a validated same-UUID keyfile with nmcli --offline instead"
    fi

    owner_mode="$(stat -c '%U:%G:%a' "$persistent_file" 2>/dev/null || true)"
    if [ "$owner_mode" != "root:root:600" ]; then
        warn "Persistent profile exists but ownership/mode is '$owner_mode', expected root:root:600."
        warn "Cloud-init will remain enabled rather than changing permissions automatically."
        return 1
    fi

    after_file="$(
        nmcli -t -f UUID,FILENAME connection show 2>/dev/null |
        awk -F: -v u="$active_uuid" '$1 == u {sub(/^[^:]*:/, ""); print; exit}'
    )"

    printf '%s\n' "$persistent_file" > "$STATE/network_profile_persistent_path"

    say "  verified same UUID: $active_uuid"
    say "  verified persistent keyfile: $persistent_file"
    say "  verified ownership/mode: $owner_mode"
    say "  active NetworkManager-reported file: ${after_file:-unknown}"
    say "  SSH-target IPv4 remained present"

    return 0
}


# Return success only when cloud-init can be disabled without relying on guesses.
#
# This deliberately requires stronger evidence than merely seeing NoCloud.
# If any proof is missing, ApplySafe leaves cloud-init enabled and tells the user why.
cloud_init_safe_to_disable() {

    CHECK_REASONS=""

    if ! command -v cloud-init >/dev/null 2>&1; then
        add_reason "cloud-init executable is not installed"
        return 1
    fi

    if [ -e /etc/cloud/cloud-init.disabled ]; then
        return 0
    fi

    local status
    status="$(cloud-init status --long 2>&1 || true)"

    if ! printf '%s\n' "$status" | grep -Eq '^status:[[:space:]]+done$'; then
        add_reason "cloud-init has not reached status=done"
    fi

    if ! printf '%s\n' "$status" | grep -Fq 'DataSourceNoCloud'; then
        add_reason "active cloud-init datasource is not proven to be NoCloud"
    fi

    if ! printf '%s\n' "$status" | grep -Fq 'file:///boot/firmware'; then
        add_reason "NoCloud seed is not proven to be local /boot/firmware data"
    fi

    # Root/network filesystems must not depend on cloud-init networking.
    local rootfs
    rootfs="$(findmnt -nro FSTYPE / 2>/dev/null || true)"

    if [[ "$rootfs" =~ ^nfs ]]; then
        add_reason "root filesystem is network-backed"
    fi

    if grep -Eq \
        '(^|[[:space:]])(nfsroot=|root=/dev/nfs|rd\.neednet=1)([[:space:]]|$)' \
        /proc/cmdline 2>/dev/null
    then
        add_reason "kernel command line requires boot-time networking"
    fi

    if findmnt -rn -t nfs,nfs4,cifs,fuse.sshfs 2>/dev/null | grep -q .; then
        add_reason "a network filesystem is currently mounted"
    fi

    if [ -r /etc/fstab ] &&
       grep -Ev '^[[:space:]]*(#|$)' /etc/fstab |
       grep -Eiq '(^|[[:space:],])(nfs|nfs4|cifs|sshfs|_netdev)([[:space:],]|$)'
    then
        add_reason "/etc/fstab contains a network-dependent mount"
    fi

    # The SSH/login user must already be a local persistent account.
    if ! awk -F: -v u="$LOGIN_USER" '
        $1 == u { found=1 }
        END { exit(found ? 0 : 1) }
    ' /etc/passwd 2>/dev/null
    then
        add_reason "$LOGIN_USER is not proven to be a local /etc/passwd account"
    fi

    # Hostname must already be persisted outside cloud-init.
    local current_hostname file_hostname
    current_hostname="$(hostname 2>/dev/null || true)"
    file_hostname="$(tr -d '[:space:]' </etc/hostname 2>/dev/null || true)"

    if [ -z "$current_hostname" ] || [ "$current_hostname" != "$file_hostname" ]; then
        add_reason "current hostname is not proven to be persisted in /etc/hostname"
    fi

    # SSH must survive the reboot independently of cloud-init.
    if ! systemctl is-enabled --quiet ssh.service 2>/dev/null; then
        add_reason "ssh.service is not enabled for boot"
    fi

    if ! systemctl is-active --quiet ssh.service 2>/dev/null; then
        add_reason "ssh.service is not currently active"
    fi

    # Current architecture relies on the admin user manager starting at boot.
    local linger
    linger="$(loginctl show-user "$LOGIN_USER" -p Linger --value 2>/dev/null || true)"
    if [ "$linger" != "yes" ]; then
        add_reason "systemd lingering is not enabled for $LOGIN_USER"
    fi

    # Prove the user-level audio services that this appliance relies on are
    # enabled independently of an interactive login.
    local login_uid
    login_uid="$(id -u "$LOGIN_USER" 2>/dev/null || true)"

    if [ -z "$login_uid" ] || [ ! -S "/run/user/$login_uid/bus" ]; then
        add_reason "live systemd user-manager D-Bus is not available for $LOGIN_USER"
    elif command -v runuser >/dev/null 2>&1; then
        for audio_unit in pipewire.socket pipewire.service wireplumber.service; do
            if ! runuser -u "$LOGIN_USER" -- \
                env \
                    XDG_RUNTIME_DIR="/run/user/$login_uid" \
                    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$login_uid/bus" \
                systemctl --user is-enabled --quiet "$audio_unit" \
                2>/dev/null
            then
                add_reason "$audio_unit is not enabled in $LOGIN_USER's user manager"
            fi
        done
    else
        add_reason "runuser is unavailable, so user audio enablement cannot be proven"
    fi

    # NetworkManager must be the active, boot-enabled network owner.
    if ! command -v nmcli >/dev/null 2>&1; then
        add_reason "nmcli is unavailable"
    elif ! systemctl is-active --quiet NetworkManager.service 2>/dev/null; then
        add_reason "NetworkManager.service is not active"
    elif ! systemctl is-enabled --quiet NetworkManager.service 2>/dev/null; then
        add_reason "NetworkManager.service is not enabled"
    else
        # Prove that the exact interface carrying the SSH target address has
        # a persistent, autoconnecting NetworkManager profile in /etc.
        local target_iface active_uuid autoconnect profile_file
        target_iface="$(
            ip -o -4 addr show 2>/dev/null |
            awk -v ip="$TARGET_IPV4" '$4 ~ ("^" ip "/") {print $2; exit}'
        )"

        if [ -z "$target_iface" ]; then
            add_reason "could not map target IPv4 $TARGET_IPV4 to a local interface"
        else
            active_uuid="$(
                nmcli -t -f UUID,DEVICE connection show --active 2>/dev/null |
                awk -F: -v dev="$target_iface" '$2 == dev {print $1; exit}'
            )"

            if [ -z "$active_uuid" ]; then
                add_reason "no active NetworkManager profile is proven for $target_iface"
            else
                autoconnect="$(
                    nmcli -g connection.autoconnect connection show "$active_uuid" \
                        2>/dev/null | head -1 || true
                )"

                if [ "$autoconnect" != "yes" ]; then
                    add_reason "active NetworkManager profile is not configured to autoconnect"
                fi

                profile_file="$(
                    grep -RIl \
                        --include='*.nmconnection' \
                        -E "^uuid=${active_uuid}[[:space:]]*$" \
                        /etc/NetworkManager/system-connections \
                        2>/dev/null |
                    head -1 || true
                )"

                if [ -z "$profile_file" ]; then
                    add_reason "active NetworkManager profile is not proven persistent under /etc/NetworkManager/system-connections"
                fi
            fi
        fi
    fi

    # cloud-init has an explicitly per-boot script mechanism. Never disable it
    # if the machine is actually using that mechanism.
    if [ -d /var/lib/cloud/scripts/per-boot ] &&
       find /var/lib/cloud/scripts/per-boot -type f -print -quit 2>/dev/null |
       grep -q .
    then
        add_reason "cloud-init per-boot scripts are present in /var/lib/cloud/scripts/per-boot"
    fi

    # bootcmd and cloud-boothook can run on every boot. Check both the
    # Raspberry Pi seed and cloud-init's current instance copy.
    local userdata
    for userdata in \
        /boot/firmware/user-data \
        /var/lib/cloud/instance/user-data.txt
    do
        if [ -r "$userdata" ] &&
           grep -Eq '^[[:space:]]*bootcmd[[:space:]]*:' "$userdata"
        then
            add_reason "cloud-init user-data contains bootcmd in $userdata"
        fi

        if [ -r "$userdata" ] &&
           grep -Eiq '(#cloud-boothook|text/cloud-boothook)' "$userdata"
        then
            add_reason "cloud-init user-data contains a cloud-boothook in $userdata"
        fi
    done

    if [ -d /boot/firmware/scripts/per-boot ] &&
       find /boot/firmware/scripts/per-boot -type f -print -quit 2>/dev/null |
       grep -q .
    then
        add_reason "NoCloud seed contains scripts/per-boot content under /boot/firmware"
    fi

    if [ -n "$CHECK_REASONS" ]; then
        return 1
    fi

    return 0
}


# Inspect the bridge-specific PipeWire drop-in without changing it.
pipewire_bridge_config_audit() {

    local home conf noncomment pwerr

    home="$(getent passwd "$LOGIN_USER" | cut -d: -f6 || true)"
    conf="$home/.config/pipewire/pipewire.conf.d/20-bridge-endpoints.conf"

    if [ ! -f "$conf" ]; then
        say "20-bridge-endpoints.conf: not present"
        return 0
    fi

    noncomment="$(
        grep -Ev '^[[:space:]]*(#.*)?$' "$conf" 2>/dev/null || true
    )"

    if [ -z "$noncomment" ]; then
        say "20-bridge-endpoints.conf: comment/blank-only"

        if command -v pw-config >/dev/null 2>&1 &&
           command -v runuser >/dev/null 2>&1
        then
            pwerr="$(
                runuser -u "$LOGIN_USER" -- \
                    env HOME="$home" \
                    pw-config -n pipewire.conf list \
                    2>&1 >/dev/null || true
            )"

            if printf '%s\n' "$pwerr" | grep -Fq "$conf"; then
                say "20-bridge-endpoints.conf: currently causes a PipeWire parse diagnostic"
            else
                say "20-bridge-endpoints.conf: no matching PipeWire parse diagnostic observed"
            fi
        fi
    else
        say "20-bridge-endpoints.conf: contains active configuration; optimizer will never rename it automatically"
    fi
}


# Rename the known stale documentation-only .conf only when all conditions
# prove that it is the source of the parse error. No audio services are restarted;
# the corrected file set takes effect naturally on the next boot/restart.
fix_stale_pipewire_bridge_dropin() {

    local home conf notes noncomment before after

    home="$(getent passwd "$LOGIN_USER" | cut -d: -f6 || true)"
    conf="$home/.config/pipewire/pipewire.conf.d/20-bridge-endpoints.conf"
    notes="$home/.config/pipewire/pipewire.conf.d/20-bridge-endpoints.notes.txt"

    [ -f "$conf" ] || return 0

    noncomment="$(
        grep -Ev '^[[:space:]]*(#.*)?$' "$conf" 2>/dev/null || true
    )"

    if [ -n "$noncomment" ]; then
        say "PipeWire:"
        say "  kept $conf because it contains active configuration"
        return 0
    fi

    if ! command -v pw-config >/dev/null 2>&1 ||
       ! command -v runuser >/dev/null 2>&1
    then
        warn "Cannot validate the comment-only PipeWire drop-in; leaving it unchanged."
        return 0
    fi

    before="$(
        runuser -u "$LOGIN_USER" -- \
            env HOME="$home" \
            pw-config -n pipewire.conf list \
            2>&1 >/dev/null || true
    )"

    if ! printf '%s\n' "$before" | grep -Fq "$conf"; then
        say "PipeWire:"
        say "  comment-only file exists but is not proven to cause the current parse diagnostic"
        say "  leaving it unchanged"
        return 0
    fi

    if [ -e "$notes" ]; then
        warn "$notes already exists; refusing to overwrite it."
        return 0
    fi

    mv -- "$conf" "$notes"

    after="$(
        runuser -u "$LOGIN_USER" -- \
            env HOME="$home" \
            pw-config -n pipewire.conf list \
            2>&1 >/dev/null || true
    )"

    if printf '%s\n' "$after" | grep -Fq 'error loading config'; then
        mv -- "$notes" "$conf"
        warn "PipeWire validation still reports a config-loading error; rename was rolled back immediately."
        return 0
    fi

    printf '%s\n' "$conf"  > "$STATE/pipewire_old_path"
    printf '%s\n' "$notes" > "$STATE/pipewire_new_path"

    say "PipeWire:"
    say "  renamed stale comment-only drop-in:"
    say "    $conf"
    say "  to documentation file:"
    say "    $notes"
    say "  pw-config validation passed"
}

# Select a LarkBridge service only if explicitly supplied or uniquely detected.
select_service() {

    local candidates=()
    local u

    SELECTED_SERVICE=""

    if [ -n "$SERVICE_REQUEST" ]; then

        if have_unit "$SERVICE_REQUEST"; then
            SELECTED_SERVICE="$SERVICE_REQUEST"
            return 0
        fi

        warn "Requested system service '$SERVICE_REQUEST' does not exist."
        warn "No service-specific changes will be made."

        return 1
    fi


    while IFS= read -r u; do
        [ -n "$u" ] && candidates+=("$u")
    done < <(
        systemctl list-unit-files \
            --type=service \
            --no-legend \
            2>/dev/null |
        awk '{print $1}' |
        grep -Ei \
            '(^|[-_.])(p?lark[-_]?bridge|larkbridge)([-_.]|$)' \
            || true
    )


    if [ "${#candidates[@]}" -eq 1 ]; then

        SELECTED_SERVICE="${candidates[0]}"

        say "Auto-detected exactly one system service:"
        say "  $SELECTED_SERVICE"

        return 0
    fi


    if [ "${#candidates[@]}" -gt 1 ]; then

        warn "Multiple possible LarkBridge services were found."
        warn "Refusing to choose one automatically:"
        warn "${candidates[*]}"

    else

        say "No uniquely identifiable LarkBridge SYSTEM service was found."

    fi

    return 1
}


# Determine whether removing network wait-online is actually safe.
#
# This intentionally errs on the side of keeping wait-online.
network_wait_is_safe_to_remove() {

    local consumers
    local filtered
    local rootfs

    SAFE_WAIT_REASON=""

    rootfs="$(findmnt -nro FSTYPE / 2>/dev/null || true)"

    if [[ "$rootfs" =~ ^nfs ]]; then
        SAFE_WAIT_REASON="root filesystem is network-backed"
        return 1
    fi


    if grep -Eq \
        '(^|[[:space:]])(nfsroot=|root=/dev/nfs|rd\.neednet=1)([[:space:]]|$)' \
        /proc/cmdline \
        2>/dev/null
    then
        SAFE_WAIT_REASON="kernel command line indicates boot-time networking"
        return 1
    fi


    if findmnt \
        -rn \
        -t nfs,nfs4,cifs,fuse.sshfs \
        2>/dev/null |
        grep -q .
    then
        SAFE_WAIT_REASON="a network filesystem is currently mounted"
        return 1
    fi


    if (
        [ -r /etc/fstab ] &&
        grep -Ev \
            '^[[:space:]]*(#|$)' \
            /etc/fstab |
        grep -Eiq \
            '(^|[[:space:],])(nfs|nfs4|cifs|sshfs|_netdev)([[:space:],]|$)'
    ); then

        SAFE_WAIT_REASON="/etc/fstab contains a network-dependent mount"
        return 1
    fi


    # Look for things that actually pull network-online.target into boot.
    consumers="$(
        systemctl list-dependencies \
            --reverse \
            network-online.target \
            --all \
            --plain \
            --no-legend \
            2>/dev/null |
        sed -E 's/^[^[:alnum:]@_.-]*//' |
        awk '{print $1}' |
        grep -E '\.(service|mount|automount|target)$' \
        || true
    )"


    # Remove the target itself and the two wait implementations.
    filtered="$(
        printf '%s\n' "$consumers" |
        grep -Ev \
            '^(network-online\.target|NetworkManager-wait-online\.service|systemd-networkd-wait-online\.service)$' \
        || true
    )"


    if [ -n "$filtered" ]; then

        SAFE_WAIT_REASON="$(
            printf \
                'network-online.target has reverse dependents: %s' \
                "$(echo "$filtered" | tr '\n' ' ')"
        )"

        return 1
    fi


    return 0
}


# =====================================================================
# Lingering-user login barrier optimization
#
# Debian/systemd 257 currently ships:
#   /usr/lib/systemd/system/user@.service.d/10-login-barrier.conf
# which adds:
#   After=systemd-user-sessions.service
#
# systemd-user-sessions.service in turn has After=network.target. For a
# deliberately lingering appliance user, this serializes the user's systemd
# manager (and therefore PipeWire/WirePlumber) behind networking even though
# the user services themselves do not require networking.
#
# We DO NOT copy/replace user@.service and we DO NOT modify the vendor file.
# We only attempt an instance-specific same-name /dev/null mask for the login
# user's user@UID.service drop-in. After daemon-reload we prove that the
# effective dependency disappeared. If it did not, we immediately undo the
# attempted mask.
# =====================================================================

login_barrier_check() {

    LOGIN_BARRIER_REASONS=""
    LOGIN_BARRIER_UID="$(id -u "$LOGIN_USER" 2>/dev/null || true)"
    LOGIN_BARRIER_VENDOR="/usr/lib/systemd/system/user@.service.d/10-login-barrier.conf"
    LOGIN_BARRIER_INSTANCE_DIR=""
    LOGIN_BARRIER_INSTANCE_MASK=""

    add_lb_reason() {
        if [ -z "$LOGIN_BARRIER_REASONS" ]; then
            LOGIN_BARRIER_REASONS="$1"
        else
            LOGIN_BARRIER_REASONS="$LOGIN_BARRIER_REASONS; $1"
        fi
    }

    if [ -z "$LOGIN_BARRIER_UID" ]; then
        add_lb_reason "login user UID could not be resolved"
        return 1
    fi

    if [ "$LOGIN_BARRIER_UID" = "0" ]; then
        add_lb_reason "login user is root; login-barrier optimization is intentionally not applied to root"
        return 1
    fi

    LOGIN_BARRIER_INSTANCE_DIR="/etc/systemd/system/user@${LOGIN_BARRIER_UID}.service.d"
    LOGIN_BARRIER_INSTANCE_MASK="$LOGIN_BARRIER_INSTANCE_DIR/10-login-barrier.conf"

    if [ "$(loginctl show-user "$LOGIN_USER" -p Linger --value 2>/dev/null || true)" != "yes" ]; then
        add_lb_reason "$LOGIN_USER is not configured with Linger=yes"
    fi

    if ! systemctl is-active --quiet "user@${LOGIN_BARRIER_UID}.service" 2>/dev/null; then
        add_lb_reason "user@${LOGIN_BARRIER_UID}.service is not currently active"
    fi

    if [ ! -f "$LOGIN_BARRIER_VENDOR" ]; then
        add_lb_reason "expected vendor login-barrier drop-in is not present"
    else
        # Be intentionally strict. The known vendor file may contain arbitrary
        # comments, but the only active statements must be [Unit] and exactly
        # After=systemd-user-sessions.service. If a package update changes the
        # semantics, stop and require a new audit instead of masking new logic.
        local normalized
        normalized="$(
            sed -E \
                -e 's/\r$//' \
                -e 's/^[[:space:]]+//' \
                -e 's/[[:space:]]+$//' \
                "$LOGIN_BARRIER_VENDOR" |
            grep -Ev '^(#|;|$)' |
            sed -E 's/[[:space:]]*=[[:space:]]*/=/'
        )"

        if [ "$normalized" != $'[Unit]\nAfter=systemd-user-sessions.service' ]; then
            add_lb_reason "vendor 10-login-barrier.conf no longer has the audited minimal contents"
        fi
    fi

    # A global administrator override with the same basename would change the
    # precedence story. Refuse to guess around it.
    if [ -e /etc/systemd/system/user@.service.d/10-login-barrier.conf ] ||
       [ -L /etc/systemd/system/user@.service.d/10-login-barrier.conf ]
    then
        add_lb_reason "global /etc user@.service.d/10-login-barrier.conf already exists"
    fi

    # Refuse to overwrite any administrator-created instance-specific file.
    # A /dev/null symlink is accepted only if it is already ours/desired state.
    if [ -e "$LOGIN_BARRIER_INSTANCE_MASK" ] || [ -L "$LOGIN_BARRIER_INSTANCE_MASK" ]; then
        if [ ! -L "$LOGIN_BARRIER_INSTANCE_MASK" ] ||
           [ "$(readlink "$LOGIN_BARRIER_INSTANCE_MASK" 2>/dev/null || true)" != "/dev/null" ]
        then
            add_lb_reason "instance-specific 10-login-barrier.conf already exists and is not a /dev/null mask"
        fi
    fi

    local user_after sessions_after
    user_after="$(systemctl show "user@${LOGIN_BARRIER_UID}.service" -p After --value 2>/dev/null || true)"
    sessions_after="$(systemctl show systemd-user-sessions.service -p After --value 2>/dev/null || true)"

    # If the instance mask is already effective, that is a valid optimized state.
    if [ -L "$LOGIN_BARRIER_INSTANCE_MASK" ] &&
       [ "$(readlink "$LOGIN_BARRIER_INSTANCE_MASK" 2>/dev/null || true)" = "/dev/null" ]
    then
        if ! grep -qw 'systemd-user-sessions.service' <<<"$user_after"; then
            return 0
        fi
        add_lb_reason "existing instance mask is present but systemd-user-sessions.service is still in effective After="
    else
        if ! grep -qw 'systemd-user-sessions.service' <<<"$user_after"; then
            add_lb_reason "user@${LOGIN_BARRIER_UID}.service is not currently delayed by systemd-user-sessions.service"
        fi
    fi

    if ! grep -qw 'network.target' <<<"$sessions_after"; then
        add_lb_reason "systemd-user-sessions.service is not currently ordered after network.target"
    fi

    [ -z "$LOGIN_BARRIER_REASONS" ]
}


apply_login_barrier_optimization_if_safe() {

    if ! login_barrier_check; then
        say "Lingering-user startup:"
        say "  unchanged"
        say "  reason(s): $LOGIN_BARRIER_REASONS"
        return 1
    fi

    # Already in the desired state.
    if [ -L "$LOGIN_BARRIER_INSTANCE_MASK" ] &&
       [ "$(readlink "$LOGIN_BARRIER_INSTANCE_MASK" 2>/dev/null || true)" = "/dev/null" ]
    then
        say "Lingering-user startup:"
        say "  instance login-barrier mask already present and effective"
        say "  user@${LOGIN_BARRIER_UID}.service is not ordered after systemd-user-sessions.service"
        return 0
    fi

    mkdir -p "$LOGIN_BARRIER_INSTANCE_DIR"
    ln -s /dev/null "$LOGIN_BARRIER_INSTANCE_MASK"

    systemctl daemon-reload

    local after_now verify_output
    after_now="$(systemctl show "user@${LOGIN_BARRIER_UID}.service" -p After --value 2>/dev/null || true)"

    if grep -qw 'systemd-user-sessions.service' <<<"$after_now"; then
        rm -f "$LOGIN_BARRIER_INSTANCE_MASK"
        rmdir "$LOGIN_BARRIER_INSTANCE_DIR" 2>/dev/null || true
        systemctl daemon-reload
        warn "Instance-specific login-barrier mask did not remove the effective After= dependency."
        warn "The attempted change was rolled back immediately."
        return 1
    fi

    # There is no new unit syntax to validate here—the local object is only a
    # /dev/null symlink. Validate the *effective* instance instead: systemctl
    # must still be able to load/cat it, and the one ordering edge we intend to
    # remove must actually be absent.
    if ! systemctl cat "user@${LOGIN_BARRIER_UID}.service" >/dev/null 2>&1; then
        rm -f "$LOGIN_BARRIER_INSTANCE_MASK"
        rmdir "$LOGIN_BARRIER_INSTANCE_DIR" 2>/dev/null || true
        systemctl daemon-reload
        warn "systemctl could not load the user manager after the attempted instance mask."
        warn "The attempted change was rolled back immediately."
        return 1
    fi

    printf '%s\n' "$LOGIN_BARRIER_INSTANCE_MASK" > "$STATE/login_barrier_instance_mask"
    printf '%s\n' "$LOGIN_BARRIER_VENDOR" > "$STATE/login_barrier_vendor_path"
    sha256sum "$LOGIN_BARRIER_VENDOR" 2>/dev/null | awk '{print $1}' \
        > "$STATE/login_barrier_vendor_sha256" || true

    say "Lingering-user startup:"
    say "  masked only the vendor login barrier for user@${LOGIN_BARRIER_UID}.service"
    say "  created: $LOGIN_BARRIER_INSTANCE_MASK -> /dev/null"
    say "  verified effective After= no longer contains systemd-user-sessions.service"
    say "  effective user@ instance still loads successfully"
    say "  current running user manager was NOT restarted; change takes effect on next boot"

    return 0
}


login_barrier_audit() {

    local uid vendor instance after sessions_after
    uid="$(id -u "$LOGIN_USER" 2>/dev/null || true)"

    if [ -z "$uid" ]; then
        echo "login user UID could not be resolved"
        return 0
    fi

    vendor="/usr/lib/systemd/system/user@.service.d/10-login-barrier.conf"
    instance="/etc/systemd/system/user@${uid}.service.d/10-login-barrier.conf"
    after="$(systemctl show "user@${uid}.service" -p After --value 2>/dev/null || true)"
    sessions_after="$(systemctl show systemd-user-sessions.service -p After --value 2>/dev/null || true)"

    echo "uid=$uid linger=$(loginctl show-user "$LOGIN_USER" -p Linger --value 2>/dev/null || true)"
    echo "vendor_dropin=$vendor"

    if [ -L "$instance" ]; then
        echo "instance_override=$instance -> $(readlink "$instance" 2>/dev/null || true)"
    elif [ -e "$instance" ]; then
        echo "instance_override=$instance (regular file)"
    else
        echo "instance_override=not present"
    fi

    if grep -qw 'systemd-user-sessions.service' <<<"$after"; then
        echo "effective_login_barrier=present"
    else
        echo "effective_login_barrier=absent"
    fi

    if grep -qw 'network.target' <<<"$sessions_after"; then
        echo "user_sessions_waits_for_network=yes"
    else
        echo "user_sessions_waits_for_network=no"
    fi

    if login_barrier_check; then
        if [ -L "$LOGIN_BARRIER_INSTANCE_MASK" ] &&
           [ "$(readlink "$LOGIN_BARRIER_INSTANCE_MASK" 2>/dev/null || true)" = "/dev/null" ]
        then
            echo "state=optimized and verified"
        else
            echo "state=ELIGIBLE for instance-specific login-barrier mask"
        fi
    else
        echo "state=NOT ELIGIBLE: $LOGIN_BARRIER_REASONS"
    fi
}



# =====================================================================
# BCM43438 SCO-over-HCI boot-race hardening
#
# Observed on this exact Pi:
#   * During boot, Read_SCO_PCM_Int_Param was temporarily unreadable and
#     Write_SCO_PCM_Int_Param was rejected.
#   * Later in the same boot, the same read returned 00 02 00 01 01 and
#     the same write succeeded; readback verified 01 02 00 01 01.
#
# Therefore the safe fix is bounded retry of the ACTUAL vendor read/write,
# not a blind fixed delay. The updater refuses to replace an unfamiliar
# script or service arrangement.
# =====================================================================

BTFW_SERVICE="bridge-btfw.service"
BTFW_SCRIPT="/usr/local/lib/rpi-lark-bridge/set-sco-routing.sh"
BTFW_RETRY_MARKER="BRIDGE_BTFW_RETRY_V1"

btfw_retry_check() {
    BTFW_RETRY_REASONS=""

    add_btfw_reason() {
        if [ -n "$BTFW_RETRY_REASONS" ]; then
            BTFW_RETRY_REASONS="$BTFW_RETRY_REASONS; $1"
        else
            BTFW_RETRY_REASONS="$1"
        fi
    }

    [ "$MODEL" = "$EXPECTED_MODEL" ] || add_btfw_reason "hardware model does not match the audited Pi 3B Rev 1.2"
    have_unit "$BTFW_SERVICE" || add_btfw_reason "$BTFW_SERVICE is not installed"
    [ -f "$BTFW_SCRIPT" ] || add_btfw_reason "$BTFW_SCRIPT is not present"
    command -v hcitool >/dev/null 2>&1 || add_btfw_reason "hcitool is not installed"

    if have_unit "$BTFW_SERVICE"; then
        local exec_path unit_type remain requires after
        exec_path="$(systemctl show "$BTFW_SERVICE" -p ExecStart --value 2>/dev/null || true)"
        unit_type="$(systemctl show "$BTFW_SERVICE" -p Type --value 2>/dev/null || true)"
        remain="$(systemctl show "$BTFW_SERVICE" -p RemainAfterExit --value 2>/dev/null || true)"
        requires="$(systemctl show "$BTFW_SERVICE" -p Requires --value 2>/dev/null || true)"
        after="$(systemctl show "$BTFW_SERVICE" -p After --value 2>/dev/null || true)"

        grep -Fq "$BTFW_SCRIPT" <<<"$exec_path" || add_btfw_reason "service ExecStart is not the audited SCO-routing script"
        [ "$unit_type" = "oneshot" ] || add_btfw_reason "service is not Type=oneshot"
        [ "$remain" = "yes" ] || add_btfw_reason "service does not use RemainAfterExit=yes"
        grep -qw 'bluetooth.service' <<<"$requires" || add_btfw_reason "service does not Require=bluetooth.service"
        grep -qw 'bluetooth.service' <<<"$after" || add_btfw_reason "service is not ordered After=bluetooth.service"
    fi

    if [ -f "$BTFW_SCRIPT" ] && ! grep -Fq "$BTFW_RETRY_MARKER" "$BTFW_SCRIPT"; then
        # Semantic fingerprints of the exact audited implementation. We do not
        # overwrite a script that uses different vendor opcodes, parameters, or
        # an already-custom readiness mechanism.
        grep -Fq 'hcitool -i "$HCI" cmd 0x3f 0x1d' "$BTFW_SCRIPT" || add_btfw_reason "read opcode no longer matches audited implementation"
        grep -Fq 'hcitool -i "$HCI" cmd 0x3f 0x1c 0x01 0x02 0x00 0x01 0x01' "$BTFW_SCRIPT" || add_btfw_reason "write opcode/parameters no longer match audited implementation"
        grep -Fq 'WANT_ROUTING="01"' "$BTFW_SCRIPT" || add_btfw_reason "desired routing value is no longer 0x01"
        grep -Fq 'controller rejected Write_SCO_PCM_Int_Param' "$BTFW_SCRIPT" || add_btfw_reason "script no longer matches the audited single-attempt behavior"
    fi

    [ -z "$BTFW_RETRY_REASONS" ]
}

btfw_retry_audit() {
    echo "=== Bluetooth SCO routing readiness ==="

    if [ ! -f "$BTFW_SCRIPT" ]; then
        echo "script=not-present"
        return 0
    fi

    if grep -Fq "$BTFW_RETRY_MARKER" "$BTFW_SCRIPT"; then
        echo "script=bounded-retry-v1"
    else
        echo "script=legacy-single-attempt"
    fi

    if have_unit "$BTFW_SERVICE"; then
        local btfw_active btfw_failed
        btfw_active="$(systemctl is-active "$BTFW_SERVICE" 2>/dev/null || true)"
        btfw_failed="no"
        if systemctl is-failed --quiet "$BTFW_SERVICE" 2>/dev/null; then
            btfw_failed="yes"
        fi
        printf 'service_active=%s service_failed=%s\n' "$btfw_active" "$btfw_failed"
    fi

    local params
    params="$(hcitool -i hci0 cmd 0x3f 0x1d 2>/dev/null \
        | tr -s ' \n' ' ' \
        | grep -oE '01 1D FC [0-9A-F]{2}( [0-9A-F]{2}){5}' \
        | tail -1 \
        | awk '$4 == "00" {print $5, $6, $7, $8, $9}' || true)"

    if [ -n "$params" ]; then
        echo "current_params=$params"
    else
        echo "current_params=unreadable"
    fi

    if btfw_retry_check; then
        if grep -Fq "$BTFW_RETRY_MARKER" "$BTFW_SCRIPT"; then
            echo "state=optimized and verified"
        else
            echo "state=ELIGIBLE for bounded vendor-command retry hardening"
        fi
    else
        echo "state=NOT ELIGIBLE: $BTFW_RETRY_REASONS"
    fi
}

apply_btfw_retry_hardening_if_safe() {
    if [ ! -f "$BTFW_SCRIPT" ]; then
        return 0
    fi

    if grep -Fq "$BTFW_RETRY_MARKER" "$BTFW_SCRIPT"; then
        say "Bluetooth SCO routing:"
        say "  bounded vendor-command retry hardening is already installed"
        return 0
    fi

    if ! btfw_retry_check; then
        say "Bluetooth SCO routing:"
        say "  kept existing script"
        say "  reason(s): $BTFW_RETRY_REASONS"
        return 1
    fi

    if [ ! -f "$STATE/btfw_script.before" ]; then
        cp -a "$BTFW_SCRIPT" "$STATE/btfw_script.before"
        printf '%s\n' "$BTFW_SCRIPT" > "$STATE/btfw_script_path"
    fi

    local tmp
    tmp="$(mktemp)"

    cat > "$tmp" <<'BTFW_SCRIPT_EOF'
#!/usr/bin/env bash
# BRIDGE_BTFW_RETRY_V1
# Set Broadcom BCM43438 SCO routing to the HCI transport and verify it.
#
# The controller can briefly reject Broadcom vendor commands during boot even
# after bluetooth.service has started. Retry the actual read/write operations
# with one strict overall bound instead of sleeping for an arbitrary delay.

set -euo pipefail

HCI="${BRIDGE_HCI:-hci0}"
WANT_ROUTING="01"
MAX_ATTEMPTS="${BRIDGE_BT_MAX_ATTEMPTS:-40}"
RETRY_DELAY="${BRIDGE_BT_RETRY_DELAY:-0.25}"
# After a write is accepted, give firmware four polling intervals to expose the
# new value before trying another write. With defaults this is about one second.
WRITE_COOLDOWN_ATTEMPTS="${BRIDGE_BT_WRITE_COOLDOWN_ATTEMPTS:-4}"

log() { printf '[bridge-btfw] %s\n' "$*"; }
die() { printf '[bridge-btfw] ERROR: %s\n' "$*" >&2; exit 1; }

command -v hcitool >/dev/null 2>&1 || die "hcitool not found (package: bluez)"

read_params() {
    # Command Complete payload: 01 1D FC <status> <5 params>
    # Only return parameters when HCI command status is success (00).
    hcitool -i "$HCI" cmd 0x3f 0x1d 2>/dev/null \
        | tr -s ' \n' ' ' \
        | grep -oE '01 1D FC [0-9A-F]{2}( [0-9A-F]{2}){5}' \
        | tail -1 \
        | awk '$4 == "00" {print $5, $6, $7, $8, $9}'
}

last_params=""
write_cooldown=0
saw_readable=0

for ((attempt=1; attempt<=MAX_ATTEMPTS; attempt++)); do
    params="$(read_params || true)"

    if [ -n "$params" ]; then
        if [ "$saw_readable" -eq 0 ] || [ "$params" != "$last_params" ]; then
            log "SCO PCM params: $params"
        fi
        saw_readable=1
        last_params="$params"

        routing="${params%% *}"
        if [ "$routing" = "$WANT_ROUTING" ]; then
            log "verified: SCO routed to the HCI transport"
            exit 0
        fi

        if [ "$write_cooldown" -le 0 ]; then
            log "SCO routing is 0x$routing — requesting 0x01 (attempt ${attempt}/${MAX_ATTEMPTS})"

            if hcitool -i "$HCI" cmd 0x3f 0x1c 0x01 0x02 0x00 0x01 0x01 >/dev/null 2>&1; then
                log "Write_SCO_PCM_Int_Param accepted; waiting for verified readback"
                write_cooldown="$WRITE_COOLDOWN_ATTEMPTS"
            else
                log "controller rejected Write_SCO_PCM_Int_Param (attempt ${attempt}/${MAX_ATTEMPTS})" >&2
            fi
        fi
    else
        if [ "$attempt" -eq 1 ] || [ $((attempt % 5)) -eq 0 ]; then
            log "controller not ready for SCO vendor read (attempt ${attempt}/${MAX_ATTEMPTS})" >&2
        fi
    fi

    if [ "$write_cooldown" -gt 0 ]; then
        write_cooldown=$((write_cooldown - 1))
    fi

    sleep "$RETRY_DELAY"
done

FINAL="$(read_params || true)"
if [ -n "$FINAL" ]; then
    die "could not establish Transport(HCI) routing within the bounded retry window; final params: $FINAL"
elif [ "$saw_readable" -eq 1 ]; then
    die "SCO parameters became unreadable before Transport(HCI) routing could be verified"
else
    die "controller never became ready for Read_SCO_PCM_Int_Param within the bounded retry window"
fi
BTFW_SCRIPT_EOF

    chmod 0755 "$tmp"

    if ! bash -n "$tmp"; then
        rm -f "$tmp"
        warn "Generated SCO-routing script failed bash syntax validation; original preserved."
        return 1
    fi

    # Preserve ownership from the currently installed executable.
    local owner group
    owner="$(stat -c '%u' "$BTFW_SCRIPT")"
    group="$(stat -c '%g' "$BTFW_SCRIPT")"
    chown "$owner:$group" "$tmp"

    install -m 0755 -o "$owner" -g "$group" "$tmp" "$BTFW_SCRIPT"
    rm -f "$tmp"

    if ! grep -Fq "$BTFW_RETRY_MARKER" "$BTFW_SCRIPT" || ! bash -n "$BTFW_SCRIPT"; then
        cp -a "$STATE/btfw_script.before" "$BTFW_SCRIPT"
        warn "Post-install SCO script verification failed; original restored."
        return 1
    fi

    # Do not restart bluetooth.service. Run the idempotent routing script once
    # against the already-live controller to validate the installed executable.
    if ! "$BTFW_SCRIPT"; then
        cp -a "$STATE/btfw_script.before" "$BTFW_SCRIPT"
        warn "Installed SCO retry script failed against the live controller; original restored."
        return 1
    fi

    systemctl reset-failed "$BTFW_SERVICE" >/dev/null 2>&1 || true

    say "Bluetooth SCO routing:"
    say "  installed bounded readiness/read/write/readback retries"
    say "  no fixed startup sleep was added"
    say "  live-controller validation passed"
    say "  original script saved in rollback state"
    say "  next reboot will test the actual boot-race path"

    printf '%s\n' "$BTFW_SCRIPT" > "$STATE/btfw_retry_installed"
    return 0
}


# =====================================================================
# Early CPU-governor tuning
#
# The existing local bridge-tuning.service already pins the CPUs to the
# performance governor, but the audited unit runs After=multi-user.target.
# That means the policy intended to reduce HCI-UART interrupt latency is
# applied only after Bluetooth and the audio stack have started.
#
# v7 changes ONLY this existing service's startup ordering. It preserves
# the current performance-governor commands and steady-state policy.
# =====================================================================

BRIDGE_TUNING_SERVICE="bridge-tuning.service"
BRIDGE_TUNING_PATH="/etc/systemd/system/bridge-tuning.service"
BRIDGE_TUNING_MARKER="BRIDGE_TUNING_EARLY_V1"

bridge_tuning_early_check() {
    BRIDGE_TUNING_REASONS=""

    add_tuning_reason() {
        if [ -n "$BRIDGE_TUNING_REASONS" ]; then
            BRIDGE_TUNING_REASONS="$BRIDGE_TUNING_REASONS; $1"
        else
            BRIDGE_TUNING_REASONS="$1"
        fi
    }

    [ "$MODEL" = "$EXPECTED_MODEL" ] || add_tuning_reason "hardware model does not match the audited Pi 3B Rev 1.2"
    [ -f "$BRIDGE_TUNING_PATH" ] || add_tuning_reason "$BRIDGE_TUNING_PATH is not present"
    have_unit "$BRIDGE_TUNING_SERVICE" || add_tuning_reason "$BRIDGE_TUNING_SERVICE is not loaded"

    if [ -f "$BRIDGE_TUNING_PATH" ]; then
        grep -Fq 'echo performance > "$g"' "$BRIDGE_TUNING_PATH" ||
            add_tuning_reason "governor write no longer matches audited implementation"
        grep -Fq 'grep -qx performance /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor' "$BRIDGE_TUNING_PATH" ||
            add_tuning_reason "governor verification no longer matches audited implementation"
        grep -Fq 'WantedBy=multi-user.target' "$BRIDGE_TUNING_PATH" ||
            add_tuning_reason "unit is no longer installed via multi-user.target"

        if grep -Fq "$BRIDGE_TUNING_MARKER" "$BRIDGE_TUNING_PATH"; then
            grep -Fq 'After=systemd-modules-load.service' "$BRIDGE_TUNING_PATH" ||
                add_tuning_reason "optimizer marker exists but early After= ordering is missing"
            grep -Fq 'Before=bluetooth.service bridge-btfw.service bridge-btwatchdog.service' "$BRIDGE_TUNING_PATH" ||
                add_tuning_reason "optimizer marker exists but Bluetooth Before= ordering is missing"
            if grep -Eq '^[[:space:]]*After=.*multi-user\.target' "$BRIDGE_TUNING_PATH"; then
                add_tuning_reason "optimizer marker exists but unit is still ordered after multi-user.target"
            fi
        else
            grep -Eq '^[[:space:]]*After=multi-user\.target[[:space:]]*$' "$BRIDGE_TUNING_PATH" ||
                add_tuning_reason "unit no longer has the exact audited After=multi-user.target ordering"
            if grep -Eq '^[[:space:]]*Before=.*bluetooth\.service' "$BRIDGE_TUNING_PATH"; then
                add_tuning_reason "unit already has an unrecognized Bluetooth ordering override"
            fi
        fi
    fi

    if have_unit "$BRIDGE_TUNING_SERVICE"; then
        local tuning_type tuning_remain
        tuning_type="$(systemctl show "$BRIDGE_TUNING_SERVICE" -p Type --value 2>/dev/null || true)"
        tuning_remain="$(systemctl show "$BRIDGE_TUNING_SERVICE" -p RemainAfterExit --value 2>/dev/null || true)"
        [ "$tuning_type" = "oneshot" ] || add_tuning_reason "unit is not Type=oneshot"
        [ "$tuning_remain" = "yes" ] || add_tuning_reason "unit does not use RemainAfterExit=yes"
    fi

    [ -z "$BRIDGE_TUNING_REASONS" ]
}

bridge_tuning_early_audit() {
    echo "=== bridge CPU-governor timing ==="

    if [ ! -f "$BRIDGE_TUNING_PATH" ]; then
        echo "unit=not-present"
        return 0
    fi

    if grep -Fq "$BRIDGE_TUNING_MARKER" "$BRIDGE_TUNING_PATH"; then
        echo "ordering=early-v1"
    else
        echo "ordering=legacy-after-multi-user"
    fi

    printf 'current_governor='
    cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || echo "unreadable"

    if bridge_tuning_early_check; then
        if grep -Fq "$BRIDGE_TUNING_MARKER" "$BRIDGE_TUNING_PATH"; then
            local after before
            after="$(systemctl show "$BRIDGE_TUNING_SERVICE" -p After --value 2>/dev/null || true)"
            before="$(systemctl show "$BRIDGE_TUNING_SERVICE" -p Before --value 2>/dev/null || true)"
            if ! grep -qw 'multi-user.target' <<<"$after" &&
               grep -qw 'bluetooth.service' <<<"$before"
            then
                echo "state=optimized and verified"
            else
                echo "state=NOT ELIGIBLE: installed early ordering is not effective"
            fi
        else
            echo "state=ELIGIBLE for early performance-governor ordering"
        fi
    else
        echo "state=NOT ELIGIBLE: $BRIDGE_TUNING_REASONS"
    fi
}

apply_bridge_tuning_early_if_safe() {
    if [ ! -f "$BRIDGE_TUNING_PATH" ]; then
        return 0
    fi

    if grep -Fq "$BRIDGE_TUNING_MARKER" "$BRIDGE_TUNING_PATH"; then
        say "Bridge CPU governor:"
        say "  early ordering is already installed"
        return 0
    fi

    if ! bridge_tuning_early_check; then
        say "Bridge CPU governor:"
        say "  existing service ordering preserved"
        say "  reason(s): $BRIDGE_TUNING_REASONS"
        return 1
    fi

    if [ ! -f "$STATE/bridge_tuning.before" ]; then
        cp -a "$BRIDGE_TUNING_PATH" "$STATE/bridge_tuning.before"
        printf '%s\n' "$BRIDGE_TUNING_PATH" > "$STATE/bridge_tuning_path"
    fi

    local tmp
    tmp="$(mktemp /tmp/bridge-tuning.XXXXXX.service)"

    awk '
        BEGIN { replaced_after=0; inserted_pre=0 }
        /^After=multi-user\.target[[:space:]]*$/ {
            print "# BRIDGE_TUNING_EARLY_V1"
            print "# Apply the existing performance governor before Bluetooth/HCI startup."
            print "After=systemd-modules-load.service"
            print "Before=bluetooth.service bridge-btfw.service bridge-btwatchdog.service"
            replaced_after++
            next
        }
        index($0, "ExecStart=/bin/sh -c ") == 1 && index($0, "cpu*/cpufreq/scaling_governor") > 0 && inserted_pre == 0 {
            print "ExecStartPre=/bin/sh -c '\''i=0; while [ ! -e /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor ]; do i=$((i+1)); [ \"$i\" -ge 20 ] && { echo \"cpufreq governor interface did not appear\"; exit 1; }; sleep 0.05; done'\''"
            inserted_pre=1
        }
        { print }
        END {
            if (replaced_after != 1 || inserted_pre != 1) exit 42
        }
    ' "$BRIDGE_TUNING_PATH" > "$tmp" || {
        rm -f "$tmp"
        warn "Could not transform bridge-tuning.service exactly as audited; original preserved."
        return 1
    }

    chown --reference="$BRIDGE_TUNING_PATH" "$tmp"
    chmod --reference="$BRIDGE_TUNING_PATH" "$tmp"

    if ! systemd-analyze verify "$tmp" >/dev/null 2>&1; then
        rm -f "$tmp"
        warn "Generated early bridge-tuning unit failed systemd verification; original preserved."
        return 1
    fi

    cp -a "$tmp" "$BRIDGE_TUNING_PATH"
    rm -f "$tmp"
    systemctl daemon-reload

    local effective_after effective_before
    effective_after="$(systemctl show "$BRIDGE_TUNING_SERVICE" -p After --value 2>/dev/null || true)"
    effective_before="$(systemctl show "$BRIDGE_TUNING_SERVICE" -p Before --value 2>/dev/null || true)"

    if grep -qw 'multi-user.target' <<<"$effective_after" ||
       ! grep -qw 'bluetooth.service' <<<"$effective_before" ||
       ! grep -Fq "$BRIDGE_TUNING_MARKER" "$BRIDGE_TUNING_PATH"
    then
        cp -a "$STATE/bridge_tuning.before" "$BRIDGE_TUNING_PATH"
        systemctl daemon-reload
        warn "Early bridge-tuning ordering did not become effective; original restored."
        return 1
    fi

    say "Bridge CPU governor:"
    say "  moved existing performance-governor tuning before Bluetooth startup"
    say "  added a bounded cpufreq-interface readiness check (max ~1s)"
    say "  no frequency, voltage, overclock, or governor-value change was introduced"
    say "  existing service was NOT restarted; next reboot tests the new ordering"

    printf '%s\n' "$BRIDGE_TUNING_PATH" > "$STATE/bridge_tuning_early_installed"
    return 0
}


# =====================================================================
# Rollback
# =====================================================================

rollback() {

    local state
    local cfg
    local unit
    local drop

    state="$STATE_ROOT/state"

    if [ ! -d "$state" ]; then
        warn "No rollback state exists at $state"
        exit 2
    fi


    say "Rolling back:"
    say "  $state"
    say


    # -------------------------------------------------------------
    # config.txt
    # -------------------------------------------------------------

    if [ -f "$state/boot_config_path" ]; then

        cfg="$(cat "$state/boot_config_path")"

        if [ -f "$cfg" ]; then
            remove_managed_block "$cfg"
            say "Removed managed firmware block from:"
            say "  $cfg"
        fi

    fi


    # -------------------------------------------------------------
    # cloud-init supported disable marker
    # -------------------------------------------------------------

    if [ -f "$state/cloud_init_disabled_by_script" ]; then
        if [ -e /etc/cloud/cloud-init.disabled ]; then
            rm -f /etc/cloud/cloud-init.disabled
            say "Removed /etc/cloud/cloud-init.disabled"
        fi
    fi


    # -------------------------------------------------------------
    # Network profile persisted by this optimizer
    # -------------------------------------------------------------

    if [ -f "$state/network_profile_persistent_path" ]; then
        local net_path net_uuid
        net_path="$(cat "$state/network_profile_persistent_path")"
        net_uuid="$(cat "$state/network_profile_uuid" 2>/dev/null || true)"

        # Intentionally retain this profile during rollback. Removing an active
        # Ethernet profile over the same SSH session can strand the appliance.
        # Re-enabling cloud-init restores the old provisioning behavior; the
        # extra same-UUID persistent keyfile is benign and serves as a fallback.
        say "Retained persistent NetworkManager safety profile:"
        say "  $net_path"
        [ -n "$net_uuid" ] && say "  UUID: $net_uuid"
        say "  (not deleted during remote rollback to avoid risking SSH connectivity)"
    fi


    # -------------------------------------------------------------
    # PipeWire stale documentation file rename
    # -------------------------------------------------------------

    if [ -f "$state/pipewire_old_path" ] &&
       [ -f "$state/pipewire_new_path" ]
    then
        local pw_old pw_new
        pw_old="$(cat "$state/pipewire_old_path")"
        pw_new="$(cat "$state/pipewire_new_path")"

        if [ -f "$pw_new" ] && [ ! -e "$pw_old" ]; then
            mv -- "$pw_new" "$pw_old"
            say "Restored PipeWire file:"
            say "  $pw_old"
        elif [ -e "$pw_old" ]; then
            warn "PipeWire original path already exists; not overwriting it during rollback."
        fi
    fi


    # -------------------------------------------------------------
    # lingering user instance login-barrier mask
    # -------------------------------------------------------------

    if [ -f "$state/login_barrier_instance_mask" ]; then
        local lb_mask
        lb_mask="$(cat "$state/login_barrier_instance_mask")"

        if [ -L "$lb_mask" ] && [ "$(readlink "$lb_mask" 2>/dev/null || true)" = "/dev/null" ]; then
            rm -f "$lb_mask"
            rmdir "$(dirname "$lb_mask")" 2>/dev/null || true
            say "Removed lingering-user login-barrier mask:"
            say "  $lb_mask"
        elif [ -e "$lb_mask" ] || [ -L "$lb_mask" ]; then
            warn "Recorded login-barrier path no longer points to /dev/null; refusing to remove it."
            warn "  $lb_mask"
        fi
    fi



    # -------------------------------------------------------------
    # BCM43438 SCO routing script hardening
    # -------------------------------------------------------------

    if [ -f "$state/btfw_retry_installed" ] &&
       [ -f "$state/btfw_script.before" ] &&
       [ -f "$state/btfw_script_path" ]
    then
        local btfw_path
        btfw_path="$(cat "$state/btfw_script_path")"

        if [ -f "$btfw_path" ] && grep -Fq "$BTFW_RETRY_MARKER" "$btfw_path"; then
            cp -a "$state/btfw_script.before" "$btfw_path"
            say "Restored original Bluetooth SCO routing script:"
            say "  $btfw_path"
        elif [ -e "$btfw_path" ]; then
            warn "Recorded SCO routing path no longer contains this optimizer's marker; refusing to overwrite it."
            warn "  $btfw_path"
        fi
    fi

    # -------------------------------------------------------------
    # early bridge CPU-governor ordering
    # -------------------------------------------------------------

    if [ -f "$state/bridge_tuning_early_installed" ] &&
       [ -f "$state/bridge_tuning.before" ] &&
       [ -f "$state/bridge_tuning_path" ]
    then
        local tuning_path
        tuning_path="$(cat "$state/bridge_tuning_path")"

        if [ -f "$tuning_path" ] && grep -Fq "$BRIDGE_TUNING_MARKER" "$tuning_path"; then
            cp -a "$state/bridge_tuning.before" "$tuning_path"
            say "Restored original bridge CPU-governor service ordering:"
            say "  $tuning_path"
        elif [ -e "$tuning_path" ]; then
            warn "Recorded bridge-tuning path no longer contains this optimizer's marker; refusing to overwrite it."
            warn "  $tuning_path"
        fi
    fi

    # -------------------------------------------------------------
    # wait-online
    # -------------------------------------------------------------

    if [ -f "$state/wait_disabled" ]; then

        while IFS= read -r unit; do

            [ -n "$unit" ] || continue

            if systemctl enable "$unit" >/dev/null 2>&1; then
                say "Re-enabled $unit"
            else
                warn "Could not re-enable $unit"
            fi

        done < "$state/wait_disabled"

    fi


    # -------------------------------------------------------------
    # application drop-in
    # -------------------------------------------------------------

    if [ -f "$state/service_dropin" ]; then

        drop="$(cat "$state/service_dropin")"

        rm -f -- "$drop"

        say "Removed:"
        say "  $drop"

    fi


    # -------------------------------------------------------------
    # service enable state
    # -------------------------------------------------------------

    if [ -f "$state/service_enabled_by_script" ]; then

        unit="$(cat "$state/service_enabled_by_script")"

        if systemctl disable "$unit" >/dev/null 2>&1; then
            say "Restored $unit to disabled state."
        else
            warn "Could not disable $unit during rollback."
        fi

    fi


    # -------------------------------------------------------------
    # watchdog
    # -------------------------------------------------------------

    if [ -f "$state/watchdog_dropin" ]; then

        drop="$(cat "$state/watchdog_dropin")"

        rm -f -- "$drop"

        say "Removed:"
        say "  $drop"

    fi


    systemctl daemon-reload


    mv \
        "$state" \
        "$STATE_ROOT/rolled-back-$(date -u +%Y%m%dT%H%M%SZ)"


    say
    say "Rollback complete."
    say "Reboot to return fully to the prior boot configuration."

    exit 0
}


# =====================================================================
# Basic platform validation
# =====================================================================

if [ "$(id -u)" -ne 0 ]; then
    warn "Remote script must execute as root."
    exit 1
fi


if (
    [ -z "$LOGIN_USER" ] ||
    ! id "$LOGIN_USER" >/dev/null 2>&1
); then
    warn "SSH/login user '$LOGIN_USER' is not a local account."
    exit 1
fi


if [ ! -d /run/systemd/system ]; then
    warn "PID 1 is not systemd."
    warn "Refusing to alter systemd configuration."
    exit 1
fi


REQUIRED_COMMANDS=(
    systemctl
    systemd-analyze
    findmnt
    awk
    sed
    grep
    find
    readlink
    ip
    hostname
    loginctl
)

for cmd in "${REQUIRED_COMMANDS[@]}"; do

    if ! command -v "$cmd" >/dev/null 2>&1; then
        warn "Required command is missing: $cmd"
        exit 1
    fi

done



# =====================================================================
# Verify remote identity before any persistent mutation or rollback.
# =====================================================================

ACTUAL_HOSTNAME="$(hostname 2>/dev/null || true)"
ACTUAL_MODEL="unknown"

if [ -r /proc/device-tree/model ]; then
    ACTUAL_MODEL="$(tr -d '\0' </proc/device-tree/model)"
fi

if [ -n "$EXPECTED_HOSTNAME" ] &&
   [ "$ACTUAL_HOSTNAME" != "$EXPECTED_HOSTNAME" ]
then
    warn "Host identity mismatch: expected hostname '$EXPECTED_HOSTNAME', got '$ACTUAL_HOSTNAME'."
    warn "Refusing to continue."
    exit 3
fi

if [ -n "$EXPECTED_MODEL" ] &&
   [ "$ACTUAL_MODEL" != "$EXPECTED_MODEL" ]
then
    warn "Host identity mismatch: expected model '$EXPECTED_MODEL', got '$ACTUAL_MODEL'."
    warn "Refusing to continue."
    exit 3
fi

if [ -n "$EXPECTED_MAC" ]; then
    EXPECTED_MAC_NORM="$(
        printf '%s' "$EXPECTED_MAC" |
        tr '[:lower:]' '[:upper:]' |
        tr '-' ':'
    )"

    FOUND_MAC=0

    for address_file in /sys/class/net/*/address; do
        [ -r "$address_file" ] || continue

        ACTUAL_MAC_NORM="$(
            tr '[:lower:]' '[:upper:]' <"$address_file" |
            tr -d '\r\n'
        )"

        if [ "$ACTUAL_MAC_NORM" = "$EXPECTED_MAC_NORM" ]; then
            FOUND_MAC=1
            break
        fi
    done

    if [ "$FOUND_MAC" -ne 1 ]; then
        warn "Host identity mismatch: expected network MAC '$EXPECTED_MAC' was not found."
        warn "Refusing to continue."
        exit 3
    fi
fi


if [ "$MODE" = "rollback" ]; then
    rollback
fi


# =====================================================================
# Discover actual platform
# =====================================================================

MODEL="$ACTUAL_MODEL"


OS_PRETTY="unknown"

if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    OS_PRETTY="${PRETTY_NAME:-unknown}"
fi


DEFAULT_TARGET="$(systemctl get-default 2>/dev/null || true)"


say
say "============================================================"
say " LarkBridge boot analysis"
say "============================================================"
say
say "Model:          $MODEL"
say "OS:             $OS_PRETTY"
say "Kernel:         $(uname -r)"
say "systemd:        $(systemctl --version | head -1)"
say "Default target: ${DEFAULT_TARGET:-unknown}"
say "Identity host:  $ACTUAL_HOSTNAME"
if [ -n "$EXPECTED_MAC" ]; then
    say "Identity MAC:   $EXPECTED_MAC (verified on a local interface)"
fi
say


if [[ "$MODEL" != Raspberry\ Pi* ]]; then

    warn "Hardware does not identify itself as a Raspberry Pi."
    warn "Raspberry Pi firmware edits will therefore be skipped."

fi


# =====================================================================
# Baseline report
# =====================================================================

REPORT="$(mktemp)"


{
    echo "=== systemd-analyze ==="
    systemd-analyze 2>/dev/null || true

    echo
    echo "=== critical chain ==="
    systemd-analyze critical-chain 2>/dev/null || true

    echo
    echo "=== top boot-time units ==="
    systemd-analyze blame 2>/dev/null |
        head -40 || true

    echo
    echo "=== failed units ==="
    systemctl --failed --no-pager 2>/dev/null || true

    echo
    echo "=== network providers ==="

    for u in \
        NetworkManager.service \
        systemd-networkd.service \
        dhcpcd.service
    do

        if have_unit "$u"; then

            printf \
                '%-35s active=%s enabled=%s\n' \
                "$u" \
                "$(systemctl is-active "$u" 2>/dev/null || true)" \
                "$(systemctl is-enabled "$u" 2>/dev/null || true)"

        fi

    done


    echo
    echo "=== Bluetooth ==="

    if command -v bluetoothctl >/dev/null 2>&1; then
        bluetoothctl show 2>/dev/null || true
    else
        echo "bluetoothctl not installed/found"
    fi

    systemctl status \
        bluetooth.service \
        --no-pager \
        2>/dev/null |
        head -20 || true


    echo
    echo "=== audio executables ==="

    for x in \
        pipewire \
        wireplumber \
        wpctl \
        pactl
    do

        command -v "$x" 2>/dev/null || true

    done


    echo
    echo "=== user audio/service units ==="

    user_unit_inventory

    echo
    echo "=== linger ==="
    loginctl show-user "$LOGIN_USER" -p Linger 2>/dev/null || true

    echo
    echo "=== lingering user login barrier ==="
    login_barrier_audit

    echo
    btfw_retry_audit

    echo
    bridge_tuning_early_audit

    echo
    echo "=== boot readiness timestamps (monotonic microseconds) ==="
    for ready_unit in \
        bridge-btfw.service \
        bluetooth.service \
        NetworkManager.service \
        "user@$(id -u "$LOGIN_USER").service" \
        ssh.service
    do
        if have_unit "$ready_unit"; then
            printf '%-32s %s\n' \
                "$ready_unit" \
                "$(systemctl show "$ready_unit" -p ActiveEnterTimestampMonotonic --value 2>/dev/null || true)"
        fi
    done

    login_uid="$(id -u "$LOGIN_USER" 2>/dev/null || true)"
    if [ -n "$login_uid" ] &&
       [ -S "/run/user/$login_uid/bus" ] &&
       command -v runuser >/dev/null 2>&1
    then
        for ready_unit in pipewire.socket pipewire.service wireplumber.service; do
            printf 'user:%-27s %s\n' \
                "$ready_unit" \
                "$(runuser -u "$LOGIN_USER" -- \
                    env \
                        XDG_RUNTIME_DIR="/run/user/$login_uid" \
                        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$login_uid/bus" \
                    systemctl --user show "$ready_unit" \
                        -p ActiveEnterTimestampMonotonic --value \
                        2>/dev/null || true)"
        done
    fi

    echo
    echo "=== target NetworkManager profile storage ==="
    target_iface="$(
        ip -o -4 addr show 2>/dev/null |
        awk -v ip="$TARGET_IPV4" '$4 ~ ("^" ip "/") {print $2; exit}'
    )"
    if [ -n "$target_iface" ] && command -v nmcli >/dev/null 2>&1; then
        active_uuid="$(
            nmcli -t -f UUID,DEVICE connection show --active 2>/dev/null |
            awk -F: -v dev="$target_iface" '$2 == dev {print $1; exit}'
        )"
        if [ -n "$active_uuid" ]; then
            printf 'interface=%s uuid=%s\n' "$target_iface" "$active_uuid"
            nmcli -f NAME,UUID,TYPE,AUTOCONNECT,FILENAME connection show "$active_uuid" 2>/dev/null || true
            persistent_match="$(
                grep -RIl \
                    --include='*.nmconnection' \
                    -E "^uuid=${active_uuid}[[:space:]]*$" \
                    /etc/NetworkManager/system-connections \
                    2>/dev/null |
                head -1 || true
            )"
            if [ -n "$persistent_match" ]; then
                echo "persistent same-UUID keyfile: $persistent_match"
            else
                echo "persistent same-UUID keyfile: not found"
            fi
        else
            echo "no active profile found for $target_iface"
        fi
    else
        echo "target interface/profile could not be resolved"
    fi

    echo
    echo "=== cloud-init status ==="
    if command -v cloud-init >/dev/null 2>&1; then
        cloud-init status --long 2>&1 || true
    else
        echo "cloud-init not installed"
    fi

    echo
    echo "=== cloud-init disable eligibility ==="
    if [ -e /etc/cloud/cloud-init.disabled ]; then
        echo "cloud-init is already disabled by /etc/cloud/cloud-init.disabled"
    elif cloud_init_safe_to_disable; then
        echo "ELIGIBLE: all conservative cloud-init disable preconditions passed"
    else
        echo "NOT ELIGIBLE: $CHECK_REASONS"
    fi

    echo
    echo "=== network-online reverse dependencies ==="
    systemctl list-dependencies \
        --reverse \
        network-online.target \
        --all \
        --plain \
        --no-legend \
        2>/dev/null || true

    echo
    echo "=== NetworkManager wait-online ==="
    if have_unit NetworkManager-wait-online.service; then
        printf 'enabled=%s active=%s\n' \
            "$(systemctl is-enabled NetworkManager-wait-online.service 2>/dev/null || true)" \
            "$(systemctl is-active NetworkManager-wait-online.service 2>/dev/null || true)"
    else
        echo "not installed"
    fi

    echo
    echo "=== PipeWire bridge config audit ==="
    pipewire_bridge_config_audit

} | tee "$REPORT"


# =====================================================================
# LarkBridge service discovery
# =====================================================================

select_service || true


if [ -n "${SELECTED_SERVICE:-}" ]; then

    say
    say "============================================================"
    say " Selected LarkBridge system service"
    say "============================================================"
    say

    systemctl show \
        "$SELECTED_SERVICE" \
        -p Type \
        -p User \
        -p Group \
        -p ExecStart \
        -p After \
        -p Wants \
        -p Requires \
        -p Restart \
        -p RestartUSec \
        -p RemainAfterExit \
        -p FragmentPath \
        --no-pager \
        || true

    say
    say "--- complete unit definition ---"

    systemctl cat "$SELECTED_SERVICE" || true

fi


# =====================================================================
# Audit ends here
# =====================================================================

if [ "$MODE" = "audit" ]; then

    rm -f "$REPORT"

    say
    say "============================================================"
    say " Audit complete"
    say "============================================================"
    say
    say "No changes were made."

    exit 0
fi


# =====================================================================
# Stable rollback state
#
# We intentionally retain ONE original state rather than creating a new
# rollback point each time. This makes repeated runs idempotent: a second
# ApplySafe does not lose knowledge of what the first ApplySafe changed.
# =====================================================================

mkdir -p "$STATE_ROOT"

STATE="$STATE_ROOT/state"


if [ ! -d "$STATE" ]; then

    mkdir -p "$STATE"

    cp "$REPORT" "$STATE/before-first.txt"

    printf '%s\n' "$MODEL"          > "$STATE/model"
    printf '%s\n' "$OS_PRETTY"      > "$STATE/os"
    printf '%s\n' "$DEFAULT_TARGET" > "$STATE/default_target"

    date -u +%Y-%m-%dT%H:%M:%SZ \
        > "$STATE/first-applied-at"

fi


cp "$REPORT" "$STATE/before-last.txt"

rm -f "$REPORT"


say
say "============================================================"
say " Applying verified-safe changes"
say "============================================================"
say


# =====================================================================
# 1. Raspberry Pi firmware
# =====================================================================

BOOT_CONFIG=""


if [[ "$MODEL" == Raspberry\ Pi* ]]; then

    if BOOT_CONFIG="$(find_boot_config)"; then

        # Is the filesystem containing config.txt read-only?
        if (
            findmnt \
                -no OPTIONS \
                --target "$BOOT_CONFIG" \
                2>/dev/null |
            tr ',' '\n' |
            grep -qx 'ro'
        ); then

            warn "$BOOT_CONFIG is mounted read-only."
            warn "Firmware edits skipped."

        else

            if [ ! -f "$STATE/config.txt.before" ]; then
                cp -a \
                    "$BOOT_CONFIG" \
                    "$STATE/config.txt.before"
            fi

            printf '%s\n' \
                "$BOOT_CONFIG" \
                > "$STATE/boot_config_path"


            # Idempotent.
            remove_managed_block "$BOOT_CONFIG"


            {
                echo
                echo "$MARK_BEGIN"
                echo "[all]"

                # Purely visual firmware change. It does not disable a device
                # or infer anything about attached hardware.
                echo "disable_splash=1"

                echo "$MARK_END"

            } >> "$BOOT_CONFIG"


            say "Firmware:"
            say "  disabled rainbow splash"
            say "  no HAT/camera/display/Wi-Fi hardware was inferred or disabled"

        fi

    else

        warn "Could not identify exactly one config.txt."
        warn "Firmware configuration was left untouched."

    fi

fi


# =====================================================================
# 2. Repair the proven-stale PipeWire documentation-only drop-in
# =====================================================================

fix_stale_pipewire_bridge_dropin


# =====================================================================
# 3. Persist the currently active runtime Ethernet profile, if necessary
#
# Raspberry Pi/cloud-init currently supplies the eth0 profile under /run.
# Before disabling cloud-init, ask NetworkManager itself to persist that
# exact profile/UUID, then verify the resulting /etc keyfile and live IP.
# =====================================================================

persist_active_runtime_nm_profile_if_safe || true


# =====================================================================
# 3. Disable cloud-init only when every conservative precondition passes
#
# Official cloud-init mechanism: /etc/cloud/cloud-init.disabled
# Seed/config files are deliberately preserved, so rollback is just removal
# of the marker followed by reboot.
# =====================================================================

if [ -e /etc/cloud/cloud-init.disabled ]; then

    say "Cloud-init:"
    say "  already disabled; existing state preserved"

elif cloud_init_safe_to_disable; then

    : > /etc/cloud/cloud-init.disabled
    printf '%s\n' "/etc/cloud/cloud-init.disabled" \
        > "$STATE/cloud_init_disabled_by_script"

    say "Cloud-init:"
    say "  created /etc/cloud/cloud-init.disabled"
    say "  NoCloud seed/config files were preserved for rollback"
    say "  all persistent-network/account/SSH/hostname/per-boot checks passed"

else

    say "Cloud-init:"
    say "  kept enabled"
    say "  reason(s): $CHECK_REASONS"

fi


# =====================================================================
# 4. NetworkManager-wait-online policy
#
# Do not disable/mask it. If cloud-init was the only boot-time consumer of
# network-online.target, disabling cloud-init causes this wait unit to stop
# being pulled into the next boot naturally. This preserves NetworkManager's
# packaged behavior for any future legitimate consumer.
# =====================================================================

say "Network wait-online:"
say "  NetworkManager-wait-online.service was not disabled or masked"
if [ -e /etc/cloud/cloud-init.disabled ]; then
    say "  after reboot, Audit will verify whether it disappeared from the boot path naturally"
fi


# =====================================================================
# 5. Start the lingering appliance user's manager independently of the
#    normal-login network barrier, but ONLY when the exact audited vendor
#    arrangement is still present.
#
# This does not modify systemd-user-sessions.service, NetworkManager, login
# policy, or the vendor user@.service. It masks one vendor template drop-in
# for this exact UID and verifies the resulting effective dependency graph.
# =====================================================================

apply_login_barrier_optimization_if_safe || true


# =====================================================================
# 6. Harden BCM43438 SCO routing against the proven transient boot race.
# =====================================================================

apply_btfw_retry_hardening_if_safe || true


# =====================================================================
# 7. Move the already-existing performance-governor tuning before
#    Bluetooth/HCI startup when the exact audited local unit is present.
# =====================================================================

apply_bridge_tuning_early_if_safe || true


# =====================================================================
# 8. Application service enablement
#
# EXPLICIT opt-in only.
# =====================================================================

if [ -n "${SELECTED_SERVICE:-}" ]; then

    if [ "$ENABLE_AT_BOOT" = "1" ]; then

        if unit_enabled "$SELECTED_SERVICE"; then

            say "Service:"
            say "  $SELECTED_SERVICE is already enabled"

        else

            if systemctl \
                enable "$SELECTED_SERVICE" \
                >/dev/null 2>&1
            then

                say "Service:"
                say "  enabled $SELECTED_SERVICE at boot"

                printf '%s\n' \
                    "$SELECTED_SERVICE" \
                    > "$STATE/service_enabled_by_script"

            else

                warn "Could not enable $SELECTED_SERVICE."
                warn "It may be a static or otherwise non-enableable unit."

            fi

        fi

    fi


    # =================================================================
    # 8. Application restart behavior
    #
    # EXPLICIT opt-in.
    #
    # Never:
    #   Restart=always
    #   StartLimitIntervalSec=0
    #
    # Those can turn a startup bug into an unlimited crash/restart loop.
    #
    # Only add Restart=on-failure when:
    #   - not oneshot
    #   - not RemainAfterExit=yes
    #   - current policy is exactly Restart=no
    #
    # Existing policies are preserved.
    # =================================================================

    if [ "$TUNE_RESTART" = "1" ]; then

        TYPE="$(
            systemctl show \
                "$SELECTED_SERVICE" \
                -p Type \
                --value
        )"

        RESTART="$(
            systemctl show \
                "$SELECTED_SERVICE" \
                -p Restart \
                --value
        )"

        REMAIN="$(
            systemctl show \
                "$SELECTED_SERVICE" \
                -p RemainAfterExit \
                --value
        )"


        if (
            [ "$TYPE" = "oneshot" ] ||
            [ "$REMAIN" = "yes" ]
        ); then

            say "Service restart:"
            say "  unchanged"
            say "  unit is oneshot or RemainAfterExit=yes"


        elif [ "$RESTART" != "no" ]; then

            say "Service restart:"
            say "  existing Restart=$RESTART preserved"


        else

            DROP_DIR="/etc/systemd/system/$SELECTED_SERVICE.d"

            DROP="$DROP_DIR/90-larkbridge-boot-optimizer.conf"


            mkdir -p "$DROP_DIR"


            # Never overwrite a file we didn't create.
            if (
                [ -e "$DROP" ] &&
                {
                    [ ! -f "$STATE/service_dropin" ] ||
                    [ "$(cat "$STATE/service_dropin")" != "$DROP" ]
                }
            ); then

                warn "$DROP already exists but is not recorded as"
                warn "owned by this optimizer. Refusing to overwrite it."

            else

                cat > "$DROP" <<'EOF'
[Service]

# Restart only when the daemon fails.
# Clean intentional exits remain clean exits.
Restart=on-failure

# Avoid a rapid CPU/log-spamming retry loop.
RestartSec=2s
EOF

                printf '%s\n' \
                    "$DROP" \
                    > "$STATE/service_dropin"

                say "Service restart:"
                say "  added Restart=on-failure"
                say "  RestartSec=2s"
                say "  existing systemd start-rate limiting was preserved"

            fi

        fi

    fi


else

    if (
        [ "$ENABLE_AT_BOOT" = "1" ] ||
        [ "$TUNE_RESTART" = "1" ]
    ); then

        warn "Service-specific options were requested, but no unique"
        warn "LarkBridge SYSTEM service was selected."

    fi

fi


# =====================================================================
# 9. Hardware watchdog
#
# EXPLICIT opt-in.
#
# Requirements:
#   - actual watchdog device exists
#   - no obvious watchdog daemon is already active
#   - systemd isn't already configured with a runtime watchdog
#
# Deliberately DO NOT configure kernel_watchdog_timeout here.
#
# A firmware watchdog that starts before userspace is healthy can turn
# unusually slow filesystem recovery into a reboot loop. Runtime watchdog
# recovery provides most of the appliance robustness without changing the
# early boot failure semantics.
# =====================================================================

if [ "$ENABLE_WATCHDOG" = "1" ]; then

    EXISTING_WD="$(
        systemctl show \
            -p RuntimeWatchdogUSec \
            --value \
            2>/dev/null \
        || true
    )"


    COMPETING_WATCHDOG=0


    for u in \
        watchdog.service \
        wd_keepalive.service
    do

        if systemctl is-active \
            --quiet "$u" \
            2>/dev/null
        then
            COMPETING_WATCHDOG=1
        fi

    done


    if (
        [ ! -e /dev/watchdog0 ] &&
        [ ! -d /sys/class/watchdog/watchdog0 ]
    ); then

        warn "No hardware watchdog device is currently exposed."
        warn "Watchdog configuration skipped."


    elif [ "$COMPETING_WATCHDOG" -eq 1 ]; then

        warn "Another watchdog daemon is active."
        warn "systemd watchdog configuration skipped."


    elif (
        [ -n "$EXISTING_WD" ] &&
        [ "$EXISTING_WD" != "0" ] &&
        [ "$EXISTING_WD" != "0us" ]
    ); then

        say "Watchdog:"
        say "  existing RuntimeWatchdogUSec=$EXISTING_WD preserved"


    else

        mkdir -p /etc/systemd/system.conf.d


        WD_DROP="/etc/systemd/system.conf.d/90-larkbridge-boot-optimizer.conf"


        # Never overwrite a file we didn't create.
        if (
            [ -e "$WD_DROP" ] &&
            {
                [ ! -f "$STATE/watchdog_dropin" ] ||
                [ "$(cat "$STATE/watchdog_dropin")" != "$WD_DROP" ]
            }
        ); then

            warn "$WD_DROP already exists but is not recorded"
            warn "as owned by this optimizer."
            warn "Refusing to overwrite it."

        else

            cat > "$WD_DROP" <<'EOF'
[Manager]

# Runtime recovery for a genuinely wedged embedded system.
RuntimeWatchdogSec=60s
EOF

            printf '%s\n' \
                "$WD_DROP" \
                > "$STATE/watchdog_dropin"

            say "Watchdog:"
            say "  configured systemd RuntimeWatchdogSec=60s"
            say "  early-boot firmware watchdog intentionally left disabled"

        fi

    fi

fi


# =====================================================================
# Reload units
# =====================================================================

systemctl daemon-reload


# =====================================================================
# Explicit list of things we refused to guess about
# =====================================================================

say
say "============================================================"
say " Deliberately unchanged"
say "============================================================"
say
say "  graphical.target / multi-user.target"
say "  PipeWire user/system mode"
say "  WirePlumber user/system mode"
say "  loginctl linger configuration"
say "  global systemd-user-sessions.service policy"
say "  PulseAudio configuration"
say "  Bluetooth service enablement"
say "  hciuart"
say "  D-Bus"
say "  rfkill"
say "  Wi-Fi"
say "  Ethernet"
say "  NetworkManager-wait-online packaging/state (never masked; may become unneeded naturally)"
say "  CUPS"
say "  ModemManager"
say "  triggerhappy"
say "  filesystem checks"
say "  journald"
say "  swap"
say "  CPU governor value (performance policy preserved; only startup ordering may change)"
say "  CPU/GPU clocks"
say "  overclocking"
say "  camera auto-detection"
say "  display auto-detection"
say


say "Conditional changes:"
if [ -f "$STATE/cloud_init_disabled_by_script" ]; then
    say "  cloud-init disabled using its supported marker file"
fi
if [ -f "$STATE/pipewire_new_path" ]; then
    say "  stale PipeWire comment-only .conf renamed to .notes.txt"
fi
if [ -f "$STATE/login_barrier_instance_mask" ]; then
    say "  user@$(id -u "$LOGIN_USER").service starts without the vendor normal-login network barrier"
fi
if [ -f "$STATE/btfw_retry_installed" ]; then
    say "  BCM43438 SCO routing uses bounded vendor-command readiness retries"
fi
if [ -f "$STATE/bridge_tuning_early_installed" ]; then
    say "  existing performance-governor tuning runs before Bluetooth/HCI startup"
fi
say
say "Rollback state:"
say "  $STATE"
say
say "Rollback command:"
say "  rerun this PowerShell script with -Mode Rollback"
say
say "After reboot compare:"
say "  systemd-analyze"
say "  systemd-analyze critical-chain"
say "  systemd-analyze blame"
say


# =====================================================================
# Optional reboot
# =====================================================================

if [ "$DO_REBOOT" = "1" ]; then

    say "Reboot requested."
    sync
    systemctl reboot

fi
'@


# =====================================================================
# Resolve / discover the Pi safely on Windows
# =====================================================================

function Test-TcpPort {
    param(
        [Parameter(Mandatory = $true)][string]$Address,
        [int]$Port = 22,
        [int]$TimeoutMs = 500
    )

    $Client = [System.Net.Sockets.TcpClient]::new()
    try {
        $Task = $Client.ConnectAsync($Address, $Port)
        if (-not $Task.Wait($TimeoutMs)) {
            return $false
        }
        return $Client.Connected
    }
    catch {
        return $false
    }
    finally {
        $Client.Dispose()
    }
}

function Resolve-LarkBridgeHost {
    param(
        [Parameter(Mandatory = $true)][string]$RequestedHost,
        [switch]$NoPrompt
    )

    # IPv4 literals need no DNS/mDNS resolution.
    $ParsedIp = $null
    if (
        [System.Net.IPAddress]::TryParse(
            $RequestedHost,
            [ref]$ParsedIp
        ) -and
        $ParsedIp.AddressFamily -eq
            [System.Net.Sockets.AddressFamily]::InterNetwork
    ) {
        return $RequestedHost
    }

    # First try the same Windows resolver family that applications use.
    try {
        $Resolved = @(
            [System.Net.Dns]::GetHostAddresses($RequestedHost) |
                Where-Object {
                    $_.AddressFamily -eq
                        [System.Net.Sockets.AddressFamily]::InterNetwork
                } |
                ForEach-Object { $_.IPAddressToString } |
                Select-Object -Unique
        )

        if ($Resolved.Count -eq 1) {
            return $Resolved[0]
        }

        if ($Resolved.Count -gt 1) {
            # Multiple valid A records are not guessed between. Prefer one
            # only if exactly one is accepting SSH.
            $SshResolved = @(
                $Resolved |
                    Where-Object { Test-TcpPort -Address $_ -Port 22 }
            )
            if ($SshResolved.Count -eq 1) {
                return $SshResolved[0]
            }
        }
    }
    catch {
        # Continue to conservative local-neighbor discovery below.
    }

    Write-Warning "'$RequestedHost' could not be resolved by Windows."

    # Do NOT scan the entire LAN. That would be noisy and would still not
    # prove which host is the Pi. Instead inspect only Windows' existing
    # neighbor/ARP cache and retain hosts that currently accept SSH.
    $Candidates = @()

    if (Get-Command Get-NetNeighbor -ErrorAction SilentlyContinue) {
        try {
            $Neighbors = @(
                Get-NetNeighbor -AddressFamily IPv4 -ErrorAction Stop |
                    Where-Object {
                        $_.IPAddress -notmatch '^(127\.|169\.254\.|224\.|239\.|255\.)' -and
                        $_.State -notin @('Unreachable', 'Incomplete')
                    } |
                    Select-Object IPAddress, LinkLayerAddress, State -Unique
            )

            foreach ($Neighbor in $Neighbors) {
                if (Test-TcpPort -Address $Neighbor.IPAddress -Port 22) {
                    $Candidates += [pscustomobject]@{
                        IPAddress = $Neighbor.IPAddress
                        MAC       = $Neighbor.LinkLayerAddress
                        State     = $Neighbor.State
                    }
                }
            }
        }
        catch {
            # Discovery is optional; failure here must not make assumptions.
        }
    }

    if ($Candidates.Count -gt 0) {
        Write-Host ""
        Write-Host "Windows cannot resolve the .local name, but these known LAN neighbors accept SSH:"
        Write-Host ""
        $Candidates |
            Sort-Object IPAddress |
            Format-Table -AutoSize |
            Out-Host

        if (-not $NoPrompt) {
            $Choice = Read-Host "Enter the Pi's IPv4 address from the table, or press Enter to stop"

            if ($Choice) {
                $ChoiceIp = $null
                if (
                    [System.Net.IPAddress]::TryParse($Choice, [ref]$ChoiceIp) -and
                    $ChoiceIp.AddressFamily -eq
                        [System.Net.Sockets.AddressFamily]::InterNetwork -and
                    ($Candidates.IPAddress -contains $Choice)
                ) {
                    return $Choice
                }

                throw "The entered address was not one of the discovered SSH candidates. Nothing was changed."
            }
        }
    }

    throw @"
Could not safely resolve or identify '$RequestedHost'. Nothing was changed.

Use the Pi's IPv4 address instead. On the Pi, run:
    hostname -I

Then run this script with, for example:
    .\Optimize-LarkBridgeBoot.ps1 -PiHost 192.168.1.123 -PiUser $PiUser -Mode $Mode

You can also find the Pi's address in your router's DHCP/client list.
"@
}

$ResolvedPiHost = Resolve-LarkBridgeHost `
    -RequestedHost $PiHost `
    -NoPrompt:$NonInteractive

if (-not (Test-TcpPort -Address $ResolvedPiHost -Port 22 -TimeoutMs 1000)) {
    throw "Resolved target '$ResolvedPiHost' is not accepting TCP connections on SSH port 22. Nothing was changed."
}


# =====================================================================
# Write temporary Linux script as UTF-8 WITHOUT BOM and with LF endings
# =====================================================================

$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

$TempFile = Join-Path `
    ([System.IO.Path]::GetTempPath()) `
    ("larkbridge-" + [Guid]::NewGuid().ToString("N") + ".sh")

$RemoteFile = "/tmp/larkbridge-" + [Guid]::NewGuid().ToString("N") + ".sh"

$UnixScript = $RemoteScript.Replace("`r`n", "`n")

[System.IO.File]::WriteAllText(
    $TempFile,
    $UnixScript,
    $Utf8NoBom
)


# =====================================================================
# SSH/SCP
# =====================================================================

$Target = "$PiUser@$ResolvedPiHost"

$ServiceArgument = if ($ServiceName -eq "") {
    "''"
}
else {
    "'$ServiceName'"
}

$RemoteCommand = @(
    "sudo bash '$RemoteFile'"
    "'$RemoteMode'"
    $ServiceArgument
    "'$WatchdogArg'"
    "'$RestartArg'"
    "'$EnableArg'"
    "'$PiUser'"
    "'$RebootArg'"
    "'$ExpectedHostname'"
    "'$ExpectedMac'"
    "'$ExpectedModel'"
    "'$ResolvedPiHost'"
) -join " "


Write-Host ""
Write-Host "============================================================"
Write-Host " LarkBridge boot optimizer"
Write-Host "============================================================"
Write-Host ""
Write-Host "Target:   $Target"
if ($ResolvedPiHost -ne $PiHost) {
    Write-Host "Requested: $PiHost"
}
Write-Host "Mode:     $Mode"
Write-Host "Expect:   $ExpectedHostname / $ExpectedModel / $ExpectedMac"

if ($ServiceName) {
    Write-Host "Service:  $ServiceName"
}
else {
    Write-Host "Service:  auto-detect only if unique"
}

Write-Host ""
Write-Host "Copying inspection script to the Pi..."
Write-Host ""


try {

    & scp.exe `
        -q `
        $TempFile `
        "${Target}:$RemoteFile"

    if ($LASTEXITCODE -ne 0) {
        throw "SCP failed with exit code $LASTEXITCODE."
    }


    Write-Host "Executing on Raspberry Pi..."
    Write-Host ""
    Write-Host "sudo may request the Raspberry Pi password."
    Write-Host ""


    & ssh.exe `
        -tt `
        $Target `
        $RemoteCommand

    $SshExit = $LASTEXITCODE


    # A reboot can tear down SSH before it returns a normal exit code.
    if ($Reboot -and $Mode -eq "ApplySafe") {

        if ($SshExit -ne 0) {
            Write-Host ""
            Write-Host "SSH disconnected during the requested reboot."
        }

    }
    elseif ($SshExit -ne 0) {

        throw "Remote script failed with exit code $SshExit."

    }


    # Cleanup only when we didn't deliberately reboot.
    if (-not ($Reboot -and $Mode -eq "ApplySafe")) {

        & ssh.exe `
            $Target `
            "rm -f '$RemoteFile'" `
            2>$null

    }

}
finally {

    if (Test-Path $TempFile) {
        Remove-Item -Force $TempFile
    }

}


Write-Host ""
Write-Host "============================================================"
Write-Host " Finished"
Write-Host "============================================================"
