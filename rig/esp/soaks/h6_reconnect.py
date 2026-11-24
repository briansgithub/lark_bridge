#!/usr/bin/env python3
"""Score 100 fake-Lark USB reconnects and optional P9 fixture exercises."""

import json
from pathlib import Path
import time

from soak_common import hil, hook, observe, parser, positive, require, run


def usb_cycle(bench, executable, cycle, seconds):
    before = bench.command("s31", "status")["uac"]
    require(before["started"], "Lark capture is stopped before reconnect")
    hook(bench, executable, cycle)
    after = observe(
        bench, "s31",
        lambda r: r["uac"]["started"] and r["uac"]["opens"] > before["opens"] and
        (r["uac"]["rx"] - before["rx"]) % 2**32 > 0,
        seconds, f"USB cycle {cycle} did not resume capture")["uac"]
    require(after["disc"] == before["disc"] + 1 and after["opens"] == before["opens"] + 1,
            f"USB cycle {cycle} should report exactly one disconnect and reopen")


def scenario(bench, args, metrics):
    metrics["p7_recovered"] = 0
    metrics["p9_usb_recovered"] = 0
    metrics["p9_tx_completed"] = 0
    if args.p9_usb_hook:
        for cycle in range(1, 11):
            usb_cycle(bench, args.p9_usb_hook, cycle, args.recovery_seconds)
            metrics["p9_usb_recovered"] += 1
    if args.p9_tx_hook:
        for cycle in range(1, 6):
            before = bench.command("s31", "status")["uac"]
            hook(bench, args.p9_tx_hook, cycle)
            observe(bench, "s31", lambda r: r["uac"]["started"] and
                    r["uac"]["disc"] == before["disc"] and
                    (r["uac"]["rx"] - before["rx"]) % 2**32 > 0,
                    args.recovery_seconds, f"P9 transmitter cycle {cycle} disrupted USB capture")
            metrics["p9_tx_completed"] += 1
    if args.p7_setup_hook:
        hook(bench, args.p7_setup_hook)
    observe(bench, "s31", lambda r: r["uac"]["started"], args.recovery_seconds,
            "P7 fixture did not start UAC capture")
    start = len(bench.records)
    started = time.monotonic()
    bench.wait(2)
    bench.command("s31", "tap lark_in")
    for cycle in range(1, 101):
        usb_cycle(bench, args.p7_cycle_hook, cycle, args.recovery_seconds)
        metrics["p7_recovered"] += 1
    bench.wait(max(2, args.seconds - (time.monotonic() - started)))
    bench.command("s31", "tap lark_in")
    records = [r for r in bench.records[start:] if r.get("role") == "s31"]
    text = "\n".join(r["line"] if r["t"] == "console" else json.dumps(r) for r in records)
    boot = args.boot_log.read_text(encoding="utf-8", errors="replace") if args.boot_log else None
    verdict, scored, reasons = hil.reduce_h6_uac(
        text, boot_log=boot, min_seconds=args.seconds, span_seconds=time.monotonic() - started)
    metrics.update(scored)
    require(scored.get("uac_disconnects") == 100 and scored.get("uac_reopens") == 100,
            "P7 capture must show exactly 100 disconnects and reopens")
    return verdict, reasons


def main():
    cli = parser(__doc__)
    cli.add_argument("--p7-cycle-hook", required=True, help="fake-Lark USB replug executable, takes cycle number")
    cli.add_argument("--p7-setup-hook", help="optional fixture handover executable, takes no arguments")
    cli.add_argument("--p9-usb-hook", help="optional USB replug fixture executable, takes cycle number")
    cli.add_argument("--p9-tx-hook", help="optional transmitter off/on executable, takes cycle number")
    cli.add_argument("--boot-log", type=Path, help="S31 log with the initial UAC enumeration")
    cli.add_argument("--seconds", type=positive, default=1800)
    cli.add_argument("--recovery-seconds", type=positive, default=20)
    args = cli.parse_args()
    return run("H6", args, args.seconds + 115 * (45 + args.recovery_seconds), scenario)


if __name__ == "__main__":
    raise SystemExit(main())
