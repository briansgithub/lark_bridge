#!/usr/bin/env python3
"""Cycle the Pixel Bluetooth adapter and verify fail-closed profile recovery."""

import time

from soak_common import hook, observe, parser, positive, require, run, run_fixture


def profiles_up(record):
    bt = record["bt"]
    return bt["hfp_state"] == 3 and bt["a2dp_state"] == 2 and bt["avrcp"]


def profiles_down(record):
    bt = record["bt"]
    return (bt["hfp_state"] == 0 and bt["a2dp_state"] == 0 and
            not bt["avrcp"] and record["route"]["effective"] == 0)


def adapter(bench, args, enabled):
    if args.adapter_hook:
        hook(bench, args.adapter_hook, "on" if enabled else "off")
    else:
        run_fixture(bench, [args.adb, "-s", args.pixel_serial, "shell", "svc", "bluetooth",
                            "enable" if enabled else "disable"])


def scenario(bench, args, metrics):
    metrics["cycles"] = []
    observe(bench, "s31", profiles_up, args.recovery_seconds, "baseline profiles are down")
    saved = bench.command("s31", "peers")["bt"]["peer"]
    for cycle in range(1, args.cycles + 1):
        try:
            adapter(bench, args, False)
            observe(bench, "s31", profiles_down, args.recovery_seconds,
                    f"cycle {cycle} did not reach silence with all profiles down")
            observe(bench, "output", lambda r: not r["audio"], args.recovery_seconds,
                    f"cycle {cycle} did not revoke downstream audio")
            bench.wait(args.off_seconds)
            require(profiles_down(bench.command("s31", "stats")),
                    f"cycle {cycle} profiles came back during the off interval")
        finally:
            enabled_at = time.monotonic()
            adapter(bench, args, True)
        recovered = observe(bench, "s31", profiles_up, args.recovery_seconds,
                            f"cycle {cycle} did not recover HFP, A2DP and AVRCP")
        require(bench.command("s31", "peers")["bt"]["peer"] == saved,
                f"cycle {cycle} changed the saved phone identity")
        bench.wait(args.settle_seconds)
        require(profiles_up(bench.command("s31", "stats")),
                f"cycle {cycle} did not stay connected")
        metrics["cycles"].append({"cycle": cycle, "recovery_seconds": recovered["host"] - enabled_at})
    return "PASS", []


def main():
    cli = parser(__doc__)
    fixture = cli.add_mutually_exclusive_group(required=True)
    fixture.add_argument("--adapter-hook", help="executable accepting off or on")
    fixture.add_argument("--adb", help="ADB executable for the built-in Pixel adapter commands")
    cli.add_argument("--pixel-serial", help="required with --adb")
    cli.add_argument("--cycles", type=positive, default=10)
    cli.add_argument("--off-seconds", type=positive, default=12)
    cli.add_argument("--recovery-seconds", type=positive, default=60)
    cli.add_argument("--settle-seconds", type=positive, default=5)
    args = cli.parse_args()
    if args.adb and not args.pixel_serial:
        cli.error("--adb requires --pixel-serial")
    ttl = args.cycles * (90 + args.off_seconds + 3 * args.recovery_seconds + args.settle_seconds)
    return run("H14-adapter", args, ttl, scenario)


if __name__ == "__main__":
    raise SystemExit(main())
