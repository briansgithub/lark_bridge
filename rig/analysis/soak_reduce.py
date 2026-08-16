#!/usr/bin/env python3
"""Reduce a soak's samples.jsonl (stdin) to a verdict (stdout JSON).

    cat samples.jsonl | soak_reduce.py --nominal 133 --interval 5

A "stall" is a sample where the link is still connected but SCO packets stopped moving —
that is a silent audio dropout, and it is the failure mode this whole rig exists to catch.
Distinguished from a disconnect, which is visible and therefore much less dangerous.
"""

from __future__ import annotations

import argparse
import json
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--nominal", type=float, default=133.0, help="expected SCO packets/s each way")
    ap.add_argument("--interval", type=float, default=5.0, help="seconds between samples")
    a = ap.parse_args()

    samples, events = [], []
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        (events if "event" in o else samples).append(o)

    if not samples:
        json.dump({"verdict": "NO_DATA", "samples": 0}, sys.stdout, indent=2)
        print()
        return 1

    expected = a.nominal * a.interval
    floor = expected * 0.5          # below half nominal is a real degradation, not jitter

    connected = [s for s in samples if s.get("connected") == "yes"]
    stalls_rx = [s for s in connected if s["sco_rx_d"] == 0]
    stalls_tx = [s for s in connected if s["sco_tx_d"] == 0]
    degraded = [s for s in connected if 0 < s["sco_rx_d"] < floor or 0 < s["sco_tx_d"] < floor]
    disconnects = [s for s in samples if s.get("connected") not in ("yes", "unknown")]
    wedged = any(e.get("event") == "controller_wedged" for e in events)
    reassembly = sum(s.get("reassembly_d", 0) for s in samples)

    duration_s = (samples[-1]["t"] - samples[0]["t"]) if len(samples) > 1 else 0
    minutes = duration_s / 60.0 if duration_s else 0

    rx_rates = [s["sco_rx_d"] / a.interval for s in connected]
    tx_rates = [s["sco_tx_d"] / a.interval for s in connected]

    out = {
        "samples": len(samples),
        "duration_minutes": round(minutes, 2),
        "connected_samples": len(connected),
        "sco_rx_pps_mean": round(sum(rx_rates) / len(rx_rates), 1) if rx_rates else 0,
        "sco_tx_pps_mean": round(sum(tx_rates) / len(tx_rates), 1) if tx_rates else 0,
        "nominal_pps": a.nominal,
        "stalls_rx": len(stalls_rx),
        "stalls_tx": len(stalls_tx),
        "degraded_samples": len(degraded),
        "disconnect_samples": len(disconnects),
        "reassembly_errors": reassembly,
        "controller_wedged": wedged,
        "stalls_per_minute": round((len(stalls_rx) + len(stalls_tx)) / minutes, 2) if minutes else 0,
    }

    # Verdict, worst first. A wedge outranks everything: an appliance whose radio dies is
    # not merely degraded, it is broken until someone intervenes.
    if wedged:
        out["verdict"] = "FAIL_CONTROLLER_WEDGED"
        out["note"] = "controller stopped answering HCI; only a driver rebind recovers it"
    elif len(disconnects) > 0:
        out["verdict"] = "FAIL_DISCONNECTED"
    elif out["stalls_per_minute"] >= 1:
        out["verdict"] = "FAIL_STALLS"
        out["note"] = "link connected but SCO stopped moving - silent audio dropout"
    elif out["stalls_per_minute"] > 0 or degraded:
        out["verdict"] = "PARTIAL"
    else:
        out["verdict"] = "PASS"

    json.dump(out, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
