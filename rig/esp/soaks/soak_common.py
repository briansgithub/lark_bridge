"""Shared capture and verdict handling for the end-of-project soaks."""

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import h2h5
import hil


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return number


def parser(description):
    result = argparse.ArgumentParser(description=description)
    result.add_argument("--s31-port", required=True)
    result.add_argument("--output-port", required=True)
    result.add_argument("--out", type=Path, required=True, help="new evidence directory")
    return result


def hook(bench, executable, *args, timeout=30):
    """Keep servicing the consoles while a fixture command is running."""
    command = [executable, *map(str, args)]
    bench.add(t="hook", command=command)
    with (bench.directory / "hooks.log").open("a", encoding="utf-8") as log:
        log.write(f"{command!r}\n")
        log.flush()
        with subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT) as process:
            deadline = time.monotonic() + timeout
            try:
                while process.poll() is None:
                    if time.monotonic() >= deadline:
                        raise subprocess.TimeoutExpired(command, timeout)
                    bench.wait(0.1)
                if process.returncode:
                    raise subprocess.CalledProcessError(process.returncode, command)
            finally:
                if process.poll() is None:
                    process.kill()


def observe(bench, role, predicate, seconds, failure):
    """Require an observed state from periodic stats within the deadline."""
    start = len(bench.records)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        bench.wait(0.1)
        for record in bench.records[start:]:
            if record["t"] == "stats" and record["role"] == role and predicate(record):
                return record
        start = len(bench.records)
    raise h2h5.BenchError(failure)


def require(condition, message):
    if not condition:
        raise h2h5.BenchError(message)


def interrupted(signum, _frame):
    raise KeyboardInterrupt(f"interrupted by signal {signum}")


def run(name, args, seconds, scenario):
    out = args.out
    out.mkdir(parents=True, exist_ok=False)
    lease = hil.default_lease_path()
    owner = f"soak-{name}-{os.getpid()}"
    metrics = {}
    reasons = ["soak did not complete"]
    verdict = "FAIL"
    acquired = False
    bench = None
    original_handler = signal.signal(signal.SIGTERM, interrupted)
    try:
        hil.acquire_lease(lease, owner, ttl=seconds + 600, renew=False)
        acquired = True
        bench = h2h5.Bench((args.s31_port, args.output_port), out, hil.arrival._quiet_serial_factory)
        for role, status in bench.pair("status").items():
            require(status.get("image") == "test" and status.get("erase_bonds") == 0,
                    f"{role} requires a test image with bond erase disabled")
        reducer_verdict, reasons = scenario(bench, args, metrics)
        metrics["scenario_verdict"] = reducer_verdict
        verdict = "PASS" if reducer_verdict == "PASS" else "FAIL"
    except (Exception, KeyboardInterrupt) as error:
        reasons = [str(error) or type(error).__name__]
    finally:
        try:
            if bench is not None:
                bench.close()
        except OSError as error:
            verdict = "FAIL"
            reasons.append(f"console close failed: {error}")
        finally:
            try:
                if acquired:
                    hil.release_lease(lease, owner)
            except (OSError, hil.HilError) as error:
                verdict = "FAIL"
                reasons.append(f"lease release failed: {error}")
            finally:
                summary = dict(soak=name, verdict=verdict, metrics=metrics, reasons=reasons)
                temp = out / "summary.json.tmp"
                temp.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
                temp.replace(out / "summary.json")
                signal.signal(signal.SIGTERM, original_handler)
    return 0 if verdict == "PASS" else 2
