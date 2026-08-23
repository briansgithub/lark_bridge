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


def lark_usb_path() -> str | None:
    """Find the Lark's USB device directory by product string, not a hardcoded port."""
    for node in Path("/sys/bus/usb/devices").glob("*/product"):
        try:
            if "Wireless Microphone" in node.read_text():
                return str(node.parent)
        except OSError:
            continue
    return None


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
    ap.add_argument("--no-audio-check", dest="audio_check", action="store_false",
                    help="skip the post-fault speaker/downlink measurement")
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
    report = {
        "fault": args.fault,
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
