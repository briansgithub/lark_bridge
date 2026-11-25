#!/usr/bin/env python3
"""Interactive P9 runner for H6: 10 real-Lark USB receiver replugs + 5 transmitter off/on cycles.

Watches S31 JSONL telemetry on COM3, verifies exact disc/opens/rx/lost counters per cycle,
and writes raw console logs + summary.json to the output directory.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
import serial


def read_status(ser: serial.Serial, log_fp, timeout: float = 5.0) -> dict:
    ser.write(b"status\n")
    ser.flush()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        raw = ser.readline()
        if not raw:
            continue
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        log_fp.write(f"{time.time():.3f} {line}\n")
        log_fp.flush()
        # Handle occasional concatenated stats+status JSON on one line
        for idx in [i for i, ch in enumerate(line) if ch == "{"]:
            try:
                obj = json.loads(line[idx:])
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and obj.get("t") in ("status", "stats") and "uac" in obj:
                return obj
    raise TimeoutError("Timed out waiting for S31 status/stats JSON")


def wait_for(ser: serial.Serial, log_fp, pred, timeout: float, label: str) -> dict:
    deadline = time.monotonic() + timeout
    last_poll = 0.0
    while time.monotonic() < deadline:
        now = time.monotonic()
        if now - last_poll >= 0.8:
            ser.write(b"status\n")
            ser.flush()
            last_poll = now
        raw = ser.readline()
        if not raw:
            continue
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        log_fp.write(f"{time.time():.3f} {line}\n")
        log_fp.flush()
        for idx in [i for i, ch in enumerate(line) if ch == "{"]:
            try:
                obj = json.loads(line[idx:])
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and obj.get("t") in ("status", "stats") and "uac" in obj:
                if pred(obj):
                    return obj
    raise TimeoutError(f"Timed out waiting for: {label}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM3")
    ap.add_argument("--out", type=Path, default=Path("../lark_bridge-evidence/20261125-h6-p9"))
    ap.add_argument("--usb-cycles", type=int, default=10)
    ap.add_argument("--tx-cycles", type=int, default=5)
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    log_path = args.out / "s31_console.log"
    summary_path = args.out / "summary.json"

    results: dict = {
        "gate": "H6-P9",
        "port": args.port,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "usb_cycles": [],
        "tx_cycles": [],
        "verdict": "INCOMPLETE",
    }

    ser = serial.Serial()
    ser.port = args.port
    ser.baudrate = 115200
    ser.timeout = 0.25
    ser.dtr = False
    ser.rts = False
    ser.open()
    with ser, log_path.open("a", encoding="utf-8") as log_fp:
        print("\n=== Waiting for real Lark A1 receiver on S31 USB-A (uac.started=true) ===")
        st = wait_for(ser, log_fp, lambda o: o["uac"].get("started") is True, 120.0, "Lark UAC started")
        results["firmware_version"] = st.get("version")
        results["baseline_uac"] = st["uac"]
        print(f"OK! S31={st.get('version')} uac={st['uac']}")
        print("Make sure the Lark A1 TRANSMITTER is turned ON (solid link), then press Enter to start...")
        input()

        # Part 1: 10 USB receiver unplug/replug cycles
        print(f"\n=== Part 1: {args.usb_cycles} Lark USB receiver unplug/replug cycles ===")
        for cycle in range(1, args.usb_cycles + 1):
            before = read_status(ser, log_fp)["uac"]
            if not before["started"]:
                print("Waiting for UAC capture to be active before cycle...")
                before = wait_for(ser, log_fp, lambda o: o["uac"]["started"], 20.0, "UAC started")["uac"]
            print(f"\n[USB {cycle}/{args.usb_cycles}] UNPLUG the Lark receiver from S31 USB-A now...")
            t0 = time.monotonic()
            disc_st = wait_for(
                ser, log_fp,
                lambda o: (not o["uac"]["started"]) and o["uac"]["disc"] > before["disc"],
                60.0, f"USB cycle {cycle} disconnect"
            )["uac"]
            print(f"  Disconnect detected (disc={disc_st['disc']}). Wait 2s, then REPLUG the Lark receiver...")
            t_replug = time.monotonic()
            after = wait_for(
                ser, log_fp,
                lambda o: o["uac"]["started"]
                and o["uac"]["opens"] > before["opens"]
                and ((o["uac"]["rx"] - before["rx"]) % (2**32)) > 50,
                60.0, f"USB cycle {cycle} reconnect + audio rx"
            )["uac"]
            rec_s = round(time.monotonic() - t_replug, 2)
            ok = (
                after["disc"] == before["disc"] + 1
                and after["opens"] == before["opens"] + 1
                and after["lost"] == before["lost"]
                and after["read_err"] == before["read_err"]
                and after["xfer_err"] == before["xfer_err"]
            )
            entry = {
                "cycle": cycle,
                "ok": ok,
                "recovery_s": rec_s,
                "before": before,
                "after": after,
            }
            results["usb_cycles"].append(entry)
            status_txt = "PASS" if ok else "FAIL"
            print(
                f"  -> [USB {cycle}/{args.usb_cycles}] {status_txt} "
                f"(disc={after['disc']}, opens={after['opens']}, rx=+{after['rx']-before['rx']}, "
                f"lost={after['lost']}, errs={after['read_err']}/{after['xfer_err']}, {rec_s}s)"
            )
            if not ok:
                print("  WARNING: counter mismatch on this cycle!")
            time.sleep(2.0)

        # Part 2: 5 transmitter off/on cycles
        print(f"\n=== Part 2: {args.tx_cycles} Lark TRANSMITTER off/on cycles (receiver stays plugged in) ===")
        for cycle in range(1, args.tx_cycles + 1):
            before = read_status(ser, log_fp)["uac"]
            print(f"\n[TX {cycle}/{args.tx_cycles}] Turn Lark TRANSMITTER OFF, wait ~3s, turn it back ON, then press Enter...")
            input()
            time.sleep(2.0)
            after = wait_for(
                ser, log_fp,
                lambda o: o["uac"]["started"] and ((o["uac"]["rx"] - before["rx"]) % (2**32)) > 50,
                20.0, f"TX cycle {cycle} steady UAC rx"
            )["uac"]
            ok = (
                after["started"]
                and after["disc"] == before["disc"]
                and after["lost"] == before["lost"]
                and after["read_err"] == before["read_err"]
                and after["xfer_err"] == before["xfer_err"]
            )
            entry = {"cycle": cycle, "ok": ok, "before": before, "after": after}
            results["tx_cycles"].append(entry)
            print(
                f"  -> [TX {cycle}/{args.tx_cycles}] {'PASS' if ok else 'FAIL'} "
                f"(disc unchanged={after['disc'] == before['disc']}, rx=+{after['rx']-before['rx']}, lost={after['lost']})"
            )

        usb_pass = sum(1 for c in results["usb_cycles"] if c["ok"])
        tx_pass = sum(1 for c in results["tx_cycles"] if c["ok"])
        results["usb_passed"] = usb_pass
        results["tx_passed"] = tx_pass
        results["verdict"] = "PASS" if (usb_pass == args.usb_cycles and tx_pass == args.tx_cycles) else "FAIL"
        results["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        summary_path.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")

        print(f"\n=== P9 COMPLETE: {results['verdict']} (USB {usb_pass}/{args.usb_cycles}, TX {tx_pass}/{args.tx_cycles}) ===")
        print(f"Evidence saved to: {summary_path.resolve()}")
        return 0 if results["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
