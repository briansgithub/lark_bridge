#!/usr/bin/env python3
"""Run four qualification soaks under one rig lease and console capture."""

import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import time
from types import SimpleNamespace

import h5_long_drift
import h6_reconnect
import h14_adapter_cycles
import s31_32ue_streaming
from soak_common import h2h5, hil, interrupted, parser, require

LONG_SECONDS = 4 * 3600
UAC_SECONDS = 1800
USB_CYCLES = 100
ADAPTER_CYCLES = 10
ADAPTER_RECOVERY_SECONDS = 60
STAGES = ("H5", "H6", "H14-adapter", "S31-32UE")
ERROR_COUNTERS = ("under", "over", "i2s_err")
PREFLIGHT_CHECKS = ("ports", "test_images", "pixel_adb", "speaker_keep_alive", "fixtures")
RMS_MIN = 40
RMS_MAX = 175


def number(value):
    return type(value) is int or (type(value) is float and math.isfinite(value))


def metric_proof(stage, metrics):
    """Check independent completion markers alongside the scenario's verdict."""
    def at_least(key, minimum):
        value = metrics.get(key)
        return number(value) and value >= minimum

    def counters(expected):
        return all(type(metrics.get(key)) is int and metrics[key] == value
                   for key, value in expected.items())

    if stage == "H5":
        return (
            at_least("seconds", LONG_SECONDS)
            and at_least("fifo_min", 1024) and at_least("fifo_max", metrics["fifo_min"])
            and metrics["fifo_max"] <= 7168
            and at_least("max_abs_ppm", 0) and metrics["max_abs_ppm"] < 500
            and counters(dict.fromkeys(ERROR_COUNTERS, 0))
        )
    if stage == "H6":
        return at_least("capture_seconds", UAC_SECONDS) and counters({
            **dict.fromkeys(("p7_recovered", "uac_disconnects", "uac_reopens",
                             "uac_disconnect_lines"), USB_CYCLES),
            **dict.fromkeys(("uac_failure_lines", "uac_rx_gap_intervals", "reboot_markers",
                             "uac_xfer_err_delta", "uac_read_err_delta"), 0),
        })
    if stage == "H14-adapter":
        cycles = metrics.get("cycles")
        return isinstance(cycles, list) and len(cycles) == ADAPTER_CYCLES and all(
            isinstance(row, dict) and type(row.get("cycle")) is int and row["cycle"] == i
            and number(row.get("recovery_seconds"))
            and 0 <= row["recovery_seconds"] <= ADAPTER_RECOVERY_SECONDS + 30
            for i, row in enumerate(cycles, 1)
        )
    if stage == "S31-32UE":
        return (at_least("seconds", LONG_SECONDS) and at_least("nonzero_output_blocks", 1)
                and counters(dict.fromkeys(ERROR_COUNTERS, 0)))
    return False


def reduce_campaign(preflight, stages, fatal=None):
    reasons = []
    if fatal is not None:
        reasons.append(str(fatal))
    for check in PREFLIGHT_CHECKS:
        if preflight.get(check) is not True:
            reasons.append(f"preflight incomplete: {check}")

    names = [entry.get("soak") for entry in stages if isinstance(entry, dict)]
    if tuple(names) != STAGES or len(names) != len(stages):
        reasons.append("expected one result for each soak in campaign order")
    for entry in stages:
        if not isinstance(entry, dict):
            reasons.append("malformed soak result")
            continue
        name = entry.get("soak")
        if entry.get("verdict") != "PASS":
            reasons.append(f"{name}: {entry.get('verdict', 'missing verdict')}")
            continue
        metrics = entry.get("metrics")
        if entry.get("reasons") != [] or not isinstance(metrics, dict) or not metric_proof(name, metrics):
            reasons.append(f"{name}: PASS lacks consistent completion evidence")
    return {
        "verdict": "FAIL" if reasons else "PASS",
        "reasons": reasons, "preflight": preflight, "stages": stages,
    }


def write_summary(out, summary):
    lines = ["# End-of-project soak", "", f"Verdict **{summary['verdict']}**", "",
             "| Soak | Verdict |", "| --- | --- |"]
    results = {entry["soak"]: entry for entry in summary["stages"]}
    for name in STAGES:
        lines.append(f"| {name} | {results.get(name, {}).get('verdict', 'SKIPPED')} |")
    for reason in summary["reasons"]:
        lines.extend(("", f"- {reason}"))
    for entry in summary["stages"]:
        for reason in entry.get("reasons", []) + entry.get("notes", []):
            lines.extend(("", f"- {entry['soak']}. {reason}"))
    for filename, data in (
        ("summary.json", json.dumps(summary, indent=2, allow_nan=False) + "\n"),
        ("summary.md", "\n".join(lines) + "\n"),
    ):
        temp = out / f"{filename}.tmp"
        temp.write_text(data, encoding="utf-8")
        temp.replace(out / filename)


class CampaignBench(h2h5.Bench):
    """Keep one console session across stages and latch hardware stop conditions."""

    fatal = None
    next_usb_check = 0.0

    def poll(self):
        if self.fatal is not None:
            raise h2h5.BenchError(self.fatal)
        try:
            now = time.monotonic()
            if now >= self.next_usb_check:
                roles = h2h5.ROLES
                h2h5.identify_ports(tuple(self.ports[r] for r in roles),
                                    tuple(self.hardware[r] for r in roles))
                self.next_usb_check = now + 1
            super().poll()
        except (h2h5.BenchError, OSError) as error:
            self.fatal = str(error)
            raise

    def add(self, **record):
        super().add(**record)
        if record["t"] != "console":
            return
        line = record["line"]
        reset = record["role"] in self.last_uptime and hil.BOOT_MARKER_RE.search(line)
        unidentified_usb = any(message in line for message in (
            "Ignoring UAC microphone", "Could not identify UAC device",
        ))
        if reset or hil.CRASH_RE.search(line) or unidentified_usb:
            self.fatal = f"{record['role']}: {line}"
            raise h2h5.BenchError(self.fatal)


def preflight(bench, args, checks):
    checks["ports"] = True
    for executable in (args.p7_cycle_hook, args.p7_setup_hook, args.p9_usb_hook, args.p9_tx_hook):
        if executable is not None:
            require(shutil.which(executable) is not None, f"fixture executable not found: {executable}")
    checks["fixtures"] = True
    for role, status in bench.pair("status").items():
        require(status.get("image") == "test" and status.get("erase_bonds") == 0,
                f"{role} requires a test image with bond erase disabled")
    checks["test_images"] = True
    state = subprocess.run([args.adb, "-s", args.pixel_serial, "get-state"],
                           capture_output=True, text=True, timeout=10, check=True)
    require(state.stdout.strip() == "device", "Pixel adb transport is not online")
    checks["pixel_adb"] = True
    peer = bench.command("output", "peers")["bt"]
    require(peer.get("connected") and peer.get("peer", "").lower() == args.sink_address.lower(),
            "the specified speaker must already be paired and connected")
    require(h2h5.streaming(bench.command("output", "status")),
            "output is not streaming the keep-alive tone")
    first = bench.command("output", "tap a2dp_out")
    bench.wait(1)
    last = bench.command("output", "tap a2dp_out")
    require(all(tap["ok"] and RMS_MIN <= tap["rms_l"] <= RMS_MAX for tap in (first, last))
            and 950000 <= last["tone_mhz_l"] <= 1050000
            and (last["nz_blocks"] - first["nz_blocks"]) % 2**32 > 0,
            "speaker keep-alive needs fresh 1 kHz PCM near -50 dBFS")
    checks["speaker_keep_alive"] = True
    checks["speaker_keep_alive_rms"] = [last["rms_l"], last["rms_r"]]


def scenarios(args):
    return (
        (h5_long_drift.scenario, SimpleNamespace(
            seconds=LONG_SECONDS, fake_car_address=args.sink_address, recording=None)),
        (h6_reconnect.scenario, SimpleNamespace(
            seconds=UAC_SECONDS, recovery_seconds=20, p7_cycle_hook=args.p7_cycle_hook,
            p7_setup_hook=args.p7_setup_hook, p9_usb_hook=args.p9_usb_hook,
            p9_tx_hook=args.p9_tx_hook, boot_log=args.boot_log)),
        (h14_adapter_cycles.scenario, SimpleNamespace(
            cycles=ADAPTER_CYCLES, off_seconds=12, recovery_seconds=ADAPTER_RECOVERY_SECONDS,
            settle_seconds=5, adapter_hook=None, adb=args.adb, pixel_serial=args.pixel_serial)),
        (s31_32ue_streaming.scenario, SimpleNamespace(
            seconds=LONG_SECONDS, sink_address=args.sink_address, media_hook=None,
            adb=args.adb, pixel_serial=args.pixel_serial)),
    )


def run(args):
    args.out.mkdir(parents=True, exist_ok=False)
    checks = {}
    results = []
    fatal = None
    bench = None
    acquired = False
    owner = f"end-of-project-{os.getpid()}"
    lease = hil.default_lease_path()
    original_handler = signal.signal(signal.SIGTERM, interrupted)
    try:
        write_summary(args.out, reduce_campaign(checks, results, "campaign still running"))
        hil.acquire_lease(lease, owner, ttl=12 * 3600, renew=False)
        acquired = True
        bench = CampaignBench((args.s31_port, args.output_port), args.out, hil.arrival._quiet_serial_factory)
        preflight(bench, args, checks)
        for name, (scenario, options) in zip(STAGES, scenarios(args), strict=True):
            metrics = {}
            bench.add(t="stage", soak=name, phase="start")
            try:
                bench.poll()
                verdict, reasons = scenario(bench, options, metrics)
                bench.poll()
            except (Exception, KeyboardInterrupt) as error:
                verdict, reasons = "FAIL", [str(error) or type(error).__name__]
            if verdict == "PASS" and not metric_proof(name, metrics):
                verdict = "FAIL"
                reasons = ["stage completion evidence is missing or outside the campaign limits"]
            entry = dict(soak=name, verdict=verdict, metrics=metrics,
                         reasons=[] if verdict == "PASS" else reasons,
                         notes=reasons if verdict == "PASS" else [])
            results.append(entry)
            bench.add(t="stage", soak=name, phase="end", verdict=verdict)
            checkpoint = reduce_campaign(checks, results, bench.fatal or "campaign still running")
            write_summary(args.out, checkpoint)
            if verdict != "PASS":
                break
    except (Exception, KeyboardInterrupt) as error:
        fatal = str(error) or type(error).__name__
    finally:
        if bench is not None:
            fatal = bench.fatal or fatal
            try:
                bench.close()
            except Exception as error:
                fatal = fatal or f"console close failed: {error}"
        if acquired:
            try:
                require(hil.release_lease(lease, owner), "rig lease disappeared during campaign")
            except Exception as error:
                fatal = fatal or f"lease release failed: {error}"
        signal.signal(signal.SIGTERM, original_handler)
    summary = reduce_campaign(checks, results, fatal)
    write_summary(args.out, summary)
    return 0 if summary["verdict"] == "PASS" else 2


def main():
    cli = parser(__doc__)
    cli.add_argument("--sink-address", required=True, help="bonded speaker Bluetooth address")
    cli.add_argument("--adb", required=True, help="ADB executable")
    cli.add_argument("--pixel-serial", required=True)
    cli.add_argument("--p7-cycle-hook", required=True)
    cli.add_argument("--p7-setup-hook")
    cli.add_argument("--p9-usb-hook")
    cli.add_argument("--p9-tx-hook")
    cli.add_argument("--boot-log", type=Path)
    args = cli.parse_args()
    if args.s31_port.casefold() == args.output_port.casefold():
        cli.error("the two console ports must be distinct")
    if not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", args.sink_address):
        cli.error("--sink-address must be a Bluetooth address")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
