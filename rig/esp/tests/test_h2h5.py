import math
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hil

h2h5 = hil.h2h5_module()
from logic_decode import decode_i2s


def samples_on_wire(frames):
    raw = bytearray()
    # At a WS change the previous slot's LSB is still on the wire.
    last_bit = 0
    for left, right in frames:
        for ws, value in enumerate((left, right)):
            bits = [(value >> bit) & 1 for bit in range(15, -1, -1)]
            for bit in [last_bit] + bits[:-1]:
                level = (ws << 1) | (bit << 2)
                raw.extend((level, level | 1))
            last_bit = bits[-1]
    return bytes(raw)


def tap(host, blocks, level):
    return {"host": host, "ok": True, "bytes": 960, "blocks": blocks, "nz_blocks": 100,
            "rms_l": 2317 if level else 0, "rms_r": 1158 if level else 0}


def stream(host, up, kind="status"):
    return {"host": host, "up_ms": up, "t": kind, "role": "output", "stats_period_ms": 1000,
            "audio": True, "bt": {"connected": True, "streaming": True},
            "fifo": {"min": 3800, "level": 4096, "max": 4300, "ppm": -15},
            "a2dp": {"under": 0, "over": 0, "i2s_err": 0}}


class TwoBoardTests(unittest.TestCase):
    def test_i2s_word_delay_sign_and_channel(self):
        frames = [(-32768, 16384), (999, -499), (-1025, 512), (0, 0)]
        pcm = decode_i2s(samples_on_wire(frames), 48000 * 64)
        self.assertEqual(list(zip(pcm.left, pcm.right)), frames[:-1])
        self.assertEqual(pcm.slot_widths, {16})
        self.assertEqual(pcm.frame_hz, 48000)

    def test_h4_analyzer_and_tap_verdicts(self):
        records = [
            {"t": "taps", "phase": "tone", "host": 1, "data": dict.fromkeys(h2h5.ROLES, tap(1, 100, True))},
            {"t": "start", "phase": "silence", "host": 2, "data": dict.fromkeys(h2h5.ROLES, tap(2, 200, False))},
            {"t": "end", "phase": "silence", "host": 12, "data": dict.fromkeys(h2h5.ROLES, tap(12, 2200, False))},
        ]
        limits = {**hil.load_gates()["H4"], "analyzer_rate": 48000 * 64}
        self.assertEqual(h2h5.reduce("H4", limits, records)[0], "PARTIAL")
        with tempfile.TemporaryDirectory() as directory:
            tone, silence = (Path(directory) / f"{kind}.bin" for kind in ("tone", "silence"))
            left = [round(1036 * math.sin(2 * math.pi * i / 48)) for i in range(500)]
            tone.write_bytes(samples_on_wire([(l, -int(l / 2)) for l in left]))
            silence.write_bytes(samples_on_wire([(0, 0)] * 500))
            self.assertEqual(h2h5.reduce("H4", limits, records, (tone, silence))[0], "PASS")
            tone.write_bytes(samples_on_wire([(l, l) for l in left]))
            self.assertEqual(h2h5.reduce("H4", limits, records, (tone, silence))[0], "FAIL")

    def test_h2_requires_matching_decoder_probe_and_duplicate_counts(self):
        limits = {**hil.load_gates()["H2"], "clean_seconds": 1, "post_seconds": 1}
        records = []

        def add(record_type, **fields):
            record = {"host": len(records), "t": record_type, **fields}
            records.append(record)
            return record

        def status(role, up=0, **increments):
            uart = dict.fromkeys(h2h5.CLEAN + ("exec", "dup", "hb_rx"), 0)
            return {"role": role, "up_ms": up, "stats_period_ms": 1000,
                    "uart": {**uart, "peer": True, "alive": True, "hb_gap_max_ms": 500, **increments}}

        def clean(phase):
            add("start", phase=phase, data={r: status(r) for r in h2h5.ROLES})
            for role in h2h5.ROLES:
                add("stats", **status(role, 500))
            add("end", phase=phase, data={r: status(r, up=1000, hb_rx=2) for r in h2h5.ROLES})

        clean("clean")
        for role in h2h5.ROLES:
            peer = h2h5.other(role)
            for kind, fault in enumerate(h2h5.FAULTS, 1):
                phase = f"{role}/{fault}"
                add("start", phase=phase, data=status(peer))
                add("event", role=role, ev="inject", kind=kind, probe_seq=42)
                probe = add("event", role=peer, ev="rx_recover", type=2, seq=42)
                add("end", phase=phase, data=status(peer, dec_err=1, proto_err=1))
        add("start", phase="s31/uart_dup", data=status("output"))
        add("end", phase="s31/uart_dup", data=status("output", exec=1, dup=2))
        clean("post")

        self.assertEqual(h2h5.reduce("H2", limits, records)[0], "PASS")
        probe["seq"] = 43
        self.assertEqual(h2h5.reduce("H2", limits, records)[0], "FAIL")

    def test_h5_detects_counter_change_and_coverage(self):
        before, after = stream(0, 0), stream(4, 4000)
        rows = [stream(i, i * 1000, "stats") for i in range(4)]
        records = [
            {"t": "start", "phase": "stream", "host": 0, "data": {"output": before}},
            *rows,
            {"t": "end", "phase": "stream", "host": 4, "data": {"output": after}},
        ]
        limits = {**hil.load_gates()["H5"], "stream_seconds": 4}
        self.assertEqual(h2h5.reduce("H5", limits, records)[0], "PASS")
        self.assertEqual(h2h5.reduce("H5", limits, records[1:])[0], "INCONCLUSIVE")
        self.assertEqual(h2h5.reduce("H5", limits, records[:1] + records[2:])[0], "INCONCLUSIVE")
        rows[1]["fifo"]["ppm"] = 500
        self.assertEqual(h2h5.reduce("H5", limits, records)[0], "FAIL")


if __name__ == "__main__":
    unittest.main()
