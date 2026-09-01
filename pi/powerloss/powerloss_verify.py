#!/usr/bin/env python3
"""Post-cut acceptance probe for a read-only LarkBridge appliance."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from lark_state import (
    StateError,
    select_config,
    sha256_file,
    validate_bluez_tree,
    validate_toml,
)

SYSTEM_UNITS = (
    "ssh.service",
    "NetworkManager.service",
    "bluetooth.service",
    "bridge-storage-guard.service",
    "bridge-tuning.service",
    "bridge-btwatchdog@call.service",
    "bridge-pairing-seal.timer",
)
USER_UNITS = (
    "pipewire.service",
    "wireplumber.service",
    "bridge-supervisor.service",
)
CRITICAL_LOG = re.compile(
    r"EXT4-fs (?:error|warning)|FAT-fs.*error|I/O error|Buffer I/O|"
    r"under-?voltage|watchdog.*(?:lockup|BUG)|journal.*corrupt",
    re.IGNORECASE,
)


def run(arguments: list[str], timeout: int = 30) -> dict[str, Any]:
    try:
        result = subprocess.run(
            arguments, capture_output=True, text=True, timeout=timeout, check=False
        )
        return {
            "rc": result.returncode,
            "stderr": result.stderr.strip(),
            "stdout": result.stdout.strip(),
        }
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"rc": 127, "stderr": str(error), "stdout": ""}


def unit_state(unit: str, *, user: bool = False) -> str:
    command = ["systemctl", "is-active", unit]
    if user:
        command = [
            "runuser",
            "-u",
            "admin",
            "--",
            "env",
            "XDG_RUNTIME_DIR=/run/user/1000",
            "systemctl",
            "--user",
            "is-active",
            unit,
        ]
    return run(command)["stdout"]


def findmnt(path: str) -> dict[str, str]:
    result = run(["findmnt", "-n", "-o", "SOURCE,FSTYPE,OPTIONS", "--target", path])
    fields = result["stdout"].split(maxsplit=2)
    return {
        "fstype": fields[1] if len(fields) > 1 else "",
        "options": fields[2] if len(fields) > 2 else "",
        "source": fields[0] if fields else "",
    }


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return {"error": str(error)}


def pairing_identity(path: Path) -> dict[str, str]:
    identity: dict[str, str] = {}
    for info in sorted(path.rglob("info")):
        relative = info.relative_to(path).as_posix()
        identity[relative] = hashlib.sha256(info.read_bytes()).hexdigest()
    return identity


def selected_microphone(
    bridge: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Return the selected microphone while accepting pre-candidate status snapshots.

    Once the generic ``microphone`` object exists it is authoritative.  In particular,
    do not resurrect a stale endpoint during an ambiguous/conflicting selection.
    """

    endpoints = bridge.get("endpoints") or {}
    if not isinstance(endpoints, dict):
        return None, "bridge endpoints are malformed"
    if "microphone" in bridge:
        microphone = bridge.get("microphone")
        if not isinstance(microphone, dict):
            return None, "microphone status is malformed"
        selected = microphone.get("selected")
        if not isinstance(selected, dict):
            reason = microphone.get("selection_reason")
            return None, str(reason or "no microphone candidate is selected")
        candidate_id = selected.get("id")
        node = selected.get("node")
        if not isinstance(candidate_id, str) or not candidate_id:
            return None, "selected microphone has no candidate id"
        if not isinstance(node, str) or not node:
            return None, "selected microphone has no node"
        if endpoints.get("microphone") != node:
            return None, "selected microphone does not match endpoints.microphone"
        return selected, None

    # Schema-1/E17 compatibility: the only selectable microphone was the Lark.
    legacy_node = endpoints.get("microphone") or endpoints.get("lark")
    if isinstance(legacy_node, str) and legacy_node:
        return {"id": "lark-a1", "node": legacy_node, "legacy": True}, None
    return None, "no microphone endpoint is present"


def call_bluetooth_failures(
    adapter_report: dict[str, Any], watchdog: dict[str, Any]
) -> list[str]:
    """Validate the exact BT500's safe idle state and reconnect state schema."""
    failures: list[str] = []
    output = str(adapter_report.get("stdout") or "")
    if adapter_report.get("rc") != 0:
        failures.append("configured call adapter status is unavailable")
    else:
        if "Powered: yes" not in output:
            failures.append("configured call adapter is not powered")
        if "Pairable: no" not in output:
            failures.append(
                "configured call adapter is pairable outside a repair window"
            )
        if "Discoverable: no" not in output:
            failures.append(
                "configured call adapter is discoverable outside a repair window"
            )
        if "0000110b-0000-1000-8000-00805f9b34fb" not in output:
            failures.append("configured call adapter lacks the local A2DP Sink role")
        if "0000111e-0000-1000-8000-00805f9b34fb" not in output:
            failures.append("configured call adapter lacks the local Handsfree role")

    required = {
        "bond_state",
        "repair_state",
        "repair_trigger",
        "repair_deadline_monotonic",
        "reconnect_attempts",
        "reconnect_next_monotonic",
        "startup_phase",
        "startup_connect_attempts",
        "startup_missing_local_uuids",
        "last_action",
    }
    if watchdog.get("error"):
        failures.append("call watchdog status is unavailable")
    else:
        missing = sorted(required - watchdog.keys())
        if missing:
            failures.append(f"call watchdog status lacks fields: {', '.join(missing)}")
        if watchdog.get("repair_state") in {"requested", "preparing", "pairing_window"}:
            failures.append("call watchdog has an active pairing repair transaction")
        if watchdog.get("bond_state") not in {"trusted", "connected"}:
            failures.append("configured Pixel bond is not trusted")
    return failures


def main() -> int:
    failures: list[str] = []
    details: dict[str, Any] = {}
    root = findmnt("/")
    boot = findmnt("/boot/firmware")
    data = findmnt("/var/lib/larkbridge-persist")
    details["mounts"] = {"boot": boot, "data": data, "root": root}
    if root["fstype"] != "overlay":
        failures.append("root is not an overlay filesystem")
    lower_match = re.search(r"(?:^|,)lowerdir=([^,]+)", root["options"])
    if not lower_match:
        failures.append("root overlay has no lowerdir")
    else:
        lower = findmnt(lower_match.group(1).split(":", maxsplit=1)[0])
        details["mounts"]["root_lower"] = lower
        if "ro" not in lower["options"].split(","):
            failures.append("root lower filesystem is not read-only")
    if "ro" not in boot["options"].split(","):
        failures.append("boot filesystem is not read-only")

    storage = read_json(Path("/run/larkbridge/storage-health.json"))
    details["storage"] = storage
    if storage.get("state") not in {"READY", "DEGRADED"}:
        failures.append("storage guard did not produce a recoverable state")
    if storage.get("state") == "READY":
        if data["fstype"] != "ext4" or "rw" not in data["options"].split(","):
            failures.append("healthy LARKDATA is not mounted ext4,rw")
        label = run(["blkid", "-s", "LABEL", "-o", "value", data["source"]])
        details["data_label"] = label
        if label["stdout"] != "LARKDATA":
            failures.append("persistent filesystem label is not LARKDATA")
    elif data["source"] and data["source"] != root["source"]:
        failures.append("DEGRADED state has an unexpected persistent mount")

    system_units = {unit: unit_state(unit) for unit in SYSTEM_UNITS}
    user_units = {unit: unit_state(unit, user=True) for unit in USER_UNITS}
    details["system_units"] = system_units
    details["user_units"] = user_units
    failures.extend(
        f"{unit}={state or 'unknown'}"
        for unit, state in {**system_units, **user_units}.items()
        if state != "active"
    )
    failed_units = run(["systemctl", "--failed", "--no-legend", "--plain"])
    details["failed_units"] = failed_units
    if failed_units["stdout"]:
        failures.append("systemd reports failed units")

    controller_address = run(
        [
            "python3",
            "/home/admin/rpi-lark-bridge/pi/bridged/controller_roles.py",
            "--policy",
            "final-usb",
            "resolve",
            "call",
            "--field",
            "address",
        ]
    )
    details["call_controller_address"] = controller_address
    bluetooth = (
        run(["bluetoothctl", "show", controller_address["stdout"]])
        if controller_address["rc"] == 0 and controller_address["stdout"]
        else {"rc": 127, "stderr": "call controller did not resolve", "stdout": ""}
    )
    watchdog = read_json(Path("/run/larkbridge/bt-watchdog/call.json"))
    details["bluetooth"] = bluetooth
    details["call_watchdog"] = watchdog
    if controller_address["rc"] != 0:
        failures.append("configured call controller identity did not resolve")
    failures.extend(call_bluetooth_failures(bluetooth, watchdog))

    bridge = read_json(Path("/run/user/1000/bridge-status.json"))
    details["bridge"] = bridge
    if bridge.get("error"):
        failures.append("bridge status is unavailable")
    elif bridge.get("state") == "DEGRADED" or bridge.get("last_failure"):
        failures.append("bridge reports a degraded state or last failure")
    else:
        endpoints = bridge.get("endpoints") or {}
        graph = bridge.get("graph") or {}
        microphone, microphone_error = selected_microphone(bridge)
        details["microphone"] = microphone
        if microphone_error:
            failures.append(f"selected microphone is absent: {microphone_error}")
        if not endpoints.get("wired_output"):
            failures.append("configured wired output is absent")
        if graph.get("missing_links"):
            failures.append("bridge graph has missing links")
        if graph.get("unexpected_links"):
            failures.append("bridge graph has unexpected or feedback links")
        if bridge.get("state") == "CALL_DOWN" and (bridge.get("aec") or {}).get(
            "owner_pid"
        ):
            failures.append("stale AEC owner exists while the call is down")

    state_root = Path("/var/lib/larkbridge-persist")
    active = Path("/home/admin/rpi-lark-bridge/config/bridge.toml")
    try:
        config, slot, config_failures = select_config(state_root)
        details["config"] = {
            "failures": config_failures,
            "sha256": sha256_file(config),
            "slot": slot,
        }
        if sha256_file(active) != sha256_file(config):
            failures.append("active configuration does not match the selected slot")
    except (OSError, StateError) as error:
        if storage.get("state") == "READY":
            failures.append(f"checksummed configuration is invalid: {error}")
        else:
            try:
                validate_toml(active)
                details["config"] = {
                    "failures": [str(error)],
                    "sha256": sha256_file(active),
                    "slot": "immutable-fallback",
                }
            except (OSError, StateError) as fallback_error:
                failures.append(f"fallback configuration is invalid: {fallback_error}")
    try:
        validate_bluez_tree(Path("/var/lib/bluetooth"))
        details["pairing_identity"] = pairing_identity(Path("/var/lib/bluetooth"))
    except StateError as error:
        failures.append(str(error))

    seed = Path("/var/lib/systemd/random-seed")
    try:
        seed_size = seed.stat().st_size
        if not 32 <= seed_size <= 4096:
            failures.append("random seed size is invalid")
        details["random_seed"] = {
            "mode": oct(seed.stat().st_mode & 0o777),
            "size": seed_size,
        }
        if seed.stat().st_mode & 0o077:
            failures.append("random seed permissions are not 0600")
    except OSError as error:
        failures.append(f"random seed is unavailable: {error}")

    host_keys = sorted(Path("/etc/ssh").glob("ssh_host_*_key.pub"))
    if not host_keys:
        failures.append("SSH host identity is missing")
    details["ssh_identity_sha256"] = {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in host_keys
    }
    entropy = Path("/proc/sys/kernel/random/entropy_avail").read_text().strip()
    details["entropy_available"] = entropy
    try:
        if int(entropy) < 128:
            failures.append("kernel entropy availability is below 128 bits")
    except ValueError:
        failures.append("kernel entropy availability is not numeric")

    power = run(["vcgencmd", "get_throttled"])
    details["power"] = power
    if power["rc"] == 0 and power["stdout"] != "throttled=0x0":
        failures.append(f"power flags are not clear: {power['stdout']}")
    temperature = run(["vcgencmd", "measure_temp"])
    details["temperature"] = temperature
    match = re.search(r"temp=([0-9.]+)", temperature["stdout"])
    if match and float(match.group(1)) >= 80:
        failures.append(
            f"CPU temperature is at the throttle threshold: {match.group(1)}C"
        )

    ledger = state_root / "recovery/ledger.jsonl"
    try:
        ledger_records = [
            json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()
        ]
        details["ledger_records"] = len(ledger_records)
        if ledger.stat().st_size > 1024 * 1024:
            failures.append("recovery ledger exceeded its 1 MiB active bound")
    except (OSError, json.JSONDecodeError) as error:
        failures.append(f"recovery ledger verification failed: {error}")
    journal_verify = run(["journalctl", "--verify", "--quiet"], timeout=60)
    details["journal_verify"] = journal_verify
    if journal_verify["rc"] != 0:
        failures.append("journal verification failed")
    current_log = run(["journalctl", "-b", "--no-pager", "-o", "short-monotonic"])
    critical_lines = [
        line for line in current_log["stdout"].splitlines() if CRITICAL_LOG.search(line)
    ]
    details["critical_log_lines"] = critical_lines
    if critical_lines:
        failures.append(
            "current boot contains critical storage, power, or watchdog errors"
        )
    restart_count = current_log["stdout"].count("Scheduled restart job")
    details["service_restart_count"] = restart_count
    if restart_count > 2:
        failures.append("current boot contains a service restart loop")

    result = {
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "details": details,
        "failures": failures,
        "ready": not failures,
        "schema": 1,
        "uptime_s": float(Path("/proc/uptime").read_text().split()[0]),
    }
    print(json.dumps(result, sort_keys=True))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
