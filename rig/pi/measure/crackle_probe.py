#!/usr/bin/env python3
"""Reproduce far-end playback crackle without a phone, a call, or the Lark.

The far-end path is  SCO -> bridge.callout -> bridge.aec.sink -> WebRTC APM ->
echo-cancel-playback -> onboard jack.  Everything downstream of Bluetooth can be
exercised by playing a stimulus straight into bridge.aec.sink, so this needs no
call fixture: if crackle appears here, Bluetooth is not involved.

The AEC host is constructed from the REAL supervisor module and the REAL deployed
config, so the module arguments are byte-for-byte what production loads. The one
substitution is the capture target: production aims it at the Lark, and the Lark
is not always plugged in, so a null sink stands in for it.

    crackle_probe.py --label production            # exactly what the unit runs
    crackle_probe.py --label lat1920 --latency-frames 1920

Records what the DAC actually receives (the output sink's monitor) plus a pw-top
trace, and prints a JSON summary. Analyse the WAV with rig/analysis/glitch_detect.py.
"""

from __future__ import annotations

import argparse
import dataclasses
import importlib.util
import json
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SUPERVISOR_PATH = REPO / "pi" / "bridged" / "bridge_supervisor.py"
NULL_SINK = "bridge.probe.fakemic"


def load_supervisor():
    spec = importlib.util.spec_from_file_location("crackle_probe_supervisor", SUPERVISOR_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load supervisor from {SUPERVISOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=False, **kw)


def wait_nodes(module, names: set[str], timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if names <= set(module.pw_nodes() or {}):
            return True
        time.sleep(0.25)
    return False


def parse_pwtop(path: Path, interest: list[str]) -> dict:
    """Pull the final snapshot's QUANT/ERR for the nodes we care about."""
    if not path.exists():
        return {}
    blocks = re.split(r"^S\s+ID\s+QUANT.*$", path.read_text(), flags=re.MULTILINE)
    if not blocks:
        return {}
    out: dict = {}
    for line in blocks[-1].splitlines():
        cols = line.split()
        if len(cols) < 10:
            continue
        name = cols[-1]
        if not any(k in name for k in interest):
            continue
        try:
            out[name] = {"quantum": int(cols[2]), "rate": int(cols[3]), "err": int(cols[8])}
        except (ValueError, IndexError):
            continue
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--label", required=True, help="name for this run's output files")
    ap.add_argument("--latency-frames", type=int, default=None,
                    help="override node.latency; default follows the config like production")
    ap.add_argument("--no-latency", action="store_true",
                    help="send no node.latency at all -- the pre-fix production behaviour")
    ap.add_argument("--play-delay-frames", type=int, default=None)
    ap.add_argument("--stimulus", default="/tmp/crackle/sine1k.wav")
    ap.add_argument("--outdir", default="/tmp/crackle")
    args = ap.parse_args()

    module = load_supervisor()
    settings = module.load_settings()
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    wav = outdir / f"mon_{args.label}.wav"
    top = outdir / f"pwtop_{args.label}.txt"
    output_node = settings.wired_output

    # Stand-in for the Lark so the capture side has a real, linkable target.
    made = run(["pactl", "load-module", "module-null-sink",
                f"sink_name={NULL_SINK}",
                f"sink_properties=node.description={NULL_SINK}"])
    if made.returncode != 0:
        print(f"could not create null sink: {made.stderr.strip()}", file=sys.stderr)
        return 1
    null_id = made.stdout.strip()

    tuning = dataclasses.replace(settings.aec, enabled=True)
    # Mirror what begin_build() does, so "no flags" measures real production.
    latency = None if args.no_latency else (
        args.latency_frames if args.latency_frames is not None
        else getattr(tuning, "node_latency_frames", None)
    )
    play_delay = args.play_delay_frames if args.play_delay_frames is not None else (
        getattr(tuning, "play_delay_frames", None)
    )
    # Capture targets the null SINK, not "<name>.monitor": in PipeWire a monitor is a
    # set of ports on the sink node, not a node of its own, so the PulseAudio-style
    # ".monitor" name matches nothing and the capture stream silently lands on the
    # default source -- which here is the output sink, feeding the AEC its own playback.
    host = module.NativeAecHost(
        tuning,
        NULL_SINK,
        output_node,
        latency_frames=latency,
        play_delay_frames=play_delay,
    )
    result: dict = {
        "label": args.label,
        "latency_frames": latency,
        "play_delay_frames": play_delay,
        "module_command": host.module_command().strip(),
        "aec_settings": dataclasses.asdict(tuning),
        "monitor_wav": str(wav),
    }
    recorder = topper = None
    try:
        host.start()
        result["aec_nodes_ready"] = wait_nodes(module, {module.AEC_SOURCE, module.AEC_SINK})
        if not result["aec_nodes_ready"]:
            result["error"] = "AEC nodes never appeared"
        else:
            # Production builds the sink muted and unmutes only after verification.
            result["unmuted"] = module.set_aec_mute(False)
            time.sleep(0.5)

            recorder = subprocess.Popen(
                ["pw-record", "--target", f"{output_node}.monitor", str(wav)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            with open(top, "w") as top_handle:
                topper = subprocess.Popen(
                    ["pw-top", "-b", "-n", "40"], stdout=top_handle, stderr=subprocess.STDOUT
                )
                time.sleep(1.0)

                played = run(["pw-play", "--target", module.AEC_SINK, args.stimulus])
                result["play_rc"] = played.returncode
                if played.returncode != 0:
                    result["play_stderr"] = played.stderr.strip()[:400]
                time.sleep(1.0)
                result["links"] = [
                    list(link)
                    for link in (module.pw_links() or [])
                    if any("aec" in end or "echo-cancel" in end for end in link)
                ]
    finally:
        # Teardown must happen even if the probe raised. It deliberately does NOT
        # return: swallowing the exception here would hide the very failure we came
        # to diagnose.
        _teardown(host, null_id, recorder, topper)

    result["pwtop"] = parse_pwtop(top, [output_node, "aec", "echo-cancel"])
    json.dump(result, sys.stdout, indent=2)
    print()
    return 1 if result.get("error") else 0


def _teardown(host, null_id: str, recorder, topper) -> None:
    for proc in (recorder, topper):
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    host.stop("probe complete")
    run(["pactl", "unload-module", null_id])


if __name__ == "__main__":
    raise SystemExit(main())
