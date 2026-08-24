#!/usr/bin/env python3
"""Verify that the selected speaker is audible at the Lark before acoustic tests.

This is a fixture check, not benchmark evidence.  It records room noise, plays a
short low-level tone directly to the selected physical output, records the Lark
again, and rejects the fixture unless the tone is clearly above the idle level.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType


REPO = Path(__file__).resolve().parents[3]
RECORDER_NAME = "bridge.fixture.speaker-recorder"


def load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
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
        process.wait(timeout=3)


def wait_for_recorder(module: ModuleType, source: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        nodes = module.pw_nodes() or {}
        links = module.pw_links() or []
        if RECORDER_NAME in nodes and (source, RECORDER_NAME) in links:
            recorder_inputs = {
                pair for pair in links if pair[1] == RECORDER_NAME
            }
            if recorder_inputs != {(source, RECORDER_NAME)}:
                raise RuntimeError(
                    f"recorder targeting mismatch: {sorted(recorder_inputs)}"
                )
            return
        time.sleep(0.1)
    raise RuntimeError(f"Lark recorder did not attach to {source}")


def record_lark(module: ModuleType, source: str, path: Path, seconds: float) -> None:
    properties = json.dumps(
        {"node.name": RECORDER_NAME, "node.dont-reconnect": True},
        separators=(",", ":"),
    )
    recorder = subprocess.Popen(
        [
            "pw-record",
            "--target",
            source,
            "--properties",
            properties,
            "--rate",
            "48000",
            "--channels",
            "1",
            "--channel-map",
            "mono",
            "--format",
            "s16",
            str(path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_for_recorder(module, source)
        time.sleep(seconds)
        if recorder.poll() is not None:
            raise RuntimeError("Lark recorder exited early")
    finally:
        stop_process(recorder)


def record_while_playing(
    module: ModuleType,
    source: str,
    output: str,
    stimulus: Path,
    path: Path,
) -> None:
    properties = json.dumps(
        {"node.name": RECORDER_NAME, "node.dont-reconnect": True},
        separators=(",", ":"),
    )
    recorder = subprocess.Popen(
        [
            "pw-record",
            "--target",
            source,
            "--properties",
            properties,
            "--rate",
            "48000",
            "--channels",
            "1",
            "--channel-map",
            "mono",
            "--format",
            "s16",
            str(path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_for_recorder(module, source)
        time.sleep(0.5)
        subprocess.run(
            ["pw-play", "--target", output, str(stimulus)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        time.sleep(0.25)
        if recorder.poll() is not None:
            raise RuntimeError("Lark recorder exited during playback")
    finally:
        stop_process(recorder)


def selected_output(module: ModuleType, explicit: str | None) -> str | None:
    if explicit:
        return explicit
    try:
        status = json.loads(module.default_status_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    chosen = (status.get("output") or {}).get("chosen") or {}
    return str(chosen.get("node") or "") or None


def channel_with_highest(metrics: dict, field: str) -> dict:
    channels = metrics.get("per_channel") or []
    if not channels:
        raise RuntimeError("capture contains no audio channels")
    return max(channels, key=lambda item: float(item[field]))


def verdict(idle: dict, active: dict) -> dict:
    idle_channel = channel_with_highest(idle, "rms_dbfs")
    active_channel = channel_with_highest(active, "tone_dbfs")
    level_margin = float(active_channel["rms_dbfs"]) - float(idle_channel["rms_dbfs"])
    tone_margin = float(active_channel["tone_dbfs"]) - float(idle_channel["rms_dbfs"])
    signal_reasons: list[str] = []
    if level_margin < 10.0:
        signal_reasons.append(f"active level margin is only {level_margin:.1f} dB")
    if tone_margin < 10.0:
        signal_reasons.append(f"tone margin is only {tone_margin:.1f} dB")
    # Do not gate fixture presence on tone SNR. A real speaker/room/microphone path can
    # add harmonics, reverberation and transmitter processing while still proving exactly
    # what this preflight needs to prove: audible output reached the Lark. Tone SNR stays
    # in the captured metrics for diagnosis; level and narrow-band margins own presence.

    safety_reasons: list[str] = []
    if float(active_channel["peak_dbfs"]) > -0.1:
        safety_reasons.append("recorded peak is at full scale")
    if float(active_channel["clipped_pct"]) > 0.01:
        safety_reasons.append(
            f"recording clipped {float(active_channel['clipped_pct']):.4f}%"
        )

    state = "ready"
    exit_code = 0
    if signal_reasons:
        state = "speaker-not-detected"
        exit_code = 78
    elif safety_reasons:
        state = "unsafe-level"
        exit_code = 1
    return {
        "verdict": state,
        "exit_code": exit_code,
        "level_margin_db": round(level_margin, 2),
        "tone_margin_db": round(tone_margin, 2),
        "signal_reasons": signal_reasons,
        "safety_reasons": safety_reasons,
        "idle": idle_channel,
        "active": active_channel,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--output", help="explicit PipeWire output node")
    parser.add_argument("--tone", type=float, default=1000.0)
    parser.add_argument("--tone-dbfs", type=float, default=-30.0)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    supervisor = load_module(
        "speaker_preflight_supervisor", REPO / "pi" / "bridged" / "bridge_supervisor.py"
    )
    levels = load_module("speaker_preflight_levels", REPO / "rig" / "analysis" / "wav_level.py")
    nodes = supervisor.pw_nodes()
    if nodes is None:
        raise SystemExit("PipeWire graph unavailable")
    settings = supervisor.load_settings()
    lark = supervisor.find_lark(nodes, settings)
    output = selected_output(supervisor, args.output)
    if lark is None:
        raise SystemExit("Lark is absent")
    if output is None or output not in nodes:
        print(json.dumps({"verdict": "speaker-not-detected", "reason": "selected output is absent"}))
        return 78
    if settings.hfp_source in nodes or settings.hfp_sink in nodes:
        raise SystemExit("phone call audio is present; run the fixture check before the call")
    if supervisor.AEC_SOURCE in nodes or supervisor.AEC_SINK in nodes:
        raise SystemExit("AEC nodes are present; fixture checks must bypass AEC")

    stimulus = args.out / "stimulus.wav"
    idle_path = args.out / "idle-lark.wav"
    active_path = args.out / "active-lark.wav"
    subprocess.run(
        [
            sys.executable,
            str(REPO / "tools" / "audio" / "tone_gen.py"),
            "--mode",
            "sine",
            "--freq",
            str(args.tone),
            "--seconds",
            "2",
            "--rate",
            "48000",
            "--channels",
            "1",
            "--dbfs",
            str(args.tone_dbfs),
            "--out",
            str(stimulus),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )

    record_lark(supervisor, lark, idle_path, 2.0)
    record_while_playing(supervisor, lark, output, stimulus, active_path)
    idle = levels.analyse(str(idle_path), None, skip_start=0.25)
    active = levels.analyse(
        str(active_path), args.tone, skip_start=0.75, search_hz=5.0
    )
    result = {
        "fixture_check": True,
        "benchmark_evidence": False,
        "output": output,
        "lark": lark,
        "tone_hz": args.tone,
        "tone_dbfs": args.tone_dbfs,
        **verdict(idle, active),
    }
    (args.out / "preflight.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, indent=2))
    return int(result["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
