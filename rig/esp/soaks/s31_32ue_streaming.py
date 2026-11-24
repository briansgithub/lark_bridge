#!/usr/bin/env python3
"""Stream Pixel media through both boards to the bonded 32UE."""

import time

from soak_common import h2h5, hook, observe, parser, positive, require, run


def scenario(bench, args, metrics):
    peer = bench.command("output", "peers")["bt"]
    require(peer.get("connected") and peer.get("peer", "").lower() == args.sink_address.lower(),
            "the output board must be connected to the specified 32UE")
    try:
        hook(bench, args.media_hook, "start", args.seconds + 120)
        observe(bench, "output", h2h5.streaming, 60, "output never started streaming")
        bench.wait(10)
        bench.command("output", "fault fifo_window")
        start = len(bench.records)
        before = bench.pair("status")
        begin = time.monotonic()
        tap_before = bench.command("output", "tap a2dp_out")
        require(tap_before["ok"], "could not read a consistent downstream PCM tap")
        tap = tap_before
        while time.monotonic() < begin + args.seconds:
            bench.wait(min(60, max(0, begin + args.seconds - time.monotonic())))
            current = bench.command("output", "tap a2dp_out")
            require(current["ok"] and (current["nz_blocks"] - tap["nz_blocks"]) % 2**32 > 0,
                    "no new nonzero PCM blocks at the downstream A2DP tap")
            tap = current
        after = bench.pair("status")
        records = bench.records[start:]
        evidence = h2h5.Evidence(records)
        for role in ("s31", "output"):
            h2h5.coverage(evidence, records, before[role], after[role], role, 0.98)
        output = [before["output"]] + [
            r for r in records if r["role"] == "output" and r["t"] == "stats"
        ] + [after["output"]]
        source = [before["s31"]] + [
            r for r in records if r["role"] == "s31" and r["t"] == "stats" and
            before["s31"]["up_ms"] < r["up_ms"] < after["s31"]["up_ms"]
        ] + [after["s31"]]
        errors = h2h5.delta(before["output"], after["output"], ("under", "over", "i2s_err"), "a2dp")
        metrics.update(seconds=time.monotonic() - begin, **errors)
        metrics["nonzero_output_blocks"] = (tap["nz_blocks"] - tap_before["nz_blocks"]) % 2**32
        evidence.check(not any(errors.values()), "output audio errors increased")
        evidence.check(all(map(h2h5.streaming, output)), "downstream stream or route interrupted")
        evidence.check(all(r["session"]["a2dp"] for r in source), "upstream A2DP disconnected")
        evidence.check(all(
            b["up_ms"] - a["up_ms"] < 500 or
            (b["bt"]["a2dp_bytes"] - a["bt"]["a2dp_bytes"]) % 2**32
            >= 96 * (b["up_ms"] - a["up_ms"])
            for a, b in zip(source, source[1:])
        ), "an upstream PCM interval fell below half the nominal byte rate")
        verdict, _, reasons = evidence.verdict()
        return verdict, reasons
    finally:
        hook(bench, args.media_hook, "stop")


def main():
    cli = parser(__doc__)
    cli.add_argument("--sink-address", required=True, help="Bluetooth address of the bonded 32UE")
    cli.add_argument("--media-hook", required=True,
                     help="executable accepting start SECONDS or stop for continuous Pixel media")
    cli.add_argument("--seconds", type=positive, default=4 * 3600)
    args = cli.parse_args()
    return run("S31-32UE", args, args.seconds + 180, scenario)


if __name__ == "__main__":
    raise SystemExit(main())
