#!/usr/bin/env python3
"""Inject one named fault and record what the bridge does about it, as a timeline.

A single before/after pair cannot tell "recovered in 4 s" from "recovered in 90 s" from
"never recovered", and those are three completely different products. So every fault is
followed by repeated invariant sampling until the system is healthy again or the observation
window expires, and the report carries the whole trace.

    faultctl.py --list
    faultctl.py --fault kill-aec --observe 60
    faultctl.py --fault none --observe 20        # control: prove the harness is quiet

Runs on the Pi, strict stdlib, and imports invariants.py rather than shelling out to it.

WHY NO AUTOMATIC RESTORE BETWEEN FAULTS: self-recovery is the property under test. Forcing
the system back to a known-good baseline after each fault would hide exactly the defects
this campaign exists to find. Faults that take something away put it back (that is part of
the fault, not a cleanup step); nothing else is tidied. If a fault wedges the unit, that is
the finding, and it is reported rather than papered over.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import subprocess
import sys
import time
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKOFF_STEPS = (2, 4, 8, 16, 30)  # min(2**n, 30) from the supervisor
MAX_BUILD_ATTEMPTS = 5


def load_invariants():
    spec = importlib.util.spec_from_file_location("e13_invariants", HERE / "invariants.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load invariants.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


INV = load_invariants()


def sh(cmd: str, timeout: float = 30.0) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=timeout, check=False)
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.SubprocessError as exc:
        return -1, str(exc)


def _read_sysfs(path: Path, name: str) -> str | None:
    try:
        value = (path / name).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _selected_microphone(status: dict, expected_id: str) -> dict:
    authoritative = "microphone" in status
    microphone = status.get("microphone") or {}
    selected = microphone.get("selected") if isinstance(microphone, dict) else None
    if not isinstance(selected, dict) and not authoritative:
        legacy = (status.get("endpoints") or {}).get("lark")
        if legacy and expected_id == "lark-a1":
            selected = {
                "id": "lark-a1",
                "node": legacy,
                "identity": {
                    "usb_vendor_id": "3547",
                    "usb_product_id": "0407",
                    "usb_product": "Wireless Microphone",
                },
                "legacy": True,
            }
    if not isinstance(selected, dict):
        raise RuntimeError("bridge status has no selected microphone")  # noqa: TRY004
    if selected.get("id") != expected_id:
        raise RuntimeError(
            f"selected microphone is {selected.get('id')!r}, expected {expected_id!r}"
        )
    return selected


def microphone_usb_path(
    expected_id: str,
    *,
    status: dict | None = None,
    sysfs_root: Path = Path("/sys/bus/usb/devices"),
) -> Path:
    """Resolve exactly one selected USB instance and verify its reported fingerprint."""
    if status is None:
        status, error = INV.read_status()
        if error:
            raise RuntimeError(f"bridge status unavailable: {error}")
    selected = _selected_microphone(status, expected_id)
    identity = selected.get("identity") or {}
    if not isinstance(identity, dict):
        raise RuntimeError("selected microphone identity is malformed")  # noqa: TRY004

    vendor = str(identity.get("usb_vendor_id") or "").lower().removeprefix("0x").zfill(4)
    product_id = (
        str(identity.get("usb_product_id") or "").lower().removeprefix("0x").zfill(4)
    )
    if len(vendor) != 4 or len(product_id) != 4:
        raise RuntimeError("selected microphone status has no USB VID:PID")

    def matches(path: Path) -> bool:
        if (_read_sysfs(path, "idVendor") or "").lower() != vendor:
            return False
        if (_read_sysfs(path, "idProduct") or "").lower() != product_id:
            return False
        for key, filename in (("usb_product", "product"), ("usb_serial", "serial")):
            expected = identity.get(key)
            if expected and _read_sysfs(path, filename) != str(expected):
                return False
        return True

    port = str(identity.get("usb_port_path") or "").strip()
    if port:
        pinned = sysfs_root / port
        if matches(pinned):
            return pinned
        raise RuntimeError(
            f"selected microphone USB port {port!r} no longer matches its fingerprint"
        )

    matches_found = [
        path
        for path in sysfs_root.iterdir()
        if path.is_dir() and matches(path)
    ]
    if len(matches_found) != 1:
        raise RuntimeError(
            f"selected microphone fingerprint resolves to {len(matches_found)} USB devices"
        )
    return matches_found[0]


def _set_usb_authorized(path: Path, authorized: int) -> dict:
    try:
        result = subprocess.run(
            ["sudo", "tee", str(path / "authorized")],
            input=f"{authorized}\n",
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "usb_path": str(path),
            "authorized": authorized,
            "rc": -1,
            "out": str(exc),
        }
    return {
        "usb_path": str(path),
        "authorized": authorized,
        "rc": result.returncode,
        "out": (result.stdout + result.stderr).strip(),
    }


def speaker_level(seconds: float = 6.0) -> dict:
    """What is actually coming out of the jack, right now.

    The whole point of the looping far-end source: with a known signal playing, silence
    at the speaker means the bridge is broken. Without one it means nothing at all, which
    is the mistake E12 made. So this reports the level AND whether the far-end signal was
    present upstream, and refuses to call it a failure when the source itself has stopped.
    """
    status, _ = INV.read_status()
    output = (status.get("endpoints") or {}).get("wired_output")
    source = (status.get("endpoints") or {}).get("hfp_source")
    result: dict = {"output_node": output}
    if not output:
        result["skipped"] = "no wired output endpoint"
        return result

    def capture(target: str, name: str, capture_sink: bool) -> float | None:
        path = f"/tmp/e13/{name}.wav"
        props = {"node.name": f"e13.{name}", "node.dont-reconnect": True}
        if capture_sink:
            props["stream.capture.sink"] = True
        sh(f"timeout {seconds + 2} pw-record --target {target} "
           f"--properties '{json.dumps(props, separators=(',', ':'))}' "
           f"--rate 48000 --channels 1 --channel-map mono --format s16 {path}",
           timeout=seconds + 6)
        try:
            with wave.open(path, "rb") as handle:
                frames = handle.readframes(handle.getnframes())
        except (OSError, wave.Error):
            return None
        if len(frames) < 2:
            return None
        import array
        data = array.array("h")
        data.frombytes(frames[: len(frames) // 2 * 2])
        if not data:
            return None
        rms = math.sqrt(sum(v * v for v in data) / len(data)) / 32768.0
        return round(20.0 * math.log10(max(rms, 1e-9)), 2)

    result["speaker_rms_dbfs"] = capture(output, "spk", True)
    if source:
        result["downlink_rms_dbfs"] = capture(source, "dl", False)
    # Only meaningful if the far end is actually sending something.
    down = result.get("downlink_rms_dbfs")
    spk = result.get("speaker_rms_dbfs")
    if down is None or down < -80:
        result["verdict"] = "inconclusive: far-end source silent or unmeasurable"
    elif spk is not None and spk < -80:
        result["verdict"] = "AUDIO DEAD: far end arriving but nothing at the jack"
    else:
        result["verdict"] = "audio flowing"
    return result


def sample() -> dict:
    status, err = INV.read_status()
    violations, observations = INV.check(status, err)
    return {"t": time.time(), "healthy": not violations,
            "violations": violations, "observations": observations}


# ----------------------------------------------------------------------------- faults

def fault_none(_args) -> dict:
    """Control. Injects nothing: any violation seen here is the harness, not the system."""
    return {"action": "none"}


def fault_kill_aec(_args) -> dict:
    """Kill the pw-cli process holding the echo-cancel module."""
    status, _ = INV.read_status()
    pid = (status.get("aec") or {}).get("owner_pid")
    if not pid:
        return {"action": "kill-aec", "skipped": "no AEC owner pid in status"}
    rc, out = sh(f"kill -9 {pid}")
    return {"action": "kill-aec", "pid": pid, "rc": rc, "out": out}


def fault_kill_aec_x5(args) -> dict:
    """Deny the AEC host a successful rebuild until the supervisor gives up.

    Two traps make the naive version useless, both of which this hit on the first run:

    1. The status file keeps reporting the OLD owner_pid for a moment after a kill, so a
       loop that just re-reads it kills an already-dead pid and counts a failure that
       never happened. Wait for a pid that is both DIFFERENT and alive.
    2. tick() sets attempts = 0 on every successful verification. Waiting out the backoff
       between kills lets the graph fully rebuild, which resets the counter -- so a
       patient attacker never reaches FAILED. Failures only accumulate if each new host
       dies BEFORE it verifies.

    So this drives the observed `attempts` counter rather than a kill count: kill each new
    host the instant it appears and stop when the supervisor reports FAILED, attempts has
    reached the cap, or the kill budget runs out.
    """
    events: list[dict] = []
    seen: set[int] = set()
    deadline = time.monotonic() + args.observe
    while len(events) < args.kill_budget and time.monotonic() < deadline:
        status, _ = INV.read_status()
        state = status.get("state")
        attempts = status.get("attempts") or 0
        if state == "FAILED" or attempts >= MAX_BUILD_ATTEMPTS:
            events.append({"note": f"stopping: state={state} attempts={attempts}"})
            break
        pid = (status.get("aec") or {}).get("owner_pid")
        if not pid or pid in seen or not Path(f"/proc/{pid}").exists():
            time.sleep(0.2)
            continue
        seen.add(pid)
        sh(f"kill -9 {pid}")
        gone = not Path(f"/proc/{pid}").exists()
        events.append({"kill": len(events) + 1, "pid": pid, "confirmed_gone": gone,
                       "attempts_before": attempts, "state_before": state})
    status, _ = INV.read_status()
    return {"action": "kill-aec-x5", "events": events,
            "attempts_after": status.get("attempts"), "state_after": status.get("state")}


def fault_kill_loopback(_args) -> dict:
    rc, pids = sh("pgrep -f pw-loopback | head -1")
    pid = pids.strip().splitlines()[0] if pids.strip() else None
    if not pid:
        return {"action": "kill-loopback", "skipped": "no pw-loopback running"}
    rc, out = sh(f"kill -9 {pid}")
    return {"action": "kill-loopback", "pid": pid, "rc": rc, "out": out}


def fault_restart_supervisor(_args) -> dict:
    rc, out = sh("systemctl --user restart bridge-supervisor.service")
    return {"action": "restart-supervisor", "rc": rc, "out": out}


def fault_restart_wireplumber(_args) -> dict:
    rc, out = sh("systemctl --user restart wireplumber")
    return {"action": "restart-wireplumber", "rc": rc, "out": out,
            "note": "restarting the session manager is itself a graph perturbation"}


def fault_restart_pipewire(_args) -> dict:
    rc, out = sh("systemctl --user restart pipewire")
    return {"action": "restart-pipewire", "rc": rc, "out": out,
            "note": "bridge-supervisor is PartOf=pipewire.service and follows it down"}


def _fault_microphone_cycle(args, *, action: str, expected_id: str) -> dict:
    """Remove one identity-verified microphone and put the same USB instance back.

    Deauthorizing is not electrically identical to unplugging -- the device stays powered
    and the hub port is untouched -- but it drives the same kernel/ALSA/PipeWire removal
    path, which is all the supervisor can see.
    """
    try:
        path = microphone_usb_path(expected_id)
    except (OSError, RuntimeError) as exc:
        return {
            "action": action,
            "expected_microphone": expected_id,
            "error": str(exc),
        }
    off = _set_usb_authorized(path, 0)
    time.sleep(args.gap)
    on = _set_usb_authorized(path, 1)
    return {
        "action": action,
        "expected_microphone": expected_id,
        "gap_s": args.gap,
        "off": off,
        "on": on,
    }


def fault_microphone_cycle(args) -> dict:
    if not args.expected_microphone:
        return {
            "action": "microphone-cycle",
            "error": "--expected-microphone is required for a generic microphone fault",
        }
    return _fault_microphone_cycle(
        args, action="microphone-cycle", expected_id=args.expected_microphone
    )


def fault_lark_cycle(args) -> dict:
    """Compatibility name for the historical Lark-only campaign."""
    return _fault_microphone_cycle(args, action="lark-cycle", expected_id="lark-a1")


def _fault_microphone_unplug_during_build(
    args, *, action: str, expected_id: str
) -> dict:
    """Remove the selected microphone mid-BUILDING -- a window unhittable by hand.

    Restart the supervisor to force a rebuild, then deauthorize `--delay` seconds in.
    BUILD_TIMEOUT_SECONDS is 10, so a delay of 1-3 s lands inside the window between the
    AEC module starting and its nodes appearing.
    """
    try:
        path = microphone_usb_path(expected_id)
    except (OSError, RuntimeError) as exc:
        return {
            "action": action,
            "expected_microphone": expected_id,
            "error": str(exc),
        }
    sh("systemctl --user restart bridge-supervisor.service")
    time.sleep(args.delay)
    off = _set_usb_authorized(path, 0)
    time.sleep(args.gap)
    on = _set_usb_authorized(path, 1)
    return {
        "action": action,
        "expected_microphone": expected_id,
        "delay_s": args.delay,
        "gap_s": args.gap,
        "off": off,
        "on": on,
    }


def fault_microphone_unplug_during_build(args) -> dict:
    if not args.expected_microphone:
        return {
            "action": "microphone-unplug-during-build",
            "error": "--expected-microphone is required for a generic microphone fault",
        }
    return _fault_microphone_unplug_during_build(
        args,
        action="microphone-unplug-during-build",
        expected_id=args.expected_microphone,
    )


def fault_lark_unplug_during_build(args) -> dict:
    """Compatibility name for the historical Lark-only campaign."""
    return _fault_microphone_unplug_during_build(
        args, action="lark-unplug-during-build", expected_id="lark-a1"
    )


def fault_restart_churn(args) -> dict:
    """Reproduce the E08 controller wedge by churning the graph during active SCO.

    E12 ran four supervisor restarts in ~2.5 minutes during a live call and the Bluetooth
    controller wedged: it stopped answering HCI commands, BlueZ kept reporting Connected
    while no HFP nodes existed, and the phone could not reconnect until bt-reset.sh
    rfkill-cycled the adapter. A later single-restart run did not wedge. E08's own open
    questions name loopback churn as a suspect, so this is a deliberate attempt at the
    trigger rather than waiting to be surprised by it again.

    Samples the controller between restarts, because the wedge is defined by the
    controller ceasing to answer, not by anything the supervisor reports.
    """
    events: list[dict] = []
    for i in range(args.churn_count):
        sh("systemctl --user restart bridge-supervisor.service")
        time.sleep(args.churn_gap)
        # `hcitool con` needs the controller to answer, so a timeout IS the wedge signal.
        rc, out = sh("timeout 6 hcitool con", timeout=10)
        status, _ = INV.read_status()
        events.append({
            "restart": i + 1,
            "controller_answered": rc == 0,
            "acl": "ACL" in out,
            "sco": "SCO" in out,
            "state": status.get("state"),
        })
        if rc != 0:
            events.append({"note": "CONTROLLER STOPPED ANSWERING -- wedge reproduced"})
            break
    return {"action": "restart-churn", "count": args.churn_count,
            "gap_s": args.churn_gap, "events": events}


def fault_config_corrupt(_args) -> dict:
    cfg = Path.home() / "rpi-lark-bridge/config/bridge.toml"
    backup = Path("/tmp/e13/bridge.toml.faultctl")
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text(cfg.read_text())
    cfg.write_text("[audio.aec]\nenabled = true\nthis is not valid toml =\n")
    rc, out = sh("systemctl --user restart bridge-supervisor.service")
    return {"action": "config-corrupt", "backup": str(backup), "rc": rc, "out": out,
            "note": "restore with --fault config-restore"}


def fault_config_restore(_args) -> dict:
    cfg = Path.home() / "rpi-lark-bridge/config/bridge.toml"
    backup = Path("/tmp/e13/bridge.toml.faultctl")
    if not backup.exists():
        return {"action": "config-restore", "skipped": "no backup present"}
    cfg.write_text(backup.read_text())
    rc, out = sh("systemctl --user restart bridge-supervisor.service")
    return {"action": "config-restore", "rc": rc, "out": out}


FAULTS = {
    "none": fault_none,
    "kill-aec": fault_kill_aec,
    "kill-aec-x5": fault_kill_aec_x5,
    "kill-loopback": fault_kill_loopback,
    "restart-supervisor": fault_restart_supervisor,
    "restart-wireplumber": fault_restart_wireplumber,
    "restart-pipewire": fault_restart_pipewire,
    "microphone-cycle": fault_microphone_cycle,
    "microphone-unplug-during-build": fault_microphone_unplug_during_build,
    "lark-cycle": fault_lark_cycle,
    "lark-unplug-during-build": fault_lark_unplug_during_build,
    "restart-churn": fault_restart_churn,
    "config-corrupt": fault_config_corrupt,
    "config-restore": fault_config_restore,
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fault", choices=sorted(FAULTS))
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--observe", type=float, default=60.0, help="seconds to watch after the fault")
    ap.add_argument("--settle", type=float, default=3.0, help="seconds of baseline before the fault")
    ap.add_argument("--delay", type=float, default=2.0, help="race-window delay for timed faults")
    ap.add_argument("--gap", type=float, default=5.0, help="seconds to leave the microphone removed")
    ap.add_argument(
        "--expected-microphone",
        metavar="ID",
        help="candidate ID targeted by generic microphone faults",
    )
    ap.add_argument("--no-audio-check", dest="audio_check", action="store_false",
                    help="skip the post-fault speaker/downlink measurement")
    ap.add_argument("--churn-count", type=int, default=8, help="supervisor restarts for restart-churn")
    ap.add_argument("--churn-gap", type=float, default=12.0, help="seconds between churn restarts")
    ap.add_argument("--kill-budget", type=int, default=12,
                    help="max kills for kill-aec-x5 before giving up")
    ap.add_argument("--outdir", default="/tmp/e13")
    args = ap.parse_args()

    if args.list or not args.fault:
        for name in sorted(FAULTS):
            print(f"  {name}")
        return 0

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    baseline_status, _ = INV.read_status()
    before = [sample()]
    end = time.monotonic() + args.settle
    while time.monotonic() < end:
        before.append(sample())

    injected_at = time.time()
    detail = FAULTS[args.fault](args)

    timeline: list[dict] = []
    recovered_at: float | None = None
    left_active = False
    quantum_low_run = 0
    quantum_low_max = 0
    deadline = time.monotonic() + args.observe
    while time.monotonic() < deadline:
        point = sample()
        timeline.append(point)
        state = point["observations"]["state"]

        # Recovery means ACTIVE *again*, so the fault must be seen to land first. Without
        # this the first post-injection sample -- taken before the supervisor's 2 s poll
        # notices anything -- is still ACTIVE and gets reported as "recovered in 0.4 s".
        if state != "ACTIVE":
            left_active = True
        if recovered_at is None and left_active and point["healthy"] and state == "ACTIVE":
            recovered_at = point["t"]

        # I7: a ratchet only counts if it persists past teardown.
        if point["observations"].get("quantum_below_configured"):
            quantum_low_run += 1
            quantum_low_max = max(quantum_low_max, quantum_low_run)
        else:
            quantum_low_run = 0

    audio = speaker_level() if args.audio_check else None
    final_status, _ = INV.read_status()
    report = {
        "fault": args.fault,
        "expected_microphone": args.expected_microphone,
        "microphone_before": (baseline_status.get("microphone") or {}).get("selected"),
        "graph_generation_before": baseline_status.get("generation"),
        "microphone_after": (final_status.get("microphone") or {}).get("selected"),
        "graph_generation_after": final_status.get("generation"),
        "audio_after": audio,
        "detail": detail,
        "injected_at": injected_at,
        "baseline_healthy": all(p["healthy"] for p in before),
        "baseline_state": before[-1]["observations"]["state"],
        "recovered": recovered_at is not None,
        "recovery_seconds": round(recovered_at - injected_at, 1) if recovered_at else None,
        "states_seen": sorted({p["observations"]["state"] for p in timeline if p["observations"]["state"]}),
        "violations_seen": sorted({v["id"] for p in timeline for v in p["violations"]}),
        "fault_observed": left_active,
        "quantum_low_max_run": quantum_low_max,
        "quantum_ratcheted": quantum_low_max >= 5,
        "final": timeline[-1] if timeline else None,
        "samples": len(timeline),
        "timeline": timeline,
    }
    path = outdir / f"fault_{args.fault}_{int(injected_at)}.json"
    path.write_text(json.dumps(report, indent=2))

    summary = {k: v for k, v in report.items() if k != "timeline"}
    summary["report_path"] = str(path)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
