<#
.SYNOPSIS
    Safely analyzes and optimizes Raspberry Pi boot for LarkBridge.

.DESCRIPTION
    This script intentionally avoids assumptions about:
      - Raspberry Pi OS version
      - network manager
      - LarkBridge systemd service name
      - graphical/headless configuration
      - PipeWire/WirePlumber service scope
      - watchdog availability
      - Wi-Fi requirements
      - unrelated installed services

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
# 5. Application service enablement
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
    # 6. Application restart behavior
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
# 7. Hardware watchdog
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
say "  CPU governor"
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
