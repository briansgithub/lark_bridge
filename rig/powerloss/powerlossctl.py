#!/usr/bin/env python3
"""Manual, evidence-preserving abrupt-power-loss campaign controller."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import shlex
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import tomllib

REPO = Path(__file__).resolve().parents[2]
DEFAULT_INVENTORY = REPO / "rig/inventory.toml"
DEFAULT_ARTIFACTS = REPO / "artifacts/powerloss"
SAFETY_TOOL = REPO / "scripts/powerloss/safety-evidence.py"
REMOTE_VERIFY = "/usr/local/lib/rpi-lark-bridge/powerloss/powerloss_verify.py"
EARLY_DELAYS = (1, 3, 5, 10, 20)
CONTEXTS = (
    "idle",
    "bluetooth-recovery",
    "pipewire-restart",
    "supervisor-construction",
    "active-call-aec",
    "call-teardown",
    "persistent-write",
)
REQUIREMENTS = {
    **{f"early-boot-{delay}": 1 for delay in EARLY_DELAYS},
    **{context: 5 for context in CONTEXTS},
    "random": 10,
}
PIXEL_CHAOS_PROFILE = "pixel-chaos"
PIXEL_CHAOS_OFF_SECONDS = 12.0
PHONE_CONNECT_LIMIT_SECONDS = 25.0


def stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def pixel_chaos_schedule(seed: int) -> list[dict[str, Any]]:
    """Build a compact, reproducible set of high-risk car-power cuts."""
    rng = random.Random(seed)
    early_delays = (1, rng.randint(3, 7), rng.randint(12, 18))
    schedule = [
        {
            "case": f"early-boot-{delay}",
            "state": f"early-boot-{delay}",
            "random_context": None,
            "seed": None,
        }
        for delay in early_delays
    ]
    for context in ("bluetooth-recovery", "persistent-write"):
        case_seed = rng.randrange(1, 2**31)
        schedule.append(
            {
                "case": f"random-{context}",
                "state": "random",
                "random_context": context,
                "seed": case_seed,
            }
        )
    for index, case in enumerate(schedule, start=1):
        case["index"] = index
    return schedule


def schedule_requirements(schedule: list[dict[str, Any]]) -> dict[str, int]:
    requirements: dict[str, int] = {}
    for case in schedule:
        category = str(case["state"])
        requirements[category] = requirements.get(category, 0) + 1
    return requirements


def write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{time.time_ns()}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
    temporary.replace(path)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ControllerError(RuntimeError):
    pass


class Controller:
    def __init__(self, inventory: Path):
        with inventory.open("rb") as handle:
            data = tomllib.load(handle)
        self.host = str(data.get("pi_host", "larkbridge"))
        self.probe_host = str(
            data.get("boot_probe_host") or data.get("pi_ip") or self.host
        )
        self.ssh_timeout = int(data.get("boot_ssh_timeout_seconds", 8))
        self.boot_timeout = int(data.get("boot_timeout_seconds", 120))
        self.off_seconds = max(10.0, float(data.get("boot_cold_off_seconds", 10)))
        functional = data.get("boot_functional_probe_command", [])
        if not isinstance(functional, list) or not all(
            isinstance(part, str) for part in functional
        ):
            raise ControllerError(
                "boot_functional_probe_command must be a string array"
            )
        self.functional_probe = tuple(functional)

    def ssh(
        self, command: list[str], *, timeout: int = 30
    ) -> subprocess.CompletedProcess[str]:
        remote = shlex.join(command)
        return subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                f"ConnectTimeout={self.ssh_timeout}",
                self.host,
                remote,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def port_open(self) -> bool:
        try:
            with socket.create_connection((self.probe_host, 22), timeout=1):
                return True
        except OSError:
            return False

    def wait_port(self, present: bool, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.port_open() is present:
                return True
            time.sleep(0.5 if not present else 1.0)
        return False

    def verify(self) -> dict[str, Any]:
        result = self.ssh(["sudo", "-n", "python3", REMOTE_VERIFY], timeout=90)
        try:
            document = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise ControllerError(
                f"remote verifier did not return JSON: {result.stderr or result.stdout}"
            ) from error
        document["remote_rc"] = result.returncode
        return document

    def run_functional_probe(self, run_path: Path, context: str) -> dict[str, Any]:
        if not self.functional_probe:
            return {"available": False, "required": False}
        command = [
            part.replace("{run_dir}", str(run_path)).replace("{state}", context)
            for part in self.functional_probe
        ]
        try:
            result = subprocess.run(
                command,
                cwd=REPO,
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return {
                "available": True,
                "command": command,
                "rc": 127,
                "required": True,
                "stderr": str(error),
                "stdout": "",
            }
        return {
            "available": True,
            "command": command,
            "rc": result.returncode,
            "required": True,
            "stderr": result.stderr,
            "stdout": result.stdout,
        }


def load_campaign(path: Path) -> dict[str, Any]:
    campaign = read_json(path / "campaign.json")
    evidence = Path(campaign["safety_evidence"])
    if (
        not evidence.is_file()
        or file_sha256(evidence) != campaign["safety_evidence_sha256"]
    ):
        raise ControllerError("safety evidence is missing or changed")
    return campaign


def run_files(campaign_path: Path) -> list[Path]:
    return sorted((campaign_path / "runs").glob("*/run.json"))


def incomplete_run(campaign_path: Path) -> tuple[Path, dict[str, Any]] | None:
    for path in run_files(campaign_path):
        document = read_json(path)
        if document.get("phase") not in {"PASSED", "FAILED"}:
            return path.parent, document
    return None


def active_run(campaign_path: Path) -> tuple[Path, dict[str, Any]]:
    active = incomplete_run(campaign_path)
    if not active:
        raise ControllerError("campaign has no active run")
    return active


def campaign_init(arguments: argparse.Namespace) -> None:
    evidence = arguments.safety_evidence.resolve(strict=True)
    check = subprocess.run(
        [sys.executable, str(SAFETY_TOOL), "verify", "--evidence", str(evidence)],
        capture_output=True,
        text=True,
        check=False,
    )
    if check.returncode:
        raise ControllerError(
            check.stderr.strip() or "safety evidence failed verification"
        )
    evidence_result = json.loads(check.stdout)
    campaign_path = arguments.output or DEFAULT_ARTIFACTS / f"campaign-{stamp()}"
    if campaign_path.exists():
        raise ControllerError(f"campaign directory already exists: {campaign_path}")
    campaign_path.mkdir(parents=True)
    (campaign_path / "runs").mkdir()
    profile = getattr(arguments, "profile", "full")
    if profile == PIXEL_CHAOS_PROFILE:
        schedule = pixel_chaos_schedule(arguments.seed)
        requirements = schedule_requirements(schedule)
        minimum_off_seconds = PIXEL_CHAOS_OFF_SECONDS
        require_phone = True
    else:
        schedule = []
        requirements = REQUIREMENTS
        minimum_off_seconds = 10
        require_phone = False
    write_json(
        campaign_path / "campaign.json",
        {
            "backup_sha256": evidence_result["backup_sha256"],
            "created": stamp(),
            "minimum_off_seconds": minimum_off_seconds,
            "phone_connect_limit_seconds": PHONE_CONNECT_LIMIT_SECONDS,
            "profile": profile,
            "require_phone": require_phone,
            "requirements": requirements,
            "schedule": schedule,
            "seed": arguments.seed if schedule else None,
            "safety_evidence": str(evidence),
            "safety_evidence_sha256": file_sha256(evidence),
            "schema": 2,
        },
    )
    print(campaign_path.resolve())


def validate_context(
    document: dict[str, Any], context: str, acknowledged: bool
) -> None:
    bridge = (document.get("details") or {}).get("bridge") or {}
    state = bridge.get("state")
    owner = (bridge.get("aec") or {}).get("owner_pid")
    if context == "idle" and state != "CALL_DOWN":
        raise ControllerError(f"idle cut requires CALL_DOWN, found {state}")
    if context == "active-call-aec" and (state == "CALL_DOWN" or not owner):
        raise ControllerError("active-call cut requires a live call and an AEC owner")
    if context == "call-teardown" and (state != "CALL_DOWN" or not acknowledged):
        raise ControllerError("call-teardown requires CALL_DOWN and --ack-state-ready")


def trigger_context(controller: Controller, context: str) -> None:
    commands = {
        "bluetooth-recovery": [
            "sudo",
            "-n",
            "systemd-run",
            "--no-block",
            "--collect",
            "--unit=larkbridge-cut-bluetooth",
            "systemctl",
            "restart",
            "bluetooth.service",
        ],
        "pipewire-restart": [
            "systemd-run",
            "--user",
            "--no-block",
            "--collect",
            "--unit=larkbridge-cut-pipewire",
            "systemctl",
            "--user",
            "restart",
            "pipewire.service",
            "wireplumber.service",
        ],
        "supervisor-construction": [
            "systemd-run",
            "--user",
            "--no-block",
            "--collect",
            "--unit=larkbridge-cut-supervisor",
            "systemctl",
            "--user",
            "restart",
            "bridge-supervisor.service",
        ],
        "persistent-write": [
            "sudo",
            "-n",
            "systemd-run",
            "--no-block",
            "--collect",
            "--unit=larkbridge-cut-persistent-write",
            "python3",
            "/usr/local/lib/rpi-lark-bridge/powerloss/cut_activity.py",
            "--duration=120",
        ],
    }
    if context not in commands:
        return
    result = controller.ssh(commands[context])
    if result.returncode:
        raise ControllerError(f"could not enter {context}: {result.stderr.strip()}")


def arm(arguments: argparse.Namespace) -> None:
    campaign_path = arguments.campaign.resolve()
    campaign = load_campaign(campaign_path)
    if incomplete_run(campaign_path):
        raise ControllerError("finish the active run before arming another")
    controller = Controller(arguments.inventory)
    if not controller.port_open():
        raise ControllerError("Pi SSH is unavailable")
    pre = controller.verify()
    if not pre.get("ready"):
        raise ControllerError(f"pre-cut acceptance failed: {pre.get('failures')}")

    category = arguments.state
    context = category
    early_delay: int | None = None
    if category == "random":
        if not arguments.random_context:
            raise ControllerError("random cuts require --random-context")
        context = arguments.random_context
        rng = random.Random(arguments.seed)
        randomized_delay = round(rng.uniform(0.25, 8.0), 3)
    else:
        randomized_delay = None
    if category.startswith("early-boot-"):
        early_delay = int(category.rsplit("-", 1)[1])
        context = category
    elif category not in CONTEXTS and category != "random":
        raise ControllerError(f"unknown cut state: {category}")
    if context in CONTEXTS:
        validate_context(pre, context, arguments.ack_state_ready)

    run_id = f"{len(run_files(campaign_path)) + 1:03d}-{category}-{stamp()}"
    run_path = campaign_path / "runs" / run_id
    run_path.mkdir()
    write_json(run_path / "pre-cut.json", pre)
    document: dict[str, Any] = {
        "boot_id_before": pre["boot_id"],
        "category": category,
        "context": context,
        "created": stamp(),
        "early_delay_seconds": early_delay,
        "minimum_off_seconds": max(
            10.0, float(campaign["minimum_off_seconds"]), controller.off_seconds
        ),
        "phase": "PREP_OFF" if early_delay else "ARMED",
        "randomized_delay_seconds": randomized_delay,
        "run_id": run_id,
        "schedule_index": getattr(arguments, "schedule_index", None),
        "schema": 1,
        "seed": arguments.seed if category == "random" else None,
    }
    write_json(run_path / "run.json", document)

    if early_delay:
        shutdown = controller.ssh(["sudo", "-n", "systemctl", "poweroff"], timeout=15)
        if shutdown.returncode not in {0, 255} or not controller.wait_port(False, 60):
            document["phase"] = "FAILED"
            document["failure"] = "preparatory graceful shutdown did not complete"
            write_json(run_path / "run.json", document)
            raise ControllerError(document["failure"])
        document["preparatory_off_unix_ns"] = time.time_ns()
        write_json(run_path / "run.json", document)
        print(
            "Preparatory shutdown complete. Disconnect power and leave it off for 10 seconds."
        )
        print(
            f"Then run: rig powerloss early-start --campaign {shlex.quote(str(campaign_path))}"
        )
        return

    try:
        trigger_context(controller, context)
        if randomized_delay:
            time.sleep(randomized_delay)
    except (ControllerError, KeyboardInterrupt) as error:
        document["phase"] = "FAILED"
        document["failure"] = f"arming failed or was interrupted: {error}"
        write_json(run_path / "run.json", document)
        if isinstance(error, KeyboardInterrupt):
            raise ControllerError(document["failure"]) from error
        raise
    print("DISCONNECT THE PI POWER NOW, then run the observe-off command.")
    print(f"rig powerloss observe-off --campaign {shlex.quote(str(campaign_path))}")


def early_start(arguments: argparse.Namespace) -> None:
    campaign_path = arguments.campaign.resolve()
    load_campaign(campaign_path)
    run_path, document = active_run(campaign_path)
    if document["phase"] != "PREP_OFF":
        raise ControllerError("active run is not waiting for an early-boot start")
    elapsed = (time.time_ns() - document["preparatory_off_unix_ns"]) / 1e9
    if elapsed < document["minimum_off_seconds"]:
        raise ControllerError(
            f"leave power disconnected for {document['minimum_off_seconds'] - elapsed:.1f}s more"
        )
    if not arguments.ack_power_connected_now:
        raise ControllerError(
            "connect power and pass --ack-power-connected-now at that instant"
        )
    delay = float(document["early_delay_seconds"])
    document["power_connected_unix_ns"] = time.time_ns()
    document["phase"] = "COUNTDOWN"
    write_json(run_path / "run.json", document)
    print(f"Power-on recorded. Cut power in {delay:g} seconds...", flush=True)
    time.sleep(delay)
    document["phase"] = "ARMED"
    document["cut_prompt_unix_ns"] = time.time_ns()
    write_json(run_path / "run.json", document)
    print("\aDISCONNECT THE PI POWER NOW.", flush=True)
    print(
        f"rig powerloss observe-off --campaign {shlex.quote(str(campaign_path))} --acknowledge-early-cut"
    )


def observe_off(arguments: argparse.Namespace) -> None:
    campaign_path = arguments.campaign.resolve()
    load_campaign(campaign_path)
    run_path, document = active_run(campaign_path)
    if document["phase"] != "ARMED":
        raise ControllerError(f"active run is in phase {document['phase']}, not ARMED")
    controller = Controller(arguments.inventory)
    early = document.get("early_delay_seconds") is not None
    if early:
        if not arguments.acknowledge_early_cut:
            raise ControllerError(
                "early cuts require --acknowledge-early-cut after unplugging"
            )
    elif not controller.wait_port(False, 45):
        raise ControllerError("SSH did not disappear; power loss was not observed")
    document["off_detected_unix_ns"] = time.time_ns()
    document["phase"] = "OFF_HOLD"
    write_json(run_path / "run.json", document)
    print("Power-off recorded. Keep the Pi disconnected for at least 10 seconds.")
    print(
        f"Then run: rig powerloss reconnect --campaign {shlex.quote(str(campaign_path))}"
    )


def phone_recovery_acceptance(
    document: dict[str, Any], limit_seconds: float
) -> dict[str, Any]:
    """Require the configured Pixel to reconnect quickly after the recovery boot."""
    details = document.get("details") or {}
    watchdog = details.get("call_watchdog") or {}
    bridge = details.get("bridge") or {}
    phone = bridge.get("phone") or {}
    connected_at = watchdog.get("connected_monotonic")
    failures: list[str] = []
    if watchdog.get("bond_state") != "connected":
        failures.append("configured Pixel bond is not connected")
    if watchdog.get("repair_state") != "idle":
        failures.append("Pixel repair transaction is not idle")
    if phone.get("connected") is not True:
        failures.append("bridge does not report the Pixel connected")
    if not isinstance(connected_at, (int, float)):
        failures.append("Pixel connection time is unavailable")
    elif float(connected_at) > limit_seconds:
        failures.append(
            f"Pixel connected at {float(connected_at):.3f}s, exceeding "
            f"{limit_seconds:.0f}s"
        )
    return {
        "connected_monotonic": connected_at,
        "failures": failures,
        "limit_seconds": limit_seconds,
        "ready": not failures,
    }


def reconnect(arguments: argparse.Namespace) -> None:
    campaign_path = arguments.campaign.resolve()
    campaign = load_campaign(campaign_path)
    run_path, document = active_run(campaign_path)
    if document["phase"] != "OFF_HOLD":
        raise ControllerError("active run is not in the enforced power-off hold")
    elapsed = (time.time_ns() - document["off_detected_unix_ns"]) / 1e9
    remaining = document["minimum_off_seconds"] - elapsed
    if remaining > 0:
        print(f"Holding power off for {remaining:.1f}s more...", flush=True)
        time.sleep(remaining)
    document["reconnect_prompt_unix_ns"] = time.time_ns()
    document["off_seconds"] = round(
        (document["reconnect_prompt_unix_ns"] - document["off_detected_unix_ns"]) / 1e9,
        3,
    )
    write_json(run_path / "run.json", document)
    print(
        "RECONNECT THE PI POWER NOW. Waiting for SSH and full validation...", flush=True
    )
    controller = Controller(arguments.inventory)
    require_phone = bool(
        campaign.get("require_phone") or getattr(arguments, "require_phone", False)
    )
    phone_limit = float(
        campaign.get("phone_connect_limit_seconds")
        or getattr(arguments, "phone_connect_limit", PHONE_CONNECT_LIMIT_SECONDS)
    )
    if not controller.wait_port(True, controller.boot_timeout):
        document["phase"] = "FAILED"
        document["failure"] = "SSH did not return before the boot timeout"
        write_json(run_path / "run.json", document)
        raise ControllerError(document["failure"])
    deadline = time.monotonic() + controller.boot_timeout
    attempts: list[dict[str, Any]] = []
    post: dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            post = controller.verify()
        except ControllerError as error:
            post = {"failures": [str(error)], "ready": False}
        if require_phone and post.get("ready"):
            post["phone_acceptance"] = phone_recovery_acceptance(post, phone_limit)
        attempts.append(post)
        if post.get("ready") and (
            not require_phone or post["phone_acceptance"]["ready"]
        ):
            break
        if (
            require_phone
            and post.get("ready")
            and float(post.get("uptime_s") or 0) >= phone_limit
        ):
            break
        time.sleep(5)
    write_json(run_path / "post-cut-attempts.json", {"attempts": attempts})
    write_json(run_path / "post-cut.json", post)
    if post.get("boot_id") == document["boot_id_before"]:
        post.setdefault("failures", []).append("boot ID did not change")
        post["ready"] = False
    pre = read_json(run_path / "pre-cut.json")
    pre_details = pre.get("details") or {}
    post_details = post.get("details") or {}
    if pre_details.get("ssh_identity_sha256") != post_details.get(
        "ssh_identity_sha256"
    ):
        post.setdefault("failures", []).append("SSH host identity changed")
        post["ready"] = False
    pre_config = (pre_details.get("config") or {}).get("sha256")
    post_config = (post_details.get("config") or {}).get("sha256")
    if pre_config and pre_config != post_config:
        post.setdefault("failures", []).append("configuration changed across the cut")
        post["ready"] = False
    if require_phone:
        phone_acceptance = phone_recovery_acceptance(post, phone_limit)
        post["phone_acceptance"] = phone_acceptance
        if not phone_acceptance["ready"]:
            post.setdefault("failures", []).extend(phone_acceptance["failures"])
            post["ready"] = False
    pre_pairing = pre_details.get("pairing_identity") or {}
    post_pairing = post_details.get("pairing_identity") or {}
    if any(post_pairing.get(path) != digest for path, digest in pre_pairing.items()):
        post.setdefault("failures", []).append("pairing identity was lost or changed")
        post["ready"] = False
    functional = controller.run_functional_probe(run_path, document["context"])
    write_json(run_path / "functional-probe.json", functional)
    if functional.get("required") and functional.get("rc") != 0:
        post.setdefault("failures", []).append(
            "configured functional audio probe failed"
        )
        post["ready"] = False
    write_json(run_path / "post-cut.json", post)
    if not post.get("ready"):
        document["phase"] = "FAILED"
        document["failure"] = post.get("failures")
        write_json(run_path / "run.json", document)
        raise ControllerError(f"post-cut acceptance failed: {post.get('failures')}")
    document["boot_id_after"] = post["boot_id"]
    document["completed"] = stamp()
    document["phase"] = "PASSED"
    write_json(run_path / "run.json", document)
    print(f"PASS {document['run_id']} — automatic recovery validated")


def status(arguments: argparse.Namespace) -> None:
    campaign_path = arguments.campaign.resolve()
    campaign = load_campaign(campaign_path)
    counts = {category: 0 for category in campaign["requirements"]}
    failed: list[str] = []
    active: list[str] = []
    for path in run_files(campaign_path):
        document = read_json(path)
        if document["phase"] == "PASSED":
            category = document["category"]
            if category in counts:
                counts[category] += 1
        elif document["phase"] == "FAILED":
            failed.append(document["run_id"])
        else:
            active.append(document["run_id"])
    remaining = {
        category: max(0, required - counts[category])
        for category, required in campaign["requirements"].items()
    }
    print(
        json.dumps(
            {
                "active": active,
                "complete": not any(remaining.values()) and not failed and not active,
                "counts": counts,
                "failed": failed,
                "remaining": remaining,
                "total_passed": sum(counts.values()),
            },
            indent=2,
            sort_keys=True,
        )
    )


def arm_next(arguments: argparse.Namespace) -> None:
    campaign_path = arguments.campaign.resolve()
    campaign = load_campaign(campaign_path)
    schedule = campaign.get("schedule") or []
    if not schedule:
        raise ControllerError("campaign has no ordered schedule")
    if incomplete_run(campaign_path):
        raise ControllerError("finish the active run before arming another")
    completed: set[int] = set()
    for path in run_files(campaign_path):
        document = read_json(path)
        if document.get("phase") == "FAILED":
            raise ControllerError(
                f"campaign contains failed run {document.get('run_id')}"
            )
        index = document.get("schedule_index")
        if isinstance(index, int):
            completed.add(index)
    case = next((item for item in schedule if item["index"] not in completed), None)
    if case is None:
        print("Campaign schedule is complete.")
        return
    print(
        f"Arming chaos case {case['index']}/{len(schedule)}: {case['case']}",
        flush=True,
    )
    arm(
        argparse.Namespace(
            ack_state_ready=False,
            campaign=campaign_path,
            inventory=arguments.inventory,
            random_context=case.get("random_context"),
            schedule_index=case["index"],
            seed=case.get("seed") or campaign.get("seed") or 0,
            state=case["state"],
        )
    )


def abort(arguments: argparse.Namespace) -> None:
    campaign_path = arguments.campaign.resolve()
    load_campaign(campaign_path)
    run_path, document = active_run(campaign_path)
    document["phase"] = "FAILED"
    document["failure"] = f"operator aborted: {arguments.reason}"
    document["completed"] = stamp()
    write_json(run_path / "run.json", document)
    print(
        f"FAILED {document['run_id']} — start a new campaign after resolving the cause"
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    commands = result.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("campaign-init")
    initialize.add_argument("--safety-evidence", type=Path, required=True)
    initialize.add_argument("--output", type=Path)
    initialize.add_argument(
        "--profile", choices=("full", PIXEL_CHAOS_PROFILE), default="full"
    )
    initialize.add_argument("--seed", type=int, default=20260901)
    initialize.set_defaults(function=campaign_init)
    arm_command = commands.add_parser("arm")
    arm_command.add_argument("--campaign", type=Path, required=True)
    arm_command.add_argument("--state", required=True)
    arm_command.add_argument("--random-context", choices=CONTEXTS)
    arm_command.add_argument("--seed", type=int, default=20260819)
    arm_command.add_argument("--ack-state-ready", action="store_true")
    arm_command.set_defaults(function=arm)
    arm_next_command = commands.add_parser("arm-next")
    arm_next_command.add_argument("--campaign", type=Path, required=True)
    arm_next_command.set_defaults(function=arm_next)
    early = commands.add_parser("early-start")
    early.add_argument("--campaign", type=Path, required=True)
    early.add_argument("--ack-power-connected-now", action="store_true")
    early.set_defaults(function=early_start)
    off = commands.add_parser("observe-off")
    off.add_argument("--campaign", type=Path, required=True)
    off.add_argument("--acknowledge-early-cut", action="store_true")
    off.set_defaults(function=observe_off)
    reconnect_command = commands.add_parser("reconnect")
    reconnect_command.add_argument("--campaign", type=Path, required=True)
    reconnect_command.add_argument("--require-phone", action="store_true")
    reconnect_command.add_argument(
        "--phone-connect-limit",
        type=float,
        default=PHONE_CONNECT_LIMIT_SECONDS,
    )
    reconnect_command.set_defaults(function=reconnect)
    status_command = commands.add_parser("status")
    status_command.add_argument("--campaign", type=Path, required=True)
    status_command.set_defaults(function=status)
    abort_command = commands.add_parser("abort")
    abort_command.add_argument("--campaign", type=Path, required=True)
    abort_command.add_argument("--reason", required=True)
    abort_command.set_defaults(function=abort)
    return result


def main() -> int:
    arguments = parser().parse_args()
    try:
        arguments.function(arguments)
    except (ControllerError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
