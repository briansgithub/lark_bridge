#!/usr/bin/env python3
"""Capture the far-end audio path during a REAL call, at two taps at once.

E11 reproduced the playback crackle synthetically. This measures it on a live call,
where Discord, Opus and the mSBC/SCO leg are all inside the loop and inject artifacts
of their own. A single recording cannot tell those from the defect, so this records
the same far-end audio on both sides of the AEC:

    pre   bridge.aec.sink monitor  -- far-end audio BEFORE the WebRTC APM
    post  onboard sink monitor     -- what the DAC actually receives, AFTER playback

A discontinuity present in post but absent from pre is attributable to the AEC stage.
One present in both arrived over Bluetooth and is not our bug.

The content-independent metric is the pw-top ERR count on echo-cancel-playback: in E11
it matched the audible glitch count exactly (417/417), and unlike the audio it does not
care what Discord did to the signal. Treat ERR as primary and the recordings as
corroboration.

    call_capture.py --label lat1920 --seconds 20

Every recorder is named, pinned with node.dont-reconnect and checked against its
explicit target before the measurement is trusted. E10 lost an entire baseline to a
recorder silently falling back to the default source; refusing to repeat that is worth
the extra assertion.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SUPERVISOR_PATH = REPO / "pi" / "bridged" / "bridge_supervisor.py"
PRE_RECORDER = "bridge.e12.pre"
POST_RECORDER = "bridge.e12.post"


def load_supervisor():
    spec = importlib.util.spec_from_file_location("e12_supervisor", SUPERVISOR_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load supervisor from {SUPERVISOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def parse_pwtop(path: Path, interest: list[str]) -> dict:
    """Aggregate every snapshot, not just the last one.

    Two traps make the final snapshot the wrong thing to read. pw-top outlives the
    recorders, so its last frame is often captured after the audio stopped and reports
    a quantum of 0 for an idle sink. And ERR is cumulative per node since that node
    started: the AEC nodes are recreated by each supervisor restart so their absolute
    count is per-condition, but the ALSA sink persists across the whole session, so
    only its DELTA over the measurement window means anything.
    """
    if not path.exists():
        return {}
    blocks = re.split(r"^S\s+ID\s+QUANT.*$", path.read_text(), flags=re.MULTILINE)
    samples: dict[str, list[tuple[int, int]]] = {}
    for block in blocks:
        for line in block.splitlines():
            cols = line.split()
            if len(cols) < 10:
                continue
            name = cols[-1]
            if not any(key in name for key in interest):
                continue
            try:
                samples.setdefault(name, []).append((int(cols[2]), int(cols[8])))
            except (ValueError, IndexError):
                continue

    out: dict = {}
    for name, series in samples.items():
        errs = [err for _, err in series]
        running = [quantum for quantum, _ in series if quantum > 0]
        out[name] = {
            "quantum": max(running) if running else 0,
            "err_first": errs[0],
            "err_last": errs[-1],
            "err_delta": errs[-1] - errs[0],
            "snapshots": len(series),
        }
    return out


def start_recorder(target: str, name: str, path: Path) -> subprocess.Popen:
    properties = {
        "node.name": name,
        "node.dont-reconnect": True,
        # Both taps are sink monitors, not sources.
        "stream.capture.sink": True,
    }
    return subprocess.Popen(
        [
            "pw-record", "--target", target,
            "--properties", json.dumps(properties, separators=(",", ":")),
            "--rate", "48000", "--channels", "1", "--channel-map", "mono",
            "--format", "s16", str(path),
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def verify_recorder_links(module, expected: set[tuple[str, str]]) -> None:
    """Reject target fallback or duplicate recorder inputs. See E10."""
    links = set(module.pw_links() or [])
    recorder_names = {target for _, target in expected}
    actual = {(source, target) for source, target in links if target in recorder_names}
    if actual != expected:
        raise RuntimeError(
            f"recorder targeting mismatch: expected {sorted(expected)}, found {sorted(actual)}"
        )


def system_health() -> dict:
    temp = run(["vcgencmd", "measure_temp"]).stdout.strip()
    throttled = run(["vcgencmd", "get_throttled"]).stdout.strip()
    try:
        load = Path("/proc/loadavg").read_text().split()[:3]
    except OSError:
        load = []
    return {"temperature": temp, "throttled": throttled, "loadavg": load}


def read_status() -> dict:
    path = Path(f"/run/user/{os.getuid()}/bridge-status.json")
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--label", required=True)
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--outdir", default="/tmp/e12")
    ap.add_argument("--allow-call-down", action="store_true",
                    help="measure even if the supervisor is not ACTIVE (diagnostics only)")
    args = ap.parse_args()

    module = load_supervisor()
    settings = module.load_settings()
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    pre_path = outdir / f"pre_{args.label}.wav"
    post_path = outdir / f"post_{args.label}.wav"
    top_path = outdir / f"pwtop_{args.label}.txt"
    output_node = settings.wired_output

    status = read_status()
    result: dict = {
        "label": args.label,
        "seconds": args.seconds,
        "state_before": status.get("state"),
        "aec_before": status.get("aec", {}),
        "pre_wav": str(pre_path),
        "post_wav": str(post_path),
        "health_before": system_health(),
    }

    if status.get("state") != "ACTIVE" and not args.allow_call_down:
        result["error"] = (
            f"supervisor state is {status.get('state')!r}, not ACTIVE — no call graph to measure. "
            "Start the call first, or pass --allow-call-down for diagnostics."
        )
        json.dump(result, sys.stdout, indent=2)
        print()
        return 1

    pre = post = topper = None
    try:
        pre = start_recorder(module.AEC_SINK, PRE_RECORDER, pre_path)
        post = start_recorder(output_node, POST_RECORDER, post_path)
        with open(top_path, "w") as handle:
            topper = subprocess.Popen(["pw-top", "-b", "-n", str(int(args.seconds) + 6)],
                                      stdout=handle, stderr=subprocess.STDOUT)
            time.sleep(2.0)  # let the recorders link before asserting
            verify_recorder_links(module, {
                (module.AEC_SINK, PRE_RECORDER),
                (output_node, POST_RECORDER),
            })
            result["links_verified"] = True
            time.sleep(args.seconds)
    except Exception as exc:  # noqa: BLE001 - report, do not mask
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        for proc in (pre, post, topper):
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()

    result["pwtop"] = parse_pwtop(top_path, [output_node, "aec", "echo-cancel"])
    after = read_status()
    result["state_after"] = after.get("state")
    result["health_after"] = system_health()
    json.dump(result, sys.stdout, indent=2)
    print()
    return 1 if result.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
