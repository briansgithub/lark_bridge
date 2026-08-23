#!/usr/bin/env python3
"""Run an A2DP dropout measurement on the Pi and score it on this PC.

    python rig/dropoutctl.py run --duration 600 --label orientation-a
    python rig/dropoutctl.py score --dir artifacts/a2dp-<label>-<stamp>

The number this exists to produce is **dropouts per minute**, because that is the form of
the bar Mode 1 has always been gated on (PLAN.md 14.4, restated in E03):

    <1 dropout/minute over 60 minutes with zero SCO drops

WHY THE SPLIT
-------------
Capture runs on the Pi because that is where the audio is. Scoring runs here because
glitch_detect.py is unhurried by design -- its own docstring puts a 20 s clip at "a couple
of minutes on a Pi 3" -- so scoring a 60-minute run on the Pi would take longer than the
run and would load the very graph under measurement.

WHAT A "DROPOUT" IS HERE
------------------------
A glitch found by glitch_detect.py in a captured segment of a steady tone. That is a
proxy for "a human hears a gap", and the proxy is the point: E03 had to ask an operator to
count missing pips by ear for an hour, which does not scale and cannot be re-run for a
regression. Two detectors run; `hp_burst` is the one that is meaningful on this analog leg
(`step` is documented as meaningless on a noisy analog capture, so it is reported but not
used for the verdict).
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

RIG_ROOT = Path(__file__).resolve().parent
REPO_ROOT = RIG_ROOT.parent
GLITCH = RIG_ROOT / "analysis" / "glitch_detect.py"

# E03's acceptance bar, restated so the verdict is computed rather than eyeballed.
BAR_DROPOUTS_PER_MIN = 1.0


def inventory(key: str, default: str = "") -> str:
    path = RIG_ROOT / "inventory.toml"
    if not path.exists():
        return default
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0]
        if "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() == key:
            return value.strip().strip('"')
    return default


def pi_host() -> str:
    return inventory("pi_host", "larkbridge")


def pi(command: str, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", pi_host(),
            f"export XDG_RUNTIME_DIR=/run/user/$(id -u); {command}",
        ],
        capture_output=True, text=True, timeout=timeout, check=False,
    )


def fetch(remote_dir: str, local_dir: Path) -> None:
    """Stream the segment directory back as a tarball.

    rsync is absent on Windows, which is why pi_sync in rig/lib/common.sh streams a
    tarball too; this is the same trick in the other direction.
    """
    local_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as handle:
        archive = Path(handle.name)
    try:
        with archive.open("wb") as out:
            result = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", pi_host(),
                 f"tar -cz -C {remote_dir} ."],
                stdout=out, stderr=subprocess.PIPE, check=False, timeout=900,
            )
        if result.returncode != 0:
            raise SystemExit(f"fetch failed: {result.stderr.decode(errors='replace')}")
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(local_dir, filter="data")
    finally:
        archive.unlink(missing_ok=True)


def score_segment(wav: Path, tone: float) -> dict:
    result = subprocess.run(
        [sys.executable, str(GLITCH), str(wav), "--tone", str(tone), "--json"],
        capture_output=True, text=True, check=False, timeout=600,
    )
    if result.returncode != 0:
        return {"error": result.stderr.strip()[:200]}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return {"error": f"unparseable glitch_detect output: {exc}"}


def glitch_count(report: dict) -> int | None:
    """Pull the hp_burst count out of glitch_detect's report, whatever it calls it.

    Deliberately tolerant: the schema is not pinned by a contract, and returning None so
    the caller can say "could not score" beats inventing a zero, which would read as a
    clean run.
    """
    if not isinstance(report, dict) or "error" in report:
        return None
    # glitch_detect emits {"channels":[{"hp_burst_count":N,...}]}. Only hp_burst is used:
    # its own docstring says the `step` detector is "meaningless on a noisy analog one",
    # and this capture is an analog line-out into a mic input.
    channels = report.get("channels")
    if isinstance(channels, list) and channels:
        total = 0
        seen = False
        for channel in channels:
            value = channel.get("hp_burst_count") if isinstance(channel, dict) else None
            if isinstance(value, int):
                total += value
                seen = True
        if seen:
            return total
    return None


def do_run(args: argparse.Namespace) -> int:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    local = Path(args.out or REPO_ROOT / "artifacts" / f"a2dp-{args.label}-{stamp}")
    remote_out = f"/tmp/rig/a2dp-dropouts/{args.label}"

    env = (
        f"DURATION={args.duration} SEG={args.seg} LABEL={args.label} "
        f"A2DP_MAC={args.a2dp_mac} TONE={args.tone} OUTDIR={remote_out}"
    )
    print(f"[ .. ] capturing {args.duration}s on {pi_host()} (label={args.label})", file=sys.stderr)
    result = pi(
        f"cd ~/rpi-lark-bridge && {env} bash rig/pi/measure/a2dp-dropouts.sh",
        timeout=args.duration + 300,
    )
    manifest_text = result.stdout.strip().splitlines()[-1:] or [""]
    try:
        manifest = json.loads(manifest_text[0])
    except json.JSONDecodeError:
        print(result.stdout, file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        raise SystemExit("capture did not emit a manifest")
    if "error" in manifest:
        print(json.dumps(manifest, indent=2))
        return 78

    print(f"[ .. ] fetching {manifest['segments']} segments", file=sys.stderr)
    fetch(remote_out, local)
    (local / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return do_score(argparse.Namespace(dir=str(local), tone=args.tone))


def do_score(args: argparse.Namespace) -> int:
    local = Path(args.dir)
    manifest = json.loads((local / "manifest.json").read_text(encoding="utf-8"))
    tone = float(manifest.get("tone_hz", args.tone))

    link: dict[int, dict] = {}
    jsonl = local / "segments.jsonl"
    if jsonl.exists():
        for line in jsonl.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entry = json.loads(line)
                link[int(entry["index"])] = entry

    segments = sorted(local.glob("seg-*.wav"))
    print(f"[ .. ] scoring {len(segments)} segments", file=sys.stderr)

    rows: list[dict] = []
    for wav in segments:
        index = int(wav.stem.split("-")[1])
        report = score_segment(wav, tone)
        count = glitch_count(report)
        state = link.get(index, {})
        rows.append({
            "index": index,
            "dropouts": count,
            "transport_before": state.get("transport_before"),
            "transport_after": state.get("transport_after"),
            "connected": state.get("connected"),
            "sco_delta": state.get("sco_delta"),
            "raw": report if count is None else None,
        })
        print(f"       seg {index:04d}: dropouts={count} transport={state.get('transport_after')}",
              file=sys.stderr)

    scored = [r for r in rows if r["dropouts"] is not None]
    unscored = [r for r in rows if r["dropouts"] is None]
    seg_s = float(manifest.get("segment_s", 30))
    minutes = (len(scored) * seg_s) / 60.0 if scored else 0.0
    total = sum(int(r["dropouts"]) for r in scored)
    rate = (total / minutes) if minutes > 0 else None

    # SCO must never be the casualty -- E03 found it held nominal in every run, so a drop
    # here would be a genuinely new finding rather than a worse version of a known one.
    sco_deltas = [r["sco_delta"] for r in scored if isinstance(r.get("sco_delta"), int)]
    sco_stalled = [r["index"] for r in scored
                   if manifest.get("sco_active") and r.get("sco_delta") == 0]
    transport_left_active = [r["index"] for r in rows
                             if r.get("transport_after") not in (None, "active")]

    summary = {
        "label": manifest.get("label"),
        "a2dp_hci": manifest.get("a2dp_hci"),
        "call_hci": manifest.get("call_hci"),
        "same_controller": manifest.get("same_controller"),
        "sco_active": manifest.get("sco_active"),
        "elapsed_s": manifest.get("elapsed_s"),
        "requested_s": manifest.get("requested_s"),
        "completed_full_duration": manifest.get("elapsed_s", 0) >= manifest.get("requested_s", 1),
        "segments_scored": len(scored),
        "segments_unscored": len(unscored),
        "analysed_minutes": round(minutes, 2),
        "total_dropouts": total,
        "dropouts_per_minute": round(rate, 3) if rate is not None else None,
        "bar_dropouts_per_minute": BAR_DROPOUTS_PER_MIN,
        "meets_bar": (rate is not None and rate < BAR_DROPOUTS_PER_MIN
                      and not transport_left_active and not sco_stalled),
        "worst_segment_dropouts": max((int(r["dropouts"]) for r in scored), default=None),
        "median_segment_dropouts": (statistics.median(int(r["dropouts"]) for r in scored)
                                    if scored else None),
        "segments_with_transport_not_active": transport_left_active,
        "segments_with_sco_stalled": sco_stalled,
        "sco_delta_median": statistics.median(sco_deltas) if sco_deltas else None,
        "a2dp_controller_alive": manifest.get("a2dp_controller_alive"),
        "call_controller_alive": manifest.get("call_controller_alive"),
        "artifacts": str(local),
    }
    (local / "summary.json").write_text(
        json.dumps({"summary": summary, "segments": rows}, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="capture on the Pi, then score here")
    run.add_argument("--duration", type=int, default=600)
    run.add_argument("--seg", type=int, default=30)
    run.add_argument("--label", default="run")
    run.add_argument("--a2dp-mac", default="50:D7:1B:74:34:D6")
    run.add_argument("--tone", type=float, default=1000.0)
    run.add_argument("--out")
    run.set_defaults(func=do_run)

    score = sub.add_parser("score", help="re-score an already fetched run")
    score.add_argument("--dir", required=True)
    score.add_argument("--tone", type=float, default=1000.0)
    score.set_defaults(func=do_score)

    args = parser.parse_args()
    if shutil.which("ssh") is None:
        raise SystemExit("ssh is required on the control PC")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
