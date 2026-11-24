#!/usr/bin/env python3
"""Read bt_soak evidence directories and print an interim JSON report to stdout."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SERIAL_LINE = re.compile(r"^\[([^\]]+)\] (.*)$")
ROUTE_TIMEOUT = re.compile(r"\bs31_link: route ACK timed out\b", re.I)
FAULTS = {
    "brownout": re.compile(r"brownout", re.I),
    "panic": re.compile(r"guru meditation|panic(?:'ed)?\b|abort\(\) was called", re.I),
    "reset": re.compile(r"\brst:0x|\bESP-ROM:|\bboot:0x", re.I),
}


def complete_lines(path):
    """Ignore a trailing line that the live logger may still be writing."""
    if path.is_file():
        with path.open(encoding="utf-8", errors="replace") as source:
            for line in source:
                if line.endswith("\n"):
                    yield line.rstrip("\n")


def records(path):
    for line in complete_lines(path):
        if line.strip():
            yield json.loads(line)


def utc(value):
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError(f"expected timestamp with UTC offset: {value}")
    return stamp.astimezone(timezone.utc)


def pixel_event_time(value, observed, zone):
    """Pixel history supplies local month/day and milliseconds, without a year."""
    year = observed.astimezone(zone).year
    candidates = []
    for candidate_year in (year - 1, year, year + 1):
        try:
            local = datetime.strptime(f"{candidate_year} {value}", "%Y %m-%d %H:%M:%S.%f")
        except ValueError:
            continue
        candidates.extend(local.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)
                          for fold in (0, 1))
    if not candidates:
        raise ValueError(f"invalid Pixel ACL event time: {value}")
    # History is collected after the event. Prefer the closest occurrence already
    # observed, allowing for the logger rounding its UTC timestamp to seconds.
    return min(candidates, key=lambda stamp: (
        (stamp - observed).total_seconds() >= 1, abs(stamp - observed)
    ))


def acl_intervals(points):
    """Return the longest supported up span and completed timed ACL reconnects."""
    longest = 0.0
    connected_since = None
    disconnected_at = None
    reconnects = []
    for stamp, explicit, connected in sorted(points):
        if connected:
            if explicit and disconnected_at is not None:
                reconnects.append({
                    "disconnected_utc": disconnected_at,
                    "connected_utc": stamp,
                    "seconds": (stamp - disconnected_at).total_seconds(),
                })
            disconnected_at = None
            if connected_since is None:
                connected_since = stamp
        else:
            if explicit and disconnected_at is None:
                disconnected_at = stamp
            if not explicit and disconnected_at is None:
                # A poll detects the state, with no exact disconnect timestamp.
                connected_since = None
        if connected_since is not None:
            longest = max(longest, (stamp - connected_since).total_seconds())
        if not connected:
            connected_since = None
    return longest, reconnects


def summarize(parts, pixel_zone):
    part_reports, spans, fault_markers = [], [], []
    history = {}
    for part in parts:
        if not part.is_dir():
            raise ValueError(f"missing evidence directory: {part}")
        events = list(records(part / "events.jsonl"))
        stamps = [utc(record["utc"]) for record in events]
        polls = {}
        for sample in records(part / "samples.jsonl"):
            stamp = utc(sample["utc"])
            stamps.append(stamp)
            polls[stamp] = sample.get("acl_bredr")
        for event in events:
            if event["kind"] != "pixel_acl":
                continue
            state = event["state"].lower()
            if state in ("connected", "disconnected"):
                observed = utc(event["utc"])
                stamp = pixel_event_time(event["pixel_time"], observed, pixel_zone)
                key = (stamp, state, event["reason"])
                if key not in history or observed < history[key]["observed_utc"]:
                    history[key] = {
                        "utc": stamp, "pixel_time": event["pixel_time"], "state": state,
                        "reason": event["reason"], "observed_utc": observed,
                    }
        timeouts = 0
        for line in complete_lines(part / "s31-serial.log"):
            match = SERIAL_LINE.fullmatch(line)
            if not match:
                continue
            stamp, message = utc(match[1]), match[2]
            stamps.append(stamp)
            if ROUTE_TIMEOUT.search(message):
                timeouts += 1
                continue
            for kind, pattern in FAULTS.items():
                if pattern.search(message):
                    fault_markers.append({
                        "part": part.name, "utc": stamp, "kind": kind, "line": message,
                    })
        if not stamps:
            raise ValueError(f"no complete timestamped evidence in {part}")
        start, end = min(stamps), max(stamps)
        spans.append((start, end, polls, {
            "part": part.name, "start_utc": start, "end_utc": end,
            "span_seconds": (end - start).total_seconds(),
            "acl_polls": len(polls), "acl_up_polls": sum(up is True for up in polls.values()),
            "route_ack_timeouts": timeouts,
        }))

    acl_events = [event for _, event in sorted(history.items())]
    covered, longest, reconnects = 0.0, 0.0, []
    spans.sort(key=lambda span: span[:2])
    previous_end = spans[0][0]
    for start, end, polls, report in spans:
        covered += max(0.0, (end - max(start, previous_end)).total_seconds())
        previous_end = max(previous_end, end)
        points = [(stamp, False, up) for stamp, up in polls.items() if isinstance(up, bool)]
        points.extend((event["utc"], True, event["state"] == "connected")
                      for event in acl_events if start <= event["utc"] <= end)
        span, recovered = acl_intervals(points)
        report["longest_observed_acl_seconds"] = span
        part_reports.append(report)
        longest = max(longest, span)
        reconnects.extend(r for r in recovered if r not in reconnects)
    for event in acl_events:
        event["in_capture_window"] = any(start <= event["utc"] <= end for start, end, *_ in spans)
    elapsed = (previous_end - part_reports[0]["start_utc"]).total_seconds()
    return {
        "elapsed_seconds": elapsed, "covered_seconds": covered, "gap_seconds": elapsed - covered,
        "parts": part_reports, "pixel_timezone": str(pixel_zone),
        "acl_events": acl_events, "longest_observed_acl_seconds": longest,
        "acl_reconnects": reconnects, "serial_fault_markers": fault_markers,
        "route_ack_timeouts": sum(part["route_ack_timeouts"] for part in part_reports),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("parts", type=Path, nargs="+", help="part directories containing bt_soak logs")
    parser.add_argument("--pixel-timezone", required=True, help="IANA time zone of the Pixel log clock")
    args = parser.parse_args()
    try:
        report = summarize(args.parts, ZoneInfo(args.pixel_timezone))
    except (KeyError, OSError, ValueError, ZoneInfoNotFoundError) as error:
        parser.exit(1, f"{parser.prog}: {error}\n")
    print(json.dumps(report, default=datetime.isoformat, indent=2))


if __name__ == "__main__":
    main()
