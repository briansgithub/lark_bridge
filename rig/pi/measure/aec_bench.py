#!/usr/bin/env python3
"""Run a low-level real-speaker WebRTC AEC bench capture on the Pi."""

from __future__ import annotations

import argparse
import importlib.util
import json
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SUPERVISOR_PATH = REPO / "pi" / "bridged" / "bridge_supervisor.py"


def load_supervisor():
    spec = importlib.util.spec_from_file_location(
        "aec_bench_supervisor", SUPERVISOR_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load bridge supervisor")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


def wait_nodes(module, names: set[str], timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        nodes = module.pw_nodes() or {}
        if names <= set(nodes):
            return
        time.sleep(0.25)
    raise RuntimeError(f"PipeWire nodes did not appear: {sorted(names)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--seconds", type=float, default=8.0)
    parser.add_argument("--tone", type=float, default=1000.0)
    parser.add_argument("--tone-dbfs", type=float, default=-30.0)
    parser.add_argument(
        "--output", help="explicit PipeWire output node for instrument tests"
    )
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    module = load_supervisor()
    settings = module.load_settings()
    nodes = module.pw_nodes()
    if nodes is None:
        raise SystemExit("PipeWire graph unavailable")
    if settings.hfp_source in nodes or settings.hfp_sink in nodes:
        raise SystemExit("HFP nodes are present; refusing an acoustic bench injection")
    if module.AEC_SOURCE in nodes or module.AEC_SINK in nodes:
        raise SystemExit("AEC nodes already exist; refusing a second instance")
    lark = module.find_lark(nodes, settings)
    output_node = args.output or settings.wired_output
    if lark is None or output_node not in nodes:
        raise SystemExit("Lark or wired output is absent")

    tone_path = args.out / "reference.wav"
    raw_path = args.out / "raw-mic.wav"
    clean_path = args.out / "clean-mic.wav"
    subprocess.run(
        [
            sys.executable,
            str(REPO / "tools" / "audio" / "tone_gen.py"),
            "--mode",
            "sine",
            "--freq",
            str(args.tone),
            "--seconds",
            str(args.seconds),
            "--rate",
            "48000",
            "--channels",
            "1",
            "--dbfs",
            str(args.tone_dbfs),
            "--out",
            str(tone_path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )

    host = module.NativeAecHost(module.AecSettings(enabled=True), lark, output_node)
    recorders: list[subprocess.Popen[bytes]] = []
    started = time.monotonic()
    try:
        host.start()
        wait_nodes(module, {module.AEC_SOURCE, module.AEC_SINK})
        if not module.set_aec_mute(False):
            raise RuntimeError("AEC sink could not be unmuted")
        for target, output in ((lark, raw_path), (module.AEC_SOURCE, clean_path)):
            recorders.append(
                subprocess.Popen(
                    [
                        "pw-record",
                        "--target",
                        target,
                        "--rate",
                        "48000",
                        "--channels",
                        "1",
                        "--channel-map",
                        "mono",
                        "--format",
                        "s16",
                        str(output),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )
        time.sleep(1)
        subprocess.run(
            ["pw-play", "--target", module.AEC_SINK, str(tone_path)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.5)
    finally:
        for recorder in recorders:
            stop_process(recorder)
        host.stop("bench capture complete")

    time.sleep(1)
    remaining = module.pw_nodes() or {}
    if module.AEC_SOURCE in remaining or module.AEC_SINK in remaining:
        raise SystemExit("AEC nodes remained after bench owner exit")

    metrics_run = subprocess.run(
        [
            sys.executable,
            str(REPO / "rig" / "analysis" / "aec_metrics.py"),
            "--raw",
            str(raw_path),
            "--clean",
            str(clean_path),
            "--tone",
            str(args.tone),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        metrics = json.loads(metrics_run.stdout)
    except json.JSONDecodeError:
        metrics = {
            "verdict": "FAIL",
            "failures": [metrics_run.stderr or "metrics failed"],
        }
    health_run = subprocess.run(
        [sys.executable, str(REPO / "rig" / "analysis" / "system_health.py")],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        health = json.loads(health_run.stdout)
    except json.JSONDecodeError:
        health = {"error": health_run.stderr or "health capture failed"}

    result = {
        "verdict": metrics.get("verdict", "FAIL"),
        "duration_s": round(time.monotonic() - started, 3),
        "lark": lark,
        "wired_output": output_node,
        "tone_dbfs": args.tone_dbfs,
        "metrics": metrics,
        "system": health,
    }
    (args.out / "bench.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if result["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
