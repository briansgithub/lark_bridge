#!/usr/bin/env python3
"""External closed-loop boot controller for the LarkBridge test rig."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import shlex
import socket
import statistics
import subprocess
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import tomllib

REPO = Path(__file__).resolve().parents[2]
DEFAULT_INVENTORY = REPO / "rig" / "inventory.toml"
DEFAULT_ARTIFACTS = REPO / "artifacts"
DEFAULT_EXPECTED_MICROPHONE = "lark-a1"
PHONE_CONNECT_LIMIT_S = 25.0
SYSTEM_UNITS = (
    "bluetooth.service",
    "bridge-btwatchdog@call.service",
    "bridge-tuning.service",
)
USER_UNITS = ("pipewire.service", "wireplumber.service", "bridge-supervisor.service")
HEALTH_PATTERNS = {
    "xrun": re.compile(r"\b(?:xrun|underrun|overrun)\b", re.IGNORECASE),
    "undervoltage": re.compile(r"under-?voltage", re.IGNORECASE),
    "hci_failure": re.compile(
        r"(?:Bluetooth|hci\d).*?(?:timed? out|failed|error)", re.IGNORECASE
    ),
    "service_restart": re.compile(r"Scheduled restart job", re.IGNORECASE),
    "filesystem": re.compile(
        r"(?:EXT4-fs error|I/O error|filesystem error)", re.IGNORECASE
    ),
}

REMOTE_PROBE = r"""
import json, os, pathlib, subprocess

def run(args, timeout=8):
    env = os.environ.copy()
    env["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
    try:
        result = subprocess.run(args, text=True, capture_output=True, timeout=timeout, env=env)
        return {"rc": result.returncode, "stdout": result.stdout.strip(), "stderr": result.stderr.strip()}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"rc": 127, "stdout": "", "stderr": str(exc)}

def unit_states(scope, units):
    prefix = ["systemctl"] + (["--user"] if scope == "user" else [])
    return {unit: run(prefix + ["is-active", unit])["stdout"] for unit in units}

status_path = pathlib.Path(f"/run/user/{os.getuid()}/bridge-status.json")
try:
    bridge = json.loads(status_path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as exc:
    bridge = {"error": str(exc)}

watchdog_path = pathlib.Path("/run/larkbridge/bt-watchdog/call.json")
try:
    call_watchdog = json.loads(watchdog_path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as exc:
    call_watchdog = {"error": str(exc)}

bt_list = run(["bluetoothctl", "list"])
bt_show = run(["bluetoothctl", "show"])
failed_units = run(["systemctl", "--failed", "--no-legend", "--plain"])
system = unit_states("system", __SYSTEM_UNITS__)
user = unit_states("user", __USER_UNITS__)
expected_microphone = __EXPECTED_MICROPHONE__
failures = []
failures += [f"{name}={state or 'unknown'}" for name, state in system.items() if state != "active"]
failures += [f"user:{name}={state or 'unknown'}" for name, state in user.items() if state != "active"]
if not bt_list["stdout"] or "Powered: yes" not in bt_show["stdout"]:
    failures.append("Bluetooth adapter is not registered and powered")
if failed_units["stdout"]:
    failures.append("failed systemd units are present")
endpoints = bridge.get("endpoints") or {}
selected_microphone = None
microphone_error = None
if bridge.get("error"):
    failures.append("bridge status is unavailable")
    microphone_error = "bridge status is unavailable"
elif not isinstance(endpoints, dict):
    failures.append("bridge endpoints are malformed")
    microphone_error = "bridge endpoints are malformed"
else:
    if "microphone" in bridge:
        microphone = bridge.get("microphone")
        if not isinstance(microphone, dict):
            microphone_error = "microphone status is malformed"
        else:
            selected = microphone.get("selected")
            if not isinstance(selected, dict):
                microphone_error = str(
                    microphone.get("selection_reason") or "no candidate selected"
                )
            else:
                candidate_id = selected.get("id")
                microphone_node = selected.get("node")
                if not candidate_id:
                    microphone_error = "selected microphone has no candidate id"
                elif not microphone_node:
                    microphone_error = "selected microphone has no node"
                elif endpoints.get("microphone") != microphone_node:
                    microphone_error = (
                        "selected microphone does not match endpoints.microphone"
                    )
                else:
                    selected_microphone = selected
    else:
        # Schema-1/E17 compatibility: the only selectable microphone was the Lark.
        legacy_node = endpoints.get("lark")
        if legacy_node:
            selected_microphone = {
                "id": "lark-a1", "node": legacy_node, "legacy": True
            }
        else:
            microphone_error = "no legacy Lark endpoint is present"

    if microphone_error:
        failures.append("selected microphone is absent: " + microphone_error)
    elif selected_microphone.get("id") != expected_microphone:
        failures.append(
            "selected microphone is " + repr(selected_microphone.get("id"))
            + ", expected " + repr(expected_microphone)
        )
    if bridge.get("state") == "DEGRADED" or bridge.get("last_failure"):
        failures.append(f"bridge unhealthy: {bridge.get('last_failure') or bridge.get('state')}")
    if not endpoints.get("wired_output"):
        failures.append("configured output is absent")
if (bridge.get("graph") or {}).get("missing_links"):
    failures.append("bridge graph has missing links")
if (bridge.get("graph") or {}).get("unexpected_links"):
    failures.append("bridge graph has unexpected links")
if bridge.get("state") == "CALL_DOWN" and (bridge.get("aec") or {}).get("owner_pid"):
    failures.append("stale AEC owner exists while the call is down")

power = run(["vcgencmd", "get_throttled"])
if power["rc"] == 0 and power["stdout"] != "throttled=0x0":
    failures.append(f"power flags are not clear: {power['stdout']}")

phone_failures = []
phone_failures += [
    f"{name}={state or 'unknown'}" for name, state in system.items()
    if state != "active"
]
phone_failures += [
    f"user:{name}={state or 'unknown'}" for name, state in user.items()
    if state != "active"
]
if failed_units["stdout"]:
    phone_failures.append("failed systemd units are present")
phone = bridge.get("phone") or {}
call_controller = (bridge.get("controllers") or {}).get("call") or {}
call_address = call_controller.get("configured_address")
bt_call_show = (
    run(["bluetoothctl", "show", call_address])
    if call_address
    else {"rc": 127, "stdout": "", "stderr": "configured call address is absent"}
)
show_output = bt_call_show["stdout"]
for required, reason in (
    ("Powered: yes", "configured BT500 is not powered"),
    ("Pairable: no", "configured BT500 remains pairable"),
    ("Discoverable: no", "configured BT500 remains discoverable"),
    ("0000110b-0000-1000-8000-00805f9b34fb", "local A2DP Sink role is missing"),
    ("0000111e-0000-1000-8000-00805f9b34fb", "local Handsfree role is missing"),
):
    if required not in show_output:
        phone_failures.append(reason)
if bridge.get("error"):
    phone_failures.append("bridge status is unavailable")
elif phone.get("connected") is not True:
    phone_failures.append("Pixel is not connected")
if call_controller.get("ready") is not True:
    phone_failures.append("configured call controller is not ready")
if call_controller.get("observed_address") != call_controller.get("configured_address"):
    phone_failures.append("call controller identity does not match configuration")
if call_watchdog.get("error"):
    phone_failures.append("call watchdog status is unavailable")
else:
    if call_watchdog.get("bond_state") != "connected":
        phone_failures.append("Pixel bond is not connected")
    if call_watchdog.get("repair_state") != "idle":
        phone_failures.append("Pixel repair state is not idle")
    if call_watchdog.get("startup_missing_local_uuids"):
        phone_failures.append("watchdog still reports missing local phone profiles")
    watchdog_controller = call_watchdog.get("controller") or {}
    if watchdog_controller.get("address") != call_controller.get("configured_address"):
        phone_failures.append("watchdog is not bound to the configured BT500")

mounts = {
    name: run(["findmnt", "-n", "-o", "OPTIONS", "--target", target])
    for name, target in (
        ("root_lower", "/media/root-ro"),
        ("boot", "/boot/firmware"),
    )
}
for name, report in mounts.items():
    options = set(report["stdout"].split(","))
    if report["rc"] != 0 or "ro" not in options:
        phone_failures.append(f"{name} is not mounted read-only")

print(json.dumps({
    "boot_id": pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
    "uptime_s": float(pathlib.Path("/proc/uptime").read_text().split()[0]),
    "system_units": system,
    "user_units": user,
    "bluetooth": {"list": bt_list, "show": bt_show, "call_show": bt_call_show},
    "failed_units": failed_units,
    "bridge": bridge,
    "call_watchdog": call_watchdog,
    "microphone": {
        "expected_id": expected_microphone,
        "observed": selected_microphone,
        "error": microphone_error,
    },
    "power": power,
    "mounts": mounts,
    "phone_ready": not phone_failures,
    "phone_failures": phone_failures,
    "ready": not failures,
    "failures": failures,
}))
"""

REMOTE_MANIFEST = r"""
import hashlib, json, pathlib, subprocess

def text(path):
    try:
        return pathlib.Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""

def run(args):
    result = subprocess.run(args, text=True, capture_output=True, check=False)
    return result.stdout.strip()

def digest(path):
    try:
        return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()
    except OSError:
        return ""

files = [
    "/etc/systemd/system/bridge-tuning.service",
    "/etc/systemd/system/bridge-btwatchdog@.service",
    "/usr/local/lib/rpi-lark-bridge/set-sco-routing.sh",
    "/usr/local/lib/rpi-lark-bridge/boot-path/netplan",
    "/etc/systemd/system/NetworkManager.service.d/10-larkbridge-netplan-startup.conf",
    "/boot/firmware/config.txt",
    "/boot/firmware/cmdline.txt",
]
packages = ["systemd", "bluez", "network-manager", "pipewire", "wireplumber", "cloud-init"]
print(json.dumps({
    "os_release": text("/etc/os-release"),
    "kernel": run(["uname", "-a"]),
    "firmware": run(["vcgencmd", "version"]),
    "model": text("/proc/device-tree/model").replace("\x00", ""),
    "machine_id_sha256": hashlib.sha256(text("/etc/machine-id").encode()).hexdigest(),
    "packages": {name: run(["dpkg-query", "-W", "-f=${Version}", name]) for name in packages},
    "file_sha256": {path: digest(path) for path in files},
    "default_target": run(["systemctl", "get-default"]),
}))
"""


def utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def selected_microphone(
    bridge: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Interpret current and legacy bridge status without reviving stale endpoints."""

    endpoints = bridge.get("endpoints") or {}
    if not isinstance(endpoints, dict):
        return None, "bridge endpoints are malformed"
    if "microphone" in bridge:
        microphone = bridge.get("microphone")
        if not isinstance(microphone, dict):
            return None, "microphone status is malformed"
        selected = microphone.get("selected")
        if not isinstance(selected, dict):
            return None, str(
                microphone.get("selection_reason")
                or "no microphone candidate is selected"
            )
        candidate_id = selected.get("id")
        node = selected.get("node")
        if not isinstance(candidate_id, str) or not candidate_id:
            return None, "selected microphone has no candidate id"
        if not isinstance(node, str) or not node:
            return None, "selected microphone has no node"
        if endpoints.get("microphone") != node:
            return None, "selected microphone does not match endpoints.microphone"
        return selected, None
    # Schema-1/E17 compatibility is Lark-specific. A generic endpoint without the
    # identity-bearing microphone object cannot prove which candidate was selected.
    legacy_node = endpoints.get("lark")
    if isinstance(legacy_node, str) and legacy_node:
        return {"id": "lark-a1", "node": legacy_node, "legacy": True}, None
    return None, "no legacy Lark endpoint is present"


def microphone_evidence(
    probe: dict[str, Any], expected_microphone: str
) -> dict[str, Any]:
    bridge = probe.get("bridge")
    if not isinstance(bridge, dict):
        return {
            "expected_id": expected_microphone,
            "observed": None,
            "observed_id": None,
            "matches": False,
            "error": "probe bridge status is missing or malformed",
        }
    observed, error = selected_microphone(bridge)
    observed_id = observed.get("id") if observed else None
    return {
        "expected_id": expected_microphone,
        "observed": observed,
        "observed_id": observed_id,
        "matches": error is None and observed_id == expected_microphone,
        "error": error,
    }


def require_expected_microphone(
    probe: dict[str, Any], expected_microphone: str
) -> dict[str, Any]:
    evidence = microphone_evidence(probe, expected_microphone)
    if evidence["error"]:
        raise RuntimeError(f"selected microphone unavailable: {evidence['error']}")
    if not evidence["matches"]:
        raise RuntimeError(
            f"selected microphone is {evidence['observed_id']!r}, "
            f"expected {expected_microphone!r}"
        )
    return evidence


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def percentile(values: Iterable[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("at least one value is required")
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def bootstrap_median_delta(
    baseline: list[float], candidate: list[float], *, samples: int = 5000
) -> tuple[float, float]:
    rng = random.Random(0)
    deltas = []
    for _ in range(samples):
        left = [rng.choice(baseline) for _ in baseline]
        right = [rng.choice(candidate) for _ in candidate]
        deltas.append(statistics.median(left) - statistics.median(right))
    return percentile(deltas, 0.025), percentile(deltas, 0.975)


@dataclass(frozen=True)
class Config:
    inventory: Path
    pi_host: str
    probe_host: str
    pi_user: str
    ssh_timeout_s: int
    boot_timeout_s: int
    shutdown_timeout_s: int
    cold_off_seconds: float
    expected_microphone: str
    artifacts: Path
    power_off: tuple[str, ...]
    power_on: tuple[str, ...]
    serial_capture: tuple[str, ...]
    functional_probe: tuple[str, ...]
    variant_apply: tuple[str, ...]

    @classmethod
    def load(cls, path: Path, artifacts: Path | None = None) -> Config:
        with path.open("rb") as handle:
            data = tomllib.load(handle)

        def command(name: str) -> tuple[str, ...]:
            value = data.get(name, [])
            if not isinstance(value, list) or not all(
                isinstance(part, str) for part in value
            ):
                raise ValueError(f"{name} must be a TOML array of strings")
            return tuple(value)

        host = str(data.get("pi_host", "larkbridge"))
        expected_microphone = str(
            data.get("boot_expected_microphone", DEFAULT_EXPECTED_MICROPHONE)
        ).strip()
        if not expected_microphone:
            raise ValueError("boot_expected_microphone cannot be empty")
        return cls(
            inventory=path,
            pi_host=host,
            probe_host=str(data.get("boot_probe_host") or data.get("pi_ip") or host),
            pi_user=str(data.get("pi_user", "admin")),
            ssh_timeout_s=int(data.get("boot_ssh_timeout_seconds", 8)),
            boot_timeout_s=int(data.get("boot_timeout_seconds", 120)),
            shutdown_timeout_s=int(data.get("boot_shutdown_timeout_seconds", 30)),
            cold_off_seconds=float(data.get("boot_cold_off_seconds", 10)),
            expected_microphone=expected_microphone,
            artifacts=artifacts or DEFAULT_ARTIFACTS,
            power_off=command("boot_power_off_command"),
            power_on=command("boot_power_on_command"),
            serial_capture=command("boot_serial_capture_command"),
            functional_probe=command("boot_functional_probe_command"),
            variant_apply=command("boot_variant_apply_command"),
        )


class Recorder:
    def __init__(self, directory: Path):
        self.directory = directory
        self.started = time.perf_counter()
        self.events: list[dict[str, Any]] = []

    def event(self, name: str, **details: Any) -> float:
        elapsed = round(time.perf_counter() - self.started, 3)
        item = {"event": name, "elapsed_s": elapsed, **details}
        self.events.append(item)
        with (self.directory / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(item, sort_keys=True) + "\n")
        print(f"[{elapsed:8.3f}s] {name}", file=sys.stderr, flush=True)
        return elapsed


class Ssh:
    def __init__(self, config: Config):
        self.config = config

    def _base(self) -> list[str]:
        return [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={self.config.ssh_timeout_s}",
            self.config.pi_host,
        ]

    def run(
        self, remote: str, *, timeout: int | None = None, input_text: str | None = None
    ):
        return subprocess.run(
            self._base() + [remote],
            input=input_text,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout or self.config.ssh_timeout_s + 5,
            check=False,
        )

    def probe(self, expected_microphone: str | None = None) -> dict[str, Any]:
        expected = expected_microphone or self.config.expected_microphone
        script = REMOTE_PROBE.replace("__SYSTEM_UNITS__", repr(SYSTEM_UNITS)).replace(
            "__USER_UNITS__", repr(USER_UNITS)
        )
        script = script.replace("__EXPECTED_MICROPHONE__", repr(expected))
        result = self.run("python3 -", input_text=script)
        if result.returncode != 0:
            raise RuntimeError(
                result.stderr.strip() or f"SSH probe exited {result.returncode}"
            )
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid remote probe JSON: {exc}") from exc

    def manifest(self) -> dict[str, Any]:
        result = self.run("python3 -", input_text=REMOTE_MANIFEST)
        if result.returncode != 0:
            raise RuntimeError(
                result.stderr.strip() or f"remote manifest exited {result.returncode}"
            )
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid remote manifest JSON: {exc}") from exc


def port_open(host: str, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, 22), timeout=timeout):
            return True
    except OSError:
        return False


def expanded(command: tuple[str, ...], **values: str) -> list[str]:
    return [part.format(**values) for part in command]


def run_hook(command: tuple[str, ...], *, timeout: int, **values: str):
    if not command:
        raise RuntimeError("required command hook is not configured")
    return subprocess.run(expanded(command, **values), timeout=timeout, check=False)


def git_metadata() -> dict[str, Any]:
    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=REPO, text=True, capture_output=True, check=False
        )
        return result.stdout.strip()

    return {
        "head": git("rev-parse", "HEAD"),
        "branch": git("branch", "--show-current"),
        "tracked_status": git("status", "--short", "--untracked-files=no"),
        "full_status": git("status", "--short"),
    }


def collect_evidence(ssh: Ssh, directory: Path) -> None:
    commands = {
        "systemd-analyze.txt": "systemd-analyze",
        "critical-chain.txt": "systemd-analyze critical-chain --no-pager",
        "blame.txt": "systemd-analyze blame --no-pager",
        "journal.txt": "journalctl -b -o short-monotonic --no-pager",
        "system-units.txt": "systemctl show " + " ".join(SYSTEM_UNITS),
        "user-units.txt": "export XDG_RUNTIME_DIR=/run/user/$(id -u); systemctl --user show "
        + " ".join(USER_UNITS),
    }
    for filename, command in commands.items():
        try:
            result = ssh.run(command, timeout=30)
            text = (result.stdout or "") + (
                "\nSTDERR:\n" + result.stderr if result.stderr else ""
            )
        except subprocess.TimeoutExpired as exc:
            text = f"collection timed out: {exc}\n"
        (directory / filename).write_text(text, encoding="utf-8")


def summarize_health(journal: str) -> dict[str, int]:
    return {
        name: len(pattern.findall(journal)) for name, pattern in HEALTH_PATTERNS.items()
    }


def validate_functional_result(
    path: Path, run_id: str, watermark: str
) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"functional result is unavailable or invalid: {exc}"
        ) from exc
    if value.get("schema_version") != 1 or value.get("run_id") != run_id:
        raise RuntimeError("functional result schema or run identity is invalid")
    if value.get("pass") is not True or value.get("call_active") is not True:
        raise RuntimeError("functional result did not prove an active passing call")
    for direction in ("lark_to_far_end", "far_end_to_output"):
        proof = value.get(direction) or {}
        if proof.get("watermark") != watermark or proof.get("detected") is not True:
            raise RuntimeError(
                f"functional result lacks the {direction} watermark proof"
            )
    if value.get("feedback_detected") is not False:
        raise RuntimeError("functional result did not prove feedback absence")
    if int(value.get("dropouts", -1)) != 0:
        raise RuntimeError("functional result reports dropouts")
    return value


def confirm_trial(ssh: Ssh) -> str:
    result = ssh.run(
        "sudo -n /usr/local/lib/rpi-lark-bridge/boot-trial.sh confirm", timeout=15
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "boot trial confirmation failed")
    return result.stdout.strip()


def wait_for_port(host: str, wanted: bool, deadline: float) -> bool:
    while time.monotonic() < deadline:
        if port_open(host) is wanted:
            return True
        time.sleep(0.25)
    return False


def start_serial(
    config: Config, directory: Path, run_id: str
) -> subprocess.Popen | None:
    if not config.serial_capture:
        return None
    command = expanded(
        config.serial_capture,
        output=str(directory / "serial.log"),
        run_dir=str(directory),
        run_id=run_id,
    )
    return subprocess.Popen(
        command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


def stop_serial(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


def run_boot(
    config: Config,
    *,
    mode: str,
    candidate: str,
    require_functional: bool,
    expected_microphone: str | None = None,
    manual_power: bool = False,
    readiness_profile: str = "full",
) -> Path:
    if readiness_profile not in {"full", "phone"}:
        raise ValueError(f"unsupported readiness profile: {readiness_profile}")
    if readiness_profile == "phone" and require_functional:
        raise ValueError("phone readiness cannot require the full functional probe")
    expected = (expected_microphone or config.expected_microphone).strip()
    if not expected:
        raise ValueError("expected microphone id cannot be empty")
    run_id = f"{utc_stamp()}-{candidate}-{mode}"
    directory = config.artifacts / f"boot-run-{run_id}"
    directory.mkdir(parents=True, exist_ok=False)
    recorder = Recorder(directory)
    ssh = Ssh(config)
    meta = git_metadata()
    remote_manifest = ssh.manifest()
    manifest = {
        "run_id": run_id,
        "candidate": candidate,
        "mode": mode,
        "git": meta,
        "remote": remote_manifest,
        "microphone": {
            "expected_id": expected,
            "preboot": None,
            "idle": None,
            "functional": None,
        },
        "manual_power": manual_power,
        "readiness_profile": readiness_profile,
    }
    write_json(directory / "manifest.json", manifest)
    result: dict[str, Any] = {
        "run_id": run_id,
        "candidate": candidate,
        "mode": mode,
        "verdict": "FAIL",
        "readiness_level": "none",
        "timings_s": {},
        "git": meta,
        "expected_microphone": expected,
        "manual_power": manual_power,
        "readiness_profile": readiness_profile,
        "observed_microphone": None,
        "microphone_evidence": {
            "preboot": None,
            "idle": None,
            "functional": None,
        },
    }
    serial: subprocess.Popen | None = None
    try:
        if meta["tracked_status"]:
            raise RuntimeError(
                "tracked worktree changes exist; commit or restore them before timing"
            )
        before = ssh.probe(expected)
        write_json(directory / "preboot.json", before)
        preboot_microphone = microphone_evidence(before, expected)
        manifest["microphone"]["preboot"] = preboot_microphone
        result["microphone_evidence"]["preboot"] = preboot_microphone
        result["observed_microphone"] = preboot_microphone["observed"]
        write_json(directory / "manifest.json", manifest)
        old_boot_id = before["boot_id"]

        if mode == "cold":
            recorder.event("power_off_requested")
            if manual_power:
                print(
                    "MANUAL POWER: turn the car/Pi power OFF now.",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                hook = run_hook(
                    config.power_off,
                    timeout=30,
                    run_dir=str(directory),
                    run_id=run_id,
                )
                if hook.returncode != 0:
                    raise RuntimeError(f"power-off hook exited {hook.returncode}")
            if not wait_for_port(
                config.probe_host, False, time.monotonic() + config.shutdown_timeout_s
            ):
                raise RuntimeError("SSH port did not close after power-off")
            recorder.event("ssh_down")
            time.sleep(config.cold_off_seconds)
            serial = start_serial(config, directory, run_id)
            recorder.started = time.perf_counter()
            recorder.event("power_on_requested")
            if manual_power:
                print(
                    "MANUAL POWER: turn the car/Pi power ON now.",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                hook = run_hook(
                    config.power_on,
                    timeout=30,
                    run_dir=str(directory),
                    run_id=run_id,
                )
                if hook.returncode != 0:
                    raise RuntimeError(f"power-on hook exited {hook.returncode}")
        else:
            serial = start_serial(config, directory, run_id)
            recorder.event("reboot_requested")
            try:
                ssh.run("sudo -n systemctl reboot", timeout=15)
            except subprocess.TimeoutExpired:
                pass
            if not wait_for_port(
                config.probe_host, False, time.monotonic() + config.shutdown_timeout_s
            ):
                raise RuntimeError("SSH port did not close after reboot request")
            result["timings_s"]["ssh_down"] = recorder.event("ssh_down")

        deadline = time.monotonic() + config.boot_timeout_s
        if not wait_for_port(config.probe_host, True, deadline):
            raise RuntimeError("SSH port did not return before the boot timeout")
        result["timings_s"]["ssh_port_open"] = recorder.event("ssh_port_open")

        ready = None
        ssh_seen = False
        last_error = ""
        while time.monotonic() < deadline:
            try:
                probe = ssh.probe(expected)
                if probe.get("boot_id") == old_boot_id:
                    last_error = "SSH answered from the previous boot"
                else:
                    if not ssh_seen:
                        result["timings_s"]["new_boot_ssh"] = recorder.event(
                            "new_boot_ssh"
                        )
                        ssh_seen = True
                    probe_ready = (
                        probe.get("phone_ready")
                        if readiness_profile == "phone"
                        else probe.get("ready")
                    )
                    if probe_ready:
                        ready = probe
                        break
                    failure_key = (
                        "phone_failures" if readiness_profile == "phone" else "failures"
                    )
                    last_error = "; ".join(probe.get(failure_key, []))
            except (RuntimeError, subprocess.TimeoutExpired) as exc:
                last_error = str(exc)
            time.sleep(1)
        if ready is None:
            raise RuntimeError(f"idle readiness timed out: {last_error}")

        write_json(directory / "ready.json", ready)
        idle_microphone = microphone_evidence(ready, expected)
        manifest["microphone"]["idle"] = idle_microphone
        result["microphone_evidence"]["idle"] = idle_microphone
        result["observed_microphone"] = idle_microphone["observed"]
        write_json(directory / "manifest.json", manifest)
        if readiness_profile == "phone":
            watchdog = ready.get("call_watchdog") or {}
            connected = watchdog.get("connected_monotonic")
            if not isinstance(connected, (int, float)):
                raise RuntimeError("watchdog lacks the phone connection time")
            result["timings_s"]["phone_connected"] = float(connected)
            if float(connected) > PHONE_CONNECT_LIMIT_S:
                raise RuntimeError(
                    f"Pixel connected at {float(connected):.3f}s, "
                    f"exceeding {PHONE_CONNECT_LIMIT_S:.0f}s"
                )
            result["readiness_level"] = "phone"
            recorder.event("phone_ready", connected_monotonic=float(connected))
        else:
            require_expected_microphone(ready, expected)
            result["timings_s"]["idle_ready"] = recorder.event("idle_ready")
            result["readiness_level"] = "idle"

        if readiness_profile != "phone" and config.functional_probe:
            watermark = "LB-" + hashlib.sha256(run_id.encode()).hexdigest()[:16]
            hook = run_hook(
                config.functional_probe,
                timeout=config.boot_timeout_s,
                run_dir=str(directory),
                run_id=run_id,
                candidate=candidate,
                expected_microphone=expected,
                watermark=watermark,
            )
            if hook.returncode != 0:
                raise RuntimeError(f"functional probe exited {hook.returncode}")
            functional = validate_functional_result(
                directory / "functional-result.json", run_id, watermark
            )
            write_json(directory / "functional-result.validated.json", functional)
            functional_probe = ssh.probe(expected)
            write_json(directory / "functional-ready.json", functional_probe)
            functional_microphone = microphone_evidence(functional_probe, expected)
            manifest["microphone"]["functional"] = functional_microphone
            result["microphone_evidence"]["functional"] = functional_microphone
            result["observed_microphone"] = functional_microphone["observed"]
            write_json(directory / "manifest.json", manifest)
            require_expected_microphone(functional_probe, expected)
            if not functional_probe.get("ready"):
                raise RuntimeError(
                    "post-functional readiness failed: "
                    + "; ".join(map(str, functional_probe.get("failures", [])))
                )
            bridge = functional_probe.get("bridge") or {}
            graph = bridge.get("graph") or {}
            aec = bridge.get("aec") or {}
            if bridge.get("state") != "ACTIVE":
                raise RuntimeError(
                    "supervisor is not ACTIVE after the functional probe"
                )
            if aec.get("enabled") and not aec.get("verified"):
                raise RuntimeError(
                    "AEC is enabled but unverified after the functional probe"
                )
            if graph.get("missing_links") or graph.get("unexpected_links"):
                raise RuntimeError(
                    "functional PipeWire graph has missing or unexpected links"
                )
            result["timings_s"]["functional_ready"] = recorder.event("functional_ready")
            result["readiness_level"] = "functional"
        elif require_functional:
            raise RuntimeError(
                "functional readiness was required but no hook is configured"
            )

        confirmation = confirm_trial(ssh)
        recorder.event("trial_confirmed", detail=confirmation)
        collect_evidence(ssh, directory)
        journal = (directory / "journal.txt").read_text(
            encoding="utf-8", errors="replace"
        )
        result["health_events"] = summarize_health(journal)
        result["verdict"] = "PASS"
    except (OSError, RuntimeError, subprocess.TimeoutExpired, ValueError) as exc:
        result["failure"] = str(exc)
        recorder.event("failed", reason=str(exc))
        try:
            collect_evidence(ssh, directory)
        except OSError:
            pass
    finally:
        stop_serial(serial)
        result["events"] = recorder.events
        write_json(directory / "result.json", result)
    print(json.dumps(result, indent=2))
    return directory


def doctor(config: Config) -> int:
    report: dict[str, Any] = {
        "inventory": str(config.inventory),
        "pi_host": config.pi_host,
        "probe_host": config.probe_host,
        "git": git_metadata(),
        "cold_power_configured": bool(config.power_off and config.power_on),
        "serial_capture_configured": bool(config.serial_capture),
        "functional_probe_configured": bool(config.functional_probe),
    }
    try:
        report["remote"] = Ssh(config).probe()
        report["ssh"] = "PASS"
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        report["ssh"] = "FAIL"
        report["error"] = str(exc)
    print(json.dumps(report, indent=2))
    return 0 if report["ssh"] == "PASS" else 1


def load_results(
    root: Path, label: str, mode: str | None = None
) -> tuple[list[dict[str, Any]], list[float], str]:
    all_runs = []
    values = []
    level = "functional"
    for path in sorted(root.glob("boot-run-*/result.json")):
        result = json.loads(path.read_text(encoding="utf-8"))
        if result.get("candidate") != label:
            continue
        if mode and result.get("mode") != mode:
            continue
        all_runs.append(result)
        timings = result.get("timings_s", {})
        if result.get("verdict") != "PASS":
            continue
        if "phone_connected" in timings:
            level = "phone"
            values.append(float(timings["phone_connected"]))
        elif "functional_ready" in timings:
            values.append(float(timings["functional_ready"]))
        elif "idle_ready" in timings:
            level = "idle"
            values.append(float(timings["idle_ready"]))
    return all_runs, values, level


def compare(
    config: Config,
    baseline_label: str,
    candidate_label: str,
    allow_idle: bool,
    allow_phone: bool,
    mode: str | None,
) -> int:
    base_runs, baseline, base_level = load_results(
        config.artifacts, baseline_label, mode
    )
    cand_runs, candidate, cand_level = load_results(
        config.artifacts, candidate_label, mode
    )
    if len(baseline) < 10 or len(candidate) < 10:
        raise SystemExit("comparison requires at least ten passing runs for each label")
    if "phone" in {base_level, cand_level} and not allow_phone:
        raise SystemExit(
            "phone-only results require --allow-phone for an acceptance verdict"
        )
    if (
        "phone" not in {base_level, cand_level}
        and not allow_idle
        and (base_level != "functional" or cand_level != "functional")
    ):
        raise SystemExit(
            "idle-only results cannot produce an acceptance verdict; use --allow-idle"
        )
    base_median = statistics.median(baseline)
    cand_median = statistics.median(candidate)
    effect = base_median - cand_median
    ci = bootstrap_median_delta(baseline, candidate)
    minimum = max(0.250, base_median * 0.02)
    candidate_failures = len(cand_runs) - len(candidate)
    p95_base = percentile(baseline, 0.95)
    p95_candidate = percentile(candidate, 0.95)
    health_regressions = {}
    for name in HEALTH_PATTERNS:
        base_max = max(
            (int(run.get("health_events", {}).get(name, 0)) for run in base_runs),
            default=0,
        )
        cand_max = max(
            (int(run.get("health_events", {}).get(name, 0)) for run in cand_runs),
            default=0,
        )
        base_total = sum(
            int(run.get("health_events", {}).get(name, 0)) for run in base_runs
        )
        cand_total = sum(
            int(run.get("health_events", {}).get(name, 0)) for run in cand_runs
        )
        base_affected = sum(
            int(run.get("health_events", {}).get(name, 0)) > 0 for run in base_runs
        )
        cand_affected = sum(
            int(run.get("health_events", {}).get(name, 0)) > 0 for run in cand_runs
        )
        if (
            cand_max > base_max
            or cand_total > base_total
            or cand_affected > base_affected
        ):
            health_regressions[name] = {
                "baseline_max": base_max,
                "candidate_max": cand_max,
                "baseline_total": base_total,
                "candidate_total": cand_total,
                "baseline_affected_runs": base_affected,
                "candidate_affected_runs": cand_affected,
            }
    performance_passes = (
        candidate_failures == 0
        and not health_regressions
        and effect >= minimum
        and ci[0] > 0
        and p95_candidate <= p95_base + 0.5
    )
    phone_passes = (
        cand_level == "phone"
        and allow_phone
        and candidate_failures == 0
        and not health_regressions
        and max(candidate) <= PHONE_CONNECT_LIMIT_S
        and max(candidate) <= max(baseline) + 0.5
        and cand_median <= base_median + 0.25
    )
    accepted = (performance_passes and cand_level == "functional") or phone_passes
    provisional_idle = performance_passes and cand_level == "idle" and allow_idle
    if phone_passes:
        verdict = "PHONE_ACCEPT"
    elif accepted:
        verdict = "PROVISIONAL_ACCEPT"
    elif provisional_idle:
        verdict = "PROVISIONAL_IDLE_IMPROVEMENT"
    else:
        verdict = "REJECT"
    report = {
        "verdict": verdict,
        "baseline": {
            "label": baseline_label,
            "runs": len(base_runs),
            "passing": len(baseline),
            "median_s": base_median,
            "p95_s": p95_base,
        },
        "candidate": {
            "label": candidate_label,
            "runs": len(cand_runs),
            "passing": len(candidate),
            "median_s": cand_median,
            "p95_s": p95_candidate,
        },
        "readiness_level": cand_level,
        "mode": mode or "all",
        "health_regressions": health_regressions,
        "median_improvement_s": effect,
        "minimum_effect_s": minimum,
        "bootstrap_95pct_ci_s": list(ci),
        "note": (
            "Phone acceptance proves reconnect reliability and timing only; explicit Bluetooth-off, out-of-range, stale-bond, power-loss, and image gates remain."
            if phone_passes
            else (
                "Idle-ready evidence cannot accept the candidate; automated two-way call audio, robustness, and soak gates remain."
                if provisional_idle
                else "A provisional acceptance still requires the robustness and soak gates."
            )
        ),
    }
    print(json.dumps(report, indent=2))
    return 0 if accepted else 1


def apply_variant(config: Config, *, label: str, revision: str) -> None:
    if not config.variant_apply:
        raise RuntimeError("boot_variant_apply_command is not configured")
    result = run_hook(
        config.variant_apply,
        timeout=config.boot_timeout_s,
        candidate=label,
        revision=revision,
        run_dir=str(config.artifacts),
        run_id=f"variant-{label}",
    )
    if result.returncode != 0:
        raise RuntimeError(f"variant apply hook exited {result.returncode}: {label}")


def screen(
    config: Config,
    *,
    baseline_label: str,
    baseline_revision: str,
    candidate_label: str,
    candidate_revision: str,
    pairs: int,
    mode: str,
    require_functional: bool,
    seed: int,
    expected_microphone: str | None = None,
) -> int:
    if pairs < 10:
        raise ValueError("candidate screening requires at least ten randomized pairs")
    expected = (expected_microphone or config.expected_microphone).strip()
    if not expected:
        raise ValueError("expected microphone id cannot be empty")
    rng = random.Random(seed)
    assignments = [
        (baseline_label, baseline_revision),
        (candidate_label, candidate_revision),
    ]
    failures = 0
    try:
        for _ in range(pairs):
            order = (
                assignments[:] if rng.randrange(2) == 0 else list(reversed(assignments))
            )
            for label, revision in order:
                apply_variant(config, label=label, revision=revision)
                path = run_boot(
                    config,
                    mode=mode,
                    candidate=label,
                    require_functional=require_functional,
                    expected_microphone=expected,
                )
                result = json.loads((path / "result.json").read_text(encoding="utf-8"))
                failures += result["verdict"] != "PASS"
                time.sleep(3)
    finally:
        apply_variant(config, label=baseline_label, revision=baseline_revision)
        confirm_trial(Ssh(config))
    return 1 if failures else 0


def trial(config: Config, action: str, transaction: str | None) -> int:
    ssh = Ssh(config)
    if action == "arm":
        if not transaction:
            raise SystemExit("trial arm requires --transaction")
        command = (
            "sudo -n /usr/local/lib/rpi-lark-bridge/boot-trial.sh arm "
            + shlex.quote(transaction)
        )
    elif action == "confirm":
        command = "sudo -n /usr/local/lib/rpi-lark-bridge/boot-trial.sh confirm"
    elif action == "rollback":
        if not transaction:
            raise SystemExit("trial rollback requires --transaction")
        command = (
            "sudo -n /usr/local/lib/rpi-lark-bridge/boot-transaction.sh rollback "
            + shlex.quote(transaction)
        )
    else:
        command = "sudo -n /usr/local/lib/rpi-lark-bridge/boot-trial.sh status"
    result = ssh.run(command, timeout=30)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    result.add_argument("--artifacts", type=Path, default=DEFAULT_ARTIFACTS)
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor")
    run = commands.add_parser("run")
    run.add_argument("--mode", choices=("warm", "cold"), default="warm")
    run.add_argument("--candidate", required=True)
    run.add_argument("--require-functional", action="store_true")
    run.add_argument("--manual-power", action="store_true")
    run.add_argument("--readiness-profile", choices=("full", "phone"), default="full")
    baseline = commands.add_parser("baseline")
    baseline.add_argument("--mode", choices=("warm", "cold"), default="warm")
    baseline.add_argument("--candidate", default="baseline")
    baseline.add_argument("--count", type=int, default=10)
    baseline.add_argument("--require-functional", action="store_true")
    baseline.add_argument("--manual-power", action="store_true")
    baseline.add_argument(
        "--readiness-profile", choices=("full", "phone"), default="full"
    )
    compare_cmd = commands.add_parser("compare")
    compare_cmd.add_argument("--baseline", required=True)
    compare_cmd.add_argument("--candidate", required=True)
    compare_cmd.add_argument("--allow-idle", action="store_true")
    compare_cmd.add_argument("--allow-phone", action="store_true")
    compare_cmd.add_argument("--mode", choices=("warm", "cold"))
    screen_cmd = commands.add_parser("screen")
    screen_cmd.add_argument("--baseline", required=True)
    screen_cmd.add_argument("--baseline-rev", required=True)
    screen_cmd.add_argument("--candidate", required=True)
    screen_cmd.add_argument("--candidate-rev", required=True)
    screen_cmd.add_argument("--pairs", type=int, default=10)
    screen_cmd.add_argument("--mode", choices=("warm", "cold"), default="warm")
    screen_cmd.add_argument("--seed", type=int, default=0)
    screen_cmd.add_argument("--require-functional", action="store_true")
    for command in (run, baseline, screen_cmd):
        command.add_argument(
            "--expected-microphone",
            metavar="ID",
            help=(
                "microphone candidate id required for readiness; defaults to "
                "boot_expected_microphone from inventory or lark-a1"
            ),
        )
    trial_cmd = commands.add_parser("trial")
    trial_cmd.add_argument("action", choices=("arm", "confirm", "rollback", "status"))
    trial_cmd.add_argument("--transaction")
    return result


def main() -> int:
    args = parser().parse_args()
    config = Config.load(args.inventory, args.artifacts)
    config.artifacts.mkdir(parents=True, exist_ok=True)
    if args.command == "doctor":
        return doctor(config)
    if args.command == "run":
        expected_microphone = args.expected_microphone or config.expected_microphone
        path = run_boot(
            config,
            mode=args.mode,
            candidate=args.candidate,
            require_functional=args.require_functional,
            expected_microphone=expected_microphone,
            manual_power=args.manual_power,
            readiness_profile=args.readiness_profile,
        )
        result = json.loads((path / "result.json").read_text(encoding="utf-8"))
        return 0 if result["verdict"] == "PASS" else 1
    if args.command == "baseline":
        expected_microphone = args.expected_microphone or config.expected_microphone
        failures = 0
        for _ in range(args.count):
            path = run_boot(
                config,
                mode=args.mode,
                candidate=args.candidate,
                require_functional=args.require_functional,
                expected_microphone=expected_microphone,
                manual_power=args.manual_power,
                readiness_profile=args.readiness_profile,
            )
            verdict = json.loads((path / "result.json").read_text(encoding="utf-8"))[
                "verdict"
            ]
            failures += verdict != "PASS"
            time.sleep(3)
        return 1 if failures else 0
    if args.command == "compare":
        return compare(
            config,
            args.baseline,
            args.candidate,
            args.allow_idle,
            args.allow_phone,
            args.mode,
        )
    if args.command == "screen":
        return screen(
            config,
            baseline_label=args.baseline,
            baseline_revision=args.baseline_rev,
            candidate_label=args.candidate,
            candidate_revision=args.candidate_rev,
            pairs=args.pairs,
            mode=args.mode,
            require_functional=args.require_functional,
            seed=args.seed,
            expected_microphone=(
                args.expected_microphone or config.expected_microphone
            ),
        )
    if args.command == "trial":
        return trial(config, args.action, args.transaction)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
