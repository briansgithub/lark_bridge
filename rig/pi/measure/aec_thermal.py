#!/usr/bin/env python3
"""Run the short speaker-only AEC thermal and timing screen without recording audio."""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

from aec_bench import (
    DEFAULT_EXPECTED_MICROPHONE,
    effective_output,
    is_pwm_output,
    load_supervisor,
    output_volume,
    resolve_selected_microphone,
    stop_process,
    wait_links,
    wait_nodes,
)
from aec_profile import PI3_ARM_CLOCK_HZ, ActiveProfiler, gate_failures, parse_pw_top

REPO = Path(__file__).resolve().parents[3]
THERMAL_CONSUMER = "bridge.thermal.clean-consumer"
THERMAL_PLAYBACK = "bridge.thermal.playback"


def live_failure(sample: dict, pw_top: dict) -> str | None:
    temperature = sample.get("temperature_c")
    if isinstance(temperature, (int, float)) and temperature >= 75:
        return f"temperature reached {temperature:.2f} C"
    memory = sample.get("mem_available_kib")
    if isinstance(memory, int) and memory < 250 * 1024:
        return f"available memory fell to {memory / 1024:.1f} MiB"
    clock = sample.get("arm_clock_hz")
    if isinstance(clock, int) and clock < PI3_ARM_CLOCK_HZ:
        return f"ARM clock fell to {clock} Hz"
    if sample.get("throttled") not in {None, "throttled=0x0"}:
        return "throttle or undervoltage flags were observed"
    errors = pw_top.get("steady_error_delta_total")
    if isinstance(errors, int) and errors > 0:
        return f"steady-state PipeWire errors increased by {errors}"
    for name, node in (pw_top.get("nodes") or {}).items():
        busy = node.get("busy_ratio_p99")
        if isinstance(busy, (int, float)) and busy > 0.70:
            return f"{name} B/Q p99 {busy:.2f} exceeds 0.70"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--minutes", type=float, default=15.0)
    parser.add_argument("--sample-seconds", type=float, default=5.0)
    parser.add_argument("--signal", choices=("multitone", "speech"), default="speech")
    parser.add_argument("--tone-dbfs", type=float, default=-28.0)
    parser.add_argument("--node-latency-frames", type=int, default=1920)
    parser.add_argument("--output", help="explicit PipeWire output node")
    parser.add_argument(
        "--expected-microphone",
        default=DEFAULT_EXPECTED_MICROPHONE,
        metavar="ID",
        help="candidate ID that must be selected (configured priority is used when omitted)",
    )
    args = parser.parse_args()
    if args.minutes <= 0 or args.sample_seconds <= 0:
        raise SystemExit("minutes and sample-seconds must be positive")
    args.out.mkdir(parents=True, exist_ok=True)

    duration = args.minutes * 60
    module = load_supervisor()
    settings = module.load_settings()
    try:
        nodes, microphone_node, microphone, microphone_resolution = (
            resolve_selected_microphone(module, settings, args.expected_microphone)
        )
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    if settings.hfp_source in nodes or settings.hfp_sink in nodes:
        raise SystemExit("HFP nodes are present; refusing the speaker thermal screen")
    if module.AEC_SOURCE in nodes or module.AEC_SINK in nodes:
        raise SystemExit("AEC nodes already exist; refusing a second instance")
    output = effective_output(module, settings.wired_output, args.output)
    if output not in nodes:
        raise SystemExit("selected output is absent")
    volume, muted = output_volume(output)
    if muted:
        raise SystemExit("selected output is muted")
    if is_pwm_output(output) and volume > 0.86:
        raise SystemExit("wired output exceeds the measured-safe 0.85 setting")

    reference = args.out / "thermal-reference.wav"
    subprocess.run(
        [
            sys.executable,
            str(REPO / "tools" / "audio" / "tone_gen.py"),
            "--mode",
            args.signal,
            "--seconds",
            str(duration),
            "--rate",
            "48000",
            "--channels",
            "1",
            "--dbfs",
            str(args.tone_dbfs),
            "--out",
            str(reference),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )

    tuning = dataclasses.replace(settings.aec, enabled=True)
    host = module.NativeAecHost(
        tuning, microphone_node, output, latency_frames=args.node_latency_frames
    )
    recorder: subprocess.Popen[bytes] | None = None
    playback: subprocess.Popen[bytes] | None = None
    profiler: ActiveProfiler | None = None
    runtime: dict = {}
    failures: list[str] = []
    started = time.monotonic()
    started_wall = time.time()
    last_sample_count = 0
    try:
        host.start()
        wait_nodes(module, {module.AEC_SOURCE, module.AEC_SINK})
        wait_links(
            module,
            {(microphone_node, module.AEC_CAPTURE), (module.AEC_PLAYBACK, output)},
        )
        if not module.set_aec_mute(False) or host.pid is None:
            raise RuntimeError("AEC graph did not become runnable")
        profiler = ActiveProfiler(
            args.out,
            host.pid,
            iterations=max(math.ceil(duration) + 5, 10),
            interval=args.sample_seconds,
        )
        profiler.start()
        recorder = subprocess.Popen(
            [
                "pw-record",
                "--target",
                module.AEC_SOURCE,
                "--properties",
                json.dumps(
                    {
                        "node.name": THERMAL_CONSUMER,
                        "node.dont-reconnect": True,
                    },
                    separators=(",", ":"),
                ),
                "--rate",
                "48000",
                "--channels",
                "1",
                "--channel-map",
                "mono",
                "--format",
                "s16",
                os.devnull,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        playback = subprocess.Popen(
            [
                "pw-play",
                "--target",
                module.AEC_SINK,
                "--properties",
                json.dumps(
                    {
                        "node.name": THERMAL_PLAYBACK,
                        "node.dont-reconnect": True,
                    },
                    separators=(",", ":"),
                ),
                str(reference),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        wait_nodes(module, {THERMAL_CONSUMER, THERMAL_PLAYBACK})
        owned_links = {
            (module.AEC_SOURCE, THERMAL_CONSUMER),
            (THERMAL_PLAYBACK, module.AEC_SINK),
        }
        wait_links(module, owned_links)
        actual_links = set(module.pw_links() or [])
        actual_owned = {
            link
            for link in actual_links
            if link[0] == THERMAL_PLAYBACK or link[1] == THERMAL_CONSUMER
        }
        if actual_owned != owned_links:
            raise RuntimeError(
                f"thermal stream targeting mismatch: expected {sorted(owned_links)}, "
                f"found {sorted(actual_owned)}"
            )
        if recorder.poll() is not None or playback.poll() is not None:
            raise RuntimeError("thermal stream exited before link verification")
        (args.out / "module-command.txt").write_text(
            host.module_command(), encoding="utf-8"
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
        while playback.poll() is None:
            if profiler is not None and len(profiler.samples) > last_sample_count:
                last_sample_count = len(profiler.samples)
                current_pw_top = parse_pw_top(args.out / "pw-top.txt")
                reason = live_failure(profiler.samples[-1], current_pw_top)
                if reason is not None:
                    failures.append(reason)
                    playback.terminate()
                    break
                journal_now = subprocess.run(
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
                ).stdout
                if "resync" in journal_now.lower():
                    failures.append("PipeWire ALSA playback resynchronized")
                    playback.terminate()
                    break
            time.sleep(0.25)
        if playback.wait(timeout=5) != 0 and not failures:
            failures.append("thermal playback process failed")
    finally:
        if playback is not None:
            stop_process(playback)
        if recorder is not None:
            stop_process(recorder)
        if profiler is not None:
            runtime = profiler.stop()
        host.stop("thermal screen complete")
        reference.unlink(missing_ok=True)

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
    (args.out / "pipewire-journal.txt").write_text(journal.stdout, encoding="utf-8")
    failures.extend(failure for failure in gate_failures(runtime) if failure not in failures)
    if "resync" in journal.stdout.lower() and "PipeWire ALSA playback resynchronized" not in failures:
        failures.append("PipeWire ALSA playback resynchronized")
    if time.monotonic() - started < duration and not failures:
        failures.append("thermal screen ended before the requested duration")
    remaining = module.pw_nodes() or {}
    stale = {
        module.AEC_SOURCE,
        module.AEC_SINK,
        THERMAL_CONSUMER,
        THERMAL_PLAYBACK,
    } & set(remaining)
    if stale:
        failures.append(f"owned nodes remained after thermal-screen teardown: {sorted(stale)}")

    result = {
        "verdict": "PASS" if not failures else "FAIL",
        "requested_minutes": args.minutes,
        "elapsed_s": round(time.monotonic() - started, 3),
        "sample_seconds": args.sample_seconds,
        "signal": args.signal,
        "tone_dbfs": args.tone_dbfs,
        "node_latency_frames": args.node_latency_frames,
        "microphone": microphone,
        "microphone_resolution": microphone_resolution,
        "expected_microphone": args.expected_microphone,
        "graph_generation": microphone["graph_generation"],
        "lark": microphone_node if microphone["id"] == "lark-a1" else None,
        "output": output,
        "recording_enabled": False,
        "runtime": runtime,
        "failures": failures,
    }
    (args.out / "thermal.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
