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
import subprocess
import sys
import time
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


def lark_usb_path() -> str | None:
    """Find the Lark's USB device directory by product string, not a hardcoded port."""
    for node in Path("/sys/bus/usb/devices").glob("*/product"):
        try:
            if "Wireless Microphone" in node.read_text():
                return str(node.parent)
        except OSError:
            continue
    return None


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


def fault_kill_aec_x5(_args) -> dict:
    """Walk the supervisor's backoff to FAILED.

    fail() increments attempts and goes DEGRADED with a min(2**n, 30) backoff until
    attempts >= MAX_BUILD_ATTEMPTS, then FAILED. Because update_signature() only resets
    attempts when (call_up, lark, output_up) CHANGES, and tick() returns immediately once
    FAILED, the prediction is that the unit stays dead for the rest of the call.
    """
    events = []
    for attempt, wait in enumerate(BACKOFF_STEPS[:MAX_BUILD_ATTEMPTS], start=1):
        deadline = time.monotonic() + wait + 25
        pid = None
        while time.monotonic() < deadline:
            status, _ = INV.read_status()
            pid = (status.get("aec") or {}).get("owner_pid")
            if pid:
                break
            time.sleep(1.0)
        if not pid:
            events.append({"attempt": attempt, "note": "AEC owner never reappeared", "killed": False})
            break
        sh(f"kill -9 {pid}")
        events.append({"attempt": attempt, "pid": pid, "killed": True})
        time.sleep(1.0)
    return {"action": "kill-aec-x5", "events": events}


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


def _set_lark(authorized: int) -> dict:
    path = lark_usb_path()
    if not path:
        return {"skipped": "Lark USB device not found"}
    rc, out = sh(f"echo {authorized} | sudo tee {path}/authorized >/dev/null")
    return {"usb_path": path, "authorized": authorized, "rc": rc, "out": out}


def fault_lark_cycle(args) -> dict:
    """Remove the Lark and put it back.

    Deauthorizing is not electrically identical to unplugging -- the device stays powered
    and the hub port is untouched -- but it drives the same kernel/ALSA/PipeWire removal
    path, which is all the supervisor can see.
    """
    off = _set_lark(0)
    time.sleep(args.gap)
    on = _set_lark(1)
    return {"action": "lark-cycle", "gap_s": args.gap, "off": off, "on": on}


def fault_lark_unplug_during_build(args) -> dict:
    """Remove the Lark mid-BUILDING -- a window that is unhittable by hand.

    Restart the supervisor to force a rebuild, then deauthorize `--delay` seconds in.
    BUILD_TIMEOUT_SECONDS is 10, so a delay of 1-3 s lands inside the window between the
    AEC module starting and its nodes appearing.
    """
    sh("systemctl --user restart bridge-supervisor.service")
    time.sleep(args.delay)
    off = _set_lark(0)
    time.sleep(args.gap)
    on = _set_lark(1)
    return {"action": "lark-unplug-during-build", "delay_s": args.delay,
            "gap_s": args.gap, "off": off, "on": on}


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
    "lark-cycle": fault_lark_cycle,
    "lark-unplug-during-build": fault_lark_unplug_during_build,
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
    ap.add_argument("--gap", type=float, default=5.0, help="seconds to leave the Lark removed")
    ap.add_argument("--outdir", default="/tmp/e13")
    args = ap.parse_args()

    if args.list or not args.fault:
        for name in sorted(FAULTS):
            print(f"  {name}")
        return 0

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    before = [sample()]
    end = time.monotonic() + args.settle
    while time.monotonic() < end:
        before.append(sample())

    injected_at = time.time()
    detail = FAULTS[args.fault](args)

    timeline: list[dict] = []
    recovered_at: float | None = None
    deadline = time.monotonic() + args.observe
    while time.monotonic() < deadline:
        point = sample()
        timeline.append(point)
        # "Recovered" means healthy AND actually carrying a call again, not merely quiet.
        if recovered_at is None and point["healthy"] and point["observations"]["state"] == "ACTIVE":
            recovered_at = point["t"]

    report = {
        "fault": args.fault,
        "detail": detail,
        "injected_at": injected_at,
        "baseline_healthy": all(p["healthy"] for p in before),
        "baseline_state": before[-1]["observations"]["state"],
        "recovered": recovered_at is not None,
        "recovery_seconds": round(recovered_at - injected_at, 1) if recovered_at else None,
        "states_seen": sorted({p["observations"]["state"] for p in timeline if p["observations"]["state"]}),
        "violations_seen": sorted({v["id"] for p in timeline for v in p["violations"]}),
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
