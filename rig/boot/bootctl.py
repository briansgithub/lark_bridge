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
SYSTEM_UNITS = (
    "bluetooth.service",
    "bridge-btfw.service",
    "bridge-btwatchdog.service",
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

bt_list = run(["bluetoothctl", "list"])
bt_show = run(["bluetoothctl", "show"])
failed_units = run(["systemctl", "--failed", "--no-legend", "--plain"])
system = unit_states("system", __SYSTEM_UNITS__)
user = unit_states("user", __USER_UNITS__)
failures = []
failures += [f"{name}={state or 'unknown'}" for name, state in system.items() if state != "active"]
failures += [f"user:{name}={state or 'unknown'}" for name, state in user.items() if state != "active"]
if not bt_list["stdout"] or "Powered: yes" not in bt_show["stdout"]:
    failures.append("Bluetooth adapter is not registered and powered")
if failed_units["stdout"]:
    failures.append("failed systemd units are present")
if bridge.get("error"):
    failures.append("bridge status is unavailable")
elif bridge.get("state") == "DEGRADED" or bridge.get("last_failure"):
    failures.append(f"bridge unhealthy: {bridge.get('last_failure') or bridge.get('state')}")
elif not (bridge.get("endpoints") or {}).get("lark"):
    failures.append("Lark endpoint is absent")
elif not (bridge.get("endpoints") or {}).get("wired_output"):
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

print(json.dumps({
    "boot_id": pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
    "uptime_s": float(pathlib.Path("/proc/uptime").read_text().split()[0]),
    "system_units": system,
    "user_units": user,
    "bluetooth": {"list": bt_list, "show": bt_show},
    "failed_units": failed_units,
    "bridge": bridge,
    "power": power,
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
    "/etc/systemd/system/bridge-btfw.service",
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
        return cls(
            inventory=path,
            pi_host=host,
            probe_host=str(data.get("boot_probe_host") or data.get("pi_ip") or host),
            pi_user=str(data.get("pi_user", "admin")),
            ssh_timeout_s=int(data.get("boot_ssh_timeout_seconds", 8)),
            boot_timeout_s=int(data.get("boot_timeout_seconds", 120)),
            shutdown_timeout_s=int(data.get("boot_shutdown_timeout_seconds", 30)),
            cold_off_seconds=float(data.get("boot_cold_off_seconds", 10)),
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

    def probe(self) -> dict[str, Any]:
        script = REMOTE_PROBE.replace("__SYSTEM_UNITS__", repr(SYSTEM_UNITS)).replace(
            "__USER_UNITS__", repr(USER_UNITS)
        )
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
    config: Config, *, mode: str, candidate: str, require_functional: bool
) -> Path:
    run_id = f"{utc_stamp()}-{candidate}-{mode}"
    directory = config.artifacts / f"boot-run-{run_id}"
    directory.mkdir(parents=True, exist_ok=False)
    recorder = Recorder(directory)
    ssh = Ssh(config)
    meta = git_metadata()
    remote_manifest = ssh.manifest()
    write_json(
        directory / "manifest.json",
        {
            "run_id": run_id,
            "candidate": candidate,
            "mode": mode,
            "git": meta,
            "remote": remote_manifest,
        },
    )
    result: dict[str, Any] = {
        "run_id": run_id,
        "candidate": candidate,
        "mode": mode,
        "verdict": "FAIL",
        "readiness_level": "none",
        "timings_s": {},
        "git": meta,
    }
    serial: subprocess.Popen | None = None
    try:
        if meta["tracked_status"]:
            raise RuntimeError(
                "tracked worktree changes exist; commit or restore them before timing"
            )
        before = ssh.probe()
        write_json(directory / "preboot.json", before)
        old_boot_id = before["boot_id"]

        if mode == "cold":
            recorder.event("power_off_requested")
            hook = run_hook(
                config.power_off, timeout=30, run_dir=str(directory), run_id=run_id
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
            hook = run_hook(
                config.power_on, timeout=30, run_dir=str(directory), run_id=run_id
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
                probe = ssh.probe()
                if probe.get("boot_id") == old_boot_id:
                    last_error = "SSH answered from the previous boot"
                else:
                    if not ssh_seen:
                        result["timings_s"]["new_boot_ssh"] = recorder.event(
                            "new_boot_ssh"
                        )
                        ssh_seen = True
                    if probe.get("ready"):
                        ready = probe
                        break
                    last_error = "; ".join(probe.get("failures", []))
            except (RuntimeError, subprocess.TimeoutExpired) as exc:
                last_error = str(exc)
            time.sleep(1)
        if ready is None:
            raise RuntimeError(f"idle readiness timed out: {last_error}")

        write_json(directory / "ready.json", ready)
        result["timings_s"]["idle_ready"] = recorder.event("idle_ready")
        result["readiness_level"] = "idle"

        if config.functional_probe:
            watermark = "LB-" + hashlib.sha256(run_id.encode()).hexdigest()[:16]
            hook = run_hook(
                config.functional_probe,
                timeout=config.boot_timeout_s,
                run_dir=str(directory),
                run_id=run_id,
                candidate=candidate,
                watermark=watermark,
            )
            if hook.returncode != 0:
                raise RuntimeError(f"functional probe exited {hook.returncode}")
            functional = validate_functional_result(
                directory / "functional-result.json", run_id, watermark
            )
            write_json(directory / "functional-result.validated.json", functional)
            functional_probe = ssh.probe()
            write_json(directory / "functional-ready.json", functional_probe)
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
        if "functional_ready" in timings:
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
    if not allow_idle and (base_level != "functional" or cand_level != "functional"):
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
    accepted = (
        candidate_failures == 0
        and not health_regressions
        and effect >= minimum
        and ci[0] > 0
        and p95_candidate <= p95_base + 0.5
        and cand_level == "functional"
    )
    report = {
        "verdict": "PROVISIONAL_ACCEPT" if accepted else "REJECT",
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
        "note": "A provisional acceptance still requires the robustness and soak gates.",
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
) -> int:
    if pairs < 10:
        raise ValueError("candidate screening requires at least ten randomized pairs")
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
                )
                result = json.loads((path / "result.json").read_text(encoding="utf-8"))
                failures += result["verdict"] != "PASS"
                time.sleep(3)
    finally:
        apply_variant(config, label=baseline_label, revision=baseline_revision)
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
    baseline = commands.add_parser("baseline")
    baseline.add_argument("--mode", choices=("warm", "cold"), default="warm")
    baseline.add_argument("--candidate", default="baseline")
    baseline.add_argument("--count", type=int, default=10)
    baseline.add_argument("--require-functional", action="store_true")
    compare_cmd = commands.add_parser("compare")
    compare_cmd.add_argument("--baseline", required=True)
    compare_cmd.add_argument("--candidate", required=True)
    compare_cmd.add_argument("--allow-idle", action="store_true")
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
        path = run_boot(
            config,
            mode=args.mode,
            candidate=args.candidate,
            require_functional=args.require_functional,
        )
        result = json.loads((path / "result.json").read_text(encoding="utf-8"))
        return 0 if result["verdict"] == "PASS" else 1
    if args.command == "baseline":
        failures = 0
        for _ in range(args.count):
            path = run_boot(
                config,
                mode=args.mode,
                candidate=args.candidate,
                require_functional=args.require_functional,
            )
            verdict = json.loads((path / "result.json").read_text(encoding="utf-8"))[
                "verdict"
            ]
            failures += verdict != "PASS"
            time.sleep(3)
        return 1 if failures else 0
    if args.command == "compare":
        return compare(
            config, args.baseline, args.candidate, args.allow_idle, args.mode
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
        )
    if args.command == "trial":
        return trial(config, args.action, args.transaction)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
