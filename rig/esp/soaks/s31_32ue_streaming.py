#!/usr/bin/env python3
"""Stream Pixel media through both boards to the bonded 32UE."""

import time

from soak_common import h2h5, hook, observe, parser, positive, require, run, run_fixture


HARNESS = "dev.larkbridge.harness"


def media(bench, args, start):
    seconds = args.seconds + 120
    if args.media_hook:
        if start:
            hook(bench, args.media_hook, "start", seconds)
        else:
            hook(bench, args.media_hook, "stop")
        return

    shell = [args.adb, "-s", args.pixel_serial, "shell"]
    if start:
        run_fixture(bench, shell + ["am", "start", "-W", "-n", f"{HARNESS}/.MainActivity"])
    broadcast = shell + ["am", "broadcast", "-f", "0x20", "-n", f"{HARNESS}/.ControlReceiver"]
    if start:
        loops = (seconds + 2) // 3
        run_fixture(bench, broadcast + ["-a", f"{HARNESS}.PLAY_STIMULUS", "--es", "usage", "media",
                                        "--ei", "loops", str(loops), "--ei", "progress_s", "30"])
    else:
        run_fixture(bench, broadcast + ["-a", f"{HARNESS}.STOP_STIMULUS"])


def check_tap(previous, current):
    elapsed = current["host"] - previous["host"]
    frames = (current["nz_frames"] - previous["nz_frames"]) % 2**32
    # Allow tone zero crossings, loop boundaries and console timing jitter.
    require(current["ok"] and elapsed > 0 and
            0.75 * 44100 * elapsed <= frames <= 48000 * (elapsed + 0.5),
            "downstream nonzero PCM frame count outside the continuous-stimulus range")


def board_window(records, role, first, last):
    rows = [first] + [r for r in records if r["role"] == role and r["t"] == "stats"
                      and first["host"] < r["host"] < last["host"]] + [last]
    for a, b in zip(rows, rows[1:]):
        elapsed = b["host"] - a["host"]
        uptime = (b["up_ms"] - a["up_ms"]) / 1000
        require(uptime >= 0 and abs(uptime - elapsed) <= 1 + 0.01 * elapsed and
                b["uart"]["sessions"] == a["uart"]["sessions"],
                f"{role} uptime or UART session changed during streaming")
    return rows


def scenario(bench, args, metrics):
    peer = bench.command("output", "peers")["bt"]
    require(peer.get("connected") and peer.get("peer", "").lower() == args.sink_address.lower(),
            "the output board must be connected to the specified 32UE")
    try:
        media(bench, args, True)
        observe(bench, "output", h2h5.streaming, 60, "output never started streaming")
        bench.wait(10)
        bench.command("output", "fault fifo_window")
        status_pair = bench.pair("status")
        periods = {r: status_pair[r].get("stats_period_ms", 1000) for r in ("s31", "output")}
        start = len(bench.records)
        before = bench.pair("stats")
        for r, period in periods.items():
            before[r].setdefault("stats_period_ms", period)
        begin = time.monotonic()
        tap_before = bench.command("output", "tap a2dp_out")
        require(tap_before["ok"], "could not read a consistent downstream PCM tap")
        tap = tap_before
        while time.monotonic() < begin + args.seconds:
            bench.wait(min(60, max(0, begin + args.seconds - time.monotonic())))
            current = bench.command("output", "tap a2dp_out")
            check_tap(tap, current)
            tap = current
        after = bench.pair("stats")
        for r, period in periods.items():
            after[r].setdefault("stats_period_ms", period)
        records = bench.records[start:]
        evidence = h2h5.Evidence(records)
        for role in ("s31", "output"):
            h2h5.coverage(evidence, records, before[role], after[role], role, 0.98)
        output = board_window(records, "output", before["output"], after["output"])
        source = board_window(records, "s31", before["s31"], after["s31"])
        evidence.check(before["s31"]["i2s"]["write_err"] == after["s31"]["i2s"]["write_err"],
                       "source I2S write errors increased")
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
        media(bench, args, False)


def main():
    cli = parser(__doc__)
    cli.add_argument("--sink-address", required=True, help="Bluetooth address of the bonded 32UE")
    fixture = cli.add_mutually_exclusive_group(required=True)
    fixture.add_argument("--media-hook", help="executable accepting start SECONDS or stop")
    fixture.add_argument("--adb", help="ADB executable for the installed Pixel harness")
    cli.add_argument("--pixel-serial", help="required with --adb")
    cli.add_argument("--seconds", type=positive, default=4 * 3600)
    args = cli.parse_args()
    if args.adb and not args.pixel_serial:
        cli.error("--adb requires --pixel-serial")
    return run("S31-32UE", args, args.seconds + 180, scenario)


if __name__ == "__main__":
    raise SystemExit(main())
