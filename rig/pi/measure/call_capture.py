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
REFERENCE_RECORDER = "bridge.e12.reference"
RAW_RECORDER = "bridge.e12.raw"
CLEAN_RECORDER = "bridge.e12.clean"


def load_supervisor():
    # Loading by file path does not add the module's directory to sys.path.  The
    # supervisor imports sibling controller helpers lazily from load_settings(),
    # so make the standalone measurement entry point behave like the installed
    # service before executing it.
    supervisor_dir = str(SUPERVISOR_PATH.parent)
    if supervisor_dir not in sys.path:
        sys.path.insert(0, supervisor_dir)
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
            # The first batch frame commonly reports newly discovered nodes as
            # ``C`` with zeroed counters before their real cumulative ``R`` row.
            # Treating that discovery row as the baseline makes old startup
            # errors look like errors acquired during this capture.
            if cols[0] != "R":
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


def start_recorder(target: str, name: str, path: Path, capture_sink: bool) -> subprocess.Popen:
    properties: dict = {
        "node.name": name,
        "node.dont-reconnect": True,
    }
    if capture_sink:
        # Only needed when the target is a SINK and we want its monitor. The Lark and
        # bridge.aec.source are real sources; setting this on them captures nothing.
        properties["stream.capture.sink"] = True
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


def microphone_from_status(status: dict) -> tuple[dict | None, str | None]:
    """Return artifact metadata and capture target without reviving stale aliases.

    Once the generic microphone block exists it is authoritative, including when its
    selection is null. ``endpoints.lark`` is a one-release compatibility field and may
    describe an observed but deliberately unselected Lark.
    """
    endpoints = status.get("endpoints") or {}
    if not isinstance(endpoints, dict):
        endpoints = {}
    if "microphone" in status:
        microphone = status.get("microphone")
        selected = microphone.get("selected") if isinstance(microphone, dict) else None
        selected = selected if isinstance(selected, dict) else None
        selected_node = selected.get("node") if selected is not None else None
        node = selected_node or endpoints.get("microphone")
        return selected, str(node) if node else None
    legacy = endpoints.get("lark")
    return None, str(legacy) if legacy else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--label", required=True)
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--outdir", default="/tmp/e12")
    ap.add_argument("--mode", choices=("crackle", "echo"), default="crackle",
                    help="crackle: taps either side of the AEC. "
                         "echo: reference/raw/clean for aec_metrics suppression.")
    ap.add_argument("--allow-call-down", action="store_true",
                    help="measure even if the supervisor is not ACTIVE (diagnostics only)")
    args = ap.parse_args()

    module = load_supervisor()
    settings = module.load_settings()
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    top_path = outdir / f"pwtop_{args.label}.txt"
    output_node = settings.wired_output

    status = read_status()
    selected_microphone, microphone_node = microphone_from_status(status)
    if args.mode == "echo":
        if not microphone_node:
            print(
                json.dumps(
                    {"error": "no selected microphone endpoint; cannot record the raw near-end tap"}
                )
            )
            return 1
        # aec_metrics wants exactly three: the echo source, the microphone that hears it,
        # and what actually leaves for the far end.
        #
        # The clean tap is the HFP SINK monitor, not bridge.aec.source. The supervisor
        # enforces exclusive consumption of the AEC source -- remove_dangerous_autolinks
        # unlinks anything that is not the bridge.mic loopback, so a recorder aimed there
        # does not survive. Tapping the HFP sink is also the truer measurement: it is
        # what is handed to Bluetooth, so it includes the loopback stage as well as the
        # AEC, and it is what the far end will actually hear.
        taps = [
            (module.AEC_SINK, REFERENCE_RECORDER, True),
            (microphone_node, RAW_RECORDER, False),
            (settings.hfp_sink, CLEAN_RECORDER, True),
        ]
    else:
        taps = [
            (module.AEC_SINK, PRE_RECORDER, True),
            (output_node, POST_RECORDER, True),
        ]

    paths = {name: outdir / f"{name.rsplit('.', 1)[-1]}_{args.label}.wav" for _, name, _ in taps}
    result: dict = {
        "label": args.label,
        "mode": args.mode,
        "seconds": args.seconds,
        "state_before": status.get("state"),
        "aec_before": status.get("aec", {}),
        "microphone": selected_microphone,
        "graph_generation": status.get("generation"),
        "wavs": {name: str(path) for name, path in paths.items()},
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

    recorders: list[subprocess.Popen] = []
    topper = None
    try:
        for target, name, capture_sink in taps:
            recorders.append(start_recorder(target, name, paths[name], capture_sink))
        with open(top_path, "w") as handle:
            topper = subprocess.Popen(["pw-top", "-b", "-n", str(int(args.seconds) + 6)],
                                      stdout=handle, stderr=subprocess.STDOUT)
            time.sleep(2.0)  # let the recorders link before asserting
            verify_recorder_links(module, {(t, n) for t, n, _ in taps})
            result["links_verified"] = True
            time.sleep(args.seconds)
    except Exception as exc:  # noqa: BLE001 - report, do not mask
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        for proc in [*recorders, topper]:
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
