#!/usr/bin/env python3
"""Run a low-level real-speaker WebRTC AEC bench capture on the Pi."""

from __future__ import annotations

import argparse
import dataclasses
import importlib.util
import json
import math
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from aec_profile import ActiveProfiler, gate_failures

REPO = Path(__file__).resolve().parents[3]
SUPERVISOR_PATH = REPO / "pi" / "bridged" / "bridge_supervisor.py"
RAW_RECORDER = "bridge.bench.raw-recorder"
CLEAN_RECORDER = "bridge.bench.clean-recorder"
REFERENCE_RECORDER = "bridge.bench.reference-recorder"


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


def verify_recorder_links(module, expected: set[tuple[str, str]]) -> None:
    """Reject target fallback or duplicate recorder inputs."""
    links = set(module.pw_links() or [])
    recorder_names = {target for _, target in expected}
    actual = {(source, target) for source, target in links if target in recorder_names}
    if actual != expected:
        raise RuntimeError(
            f"bench recorder targeting mismatch: expected {sorted(expected)}, "
            f"found {sorted(actual)}"
        )


def output_volume(node: str) -> tuple[float, bool]:
    graph = subprocess.run(
        ["pw-dump"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    try:
        objects = json.loads(graph.stdout)
        node_id = next(
            str(obj["id"])
            for obj in objects
            if ((obj.get("info") or {}).get("props") or {}).get("node.name") == node
        )
    except (json.JSONDecodeError, KeyError, StopIteration) as exc:
        raise RuntimeError(f"could not resolve wired-output id for {node}") from exc
    result = subprocess.run(
        ["wpctl", "get-volume", node_id],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    match = re.search(r"Volume:\s+([0-9.]+)", result.stdout)
    if result.returncode != 0 or match is None:
        raise RuntimeError(f"could not read wired-output volume: {result.stderr.strip()}")
    return float(match.group(1)), "[MUTED]" in result.stdout


def node_id(node: str) -> str:
    graph = subprocess.run(
        ["pw-dump"], capture_output=True, text=True, check=False, timeout=10
    )
    try:
        objects = json.loads(graph.stdout)
        return str(
            next(
                obj["id"]
                for obj in objects
                if ((obj.get("info") or {}).get("props") or {}).get("node.name")
                == node
            )
        )
    except (json.JSONDecodeError, KeyError, StopIteration) as exc:
        raise RuntimeError(f"could not resolve PipeWire id for {node}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--seconds", type=float, default=8.0)
    parser.add_argument(
        "--signal", choices=("sine", "multitone", "speech", "noise"), default="sine"
    )
    parser.add_argument("--tone", type=float, default=1000.0)
    parser.add_argument("--tone-dbfs", type=float, default=-30.0)
    parser.add_argument(
        "--silence-seconds",
        type=float,
        default=1.0,
        help="leading and trailing silence around the stimulus",
    )
    parser.add_argument("--profile-name", default="baseline")
    parser.add_argument(
        "--node-latency-frames",
        type=int,
        help="bench-only PipeWire echo-cancel node latency request",
    )
    parser.add_argument(
        "--play-delay-frames",
        type=int,
        help="bench-only delay applied to the AEC reference, not speaker playback",
    )
    parser.add_argument(
        "--internal-debug-wav",
        action="store_true",
        help="record aligned play/capture/output channels inside module-echo-cancel",
    )
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
    if args.seconds <= 0 or args.silence_seconds < 0:
        raise SystemExit("seconds must be positive and silence-seconds non-negative")
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
    wired_volume, wired_muted = output_volume(output_node)
    if wired_muted:
        raise SystemExit("wired output is muted")
    if wired_volume > 0.86:
        raise SystemExit(
            f"wired output volume {wired_volume:.2f} exceeds the measured-safe 0.85 setting"
        )

    stimulus_path = args.out / "stimulus.wav"
    reference_path = args.out / "reference.wav"
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
            "--lead-silence",
            str(args.silence_seconds),
            "--trail-silence",
            str(args.silence_seconds),
            "--rate",
            "48000",
            "--channels",
            "1",
            "--dbfs",
            str(args.tone_dbfs),
            "--out",
            str(stimulus_path),
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
    host = module.NativeAecHost(
        tuning,
        lark,
        output_node,
        latency_frames=args.node_latency_frames,
        play_delay_frames=args.play_delay_frames,
    )
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
        if args.internal_debug_wav:
            internal_path = args.out / "aec-internal.wav"
            debug_param = (
                '{ params = [ "debug.aec.wav-path" '
                f"{json.dumps(str(internal_path))} ] }}"
            )
            subprocess.run(
                [
                    "pw-cli",
                    "set-param",
                    node_id(module.AEC_SOURCE),
                    "Props",
                    debug_param,
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
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
            iterations=max(math.ceil(args.seconds + 2 * args.silence_seconds) + 4, 5),
        )
        profiler.start()
        for target, recorder_name, capture_sink, output in (
            (lark, RAW_RECORDER, False, raw_path),
            (module.AEC_SOURCE, CLEAN_RECORDER, False, clean_path),
            (module.AEC_SINK, REFERENCE_RECORDER, True, reference_path),
        ):
            properties = {
                "node.name": recorder_name,
                "node.dont-reconnect": True,
            }
            if capture_sink:
                properties["stream.capture.sink"] = True
            recorders.append(
                subprocess.Popen(
                    [
                        "pw-record",
                        "--target",
                        target,
                        "--properties",
                        json.dumps(properties, separators=(",", ":")),
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
        if any(recorder.poll() is not None for recorder in recorders):
            raise RuntimeError("one or more bench recorders exited before playback")
        recorder_links = {
            (lark, RAW_RECORDER),
            (module.AEC_SOURCE, CLEAN_RECORDER),
            (module.AEC_SINK, REFERENCE_RECORDER),
        }
        wait_nodes(module, {RAW_RECORDER, CLEAN_RECORDER, REFERENCE_RECORDER})
        wait_links(module, recorder_links)
        verify_recorder_links(module, recorder_links)
        (args.out / "links-recording.txt").write_text(
            subprocess.run(
                ["pw-link", "-l"],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            ).stdout,
            encoding="utf-8",
        )
        subprocess.run(
            ["pw-play", "--target", module.AEC_SINK, str(stimulus_path)],
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
            "_SYSTEMD_USER_UNIT=pipewire.service",
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
            str(reference_path),
            "--signal",
            args.signal,
            "--tone",
            str(args.tone),
            "--skip",
            str(3 + args.silence_seconds),
            "--trailing-silence",
            str(args.silence_seconds),
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
    failures.extend(gate_failures(runtime))
    if "resync" in (journal.stdout + journal.stderr).lower():
        failures.append("PipeWire ALSA playback resynchronized during the capture")
    metrics["failures"] = failures
    metrics["verdict"] = "PASS" if not failures else "FAIL"

    result = {
        "verdict": metrics["verdict"],
        "duration_s": round(time.monotonic() - started, 3),
        "module_startup_ms": module_started_ms,
        "lark": lark,
        "wired_output": output_node,
        "wired_output_volume": wired_volume,
        "node_latency_frames": args.node_latency_frames,
        "play_delay_frames": args.play_delay_frames,
        "internal_debug_wav": args.internal_debug_wav,
        "tone_dbfs": args.tone_dbfs,
        "silence_seconds": args.silence_seconds,
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
