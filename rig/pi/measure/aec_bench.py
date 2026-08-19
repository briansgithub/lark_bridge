#!/usr/bin/env python3
"""Run a low-level real-speaker WebRTC AEC bench capture on the Pi."""

from __future__ import annotations

import argparse
import dataclasses
import importlib.util
import json
import math
import signal
import subprocess
import sys
import time
from pathlib import Path

from aec_profile import ActiveProfiler

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


def wait_links(module, expected: set[tuple[str, str]], timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        links = module.pw_links() or []
        if expected <= set(links):
            return
        time.sleep(0.25)
    raise RuntimeError(f"PipeWire links did not appear: {sorted(expected)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--seconds", type=float, default=8.0)
    parser.add_argument(
        "--signal", choices=("sine", "multitone", "speech"), default="sine"
    )
    parser.add_argument("--tone", type=float, default=1000.0)
    parser.add_argument("--tone-dbfs", type=float, default=-30.0)
    parser.add_argument("--profile-name", default="baseline")
    parser.add_argument(
        "--high-pass-filter", action=argparse.BooleanOptionalAction, default=None
    )
    parser.add_argument(
        "--noise-suppression", action=argparse.BooleanOptionalAction, default=None
    )
    parser.add_argument(
        "--gain-control", action=argparse.BooleanOptionalAction, default=None
    )
    parser.add_argument(
        "--transient-suppression", action=argparse.BooleanOptionalAction, default=None
    )
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
            args.signal,
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

    tuning = settings.aec
    replacements = {"enabled": True}
    for field in (
        "high_pass_filter",
        "noise_suppression",
        "gain_control",
        "transient_suppression",
    ):
        value = getattr(args, field)
        if value is not None:
            replacements[field] = value
    tuning = dataclasses.replace(tuning, **replacements)
    host = module.NativeAecHost(tuning, lark, output_node)
    recorders: list[subprocess.Popen[bytes]] = []
    started = time.monotonic()
    started_wall = time.time()
    profiler = None
    runtime: dict = {}
    module_started_ms = None
    try:
        host.start()
        wait_nodes(module, {module.AEC_SOURCE, module.AEC_SINK})
        wait_links(
            module,
            {(lark, module.AEC_CAPTURE), (module.AEC_PLAYBACK, output_node)},
        )
        module_started_ms = round((time.monotonic() - started) * 1000, 2)
        (args.out / "module-command.txt").write_text(
            host.module_command(), encoding="utf-8"
        )
        (args.out / "graph-active.json").write_text(
            subprocess.run(
                ["pw-dump"], capture_output=True, text=True, check=False, timeout=10
            ).stdout,
            encoding="utf-8",
        )
        (args.out / "links-active.txt").write_text(
            subprocess.run(
                ["pw-link", "-l"],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            ).stdout,
            encoding="utf-8",
        )
        if not module.set_aec_mute(False):
            raise RuntimeError("AEC sink could not be unmuted")
        if host.pid is None:
            raise RuntimeError("AEC owner PID is absent")
        profiler = ActiveProfiler(
            args.out,
            host.pid,
            iterations=max(math.ceil(args.seconds) + 4, 5),
        )
        profiler.start()
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
        try:
            if profiler is not None:
                runtime = profiler.stop()
        finally:
            host.stop("bench capture complete")

    journal = subprocess.run(
        [
            "journalctl",
            "--user",
            "-u",
            "pipewire",
            "--since",
            f"@{int(started_wall)}",
            "--no-pager",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    (args.out / "pipewire-journal.txt").write_text(
        journal.stdout + journal.stderr, encoding="utf-8"
    )

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
            "--reference",
            str(tone_path),
            "--signal",
            args.signal,
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

    failures = list(metrics.get("failures", []))
    pw_errors = runtime.get("pw_top", {}).get("error_delta_total")
    if isinstance(pw_errors, int) and pw_errors > 0:
        failures.append(f"PipeWire ERR counters increased by {pw_errors}")
    if "resync" in (journal.stdout + journal.stderr).lower():
        failures.append("PipeWire ALSA playback resynchronized during the capture")
    if (
        runtime.get("temperature_c_max") is not None
        and runtime["temperature_c_max"] >= 75
    ):
        failures.append("temperature reached the 75 C stop threshold")
    if any(
        value not in {None, "throttled=0x0"}
        for value in (runtime.get("throttled_start"), runtime.get("throttled_end"))
    ):
        failures.append("throttle or undervoltage flags are set")
    metrics["failures"] = failures
    metrics["verdict"] = "PASS" if not failures else "FAIL"

    result = {
        "verdict": metrics["verdict"],
        "duration_s": round(time.monotonic() - started, 3),
        "module_startup_ms": module_started_ms,
        "lark": lark,
        "wired_output": output_node,
        "tone_dbfs": args.tone_dbfs,
        "signal": args.signal,
        "profile_name": args.profile_name,
        "webrtc": {
            "high_pass_filter": tuning.high_pass_filter,
            "noise_suppression": tuning.noise_suppression,
            "gain_control": tuning.gain_control,
            "voice_detection": tuning.voice_detection,
            "transient_suppression": tuning.transient_suppression,
        },
        "metrics": metrics,
        "runtime": runtime,
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
