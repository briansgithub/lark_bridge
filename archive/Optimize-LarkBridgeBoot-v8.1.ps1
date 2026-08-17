param(
    [ValidateSet("Audit", "ApplyRecommended", "Rollback")]
    [string]$Mode = "Audit",

    [string]$PiHost = "192.168.0.251",
    [string]$PiUser = "admin",

    # Incoming SSH access is NOT affected. This only masks the admin user's
    # outgoing ssh-agent/GnuPG socket activation on this dedicated appliance.
    [switch]$KeepUserAgentSockets,

    [string]$LogPath = ""
)

$ErrorActionPreference = "Stop"

$Target = "$PiUser@$PiHost"

if ([string]::IsNullOrWhiteSpace($LogPath)) {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $LogPath = Join-Path (Get-Location) "LarkBridge-v8.1-$Mode-$stamp.txt"
}

$LogPath = [System.IO.Path]::GetFullPath($LogPath)

$ModeMap = @{
    "Audit"            = "audit"
    "ApplyRecommended" = "apply"
    "Rollback"         = "rollback"
}

$RemoteScript = @'
#!/usr/bin/env bash
set -euo pipefail

export LANG=C
export LC_ALL=C
export SYSTEMD_COLORS=0
export SYSTEMD_PAGER=cat

MODE="__MODE__"
KEEP_USER_AGENTS="__KEEP_USER_AGENTS__"

EXPECTED_MODEL="Raspberry Pi 3 Model B Rev 1.2"
ADMIN_USER="admin"
ADMIN_UID="1000"

STATE="/var/lib/larkbridge-v8-optimizer"

DT_ROOT="/sys/firmware/devicetree/base"
BT_PATH="/soc/serial@7e201000/bluetooth"
BT_NODE="$DT_ROOT$BT_PATH"
BT_PCM_PROP="$BT_NODE/brcm,bt-pcm-int-params"
WANT_DT_HEX="0102000101"
WANT_SCO_PARAMS="01 02 00 01 01"

BTFW_UNIT="/etc/systemd/system/bridge-btfw.service"
BTFW_SCRIPT="/usr/local/lib/rpi-lark-bridge/set-sco-routing.sh"
BTFW_VERIFY_MARKER="BRIDGE_BTFW_VERIFY_ONLY_V2"

NETPLAN_DEFAULT="/lib/netplan/00-network-manager-all.yaml"

USER_AGENT_SOCKETS=(
    dirmngr.socket
    gpg-agent-browser.socket
    gpg-agent-extra.socket
    gpg-agent-ssh.socket
    gpg-agent.socket
    keyboxd.socket
    ssh-agent.socket
)

say()  { printf '%s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

have() {
    command -v "$1" >/dev/null 2>&1
}

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

u_systemctl() {
    sudo -u "$ADMIN_USER" \
        XDG_RUNTIME_DIR="/run/user/$ADMIN_UID" \
        systemctl --user "$@"
}

user_unit_enabled() {
    local unit="$1"
    u_systemctl is-enabled "$unit" 2>/dev/null || true
}

user_unit_active() {
    local unit="$1"
    u_systemctl is-active "$unit" 2>/dev/null || true
}

strict_read_sco_params() {
    hcitool -i hci0 cmd 0x3f 0x1d 2>/dev/null |
        awk '
            /^[[:space:]]*01 1D FC / {
                if ($4 == "00") {
                    print $5, $6, $7, $8, $9
                    exit
                }
            }
        '
}

check_checkpoint() {
    local m alias compatible status speed dtpcm

    [ -d "$DT_ROOT" ] ||
        die "Device tree is unavailable at $DT_ROOT"

    m="$(model || true)"
    [ "$m" = "$EXPECTED_MODEL" ] ||
        die "Model mismatch: expected '$EXPECTED_MODEL', found '${m:-<unknown>}'"

    [ "$(id -u "$ADMIN_USER" 2>/dev/null || true)" = "$ADMIN_UID" ] ||
        die "Expected $ADMIN_USER to be UID $ADMIN_UID"

    [ -d "$BT_NODE" ] ||
        die "Expected active Bluetooth DT node is absent: $BT_PATH"

    alias="$(prop_string "$DT_ROOT/aliases/bluetooth" 2>/dev/null || true)"
    [ "$alias" = "$BT_PATH" ] ||
        die "Bluetooth DT alias changed: expected '$BT_PATH', found '${alias:-<absent>}'"

    compatible="$(prop_string "$BT_NODE/compatible" 2>/dev/null || true)"
    printf '%s\n' "$compatible" | grep -Fqx 'brcm,bcm43438-bt' ||
        die "Active Bluetooth node is no longer brcm,bcm43438-bt"

    status="$(prop_string "$BT_NODE/status" 2>/dev/null || true)"
    [ -z "$status" ] || [ "$status" = "okay" ] ||
        die "Bluetooth DT node status is '$status', not okay"

    speed="$(prop_hex "$BT_NODE/max-speed" 2>/dev/null || true)"
    [ "$speed" = "000e1000" ] ||
        die "Bluetooth max-speed changed: '${speed:-<absent>}' (expected 921600)"

    dtpcm="$(prop_hex "$BT_PCM_PROP" 2>/dev/null || true)"
    [ "$dtpcm" = "$WANT_DT_HEX" ] ||
        die "DT-native SCO checkpoint is missing/wrong: '${dtpcm:-ABSENT}'"

    [ -f "$BTFW_UNIT" ] ||
        die "$BTFW_UNIT is missing"

    [ -f "$BTFW_SCRIPT" ] ||
        die "$BTFW_SCRIPT is missing"

    have hcitool ||
        die "hcitool is unavailable"

    have systemd-analyze ||
        die "systemd-analyze is unavailable"

    # The v7 checkpoint must still have early governor ordering.
    grep -Fq 'BRIDGE_TUNING_EARLY_V1' /etc/systemd/system/bridge-tuning.service 2>/dev/null ||
        die "v7 early bridge-tuning checkpoint marker is missing"

    local tune_after tune_before
    tune_after="$(systemctl show bridge-tuning.service -p After --value 2>/dev/null || true)"
    tune_before="$(systemctl show bridge-tuning.service -p Before --value 2>/dev/null || true)"

    ! grep -qw 'multi-user.target' <<<"$tune_after" ||
        die "bridge-tuning.service has regressed to After=multi-user.target"

    grep -qw 'bluetooth.service' <<<"$tune_before" ||
        die "bridge-tuning.service is no longer ordered before Bluetooth"

    # The login-barrier optimization must remain effective.
    local user_after
    user_after="$(systemctl show "user@$ADMIN_UID.service" -p After --value 2>/dev/null || true)"
    ! grep -qw 'systemd-user-sessions.service' <<<"$user_after" ||
        die "user@$ADMIN_UID.service is again ordered behind systemd-user-sessions.service"
}

btfw_is_old_writer() {
    grep -Fq 'BRIDGE_BTFW_RETRY_V1' "$BTFW_SCRIPT" 2>/dev/null &&
    grep -Fq 'Write_SCO_PCM_Int_Param' "$BTFW_SCRIPT" 2>/dev/null
}

btfw_is_verifier() {
    grep -Fq "$BTFW_VERIFY_MARKER" "$BTFW_SCRIPT" 2>/dev/null &&
    ! grep -Fq 'Write_SCO_PCM_Int_Param' "$BTFW_SCRIPT" 2>/dev/null
}

write_verifier_script() {
    local out="$1"

    cat > "$out" <<'VERIFY'
#!/usr/bin/env bash
# BRIDGE_BTFW_VERIFY_ONLY_V2
#
# DT-native BCM43438 SCO-over-HCI configuration is now authoritative.
# This script is intentionally READ-ONLY with respect to the controller.
# It verifies both the live DT property and the resulting controller state.
set -euo pipefail

HCI="${BRIDGE_HCI:-hci0}"
DT_PROP="/sys/firmware/devicetree/base/soc/serial@7e201000/bluetooth/brcm,bt-pcm-int-params"
WANT_DT_HEX="0102000101"
WANT_PARAMS="01 02 00 01 01"
MAX_ATTEMPTS="${BRIDGE_BT_VERIFY_ATTEMPTS:-30}"
RETRY_DELAY="${BRIDGE_BT_VERIFY_DELAY:-0.10}"

log() {
    printf '[bridge-btfw] %s\n' "$*"
}

die() {
    printf '[bridge-btfw] ERROR: %s\n' "$*" >&2
    exit 1
}

command -v hcitool >/dev/null 2>&1 ||
    die "hcitool not found"

[ -f "$DT_PROP" ] ||
    die "DT-native SCO property is missing: $DT_PROP"

dt_hex="$(
    od -An -tx1 -v "$DT_PROP" |
        tr -d ' \n'
)"

[ "$dt_hex" = "$WANT_DT_HEX" ] ||
    die "DT SCO property is '$dt_hex', expected '$WANT_DT_HEX'"

read_params() {
    hcitool -i "$HCI" cmd 0x3f 0x1d 2>/dev/null |
        awk '
            /^[[:space:]]*01 1D FC / {
                if ($4 == "00") {
                    print $5, $6, $7, $8, $9
                    exit
                }
            }
        '
}

for ((attempt=1; attempt<=MAX_ATTEMPTS; attempt++)); do
    params="$(read_params || true)"

    if [ -n "$params" ]; then
        log "SCO PCM params: $params"

        if [ "$params" = "$WANT_PARAMS" ]; then
            log "verified: DT-native SCO routing is active; no userspace write performed"
            exit 0
        fi

        die "controller SCO params are '$params', expected '$WANT_PARAMS'; verifier will not modify controller state"
    fi

    if [ "$attempt" -eq 1 ] ||
       [ "$attempt" -eq 10 ] ||
       [ "$attempt" -eq 20 ] ||
       [ "$attempt" -eq "$MAX_ATTEMPTS" ]
    then
        log "controller not yet readable for SCO verification (attempt ${attempt}/${MAX_ATTEMPTS})"
    fi

    sleep "$RETRY_DELAY"
done

die "controller never became readable for SCO verification after ${MAX_ATTEMPTS} attempts"
VERIFY
}

write_verifier_unit() {
    local out="$1"

    cat > "$out" <<'UNIT'
[Unit]
Description=Verify Bluetooth SCO-over-HCI routing (BCM43438)
Documentation=file:/usr/share/doc/rpi-lark-bridge/E01-sco-over-hci.md
After=bluetooth.service
Requires=bluetooth.service
PartOf=bluetooth.service

[Service]
Type=oneshot
RemainAfterExit=yes
TimeoutStartSec=5
ExecStart=/usr/local/lib/rpi-lark-bridge/set-sco-routing.sh

# The verifier must never need to write the filesystem.
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ProtectKernelTunables=yes
ProtectControlGroups=yes

[Install]
WantedBy=bluetooth.service
UNIT
}

validate_btfw_generated() {
    local script="$1"
    local unit="$2"

    bash -n "$script" ||
        die "Generated verification script failed bash -n"

    grep -Fq "$BTFW_VERIFY_MARKER" "$script" ||
        die "Generated verification script marker is missing"

    if grep -Fq 'Write_SCO_PCM_Int_Param' "$script"; then
        die "Generated verifier unexpectedly contains the legacy write command"
    fi

    systemd-analyze verify "$unit" >/dev/null 2>&1 ||
        die "Generated bridge-btfw.service failed systemd-analyze verify"
}

apply_btfw_verifier() {
    if btfw_is_verifier; then
        say "bridge-btfw: already verification-only"
        return 0
    fi

    if ! btfw_is_old_writer; then
        die "bridge-btfw script no longer matches the audited writer or v8 verifier; refusing to overwrite"
    fi

    mkdir -p "$STATE"

    [ -f "$STATE/btfw-script.before" ] ||
        cp -a "$BTFW_SCRIPT" "$STATE/btfw-script.before"

    [ -f "$STATE/btfw-unit.before" ] ||
        cp -a "$BTFW_UNIT" "$STATE/btfw-unit.before"

    local tmpd new_script new_unit
    tmpd="$(mktemp -d)"
    new_script="$tmpd/set-sco-routing.sh"
    new_unit="$tmpd/bridge-btfw.service"

    write_verifier_script "$new_script"
    write_verifier_unit "$new_unit"
    validate_btfw_generated "$new_script" "$new_unit"

    chmod --reference="$BTFW_SCRIPT" "$new_script"
    chown --reference="$BTFW_SCRIPT" "$new_script"

    chmod --reference="$BTFW_UNIT" "$new_unit"
    chown --reference="$BTFW_UNIT" "$new_unit"

    cp -a "$new_script" "$BTFW_SCRIPT"
    cp -a "$new_unit" "$BTFW_UNIT"
    rm -rf "$tmpd"

    systemctl daemon-reload

    # Verify effective dependency semantics before accepting the change.
    local after requires partof before
    after="$(systemctl show bridge-btfw.service -p After --value)"
    requires="$(systemctl show bridge-btfw.service -p Requires --value)"
    partof="$(systemctl show bridge-btfw.service -p PartOf --value)"
    before="$(systemctl show bridge-btfw.service -p Before --value)"

    grep -qw 'bluetooth.service' <<<"$after" ||
        die "bridge-btfw verifier lacks effective After=bluetooth.service"

    grep -qw 'bluetooth.service' <<<"$requires" ||
        die "bridge-btfw verifier lacks effective Requires=bluetooth.service"

    grep -qw 'bluetooth.service' <<<"$partof" ||
        die "bridge-btfw verifier lacks effective PartOf=bluetooth.service"

    if grep -qw 'bridge.target' <<<"$before"; then
        die "obsolete bridge.target ordering is still effective"
    fi

    # Do not restart Bluetooth in the middle of the user's current session.
    # Validate the new read-only script against the already-running controller.
    if ! "$BTFW_SCRIPT"; then
        warn "Live verifier validation failed; restoring pre-v8 files"
        cp -a "$STATE/btfw-script.before" "$BTFW_SCRIPT"
        cp -a "$STATE/btfw-unit.before" "$BTFW_UNIT"
        systemctl daemon-reload
        return 1
    fi

    : > "$STATE/btfw-verifier-installed"

    say "bridge-btfw:"
    say "  converted legacy controller writer into read-only DT/controller verifier"
    say "  added PartOf=bluetooth.service so Bluetooth restarts re-run verification"
    say "  removed obsolete bridge.target ordering"
    say "  live verification passed without restarting Bluetooth"
}

netplan_content_is_default_nm() {
    [ -f "$NETPLAN_DEFAULT" ] || return 1

    awk '
        {
            gsub(/[[:space:]]+/, "")
            if (length($0) > 0 && substr($0,1,1) != "#")
                print $0
        }
    ' "$NETPLAN_DEFAULT" |
        paste -sd ';' - |
        grep -Fqx 'network:;version:2;renderer:NetworkManager'
}

apply_netplan_permissions() {
    [ -f "$NETPLAN_DEFAULT" ] || {
        say "netplan permissions: default NetworkManager YAML not present; skipped"
        return 0
    }

    if ! netplan_content_is_default_nm; then
        warn "$NETPLAN_DEFAULT is not the simple audited default renderer file; permission fix skipped"
        return 0
    fi

    local mode owner group
    mode="$(stat -c '%a' "$NETPLAN_DEFAULT")"
    owner="$(stat -c '%U' "$NETPLAN_DEFAULT")"
    group="$(stat -c '%G' "$NETPLAN_DEFAULT")"

    [ "$owner" = "root" ] && [ "$group" = "root" ] ||
        die "$NETPLAN_DEFAULT is not root:root"

    if [ "$mode" = "600" ]; then
        say "netplan permissions: already 600"
        return 0
    fi

    mkdir -p "$STATE"

    if [ ! -f "$STATE/netplan-mode.before" ]; then
        printf '%s\n' "$mode" > "$STATE/netplan-mode.before"
    fi

    chmod 600 "$NETPLAN_DEFAULT"

    [ "$(stat -c '%a' "$NETPLAN_DEFAULT")" = "600" ] ||
        die "Failed to set $NETPLAN_DEFAULT to mode 600"

    : > "$STATE/netplan-mode-fixed"

    say "netplan permissions:"
    say "  corrected $NETPLAN_DEFAULT from mode $mode to 600"
}

agent_project_references_exist() {
    local project="/home/$ADMIN_USER/rpi-lark-bridge"

    [ -d "$project" ] || return 1

    grep -RIsE \
        --exclude-dir=.git \
        --exclude='*.log' \
        --exclude='*.txt' \
        '(gpg-agent|dirmngr|keyboxd|ssh-agent|SSH_AUTH_SOCK|GPG_AGENT_INFO)' \
        "$project" >/dev/null 2>&1
}

active_agent_processes_exist() {
    ps -eo user=,comm=,args= |
        awk -v u="$ADMIN_USER" '
            $1 == u &&
            ($2 ~ /^(gpg-agent|dirmngr|keyboxd|ssh-agent)$/ ||
             $0 ~ /[[:space:]](gpg-agent|dirmngr|keyboxd|ssh-agent)([[:space:]]|$)/) {
                found=1
            }
            END { exit found ? 0 : 1 }
        '
}

save_agent_states_once() {
    local statefile="$STATE/user-agent-sockets.before"

    [ -f "$statefile" ] && return 0

    : > "$statefile"

    local u enabled active
    for u in "${USER_AGENT_SOCKETS[@]}"; do
        enabled="$(user_unit_enabled "$u")"
        active="$(user_unit_active "$u")"
        printf '%s|%s|%s\n' "$u" "$enabled" "$active" >> "$statefile"
    done
}

apply_user_agent_trim() {
    if [ "$KEEP_USER_AGENTS" = "yes" ]; then
        say "user agent sockets: kept by PowerShell -KeepUserAgentSockets switch"
        return 0
    fi

    if active_agent_processes_exist; then
        warn "An admin GPG/SSH agent process is currently active; user-agent trim skipped"
        return 0
    fi

    if agent_project_references_exist; then
        warn "The LarkBridge project references GPG/SSH-agent functionality; user-agent trim skipped"
        return 0
    fi

    mkdir -p "$STATE"
    save_agent_states_once

    local changed=0 u enabled
    for u in "${USER_AGENT_SOCKETS[@]}"; do
        enabled="$(user_unit_enabled "$u")"

        case "$enabled" in
            masked|masked-runtime)
                ;;
            *)
                u_systemctl mask --now "$u" >/dev/null
                changed=1
                ;;
        esac
    done

    # Vendor packages can enable these sockets from /usr/lib/systemd/user.
    # A per-user mask reliably overrides those vendor Wants= symlinks.
    for u in "${USER_AGENT_SOCKETS[@]}"; do
        enabled="$(user_unit_enabled "$u")"
        case "$enabled" in
            masked|masked-runtime)
                ;;
            *)
                die "Failed to mask user socket $u (state='$enabled')"
                ;;
        esac

        if [ "$(user_unit_active "$u")" = "active" ]; then
            die "User socket $u remained active after masking"
        fi
    done

    : > "$STATE/user-agent-trim-applied"

    if [ "$changed" -eq 1 ]; then
        say "user agent sockets:"
        say "  masked unused GPG/dirmngr/keyboxd/ssh-agent socket activation for admin"
        say "  incoming SSH server access is unaffected"
        say "  rollback removes only the masks recorded by this optimizer"
    else
        say "user agent sockets: already masked/inactive"
    fi
}

rollback_agent_trim() {
    local statefile="$STATE/user-agent-sockets.before"

    [ -f "$statefile" ] || return 0

    while IFS='|' read -r unit enabled active; do
        [ -n "$unit" ] || continue

        case "$enabled" in
            masked|masked-runtime)
                # It was already masked before v8; preserve that state.
                ;;
            *)
                # Remove the v8 per-user mask. Vendor enablement (if any)
                # becomes visible again automatically.
                u_systemctl unmask "$unit" >/dev/null 2>&1 || true

                case "$enabled" in
                    enabled|enabled-runtime|alias)
                        u_systemctl enable "$unit" >/dev/null 2>&1 || true
                        ;;
                    *)
                        ;;
                esac
                ;;
        esac

        if [ "$active" = "active" ]; then
            u_systemctl start "$unit" >/dev/null 2>&1 || true
        fi
    done < "$statefile"
}

audit() {
    check_checkpoint

    say "============================================================"
    say " LarkBridge v8 post-checkpoint audit"
    say "============================================================"
    say
    say "model=$(model)"
    say "kernel=$(uname -r)"
    say "systemd=$(systemd --version | head -1)"
    say

    say "=== checkpoint invariants ==="
    say "bluetooth_alias=$(prop_string "$DT_ROOT/aliases/bluetooth")"
    say "dt_sco_hex=$(prop_hex "$BT_PCM_PROP")"
    say "governor=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || true)"
    say

    say "=== bridge-btfw architecture ==="
    if btfw_is_verifier; then
        say "mode=verification-only-v2"
    elif btfw_is_old_writer; then
        say "mode=legacy-writer-v1"
    else
        say "mode=unknown"
    fi

    systemctl show bridge-btfw.service \
        -p ActiveState \
        -p SubState \
        -p Result \
        -p After \
        -p Before \
        -p Requires \
        -p PartOf \
        -p ActiveEnterTimestampMonotonic \
        --no-pager 2>/dev/null || true

    say
    say "current_sco_params=$(strict_read_sco_params || true)"
    say

    say "=== user-agent socket contribution ==="
    local u
    for u in "${USER_AGENT_SOCKETS[@]}"; do
        printf '%-28s enabled=%-16s active=%s\n' \
            "$u" \
            "$(user_unit_enabled "$u")" \
            "$(user_unit_active "$u")"
    done

    say
    say "active_agent_processes=$(
        if active_agent_processes_exist; then echo yes; else echo no; fi
    )"
    say "project_agent_references=$(
        if agent_project_references_exist; then echo yes; else echo no; fi
    )"

    say
    say "=== netplan default-renderer permissions ==="
    if [ -f "$NETPLAN_DEFAULT" ]; then
        stat -c 'mode=%a owner=%U group=%G path=%n' "$NETPLAN_DEFAULT"
        say "simple_default_nm_file=$(
            if netplan_content_is_default_nm; then echo yes; else echo no; fi
        )"
    else
        say "not-present"
    fi

    say
    say "=== boot timing ==="
    systemd-analyze 2>/dev/null || true

    say
    say "=== selected monotonic readiness ==="
    local unit
    for unit in \
        bridge-tuning.service \
        bluetooth.service \
        bridge-btfw.service \
        user@"$ADMIN_UID".service \
        NetworkManager.service \
        ssh.service
    do
        printf '%-30s %s\n' \
            "$unit" \
            "$(systemctl show "$unit" -p ActiveEnterTimestampMonotonic --value 2>/dev/null || true)"
    done

    for unit in pipewire.service wireplumber.service bridge-supervisor.service; do
        printf 'user:%-25s %s\n' \
            "$unit" \
            "$(u_systemctl show "$unit" -p ActiveEnterTimestampMonotonic --value 2>/dev/null || true)"
    done

    say
    say "=== current-boot key audio events ==="
    journalctl -b -o short-monotonic --no-pager 2>/dev/null |
        grep -E \
        'Bluetooth management interface|Endpoint registered: sender=|Started pipewire.service|Started wireplumber.service|Started bridge-supervisor.service|watching for HFP nodes|bridge-btfw' |
        head -120 || true

    say
    say "=== failed units ==="
    systemctl --failed --plain --no-legend --no-pager 2>/dev/null || true
}

apply_all() {
    check_checkpoint

    mkdir -p "$STATE"

    say "============================================================"
    say " Applying LarkBridge v8 recommended refinements"
    say "============================================================"
    say

    apply_btfw_verifier
    say

    apply_netplan_permissions
    say

    apply_user_agent_trim
    say

    say "No Bluetooth, NetworkManager, PipeWire, or WirePlumber service was restarted."
    say "Reboot once to measure the new user-manager timing and verify clean startup."
    say

    say "=== post-apply static verification ==="
    btfw_is_verifier ||
        die "bridge-btfw is not verification-only after apply"

    [ "$(stat -c '%a' "$NETPLAN_DEFAULT" 2>/dev/null || echo 600)" = "600" ] ||
        die "Netplan permission fix did not persist"

    systemd-analyze verify "$BTFW_UNIT" >/dev/null 2>&1 ||
        die "Installed bridge-btfw unit fails systemd verification"

    say "static_verification=PASS"
}

rollback() {
    [ -d "$STATE" ] ||
        die "No v8 optimizer state exists at $STATE"

    say "============================================================"
    say " Rolling back LarkBridge v8 refinements"
    say "============================================================"
    say

    if [ -f "$STATE/btfw-verifier-installed" ] &&
       [ -f "$STATE/btfw-script.before" ] &&
       [ -f "$STATE/btfw-unit.before" ]
    then
        if btfw_is_verifier; then
            cp -a "$STATE/btfw-script.before" "$BTFW_SCRIPT"
            cp -a "$STATE/btfw-unit.before" "$BTFW_UNIT"
            systemctl daemon-reload
            say "restored legacy bridge-btfw unit/script"
        else
            warn "bridge-btfw no longer contains the v8 verifier marker; refusing to overwrite"
        fi
    fi

    if [ -f "$STATE/netplan-mode-fixed" ] &&
       [ -f "$STATE/netplan-mode.before" ] &&
       [ -f "$NETPLAN_DEFAULT" ]
    then
        local oldmode
        oldmode="$(cat "$STATE/netplan-mode.before")"
        chmod "$oldmode" "$NETPLAN_DEFAULT"
        say "restored Netplan mode to $oldmode"
    fi

    if [ -f "$STATE/user-agent-trim-applied" ]; then
        rollback_agent_trim
        say "restored recorded admin user-agent socket mask/enablement state"
    fi

    say
    say "Rollback completed. No running Bluetooth/NetworkManager/audio service was restarted."
    say "Reboot to benchmark the restored state."
}

case "$MODE" in
    audit)
        audit
        ;;
    apply)
        apply_all
        ;;
    rollback)
        rollback
        ;;
    *)
        die "Unknown mode '$MODE'"
        ;;
esac
'@

$RemoteScript = $RemoteScript.Replace("__MODE__", $ModeMap[$Mode])
$RemoteScript = $RemoteScript.Replace(
    "__KEEP_USER_AGENTS__",
    $(if ($KeepUserAgentSockets) { "yes" } else { "no" })
)

# Avoid the CRLF-over-ssh problem encountered in earlier ad-hoc audits.
$RemoteScript = $RemoteScript -replace "`r`n", "`n"

$RemoteFile = "/tmp/larkbridge-v8.1-$PID.sh"
$TempFile = Join-Path ([System.IO.Path]::GetTempPath()) "larkbridge-v8.1-$PID.sh"

$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($TempFile, $RemoteScript, $Utf8NoBom)

Write-Host ""
Write-Host "============================================================"
Write-Host " LarkBridge v8.1 post-checkpoint optimizer"
Write-Host "============================================================"
Write-Host "Target: $Target"
Write-Host "Mode:   $Mode"
Write-Host "Log:    $LogPath"
if ($KeepUserAgentSockets) {
    Write-Host "User agent sockets: KEEP"
}
else {
    Write-Host "User agent sockets: trim if unused"
}
Write-Host ""

try {
    & scp.exe -q $TempFile "${Target}:$RemoteFile"
    if ($LASTEXITCODE -ne 0) {
        throw "SCP failed with exit code $LASTEXITCODE"
    }

    Write-Host "sudo may request the Raspberry Pi password."
    Write-Host ""

    # ssh.exe writes its normal "Connection to ... closed." notice to stderr
    # when -tt is used. Do not let PowerShell's ErrorActionPreference=Stop
    # reinterpret that harmless native stderr text as a terminating error.
    #
    # Stream everything live through Tee-Object so the user sees password
    # prompts/progress and every run is simultaneously saved to a text file.
    $SavedErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"

    try {
        & ssh.exe -tt $Target "TERM=dumb sudo bash '$RemoteFile'" 2>&1 |
            Tee-Object -FilePath $LogPath

        $SshExit = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $SavedErrorActionPreference
    }

    # Cleanup is best-effort and must not replace the real remote exit status.
    $SavedErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "SilentlyContinue"
    try {
        & ssh.exe $Target "rm -f '$RemoteFile'" 2>$null | Out-Null
    }
    finally {
        $ErrorActionPreference = $SavedErrorActionPreference
    }

    if ($SshExit -ne 0) {
        throw "Remote optimizer failed with exit code $SshExit. See: $LogPath"
    }

    Write-Host ""
    Write-Host "Saved log: $LogPath"
}
finally {
    if (Test-Path $TempFile) {
        Remove-Item -Force $TempFile
    }
}
