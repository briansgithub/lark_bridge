#!/usr/bin/env python3
"""Measure what an output switch costs a live call. Runs ON THE PI, emits JSON.

    output_switch_probe.py --to a2dp:C9:5C:FD:6E:28:46 --seconds 45

THE QUESTION
------------
Changing the output rebuilds the call graph, and CallGraph.teardown() stops
`bridge.mic` as well as `bridge.callout`. So switching where the far end is heard also
drops the path carrying the USER'S OWN VOICE. That is acceptable if it is a second and
unacceptable if it is ten, and the difference cannot be judged by ear -- E13 spent a round
trip guessing at exactly this class of gap before it was measured properly.

So this measures two independent gaps around one switch:

  uplink   output.bridge.mic -> the phone's HFP sink. The far end hearing the user.
  downlink the chosen output node receiving anything at all.

WHY IT DRIVES THE SWITCH ITSELF
-------------------------------
The switch happens at a known offset inside the sampling window rather than being triggered
from another shell. Otherwise the interesting moment lands at an unknown time and the gap has
to be inferred from a log, which is how you end up reporting the sampler's own latency as the
product's.

Sampling is pw-link, not the status file: the supervisor publishes at POLL_SECONDS (2 s),
which is coarser than the thing being measured, and would quantise a 1 s gap to either 0 or 2.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "pi" / "bridged"))

import bridge_supervisor as supervisor  # noqa: E402


def links() -> list[tuple[str, str]]:
    return supervisor.pw_links() or []


def uplink_up(current: list[tuple[str, str]], hfp_sink: str) -> bool:
    """True when the microphone path reaches the phone.

    Matched on the loopback's OUTPUT side rather than its input, because the input side
    exists as soon as pw-loopback starts while the output side only exists once the link to
    the phone is actually made -- and the far end hears nothing until then.
    """
    return any(source == "output.bridge.mic" and target == hfp_sink for source, target in current)


def downlink_up(current: list[tuple[str, str]], output_node: str | None) -> bool:
    if not output_node:
        return False
    return any(target == output_node for _source, target in current)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--to", required=True, help="output id to switch to")
    parser.add_argument("--seconds", type=float, default=45.0)
    parser.add_argument("--switch-at", type=float, default=10.0)
    parser.add_argument("--interval", type=float, default=0.2)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    settings = supervisor.load_settings()
    hfp_sink = settings.hfp_sink

    # Refuse a no-op. The first run of this probe measured a 0.0 s gap and it was not a
    # result: the output was ALREADY the target, so write_desire() changed nothing, nothing
    # rebuilt, and the probe faithfully reported no interruption. A measurement that cannot
    # distinguish "no gap" from "no switch" is worse than no measurement, so the distinction
    # is enforced here rather than left to whoever remembers.
    try:
        opening = json.loads(settings.status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        opening = {}
    already = ((opening.get("output") or {}).get("chosen") or {}).get("id")
    if already == args.to:
        print(
            json.dumps(
                {
                    "error": "already on the target output; nothing would switch",
                    "chosen": already,
                    "hint": "select a different output first, e.g. bridgectl output set wired",
                },
                indent=2,
            )
        )
        return 78
    if (opening.get("state") or "") != "ACTIVE":
        print(
            json.dumps(
                {
                    "error": "no live call to measure",
                    "state": opening.get("state"),
                    "hint": "a switch only costs an uplink gap while a call is up",
                },
                indent=2,
            )
        )
        return 78
    started_from = already

    samples: list[dict] = []
    switched_at: float | None = None
    start = time.monotonic()

    while True:
        now = time.monotonic() - start
        if now >= args.seconds:
            break
        if switched_at is None and now >= args.switch_at:
            supervisor.write_desire(args.to, source="output_switch_probe")
            switched_at = now

        status = {}
        try:
            status = json.loads(settings.status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
        chosen = ((status.get("output") or {}).get("chosen") or {}).get("node")
        current = links()
        samples.append(
            {
                "t": round(now, 3),
                "uplink": uplink_up(current, hfp_sink),
                "downlink": downlink_up(current, chosen),
                "chosen": chosen,
                "state": status.get("state"),
            }
        )
        time.sleep(args.interval)

    def longest_gap(key: str, after: float) -> dict:
        """Longest contiguous run of False at or after `after`, in seconds."""
        worst = 0.0
        begin: float | None = None
        worst_start = None
        for sample in samples:
            if sample["t"] < after:
                continue
            if not sample[key]:
                if begin is None:
                    begin = sample["t"]
            elif begin is not None:
                if sample["t"] - begin > worst:
                    worst = sample["t"] - begin
                    worst_start = begin
                begin = None
        if begin is not None and samples and samples[-1]["t"] - begin > worst:
            worst = samples[-1]["t"] - begin
            worst_start = begin
        return {"seconds": round(worst, 2), "started_at": worst_start}

    before = [s for s in samples if switched_at is not None and s["t"] < switched_at]
    report = {
        "switch_from": started_from,
        "switch_to": args.to,
        "switched_at": round(switched_at, 2) if switched_at is not None else None,
        "duration_s": round(samples[-1]["t"], 2) if samples else 0,
        "interval_s": args.interval,
        "samples": len(samples),
        # A run whose uplink was already broken before the switch measures the fixture, not
        # the switch, so say so rather than quietly attributing it.
        "uplink_healthy_before_switch": all(s["uplink"] for s in before) if before else None,
        "uplink_gap": longest_gap("uplink", switched_at or 0.0),
        "downlink_gap": longest_gap("downlink", switched_at or 0.0),
        "final_chosen": samples[-1]["chosen"] if samples else None,
        "final_state": samples[-1]["state"] if samples else None,
        "chosen_changed_during_run": len({str(s["chosen"]) for s in samples}) > 1,
        "switch_took_effect": bool(
            samples
            and samples[-1]["chosen"]
            and samples[-1]["uplink"]
            and len({str(s["chosen"]) for s in samples}) > 1
        ),
    }
    # Print BEFORE writing: a 45 s run that fails on a missing directory should not throw the
    # measurement away, which is exactly what happened the first time this ran.
    print(json.dumps(report, indent=2))
    if args.out:
        try:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps({"report": report, "samples": samples}, indent=2))
        except OSError as exc:
            print(f"could not save samples to {args.out}: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
