"""Hardware scenarios and replayable evidence for LarkBridge H2 through H5."""

import array
import json
import math
import subprocess
import sys
import time
import wave

from logic_decode import decode_i2s

ROLES = ("s31", "output")
FAULTS = ("uart_crc", "uart_cobs", "uart_version", "uart_overlong")
CLEAN = ("dec_err", "proto_err", "rel_fail", "timeouts", "sessions")
TAPS = ("i2s_out", "i2s_in")


class BenchError(RuntimeError):
    pass


class Bench:
    def __init__(self, ports, directory, quiet_serial):
        import serial

        self.directory = directory
        self.open_port = quiet_serial(serial.Serial)
        self.hardware = identify_ports(ports)
        self.serial_error = serial.SerialException
        self.ports = dict(zip(ROLES, ports))
        self.connections = {}
        self.pending = dict.fromkeys(ROLES, b"")
        self.records = []
        self.restart = None
        self.boots = dict.fromkeys(ROLES, 0)
        self.last_uptime = {}
        self.log = (directory / "bench.jsonl").open("w", encoding="utf-8")
        try:
            for role in ROLES:
                self.connect(role)
        except BaseException:
            self.close()
            raise

    def connect(self, role):
        identify_ports((self.ports[role],), (self.hardware[role],))
        connection = self.open_port(port=self.ports[role], baudrate=115200, timeout=0)
        connection.write_timeout = 2
        self.connections[role] = connection
        self.pending[role] = b""

    def close(self):
        for connection in self.connections.values():
            connection.close()
        self.log.close()

    def add(self, **record):
        record["host"] = time.monotonic()
        self.records.append(record)
        self.log.write(json.dumps(record) + "\n")
        self.log.flush()

    def poll(self):
        for role in ROLES:
            try:
                if role not in self.connections:
                    self.connect(role)
                connection = self.connections[role]
                raw = self.pending[role] + connection.read(connection.in_waiting)
            except (OSError, self.serial_error) as error:
                if role != self.restart:
                    raise BenchError(f"{role} console disappeared: {error}") from error
                connection = self.connections.pop(role, None)
                if connection is not None:
                    connection.close()
                continue
            *lines, self.pending[role] = raw.split(b"\n")
            for line in lines:
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeError):
                    if line.strip():
                        self.add(role=role, t="console", line=line.decode(errors="replace"))
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("role") != role:
                    raise BenchError(f"unexpected identity on {self.ports[role]}: {record.get('role')}")
                self.add(**record)
                uptime = record.get("up_ms")
                restarted = record.get("t") == "boot" or (
                    uptime is not None and uptime < self.last_uptime.get(role, 0) - 1000
                )
                if restarted:
                    self.boots[role] += 1
                    if role != self.restart or self.boots[role] > 1:
                        raise BenchError(f"unexpected boot or reset loop on {role}")
                    self.last_uptime.pop(role, None)
                if uptime is not None:
                    self.last_uptime[role] = uptime

    def wait(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.poll()
            time.sleep(0.03)
        self.poll()

    def command(self, role, command, timeout=4):
        name, *args = command.split()
        self.poll()
        self.add(t="sent", role=role, command=command)
        start = len(self.records)
        self.connections[role].write((command + "\n").encode("ascii"))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.wait(0.05)
            for record in self.records[start:]:
                if record["role"] != role:
                    continue
                kind = record["t"]
                if kind == "resp" and record.get("cmd") == name:
                    if record.get("ok") is not True:
                        raise BenchError(f"{role} rejected {command}: {record}")
                    return record
                if kind == name and (name != "tap" or record.get("stage") == args[0]):
                    return record
            start = len(self.records)
        raise BenchError(f"{role} did not answer {command}")

    def pair(self, command):
        return {role: self.command(role, command) for role in ROLES}

    def wait_link(self, seconds, check=lambda pair: True):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if all(role in self.connections for role in ROLES):
                pair = self.pair("status")
                if all(alive(pair[role]) for role in ROLES) and check(pair):
                    return pair
            self.wait(0.5)
        raise BenchError("both UART endpoints did not recover before the deadline")

    def measure(self, phase, seconds):
        before = self.pair("status")
        self.add(t="start", phase=phase, data=before)
        self.wait(seconds)
        self.add(t="end", phase=phase, data=self.pair("status"))

    def analyzer(self, driver, phase, rate):
        path = self.directory / f"{phase}.bin"
        command = ["sigrok-cli", "-d", driver, "--config", f"samplerate={rate}",
                   "-C", "0,1,2", "--samples", str(rate // 4), "-O", "binary"]
        with path.open("wb") as output, (self.directory / f"{phase}-analyzer.log").open("wb") as log:
            process = subprocess.Popen(command, stdout=output, stderr=log)
            deadline = time.monotonic() + 30
            try:
                while process.poll() is None and time.monotonic() < deadline:
                    self.wait(0.1)
                if process.poll() != 0 or path.stat().st_size != rate // 4:
                    raise BenchError(f"analyzer failed to capture {phase}")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()


def identify_ports(ports, expected=None):
    from serial.tools import list_ports

    devices = {p.device.upper(): p for p in list_ports.comports()}
    fingerprints = {}
    for i, port in enumerate(ports):
        role = ROLES[i]
        device = devices.get(port.upper())
        if device is None:
            raise OSError(f"{port} is absent")
        identifier = (device.vid, device.pid, device.serial_number, device.location)
        if expected is None:
            valid = identifier[:2] == ((0x303A, 0x1001), (0x10C4, 0xEA60))[i]
        else:
            valid = identifier == expected[i]
        if not valid:
            raise BenchError(f"unknown USB device at {port}: {device.hwid}")
        fingerprints[role] = identifier
    return fingerprints


def alive(record):
    return record["uart"]["peer" if record["role"] == "s31" else "alive"] is True


def other(role):
    return "output" if role == "s31" else "s31"


def tone(bench, routed=False):
    stage = "routed_i2s" if routed else "i2s_out"
    bench.command("s31", f"tone {stage} 1000 -20")


def run_scenario(bench, gate, limits, analyzer=None, fake_car_address=None):
    bench.wait_link(limits["link_seconds"])
    if gate == "H2":
        bench.command("s31", "fault test_silence")
        try:
            bench.wait(2)
            bench.measure("clean", limits["clean_seconds"])
            for role in ROLES:
                for fault in FAULTS + (("uart_dup",) if role == "s31" else ()):
                    peer = other(role)
                    phase = f"{role}/{fault}"
                    bench.add(t="start", phase=phase, data=bench.command(peer, "status"))
                    bench.command(role, f"fault {fault}")
                    bench.wait(1)
                    bench.add(t="end", phase=phase, data=bench.command(peer, "status"))
            bench.measure("post", limits["post_seconds"])
        finally:
            bench.command("s31", "fault test_off")
    elif gate == "H3":
        baseline = bench.pair("peers")
        bench.add(t="peers_before", data=baseline)
        try:
            for role in ROLES:
                for fault in ("uart_mute", "reboot"):
                    tone(bench, routed=True)
                    bench.command("s31", "fault test_a2dp")
                    bench.wait(2)
                    pair = bench.pair("status")
                    audio = bench.command("output", "tap a2dp_out")
                    phase = f"{role}/{fault}"
                    bench.add(t="start", phase=phase, data=pair, audio=audio)
                    if fault == "reboot":
                        bench.boots[role] = 0
                        bench.restart = role
                    bench.command(role, f"fault {fault}")
                    if fault == "uart_mute":
                        bench.wait(1.8)
                        bench.add(t="muted", phase=phase, data=bench.command("output", "status"))
                        bench.wait(2.2)
                    else:
                        bench.wait(4)
                    # A new session is sufficient to prove recovery even when the
                    # reboot completed before the peer's heartbeat timeout.
                    recovered = bench.wait_link(
                        limits["recovery_seconds"] - 4,
                        lambda pair: fault == "uart_mute" or (
                            pair[other(role)]["uart"]["sessions"] >
                            start_sessions(bench.records, phase, other(role))
                        ),
                    )
                    bench.restart = None
                    tone(bench, routed=True)
                    bench.command("s31", "fault test_a2dp")
                    bench.wait(2)
                    bench.add(t="end", phase=phase, data=recovered,
                              audio=bench.command("output", "tap a2dp_out"),
                              audio_status=bench.command("output", "status"),
                              peers=bench.pair("peers"))
        finally:
            if bench.restart is None:
                bench.command("s31", "tone off")
                bench.command("s31", "fault test_off")
    elif gate == "H4":
        bench.command("s31", "fault test_silence")
        try:
            tone(bench)
            bench.wait(1)
            taps = lambda: {role: bench.command(role, f"tap {stage}")
                            for role, stage in zip(ROLES, TAPS)}
            bench.add(t="taps", phase="tone", data=taps())
            if analyzer:
                bench.analyzer(analyzer, "tone", limits["analyzer_rate"])
            bench.command("s31", "tone off")
            bench.wait(1)
            bench.add(t="start", phase="silence", data=taps())
            if analyzer:
                bench.analyzer(analyzer, "silence", limits["analyzer_rate"])
            bench.wait(limits["silence_seconds"])
            bench.add(t="end", phase="silence", data=taps())
        finally:
            bench.command("s31", "tone off")
            bench.command("s31", "fault test_off")
    else:
        output = bench.command("output", "peers")
        if not fake_car_address:
            raise BenchError("supply the fake-car sink address with --fake-car-address")
        if (not output["bt"].get("connected") or
                output["bt"].get("peer", "").lower() != fake_car_address.lower()):
            raise BenchError("pair and connect the DevKitC to the specified fake-car Bluetooth sink")
        try:
            tone(bench, routed=True)
            bench.command("s31", "fault test_a2dp")
            bench.wait(limits["warmup_seconds"])
            if not streaming(bench.command("output", "status")):
                raise BenchError("DevKitC is not streaming to its bonded sink")
            bench.command("output", "fault fifo_window")
            bench.measure("stream", limits["stream_seconds"])
        finally:
            bench.command("s31", "tone off")
            bench.command("s31", "fault test_off")


def start_sessions(records, phase, role):
    return next(r["data"][role]["uart"]["sessions"] for r in records
                if r["t"] == "start" and r["phase"] == phase)


def streaming(record):
    return record["audio"] and record["bt"]["connected"] and record["bt"]["streaming"]


class Evidence:
    def __init__(self, records):
        self.records = records
        self.failures = []
        self.missing = []
        self.partial = []
        self.metrics = {}

    def check(self, condition, reason):
        if not condition:
            self.failures.append(reason)

    def mark(self, kind, phase):
        return next(r for r in self.records if r["t"] == kind and r.get("phase") == phase)

    def window(self, phase):
        begin = self.mark("start", phase)
        end = self.mark("end", phase)
        return begin, end, [r for r in self.records if begin["host"] <= r["host"] <= end["host"]]

    def verdict(self):
        if self.failures:
            return "FAIL", self.metrics, self.failures + self.missing + self.partial
        if self.missing:
            return "INCONCLUSIVE", self.metrics, self.missing + self.partial
        if self.partial:
            return "PARTIAL", self.metrics, self.partial
        return "PASS", self.metrics, []


def delta(before, after, keys, group="uart"):
    return {key: after[group][key] - before[group][key] for key in keys}


def coverage(e, rows, before, after, role, minimum):
    span = (after["up_ms"] - before["up_ms"]) / 1000
    period = before["stats_period_ms"] / 1000
    samples = {r["up_ms"] for r in rows if r["t"] == "stats" and r["role"] == role}
    actual = len(samples) / max(1, math.floor(span / period))
    if actual < minimum:
        e.missing.append(f"{role} captured {actual:.1%} of expected periodic stats")
    return span


def reduce_h2(e, limits):
    for phase in ("clean", "post"):
        begin, end, rows = e.window(phase)
        for role in ROLES:
            before, after = (mark["data"][role] for mark in (begin, end))
            span = coverage(e, rows, before, after, role, limits["coverage"])
            changes = delta(before, after, CLEAN + ("hb_rx",))
            e.metrics[f"{phase}/{role}"] = changes
            expected = span * 2
            e.check(span >= limits[f"{phase}_seconds"] and
                    abs(changes["hb_rx"] - expected) <= max(2, expected * limits["hb_tolerance"]),
                    f"{phase} {role} heartbeat count or window length outside limits")
            e.check(not any(changes[key] for key in CLEAN), f"{phase} {role} UART errors or session loss")
            e.check(after["uart"]["hb_gap_max_ms"] <= limits["hb_gap_ms"],
                    f"{phase} {role} heartbeat gap exceeded limit")
            e.check(all(alive(r) for r in [before, after] + [
                r for r in rows if r["t"] == "stats" and r["role"] == role
            ]), f"{phase} {role} link dropped")
    for role in ROLES:
        for fault in FAULTS + (("uart_dup",) if role == "s31" else ()):
            phase = f"{role}/{fault}"
            begin, end, rows = e.window(phase)
            before, after = begin["data"], end["data"]
            changes = delta(before, after, CLEAN + ("exec", "dup"))
            e.metrics[phase] = changes
            expected = dict.fromkeys(changes, 0)
            if fault == "uart_dup":
                expected.update(exec=1, dup=2)
            else:
                expected.update(dec_err=1, proto_err=1)
                probes = [r["probe_seq"] for r in rows if r.get("ev") == "inject" and r["role"] == role
                          and r["kind"] == FAULTS.index(fault) + 1]
                recoveries = [r["seq"] for r in rows if r.get("ev") == "rx_recover" and
                              r["role"] == other(role) and r["type"] == 2]
                e.check(len(probes) == 1 and recoveries == probes, f"{phase} probe did not recover")
            e.check(changes == expected and alive(before) and alive(after),
                    f"{phase} receiver counters or link state outside limits")


def reduce_h3(e, limits):
    peers = e.mark("peers_before", None)["data"]
    bonds = {role: peers[role]["bt"].get("peer") for role in ROLES}
    if not all(bonds.values()):
        e.partial.append("Both boards need saved Bluetooth peers to verify bond preservation")
    for role in ROLES:
        for fault in ("uart_mute", "reboot"):
            phase = f"{role}/{fault}"
            begin, end, rows = e.window(phase)
            peer = other(role)
            before, after = (mark["data"][peer] for mark in (begin, end))
            change = delta(before, after, ("sessions", "timeouts"))
            sent = next(r for r in rows if r["t"] == "sent" and r["command"] == f"fault {fault}")
            recovery = after["host"] - sent["host"]
            timeouts = [r["hb_age_ms"] for r in rows if r.get("ev") == "hb_timeout" and r["role"] == peer]
            e.metrics[phase] = {"recover_seconds": recovery, "timeout_ms": timeouts, **change}
            e.check(alive(after) and 0 <= recovery <= limits["recovery_seconds"],
                    f"{phase} recovery exceeded deadline")
            if fault == "uart_mute":
                e.check(change == {"sessions": 0, "timeouts": 1} and len(timeouts) == 1,
                        f"{phase} expected one timeout in the original session")
            else:
                e.check(change["sessions"] == 1, f"{phase} peer did not accept exactly one new session")
            e.check(all(0 < age <= limits["mute_max_ms"] for age in timeouts),
                    f"{phase} heartbeat mute took too long")
            e.check({r: end["peers"][r]["bt"].get("peer") for r in ROLES} == bonds,
                    f"{phase} saved peer changed")
            if not (begin["audio"]["ok"] and begin["audio"]["rms_l"] > 0 and
                    streaming(begin["data"]["output"]) and end["audio"]["ok"] and
                    end["audio"]["rms_l"] > 0 and streaming(end["audio_status"])):
                e.partial.append(f"{phase} needs a live audio sink before and after the fault")
            elif fault == "uart_mute":
                e.check(not e.mark("muted", phase)["data"]["audio"], f"{phase} output audio stayed enabled")
            elif not timeouts:
                e.partial.append(f"{phase} reboot recovered before the heartbeat timeout")


def reduce_h4(e, limits, artifacts):
    tone_taps = e.mark("taps", "tone")["data"]
    begin, end, _ = e.window("silence")
    for role in ROLES:
        tone = tone_taps[role]
        e.metrics[role] = {key: tone[key] for key in ("rms_l", "rms_r")}
        e.check(tone["ok"] and all(abs(tone[key] - target) <= target * limits["tap_tolerance"]
                                  for key, target in (("rms_l", 2317), ("rms_r", 1158))),
                f"{role} tone tap RMS outside tolerance")
        first, last = (mark["data"][role] for mark in (begin, end))
        seconds = last["host"] - first["host"]
        frames = (last["blocks"] - first["blocks"]) * last["bytes"] / 4
        e.metrics[role]["silence_frames"] = frames
        e.check(first["ok"] and last["ok"] and frames >= 48000 * seconds * limits["coverage"] and
                last["nz_blocks"] == first["nz_blocks"] and last["rms_l"] == last["rms_r"] == 0
                and seconds >= limits["silence_seconds"],
                f"{role} tap did not show continuous silence")
    captures = {path.name: path for path in artifacts if path.suffix == ".bin"}
    if not {"tone.bin", "silence.bin"} <= captures.keys():
        e.partial.append("Attach a sigrok analyzer to verify frame rate, slot width and channel alignment")
        return
    from rig.analysis.glitch_detect import step

    for phase in ("tone", "silence"):
        try:
            pcm = decode_i2s(captures[f"{phase}.bin"].read_bytes(), limits["analyzer_rate"])
        except ValueError as error:
            e.failures.append(f"{phase} analyzer: {error}")
            continue
        e.check(pcm.slot_widths == {16}, f"{phase} I2S slot widths {sorted(pcm.slot_widths)}")
        e.check(abs(pcm.frame_hz / 48000 - 1) <= limits["sample_rate_ppm"] / 1e6,
                f"{phase} I2S frame rate {pcm.frame_hz:.2f} Hz")
        if phase == "silence":
            e.check(not any(pcm.left) and not any(pcm.right), "nonzero I2S wire samples during silence")
            continue
        crossings = [i - a / (b - a) for i, (a, b) in enumerate(zip(pcm.left, pcm.left[1:]))
                     if a <= 0 < b]
        freq = (len(crossings) - 1) * pcm.frame_hz / (crossings[-1] - crossings[0]) if len(crossings) > 1 else 0
        peak = max(map(abs, pcm.left))
        glitches = step([sample / 32768 for sample in pcm.left], round(pcm.frame_hz), 1000, 1)
        e.metrics["wire_tone"] = {"sample_hz": pcm.frame_hz, "tone_hz": freq, "peak": peak,
                                  "step_events": len(glitches)}
        e.check(abs(freq - 1000) <= limits["tone_hz_tolerance"] and
                abs(peak - 3277) <= 3277 * limits["amplitude_tolerance"] and not glitches,
                "wire tone frequency, amplitude or continuity outside tolerance")
        e.check(all(r == -int(l / 2) for l, r in zip(pcm.left, pcm.right)),
                "wire right channel is not the inverted half-amplitude left channel")


def score_recording(e, path, limits):
    from rig.analysis.wav_level import goertzel

    with wave.open(str(path), "rb") as wav:
        rate, channels = wav.getframerate(), wav.getnchannels()
        if wav.getsampwidth() != 2 or channels != 2:
            raise ValueError("fake-car recording must be stereo S16LE PCM")
        if wav.getnframes() < limits["stream_seconds"] * rate:
            e.missing.append("fake-car recording is shorter than the FIFO measurement")
            return
        amplitudes = []
        segment_frames = limits["recording_segment_seconds"] * rate
        for start in range(0, limits["stream_seconds"] * rate, segment_frames):
            # Probe the center of each segment. A 100 ms window tolerates
            # the clock drift allowed by the FIFO gate.
            wav.setpos(min(start + segment_frames // 2, wav.getnframes() - rate // 10))
            samples = array.array("h", wav.readframes(rate // 10))
            if sys.byteorder != "little":
                samples.byteswap()
            amplitudes.append(goertzel([value / 32768 for value in samples[::2]], rate, 1000))
        e.metrics["recording_tone_amplitude"] = amplitudes
        e.check(all(amp >= limits["recording_min_amplitude"] for amp in amplitudes),
                "one or more fake-car recording segments lack the 1 kHz tone")


def reduce_h5(e, limits, artifacts):
    begin, end, rows = e.window("stream")
    before, after = (mark["data"]["output"] for mark in (begin, end))
    span = coverage(e, rows, before, after, "output", limits["coverage"])
    rows = [before] + [r for r in rows if r["t"] == "stats" and r["role"] == "output"] + [after]
    low = min(min(r["fifo"]["min"], r["fifo"]["level"]) for r in rows)
    high = max(max(r["fifo"]["max"], r["fifo"]["level"]) for r in rows)
    ppm = max(abs(r["fifo"]["ppm"]) for r in rows)
    changes = delta(before, after, ("under", "over", "i2s_err"), group="a2dp")
    e.metrics.update(fifo_min=low, fifo_max=high, max_abs_ppm=ppm, seconds=span, **changes)
    e.check(span >= limits["stream_seconds"], "FIFO measurement window too short")
    e.check(limits["fifo_low"] <= low <= high <= limits["fifo_high"], "FIFO excursion outside band")
    e.check(ppm < limits["ppm_limit"], "clock correction reached or exceeded the saturation limit")
    e.check(not any(changes.values()), "new underrun, overrun or I2S error")
    e.check(all(streaming(r) for r in rows), "audio stream interrupted during the measurement")
    recording = next((path for path in artifacts if path.suffix.lower() == ".wav"), None)
    if recording is not None:
        try:
            score_recording(e, recording, limits)
        except (wave.Error, EOFError) as error:
            e.missing.append(f"invalid fake-car recording: {error}")


def reduce(gate, limits, records, artifacts=()):
    evidence = Evidence(records)
    errors = [r["reason"] for r in records if r["t"] == "abort"]
    if errors:
        target = evidence.failures if any("reset loop" in x or "did not recover" in x for x in errors) else evidence.missing
        target.extend(errors)
    try:
        if gate in ("H4", "H5"):
            {"H4": reduce_h4, "H5": reduce_h5}[gate](evidence, limits, artifacts)
        else:
            {"H2": reduce_h2, "H3": reduce_h3}[gate](evidence, limits)
    except (KeyError, StopIteration, TypeError, ValueError) as error:
        evidence.missing.append(f"incomplete {gate} evidence ({type(error).__name__}: {error})")
    return evidence.verdict()
