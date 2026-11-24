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


def s31_stats(path):
    for line in complete_lines(path):
        match = SERIAL_LINE.fullmatch(line)
        if not match or not match[2].startswith("{"):
            continue
        try:
            record = json.loads(match[2])
        except ValueError:
            continue
        if record.get("t") == "stats" and record.get("role", "s31") == "s31":
            yield utc(match[1]), record


def median(values):
    ordered = sorted(values)
    if not ordered:
        return None
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def s31_audio(path, gap_fraction=0.5):
    """A2DP stream state, receive-rate gaps and resets from the S31 test-image stats records."""
    stats = list(s31_stats(path))
    if not stats:
        return None
    drops, disconnects, resets, rates = [], [], 0, []
    for (before_utc, before), (now_utc, now) in zip(stats, stats[1:]):
        if now.get("up_ms", 0) < before.get("up_ms", 0):
            resets += 1
            continue
        if before.get("session", {}).get("a2dp") and not now.get("session", {}).get("a2dp"):
            drops.append({"utc": now_utc})
        if before.get("bt", {}).get("a2dp_state") == 2 and now.get("bt", {}).get("a2dp_state") != 2:
            disconnects.append({"utc": now_utc, "state": now.get("bt", {}).get("a2dp_state")})
        seconds = (now_utc - before_utc).total_seconds()
        old, new = before.get("bt", {}).get("a2dp_bytes"), now.get("bt", {}).get("a2dp_bytes")
        streaming = before.get("session", {}).get("a2dp") and now.get("session", {}).get("a2dp")
        if streaming and seconds > 0 and isinstance(old, int) and isinstance(new, int):
            rates.append((before_utc, seconds, ((new - old) % 2**32) / seconds))
    typical = median([rate for *_, rate in rates])
    gaps, current = [], None
    for start, seconds, rate in rates:
        if typical and rate < gap_fraction * typical:
            if current is None:
                current = {"start_utc": start, "seconds": 0.0}
                gaps.append(current)
            current["seconds"] += seconds
        else:
            current = None
    heap = [r["heap"]["min"] for _, r in stats if isinstance(r.get("heap", {}).get("min"), int)]
    i2s = [r.get("i2s", {}) for _, r in stats]
    return {
        "stats_records": len(stats), "first_utc": stats[0][0], "last_utc": stats[-1][0],
        "streaming_records": sum(bool(r.get("session", {}).get("a2dp")) for _, r in stats),
        "a2dp_stream_drops": drops, "a2dp_disconnects": disconnects, "uptime_resets": resets,
        "median_rx_bytes_per_s": typical, "rx_gaps": gaps,
        "rx_gap_seconds": sum(gap["seconds"] for gap in gaps),
        "heap_min": min(heap) if heap else None,
        "i2s_under_max": max((x.get("under", 0) for x in i2s), default=0),
        "i2s_over_max": max((x.get("over", 0) for x in i2s), default=0),
    }


def harness_records(path):
    for line in complete_lines(path):
        line = line.strip()
        if line.startswith("{"):
            try:
                yield json.loads(line)
            except ValueError:
                continue


def pixel_audio(path, stall_fraction=0.5):
    """Underruns, playback stalls and routing from the Pixel harness stimulus_* lines."""
    if not path.is_file():
        return None
    players, events = {}, []
    for record in harness_records(path):
        kind = record.get("event", "")
        if not kind.startswith("stimulus_"):
            continue
        stamp = datetime.fromtimestamp(record["ts_ms"] / 1000, timezone.utc)
        tag = record.get("tag")
        if kind == "stimulus_start":
            players[tag] = {"rate": record.get("rate") or 0, "last": None, "underruns": 0, "played": 0}
            continue
        player = players.setdefault(tag, {"rate": 0, "last": None, "underruns": 0, "played": 0})
        player["underruns"] = max(player["underruns"], record.get("underruns") or 0)
        played = record.get("frames_played")
        if kind == "stimulus_progress":
            events.append((stamp, tag, played, (record.get("routed_device") or {}).get("type")))
        elif kind in ("stimulus_end", "stimulus_error"):
            player["ended"] = {"utc": stamp, "event": kind, "state": record.get("state"),
                               "detail": record.get("detail")}
        if isinstance(played, int):
            player["played"] = max(player["played"], played)
    stalls = []
    for stamp, tag, played, _ in events:
        player = players[tag]
        if player["last"] is not None and isinstance(played, int) and player["rate"]:
            last_stamp, last_played = player["last"]
            expected = player["rate"] * (stamp - last_stamp).total_seconds()
            if expected and played - last_played < stall_fraction * expected:
                stalls.append({"start_utc": last_stamp, "end_utc": stamp, "tag": tag,
                               "frames_played": played - last_played, "frames_expected": expected})
        if isinstance(played, int):
            player["last"] = (stamp, played)
    return {
        "progress_records": len(events),
        "first_utc": events[0][0] if events else None, "last_utc": events[-1][0] if events else None,
        "underruns": sum(p["underruns"] for p in players.values()),
        "played_seconds": sum(p["played"] / p["rate"] for p in players.values() if p["rate"]),
        "playback_stalls": stalls,
        "non_a2dp_progress": sum(device != "BLUETOOTH_A2DP" for *_, device in events),
        "endings": [p["ended"] for p in players.values() if "ended" in p],
    }


def audio_report(part):
    s31, pixel = s31_audio(part / "s31-serial.log"), pixel_audio(part / "harness.log")
    if s31 is None and pixel is None:
        return None
    return {"s31": s31, "pixel": pixel}

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
        audio = audio_report(part)
        if audio is not None:
            spans[-1][3]["audio"] = audio

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
