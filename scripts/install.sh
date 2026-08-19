#!/usr/bin/env bash
# Install the production LarkBridge boot configuration transactionally.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_SOURCE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SOURCE_ROOT="$DEFAULT_SOURCE_ROOT"
STATE_ROOT="${BRIDGE_BOOT_STATE_ROOT:-/var/lib/rpi-lark-bridge/boot-transactions}"
LABEL="reconcile"
DRY_RUN=0
BOOT_ONLY=0
BRIDGE_USER="${BRIDGE_USER:-admin}"

usage() {
    printf 'usage: sudo %s --boot-only [--dry-run] [--source-root PATH] [--transaction-label LABEL]\n' "$0"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --boot-only) BOOT_ONLY=1 ;;
        --dry-run) DRY_RUN=1 ;;
        --source-root) shift; SOURCE_ROOT="${1:?--source-root requires a path}" ;;
        --transaction-label) shift; LABEL="${1:?--transaction-label requires a value}" ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
    shift
done

[ "$BOOT_ONLY" -eq 1 ] || { usage >&2; exit 2; }
[ "$(id -u)" -eq 0 ] || { printf 'ERROR: must run as root\n' >&2; exit 1; }
[ "$(uname -s)" = "Linux" ] || { printf 'ERROR: Linux is required\n' >&2; exit 1; }
SOURCE_ROOT="$(cd "$SOURCE_ROOT" && pwd)"

log() { printf '[boot-install] %s\n' "$*" >&2; }
warn() { printf '[boot-install] WARNING: %s\n' "$*" >&2; }
die() { printf '[boot-install] ERROR: %s\n' "$*" >&2; exit 1; }
require_cmd() { command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"; }

for command in awk cmp cp findmnt grep install loginctl nmcli od sha256sum systemctl systemd-analyze; do
    require_cmd "$command"
done

model="$(tr -d '\0' </proc/device-tree/model 2>/dev/null || true)"
[ "$model" = "Raspberry Pi 3 Model B Rev 1.2" ] ||
    die "expected Raspberry Pi 3 Model B Rev 1.2, found '${model:-unknown}'"
[ "$(id -u "$BRIDGE_USER" 2>/dev/null || true)" = "1000" ] ||
    die "expected $BRIDGE_USER to be UID 1000"

managed_sources=(
    "pi/systemd/system/bridge-tuning.service"
    "pi/systemd/system/bridge-btfw.service"
    "pi/systemd/system/bridge-boot-trial-rollback.service"
    "pi/systemd/system/bridge-boot-trial-rollback.timer"
    "pi/scripts/set-sco-routing.sh"
    "pi/scripts/boot-transaction.sh"
    "pi/scripts/boot-trial.sh"
    "pi/pipewire/pipewire.conf.d/20-bridge-endpoints.notes.txt"
)
for relative in "${managed_sources[@]}"; do
    [ -f "$SOURCE_ROOT/$relative" ] || die "source file missing: $SOURCE_ROOT/$relative"
done

if [ "$DRY_RUN" -eq 1 ]; then
    printf 'validated boot-only source at %s\n' "$SOURCE_ROOT"
    printf '%s\n' "${managed_sources[@]}"
    exit 0
fi

mkdir -p "$STATE_ROOT"
safe_label="$(printf '%s' "$LABEL" | tr -c 'A-Za-z0-9._-' '-')"
git_head="$(git -C "$SOURCE_ROOT" rev-parse --short=12 HEAD 2>/dev/null || printf unknown)"
transaction_id="$(date -u +%Y%m%dT%H%M%SZ)-${git_head}-${safe_label}"
transaction="$STATE_ROOT/$transaction_id"
mkdir -p "$transaction/backups"
: > "$transaction/paths.tsv"
: > "$transaction/units.tsv"
: > "$transaction/deployed-hashes.tsv"
if [ -L "$STATE_ROOT/current" ]; then
    previous="$(readlink "$STATE_ROOT/current")"
    case "$previous" in
        */*|.|..) die "unsafe current transaction pointer: $previous" ;;
    esac
    [ -d "$STATE_ROOT/$previous" ] || die "current transaction is missing: $previous"
    printf '%s\n' "$previous" > "$transaction/previous"
fi

record_path() {
    local target="$1" index disposition
    if awk -F '\t' -v path="$target" '$3 == path { found=1 } END { exit !found }' "$transaction/paths.tsv"; then
        return 0
    fi
    index="$(printf '%04d' "$(( $(wc -l < "$transaction/paths.tsv") + 1 ))")"
    if [ -e "$target" ] || [ -L "$target" ]; then
        disposition=present
        cp -a -- "$target" "$transaction/backups/$index"
    else
        disposition=absent
    fi
    printf '%s\t%s\t%s\n' "$index" "$disposition" "$target" >> "$transaction/paths.tsv"
}

record_unit() {
    local unit="$1" state
    state="$(systemctl is-enabled "$unit" 2>/dev/null || true)"
    [ "$state" = "enabled" ] || state=disabled
    printf '%s\t%s\n' "$unit" "$state" >> "$transaction/units.tsv"
}

install_managed() {
    local relative="$1" target="$2" mode="$3" owner="${4:-root}" group="${5:-root}" source
    source="$SOURCE_ROOT/$relative"
    record_path "$target"
    mkdir -p "$(dirname "$target")"
    if [ -e "$target" ] && cmp -s "$source" "$target"; then
        log "unchanged: $target"
    else
        install -o "$owner" -g "$group" -m "$mode" "$source" "$target"
        log "installed: $target"
    fi
    printf '%s\t%s\t%s\n' "$(sha256sum "$source" | awk '{print $1}')" "$relative" "$target" >> "$transaction/deployed-hashes.tsv"
}

rollback_on_error=1
cleanup() {
    status=$?
    if [ "$status" -ne 0 ] && [ "$rollback_on_error" -eq 1 ]; then
        warn "installation failed; restoring $transaction_id"
        bash "$SOURCE_ROOT/pi/scripts/boot-transaction.sh" rollback "$transaction" || true
    fi
    exit "$status"
}
trap cleanup EXIT

for unit in bridge-tuning.service bridge-btfw.service bridge-boot-trial-rollback.timer; do
    record_unit "$unit"
done

install_managed "pi/systemd/system/bridge-tuning.service" "/etc/systemd/system/bridge-tuning.service" 0644
install_managed "pi/systemd/system/bridge-btfw.service" "/etc/systemd/system/bridge-btfw.service" 0644
install_managed "pi/systemd/system/bridge-boot-trial-rollback.service" "/etc/systemd/system/bridge-boot-trial-rollback.service" 0644
install_managed "pi/systemd/system/bridge-boot-trial-rollback.timer" "/etc/systemd/system/bridge-boot-trial-rollback.timer" 0644
install_managed "pi/scripts/set-sco-routing.sh" "/usr/local/lib/rpi-lark-bridge/set-sco-routing.sh" 0755
install_managed "pi/scripts/boot-transaction.sh" "/usr/local/lib/rpi-lark-bridge/boot-transaction.sh" 0755
install_managed "pi/scripts/boot-trial.sh" "/usr/local/lib/rpi-lark-bridge/boot-trial.sh" 0755

pipewire_dir="/home/$BRIDGE_USER/.config/pipewire/pipewire.conf.d"
legacy_pipewire="$pipewire_dir/20-bridge-endpoints.conf"
if [ -f "$legacy_pipewire" ]; then
    if grep -Ev '^[[:space:]]*(#|$)' "$legacy_pipewire" | grep -q .; then
        die "$legacy_pipewire contains active configuration; refusing to remove it"
    fi
    record_path "$legacy_pipewire"
    rm -f "$legacy_pipewire"
    log "retired comment-only PipeWire drop-in"
fi
install_managed "pi/pipewire/pipewire.conf.d/20-bridge-endpoints.notes.txt" \
    "$pipewire_dir/20-bridge-endpoints.notes.txt" 0644 "$BRIDGE_USER" "$BRIDGE_USER"

install_login_barrier() {
    local vendor instance normalized after sessions_after vendor_hash
    vendor="/usr/lib/systemd/system/user@.service.d/10-login-barrier.conf"
    instance="/etc/systemd/system/user@1000.service.d/10-login-barrier.conf"
    [ "$(. /etc/os-release; printf '%s:%s:%s' "$ID" "$VERSION_ID" "$VERSION_CODENAME")" = "debian:13:trixie" ] || {
        warn "login barrier unchanged: platform is not audited Debian 13/trixie"; return 0; }
    systemctl --version | head -1 | grep -Eq '^systemd 257 ' || {
        warn "login barrier unchanged: systemd is not audited major 257"; return 0; }
    [ -f "$vendor" ] || { warn "login barrier unchanged: vendor drop-in missing"; return 0; }
    vendor_hash="$(sha256sum "$vendor" | awk '{print $1}')"
    [ "$vendor_hash" = "1c1452839b609b0609cccaba3c648d780372df6f244deb487da6da5ee002a993" ] || {
        warn "login barrier unchanged: vendor hash is not audited"; return 0; }
    normalized="$(sed -E -e 's/\r$//' -e 's/^[[:space:]]+//' -e 's/[[:space:]]+$//' "$vendor" |
        grep -Ev '^(#|;|$)' | sed -E 's/[[:space:]]*=[[:space:]]*/=/')"
    [ "$normalized" = $'[Unit]\nAfter=systemd-user-sessions.service' ] || {
        warn "login barrier unchanged: vendor semantics changed"; return 0; }
    [ "$(loginctl show-user "$BRIDGE_USER" -p Linger --value 2>/dev/null || true)" = yes ] || {
        warn "login barrier unchanged: $BRIDGE_USER does not linger"; return 0; }
    [ ! -e /etc/systemd/system/user@.service.d/10-login-barrier.conf ] &&
        [ ! -L /etc/systemd/system/user@.service.d/10-login-barrier.conf ] || {
        warn "login barrier unchanged: global administrator override exists"; return 0; }
    if [ -e "$instance" ] || [ -L "$instance" ]; then
        if [ -L "$instance" ] && [ "$(readlink "$instance")" = /dev/null ]; then
            log "login barrier already optimized"
            return 0
        fi
        warn "login barrier unchanged: instance override already exists"
        return 0
    fi
    after="$(systemctl show user@1000.service -p After --value 2>/dev/null || true)"
    sessions_after="$(systemctl show systemd-user-sessions.service -p After --value 2>/dev/null || true)"
    grep -qw systemd-user-sessions.service <<<"$after" || {
        warn "login barrier unchanged: audited effective dependency is absent"; return 0; }
    grep -qw network.target <<<"$sessions_after" || {
        warn "login barrier unchanged: user-sessions no longer follows network.target"; return 0; }
    record_path "$instance"
    mkdir -p "$(dirname "$instance")"
    ln -s /dev/null "$instance"
    systemctl daemon-reload
    after="$(systemctl show user@1000.service -p After --value 2>/dev/null || true)"
    if grep -qw systemd-user-sessions.service <<<"$after"; then
        die "instance login-barrier mask did not remove the effective dependency"
    fi
    log "installed audited UID-1000 login-barrier mask"
}

persist_network_and_disable_cloud_init() {
    local target_ip iface uuid autoconnect source_file persistent tmp parsed marker
    marker=/etc/cloud/cloud-init.disabled
    if [ -e "$marker" ]; then
        log "cloud-init already disabled"
        return 0
    fi
    command -v cloud-init >/dev/null 2>&1 || { warn "cloud-init absent; nothing to disable"; return 0; }
    target_ip="$(printf '%s' "${SSH_CONNECTION:-}" | awk '{print $3}')"
    iface="$(ip -o -4 addr show | awk -v ip="$target_ip" '$4 ~ ("^" ip "/") {print $2; exit}')"
    [ -n "$iface" ] || { warn "cloud-init kept enabled: SSH interface not proven"; return 0; }
    uuid="$(nmcli -t -f UUID,DEVICE connection show --active | awk -F: -v dev="$iface" '$2 == dev {print $1; exit}')"
    [ -n "$uuid" ] || { warn "cloud-init kept enabled: active NetworkManager UUID not proven"; return 0; }
    autoconnect="$(nmcli -g connection.autoconnect connection show "$uuid" | head -1)"
    [ "$autoconnect" = yes ] || { warn "cloud-init kept enabled: profile is not autoconnect=yes"; return 0; }
    persistent="$(grep -RIl --include='*.nmconnection' -E "^uuid=${uuid}[[:space:]]*$" \
        /etc/NetworkManager/system-connections 2>/dev/null | head -1 || true)"
    if [ -z "$persistent" ]; then
        source_file="$(nmcli -t -f UUID,FILENAME connection show | awk -F: -v u="$uuid" '$1 == u {sub(/^[^:]*:/, ""); print; exit}')"
        case "$source_file" in /run/NetworkManager/system-connections/*.nmconnection) ;; *)
            warn "cloud-init kept enabled: active profile is not a proven runtime keyfile"; return 0;; esac
        tmp="$(mktemp)"
        nmcli --offline connection modify connection.autoconnect yes < "$source_file" > "$tmp"
        parsed="$(awk -F= '/^\[connection\]$/{s=1;next} /^\[/{s=0} s && $1=="uuid"{print $2;exit}' "$tmp")"
        [ "$parsed" = "$uuid" ] || { rm -f "$tmp"; die "offline NetworkManager copy changed UUID"; }
        persistent="/etc/NetworkManager/system-connections/larkbridge-${iface}-${uuid}.nmconnection"
        record_path "$persistent"
        install -o root -g root -m 0600 "$tmp" "$persistent"
        rm -f "$tmp"
        log "persisted active NetworkManager profile without reloading the interface"
    fi
    cloud_status="$(cloud-init status --long 2>&1 || true)"
    printf '%s\n' "$cloud_status" | grep -Eq '^status:[[:space:]]+done$' || { warn "cloud-init kept enabled: status is not done"; return 0; }
    printf '%s\n' "$cloud_status" | grep -Fq DataSourceNoCloud || { warn "cloud-init kept enabled: datasource is not NoCloud"; return 0; }
    printf '%s\n' "$cloud_status" | grep -Fq 'file:///boot/firmware' || { warn "cloud-init kept enabled: seed is not local boot firmware"; return 0; }
    case "$(findmnt -no FSTYPE /)" in nfs*|cifs) warn "cloud-init kept enabled: root is network-backed"; return 0;; esac
    [ "$(cat /etc/hostname)" = "$(hostname)" ] || { warn "cloud-init kept enabled: hostname is not persistent"; return 0; }
    systemctl is-active --quiet ssh.service || { warn "cloud-init kept enabled: SSH is not active"; return 0; }
    systemctl is-enabled --quiet NetworkManager.service || { warn "cloud-init kept enabled: NetworkManager is not enabled"; return 0; }
    find /etc/ssh -maxdepth 1 -name 'ssh_host_*_key' -type f -print -quit | grep -q . || { warn "cloud-init kept enabled: SSH host keys missing"; return 0; }
    if find /var/lib/cloud/scripts/per-boot -type f -print -quit 2>/dev/null | grep -q .; then
        warn "cloud-init kept enabled: per-boot scripts exist"; return 0
    fi
    if grep -RqsE '(^|[[:space:]])(bootcmd|cloud-boothook):' /boot/firmware/user-data /var/lib/cloud/instance/user-data.txt 2>/dev/null; then
        warn "cloud-init kept enabled: per-boot user-data directives exist"; return 0
    fi
    record_path "$marker"
    mkdir -p "$(dirname "$marker")"
    : > "$marker"
    sync -f "$marker"
    log "disabled cloud-init after persistent-network and platform proofs"
}

install_login_barrier
persist_network_and_disable_cloud_init

systemd-analyze verify \
    "$SOURCE_ROOT/pi/systemd/system/bridge-tuning.service" \
    "$SOURCE_ROOT/pi/systemd/system/bridge-btfw.service" \
    "$SOURCE_ROOT/pi/systemd/system/bridge-boot-trial-rollback.service" \
    "$SOURCE_ROOT/pi/systemd/system/bridge-boot-trial-rollback.timer"
systemctl daemon-reload
systemctl enable bridge-tuning.service bridge-btfw.service >/dev/null
systemctl restart bridge-tuning.service bridge-btfw.service

BRIDGE_SOURCE_ROOT="$SOURCE_ROOT" BRIDGE_BOOT_TRANSACTION="$transaction" \
    bash "$SOURCE_ROOT/scripts/bootstrap/70-verify.sh" --boot-only

ln -sfn "$transaction_id" "$STATE_ROOT/current"
printf '%s\n' "$transaction_id" > "$transaction/installed"
rollback_on_error=0
trap - EXIT
log "transaction committed: $transaction_id"
printf '%s\n' "$transaction_id"
