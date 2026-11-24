#!/usr/bin/env python3
"""Measure H5 FIFO drift with the existing H5 tone and reducer."""

from pathlib import Path

from soak_common import h2h5, hil, parser, positive, run


def scenario(bench, args, metrics):
    limits = dict(hil.load_gates()["H5"], stream_seconds=args.seconds)
    h2h5.run_scenario(bench, "H5", limits, fake_car_address=args.fake_car_address)
    artifacts = [args.recording] if args.recording else []
    verdict, scored, reasons = h2h5.reduce("H5", limits, bench.records, artifacts)
    metrics.update(scored)
    return verdict, reasons


def main():
    cli = parser(__doc__)
    cli.add_argument("--fake-car-address", required=True)
    cli.add_argument("--seconds", type=positive, default=4 * 3600)
    cli.add_argument("--recording", type=Path, help="completed fake-car WAV for the measured window")
    args = cli.parse_args()
    return run("H5", args, args.seconds + 180, scenario)


if __name__ == "__main__":
    raise SystemExit(main())
