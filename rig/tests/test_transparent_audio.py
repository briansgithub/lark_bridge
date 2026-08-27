from __future__ import annotations

import json
import math
import re
import struct
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from rig import transparent_audio as ta

MIXER = """numid=1,iface=MIXER,name='Auto Gain Control'
  : values=off
numid=2,iface=MIXER,name='Mic Playback Switch'
  : values=off
numid=3,iface=MIXER,name='Mic Capture Switch'
  : values=on
numid=4,iface=MIXER,name='Mic Capture Volume'
  : values=0
"""


def good_instrument() -> dict:
    return {
        "usb_id": ta.GENERALPLUS_USB_ID,
        "required_port": ta.GENERALPLUS_PORT,
        "devices": [
            {
                "port": ta.GENERALPLUS_PORT,
                "cards": [{"number": 4, "alsa_id": "GeneralPlus"}],
            }
        ],
        "ready": True,
        "alsa_id": "GeneralPlus",
        "ephemeral_card_number": 4,
        "mixer_returncode": 0,
        "mixer_contents": MIXER,
        "mixer_sha256": ta.sha256_bytes(MIXER.encode()),
        "stream_capabilities": """Playback:
  Interface 1
    Altset 1
  Format: S16_LE
  Channels: 2
  Rates: 48000
Capture:
  Interface 2
    Altset 1
  Format: S16_LE
  Channels: 1
  Rates: 48000
""",
    }


def snapshot(
    transport: str = "MEDIA_ACTIVE",
    state: str = "CALL_DOWN",
    *,
    candidate_id: str = "candidate",
    phone_mac: str = "AA:BB:CC:DD:EE:FF",
) -> dict:
    media_node = f"bluez_input.{phone_mac.replace(':', '_')}.7"
    hfp_sink = f"bluez_output.{phone_mac.replace(':', '_')}.1"
    nodes: list[dict] = []
    if transport == "MEDIA_ACTIVE":
        nodes = [
            {
                "id": 1,
                "type": "PipeWire:Interface:Node",
                "info": {
                    "props": {
                        "node.name": media_node,
                        "api.bluez5.profile": "a2dp-source",
                        "media.class": "Stream/Output/Audio",
                    }
                },
            },
            {
                "id": 2,
                "type": "PipeWire:Interface:Node",
                "info": {"props": {"node.name": ta.FIXED_AUX_TARGET}},
            },
            {
                "id": 3,
                "type": "PipeWire:Interface:Link",
                "info": {"props": {"link.output.node": 1, "link.input.node": 2}},
            },
        ]
    elif transport == "CALL":
        nodes = [
            {
                "id": 1,
                "type": "PipeWire:Interface:Node",
                "info": {"props": {"node.name": "output.bridge.mic"}},
            },
            {
                "id": 2,
                "type": "PipeWire:Interface:Node",
                "info": {"props": {"node.name": hfp_sink}},
            },
            {
                "id": 3,
                "type": "PipeWire:Interface:Link",
                "info": {"props": {"link.output.node": 1, "link.input.node": 2}},
            },
        ]
    now = ta.time.time()
    return {
        "boot_id": "boot-1",
        "deployed_hashes": {"/home/admin/rpi-lark-bridge/config/bridge.toml": "abc"},
        "deployed_head": {"returncode": 127, "stdout": "", "stderr": "git absent"},
        "probe_epoch": now,
        "status_mtime": now,
        "services": {
            "returncode": 0,
            "stderr": "",
            "stdout": f"""Id=pipewire.service
ActiveState=active
NRestarts=0
Id=pipewire-pulse.service
ActiveState=active
NRestarts=0
Id=wireplumber.service
ActiveState=active
NRestarts=0
Id=bridge-supervisor.service
ActiveState=active
NRestarts=0
Environment=LARKBRIDGE_DEV_CANDIDATE={candidate_id}""",
        },
        "supervisor_process": {
            "returncode": 0,
            "stderr": "",
            "stdout": (
                f"LARKBRIDGE_DEV_CANDIDATE={candidate_id}\n--CMDLINE--\n"
                f"python3 /run/user/1000/larkbridge-dev/session/candidates/{candidate_id}/bridge_supervisor.py"
            ),
        },
        "system_services": {
            "returncode": 0,
            "stderr": "",
            "stdout": (
                "Id=bluetooth.service\nActiveState=active\nNRestarts=0\n"
                "Id=bridge-btwatchdog@call.service\nActiveState=inactive\nNRestarts=0"
            ),
        },
        "bluetooth": {
            "returncode": 0,
            "stderr": "",
            "stdout": "UUID: Audio Sink (0000110b-0000)",
        },
        "usb": {"returncode": 0, "stderr": "", "stdout": "1b3f:2008"},
        "usb_topology": {"returncode": 0, "stderr": "", "stdout": "1-1.5"},
        "graph": {"returncode": 0, "stderr": "", "stdout": json.dumps(nodes)},
        "links": {"returncode": 0, "stderr": "", "stdout": "link inventory"},
        "kernel_errors": {"returncode": 0, "stderr": "", "stdout": ""},
        "watchdog": {"returncode": 0, "stderr": "", "stdout": '{"recoveries":0}'},
        "android": {
            "adb_devices": {"returncode": 0, "stdout": "device", "stderr": ""},
            "audio": {
                "returncode": 0,
                "stdout": (
                    "MODE_IN_COMMUNICATION" if transport == "CALL" else "MODE_NORMAL"
                ),
                "stderr": "",
            },
            "bluetooth_manager": {"returncode": 0, "stdout": "connected", "stderr": ""},
        },
        "status": {
            "timestamp": now,
            "state": state,
            "wired_output_volume": {
                "required": True,
                "target": ta.FIXED_AUX_TARGET,
                "desired": 0.95,
                "observed": 0.95,
                "verified": True,
                "error": None,
            },
            "phone": {
                "transport": transport,
                "route_verified": transport == "MEDIA_ACTIVE",
                "media_node": media_node if transport == "MEDIA_ACTIVE" else None,
                "expected_target": ta.FIXED_AUX_TARGET,
                "android_microphone_transport": transport == "CALL",
                "target_volume": {
                    "required": True,
                    "desired": 0.95,
                    "observed": 0.95,
                    "verified": True,
                    "error": None,
                },
            },
            "endpoints": {"hfp_sink": hfp_sink if transport == "CALL" else None},
            "aec": {"verified": transport == "CALL"},
        },
    }


class FakeBackend:
    def __init__(self) -> None:
        self.pi_calls: list[tuple[str, bytes | None]] = []
        self.adb_calls: list[tuple[str, ...]] = []
        self.local_calls: list[tuple[str, ...]] = []
        self.local_cwds: list[Path | None] = []
        self.snapshot = snapshot()
        self.instrument = good_instrument()
        self.fetch_wav: Path | None = None
        self.clock = 0.0
        self.music_volume = 16
        self.unit_query_counts: dict[str, int] = {}

    def local(self, command, *, cwd=None, timeout=60):
        self.local_calls.append(tuple(str(item) for item in command))
        self.local_cwds.append(cwd)
        if "aec_metrics.py" in " ".join(str(item) for item in command):
            return ta.CommandResult(
                0,
                json.dumps(
                    {
                        "verdict": "PASS",
                        "raw_correlated_dbfs": -30.0,
                        "suppression_db": 15.0,
                        "failures": [],
                    }
                ),
                "",
            )
        return ta.CommandResult(0, "Ran 61 tests in 0.5s\n\nOK\n", "")

    def pi(self, script, *, timeout=60, stdin=None):
        self.pi_calls.append((script, stdin))
        for unit in re.findall(r"--unit=([^\s]+)", script):
            name = unit if unit.endswith((".service", ".timer")) else unit + ".service"
            self.unit_query_counts[name] = 0
        if "usb_id,required_port" in script:
            return ta.CommandResult(0, json.dumps(self.instrument), "")
        if "'deployed_hashes':hashes" in script:
            return ta.CommandResult(0, json.dumps(self.snapshot), "")
        if "'condition_probe':True" in script:
            return ta.CommandResult(0, json.dumps(self.snapshot), "")
        if "independent fixed-AUX volume" in script or "wpctl','get-volume" in script:
            return ta.CommandResult(
                0,
                json.dumps(
                    {
                        "target": ta.FIXED_AUX_TARGET,
                        "observed": 0.95,
                        "verified": True,
                        "error": None,
                    }
                ),
                "",
            )
        if "amixer -D" in script:
            return ta.CommandResult(0, MIXER, "")
        if "ActiveState --value" in script:
            match = re.search(r"show\s+([^\s]+)", script)
            unit = match.group(1).strip("'\"") if match else ""
            count = self.unit_query_counts.get(unit, 1)
            self.unit_query_counts[unit] = count + 1
            return ta.CommandResult(0, "active\n" if count == 0 else "inactive\n", "")
        if script == f"cat {ta.STATUS_PATH}":
            return ta.CommandResult(0, json.dumps(self.snapshot["status"]), "")
        return ta.CommandResult(0, "ok\n", "")

    def adb(self, args, *, timeout=60):
        values = tuple(str(item) for item in args)
        self.adb_calls.append(values)
        if values[:3] == ("shell", "pm", "path"):
            return ta.CommandResult(
                0, "package:/data/app/org.videolan.vlc/base.apk\n", ""
            )
        if values == (
            "shell",
            "cmd",
            "media_session",
            "volume",
            "--stream",
            "3",
            "--get",
        ):
            return ta.CommandResult(
                0,
                f"[V] volume is {self.music_volume} in range [0..25]\n",
                "",
            )
        if values[:7] == (
            "shell",
            "cmd",
            "media_session",
            "volume",
            "--stream",
            "3",
            "--set",
        ):
            self.music_volume = int(values[7])
            return ta.CommandResult(0, "volume changed\n", "")
        if values == ("shell", "dumpsys", "audio"):
            return ta.CommandResult(0, "MODE_NORMAL", "")
        if values == ("devices",):
            return ta.CommandResult(0, "serial\tdevice\n", "")
        return ta.CommandResult(0, "ok\n", "")

    def fetch(self, remote, local, *, recursive=False):
        local.parent.mkdir(parents=True, exist_ok=True)
        if recursive:
            local.mkdir(parents=True, exist_ok=True)
            stimulus = local.parent / "near-end-stimulus.wav"
            for role in ("reference", "raw", "clean"):
                target = local / f"{role}_quick.wav"
                if stimulus.is_file():
                    target.write_bytes(stimulus.read_bytes())
                else:
                    write_sine(target)
            (local / "pw-top.txt").write_text("observer evidence\n", encoding="utf-8")
        elif self.fetch_wav is not None:
            local.write_bytes(self.fetch_wav.read_bytes())
        else:
            write_sine(local)

    def wait(self, seconds):
        self.clock += seconds


def write_sine(
    path: Path,
    *,
    seconds: float = 3.0,
    amplitude: float = 0.25,
    frequency: float = 1000.0,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rate = ta.GENERALPLUS_RATE
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        frames = []
        for index in range(int(rate * seconds)):
            value = int(
                32767 * amplitude * math.sin(2 * math.pi * frequency * index / rate)
            )
            frames.append(struct.pack("<h", value))
        handle.writeframes(b"".join(frames))


def write_inventory(path: Path) -> None:
    path.write_text(
        """pi_host = "pi"
generalplus_usb_id = "1b3f:2008"
generalplus_port_path = "1-1.5"
generalplus_cable_id = "cable-a"
generalplus_speaker_position_id = "position-a"
pixel_bt_mac = "AA:BB:CC:DD:EE:FF"
""",
        encoding="utf-8",
    )


class InstrumentTests(unittest.TestCase):
    def test_rendered_remote_probe_compiles_and_executes(self) -> None:
        script = ta.render_remote_probe(ta.InstrumentSpec())
        body = script.split("python3 - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
        code = compile(body, "<rendered-generalplus-probe>", "exec")
        with mock.patch("glob.glob", return_value=[]), mock.patch(
            "builtins.print"
        ) as printed:
            exec(code, {})  # noqa: S102 - exercising the generated remote program
        report = json.loads(printed.call_args.args[0])
        self.assertFalse(report["ready"])
        self.assertIn("found 0", report["reason"])

    def test_locate_adb_honours_explicit_executable_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "custom-adb.exe"
            executable.write_bytes(b"")
            with mock.patch.dict(
                ta.os.environ, {"LARKBRIDGE_ADB": str(executable)}, clear=True
            ):
                self.assertEqual(ta.locate_adb(), str(executable))

    def test_locate_adb_uses_standard_localappdata_android_sdk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = (
                Path(directory) / "Android" / "Sdk" / "platform-tools" / "adb.exe"
            )
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"")
            with mock.patch.dict(
                ta.os.environ, {"LOCALAPPDATA": directory}, clear=True
            ):
                self.assertEqual(ta.locate_adb(), str(executable))

    def test_inventory_uses_generalplus_contract_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "inventory.toml"
            write_inventory(path)
            inventory = ta.Inventory.load(path)
        self.assertEqual(inventory.instrument.usb_id, "1b3f:2008")
        self.assertEqual(inventory.instrument.port_path, "1-1.5")
        self.assertEqual(inventory.instrument.capture_channels, 1)
        self.assertEqual(inventory.instrument.playback_channels, 2)
        self.assertEqual(inventory.instrument.rate, 48_000)
        self.assertEqual(inventory.aux_target, ta.FIXED_AUX_TARGET)
        self.assertEqual(inventory.thresholds.quick_aux_above_floor_db, 20.0)
        self.assertEqual(inventory.thresholds.aux_above_floor_db, 40.0)

    def test_probe_is_identity_and_port_based_not_card_number_based(self) -> None:
        backend = FakeBackend()
        result = ta.probe_instrument(backend, ta.InstrumentSpec())
        self.assertEqual(result["alsa_id"], "GeneralPlus")
        script = backend.pi_calls[0][0]
        self.assertIn("/sys/bus/usb/devices", script)
        self.assertIn("idVendor", script)
        self.assertNotIn("card=4", script)

    def test_wrong_port_pauses_as_hardware_action(self) -> None:
        backend = FakeBackend()
        backend.instrument.update(
            ready=False, reason="device is at 1-1.4, required 1-1.5"
        )
        with self.assertRaises(ta.HardwareRequired):
            ta.probe_instrument(backend, ta.InstrumentSpec())

    def test_wrong_pcm_capability_fails_before_capture(self) -> None:
        report = good_instrument()
        report["stream_capabilities"] = report["stream_capabilities"].replace(
            "Channels: 1", "Channels: 2"
        )
        with self.assertRaisesRegex(ta.HardwareRequired, "capture S16_LE/1ch"):
            ta.validate_instrument_capabilities(report, ta.InstrumentSpec())

    def test_pcm_contract_cannot_be_assembled_across_altsettings(self) -> None:
        report = good_instrument()
        report["stream_capabilities"] = """Playback:
  Interface 1
    Altset 1
      Format: S16_LE
      Channels: 1
      Rates: 48000
    Altset 2
      Format: S24_LE
      Channels: 2
      Rates: 48000
Capture:
  Interface 2
    Altset 1
      Format: S16_LE
      Channels: 2
      Rates: 48000
    Altset 2
      Format: S24_LE
      Channels: 1
      Rates: 48000
"""
        with self.assertRaisesRegex(ta.HardwareRequired, "altsetting"):
            ta.validate_instrument_capabilities(report, ta.InstrumentSpec())

    def test_inventory_rejects_weakened_contract_and_wrong_instrument(self) -> None:
        replacements = (
            ('generalplus_usb_id = "1b3f:2008"', 'generalplus_usb_id = "0d8c:0134"'),
            ('generalplus_port_path = "1-1.5"', 'generalplus_port_path = "1-1.4"'),
        )
        for old, new in replacements:
            with self.subTest(new=new), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "inventory.toml"
                write_inventory(path)
                path.write_text(path.read_text().replace(old, new), encoding="utf-8")
                with self.assertRaises(ta.SafetyFailure):
                    ta.Inventory.load(path)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "inventory.toml"
            write_inventory(path)
            path.write_text(
                path.read_text() + "e19_min_aux_above_floor_db = 10\n",
                encoding="utf-8",
            )
            with self.assertRaises(ta.SafetyFailure):
                ta.Inventory.load(path)

    def test_unqualified_mixer_map_fails_closed(self) -> None:
        report = good_instrument()
        report["mixer_contents"] = "name='Mic Capture Volume',\n"
        with self.assertRaises(ta.HardwareRequired):
            ta.validate_mixer_map(report, ta.InstrumentSpec())

    def test_safe_mixer_disables_agc_and_sidetone_and_starts_at_minimum_gain(
        self,
    ) -> None:
        script = ta.safe_mixer_script(ta.InstrumentSpec(), "GeneralPlus")
        self.assertIn("Auto Gain Control", script)
        self.assertIn("Mic Playback Switch", script)
        self.assertIn("Mic Capture Switch", script)
        self.assertIn("Mic Capture Volume", script)
        self.assertIn("0%", script)
        self.assertIn(" cset ", script)
        self.assertNotIn(" sset ", script)
        self.assertNotIn("+30", script)

    def test_mixer_map_fingerprint_ignores_values_but_state_gate_does_not(self) -> None:
        drifted = MIXER.replace("values=off", "values=on", 1)
        self.assertEqual(ta.mixer_map_sha256(MIXER), ta.mixer_map_sha256(drifted))
        report = good_instrument()
        report["mixer_contents"] = drifted
        with self.assertRaisesRegex(ta.HardwareRequired, "mixer drifted"):
            ta.validate_prepared_mixer_state(report, ta.InstrumentSpec())

    def test_mixer_preimage_uses_exact_qualified_controls_without_alsactl(self) -> None:
        preimage = ta.mixer_preimage_document(good_instrument(), ta.InstrumentSpec())
        self.assertEqual(preimage["controls"]["Mic Capture Volume"], "0")
        script = ta._mixer_restore_script("/recovery", ta.InstrumentSpec())
        self.assertIn("'amixer','-c',str(card),'cset'", script)
        self.assertIn("mixer-recovery-result.json", script)
        self.assertIn("'restored':not readback_errors", script)
        self.assertIn("'command_errors':command_errors", script)
        self.assertNotIn("alsactl", script)


class CalibrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.thresholds = ta.Thresholds()
        self.good = {
            "instrument_fingerprint": "fingerprint",
            "instrument": {
                "cable_id": "cable-a",
                "speaker_position_id": "position-a",
            },
            "stages": {
                "self-loop": {
                    "noise_floor_dbfs": -65,
                    "linearity_error_db": 1.0,
                    "dynamic_range_db": 55,
                    "clipped_pct": 0,
                },
                "aux-loop": {"above_floor_db": 45, "clipped_pct": 0},
                "acoustic": {"snr_db": 25, "clipped_pct": 0},
            },
        }

    def test_all_three_stages_pass(self) -> None:
        ta.validate_calibration(self.good, "fingerprint", self.thresholds)

    def test_changed_fixture_fingerprint_invalidates_all_stages(self) -> None:
        with self.assertRaisesRegex(ta.HardwareRequired, "stale"):
            ta.validate_calibration(self.good, "different", self.thresholds)

    def test_each_numeric_gate_fails_closed(self) -> None:
        fields = (
            ("self-loop", "noise_floor_dbfs", -50),
            ("self-loop", "linearity_error_db", 2),
            ("self-loop", "dynamic_range_db", 40),
            ("aux-loop", "above_floor_db", 30),
            ("acoustic", "snr_db", 10),
            ("acoustic", "clipped_pct", 1),
        )
        for stage, field, value in fields:
            with self.subTest(stage=stage, field=field):
                document = json.loads(json.dumps(self.good))
                document["stages"][stage][field] = value
                with self.assertRaises(ta.HardwareRequired):
                    ta.validate_calibration(document, "fingerprint", self.thresholds)

    def test_self_loop_metrics_derive_linearity_dynamic_range_and_clipping(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            silence = root / "silence.wav"
            write_sine(silence, amplitude=0.0)
            tones = {}
            for level in (-36.0, -24.0, -12.0):
                path = root / f"{level}.wav"
                write_sine(path, amplitude=10 ** (level / 20))
                tones[level] = path
            metrics = ta.self_loop_metrics(silence, tones)
        self.assertLess(metrics["linearity_error_db"], 0.1)
        self.assertGreater(metrics["dynamic_range_db"], 100)
        self.assertEqual(metrics["clipped_pct"], 0)

    def test_self_loop_metrics_remove_stable_generalplus_dc_offset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            silence = root / "offset-silence.wav"
            with wave.open(str(silence), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(ta.GENERALPLUS_RATE)
                frames = [
                    struct.pack("<h", 250 + index % 2) for index in range(144_000)
                ]
                handle.writeframes(b"".join(frames))
            tones = {}
            for level in (-36.0, -24.0, -12.0):
                path = root / f"{level}.wav"
                write_sine(path, amplitude=10 ** (level / 20))
                tones[level] = path
            metrics = ta.self_loop_metrics(silence, tones)
        self.assertLess(metrics["noise_floor_dbfs"], -90)
        self.assertGreater(metrics["dynamic_range_db"], 75)

    def test_noise_floor_removes_stable_generalplus_dc_offset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "offset.wav"
            with wave.open(str(path), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(ta.GENERALPLUS_RATE)
                frames = [struct.pack("<h", 250 + index % 2) for index in range(96_000)]
                handle.writeframes(b"".join(frames))
            ac_floor = ta.ac_rms_dbfs(path, skip_start=0.5)
        self.assertLess(ac_floor, -90)

    def test_stage_gate_reports_actionable_failure(self) -> None:
        failures = ta.calibration_stage_failures(
            "self-loop",
            {
                "noise_floor_dbfs": -50,
                "linearity_error_db": 2,
                "dynamic_range_db": 40,
                "clipped_pct": 1,
            },
            self.thresholds,
        )
        self.assertEqual(len(failures), 4)

    def test_quick_wiring_gate_covers_floor_margin_and_clipping(self) -> None:
        document = {
            "instrument_fingerprint": "fingerprint",
            "instrument": {"cable_id": "cable-a"},
            "capture_gain_request": "0%",
            "metrics": {
                "noise_floor_dbfs": -65,
                "above_floor_db": 45,
                "clipped_pct": 0,
            },
        }
        ta.validate_quick_calibration(document, "fingerprint", self.thresholds)
        for field, value in (
            ("noise_floor_dbfs", -50),
            ("above_floor_db", 10),
            ("clipped_pct", 1),
        ):
            with self.subTest(field=field):
                failed = json.loads(json.dumps(document))
                failed["metrics"][field] = value
                with self.assertRaises(ta.HardwareRequired):
                    ta.validate_quick_calibration(
                        failed, "fingerprint", self.thresholds
                    )

    def test_quick_gate_is_invalidated_by_fixture_fingerprint(self) -> None:
        document = {
            "instrument_fingerprint": "old",
            "instrument": {"cable_id": "cable-a"},
            "capture_gain_request": "0%",
            "metrics": {
                "noise_floor_dbfs": -65,
                "above_floor_db": 45,
                "clipped_pct": 0,
            },
        }
        with self.assertRaisesRegex(ta.HardwareRequired, "stale"):
            ta.validate_quick_calibration(document, "new", self.thresholds)

    def test_quick_gate_requires_recorded_cable_label(self) -> None:
        document = {
            "instrument_fingerprint": "fingerprint",
            "instrument": {"cable_id": ""},
            "capture_gain_request": "0%",
            "metrics": {
                "noise_floor_dbfs": -65,
                "above_floor_db": 45,
                "clipped_pct": 0,
            },
        }
        with self.assertRaisesRegex(ta.HardwareRequired, "physical cable label"):
            ta.validate_quick_calibration(document, "fingerprint", self.thresholds)

    def test_quick_measurement_plays_directly_to_fixed_aux(self) -> None:
        backend = FakeBackend()
        captures: list[str | None] = []

        def capture(
            _backend,
            *,
            capture_command,
            playback_command,
            remote_capture,
            local_capture,
            duration,
        ):
            del capture_command, remote_capture, duration
            captures.append(playback_command)
            write_sine(
                local_capture, amplitude=0.0 if playback_command is None else 0.25
            )

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            ta, "_calibration_capture", side_effect=capture
        ):
            inventory_file = Path(directory) / "inventory.toml"
            write_inventory(inventory_file)
            inventory = ta.Inventory.load(inventory_file)
            metrics = ta.perform_quick_calibration(
                backend,
                inventory,
                good_instrument(),
                Path(directory) / "quick",
            )
        self.assertIsNone(captures[0])
        self.assertIn(ta.FIXED_AUX_TARGET, captures[1] or "")
        self.assertGreater(metrics["above_floor_db"], 40)


class CandidateTests(unittest.TestCase):
    def make_repo(self, root: Path) -> None:
        (root / "pi/bridged").mkdir(parents=True)
        (root / "pi/wireplumber/wireplumber.conf.d").mkdir(parents=True)
        (root / "config").mkdir()
        (root / "pi/bridged/bridge_supervisor.py").write_text("print('one')\n")
        (root / "pi/bridged/helper.py").write_text("VALUE = 1\n")
        (root / "pi/wireplumber/wireplumber.conf.d/50.conf").write_text("roles\n")
        (root / "config/bridge.toml.example").write_text("value=1\n")
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(
            ["git", "-C", str(root), "config", "user.email", "test@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(root), "config", "user.name", "Test"], check=True
        )
        subprocess.run(["git", "-C", str(root), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(root), "commit", "-qm", "baseline"], check=True
        )

    def test_candidate_id_includes_head_diff_and_allowed_untracked_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_repo(root)
            first = ta.resolve_candidate(str(root))
            (root / "config/bridge.toml.example").write_text("value=2\n")
            second = ta.resolve_candidate(str(root))
            self.assertNotEqual(first.candidate_id, second.candidate_id)
            (root / "pi/bridged/new.py").write_text("NEW = 1\n")
            third = ta.resolve_candidate(str(root))
            self.assertNotEqual(second.candidate_id, third.candidate_id)
            self.assertEqual(third.untracked[0]["path"], "pi/bridged/new.py")

    def test_package_is_complete_and_policy_is_separate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_repo(root)
            candidate = ta.resolve_candidate(str(root))
            import tarfile

            with tarfile.open(
                fileobj=__import__("io").BytesIO(candidate.package_tar), mode="r:gz"
            ) as handle:
                names = set(handle.getnames())
            self.assertIn("bridge_supervisor.py", names)
            self.assertIn("helper.py", names)
            self.assertNotIn("50.conf", names)
            self.assertEqual(
                candidate.policy_files["50.conf"].replace(b"\r\n", b"\n"), b"roles\n"
            )

    def test_restart_classification(self) -> None:
        manifest = {
            "candidate_id": "id",
            "content_sha256": "same",
            "policy_files": {"50.conf": "old"},
        }
        candidate = ta.Candidate(
            "source",
            Path("."),
            "revision",
            "id",
            "diff",
            "new",
            (),
            (),
            b"tar",
            {"50.conf": b"new"},
        )
        self.assertEqual(ta.classify_restart(manifest, candidate), "audio-stack")
        manifest["policy_files"] = candidate.manifest()["policy_files"]
        self.assertEqual(ta.classify_restart(manifest, candidate), "supervisor")
        manifest["content_sha256"] = "new"
        self.assertEqual(ta.classify_restart(manifest, candidate), "none")

    def test_candidate_marker_change_forces_supervisor_restart(self) -> None:
        previous = {
            "candidate_id": "old-id",
            "content_sha256": "same",
            "policy_files": {},
        }
        candidate = ta.Candidate(
            "source",
            Path("."),
            "revision",
            "new-id",
            "diff",
            "same",
            (),
            (),
            b"tar",
            {},
        )
        self.assertEqual(ta.classify_restart(previous, candidate), "supervisor")

    def test_stale_candidate_after_tests_is_rejected(self) -> None:
        candidate = mock.Mock(candidate_id="first", source=".", repository=Path("."))
        with mock.patch.object(
            ta, "resolve_candidate", return_value=mock.Mock(candidate_id="second")
        ), self.assertRaisesRegex(ta.SafetyFailure, "changed after focused tests"):
            ta.confirm_candidate_still_current(candidate)

    def test_ref_candidate_tests_run_from_immutable_materialization(self) -> None:
        backend = FakeBackend()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_repo(root)
            candidate = ta.resolve_candidate("HEAD", repository=root)
            ta.run_focused_tests(backend, candidate)
            test_root = backend.local_cwds[-1]
            self.assertEqual(
                backend.local_calls[-1][3:],
                ("discover", "-s", "tests", "-p", "test_*.py"),
            )
            self.assertIsNotNone(test_root)
            self.assertNotEqual(test_root, root / "pi" / "bridged")
            assert test_root is not None
            self.assertEqual(test_root.parts[-2:], ("pi", "bridged"))
            self.assertFalse(test_root.parent.parent.exists())

    def test_focused_gate_rejects_zero_discovered_tests(self) -> None:
        backend = FakeBackend()
        backend.local = mock.Mock(
            return_value=ta.CommandResult(0, "Ran 0 tests in 0.0s\n\nOK\n", "")
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_repo(root)
            candidate = ta.resolve_candidate(str(root))
            with self.assertRaisesRegex(ta.RigFailure, "did not run any tests"):
                ta.run_focused_tests(backend, candidate)


class TransactionTests(unittest.TestCase):
    def test_remote_snapshot_reports_missing_optional_command_instead_of_aborting(
        self,
    ) -> None:
        script = ta._remote_manifest_script()
        helper = script.split("def run(command):\n", 1)[1].split("result={", 1)[0]
        namespace = {"subprocess": subprocess}
        exec(  # noqa: S102 - exercising the generated remote helper
            compile("def run(command):\n" + helper, "<remote-run-helper>", "exec"),
            namespace,
        )
        with mock.patch.object(subprocess, "run", side_effect=FileNotFoundError("git")):
            report = namespace["run"](["git", "rev-parse", "HEAD"])
        self.assertEqual(report["returncode"], 127)
        self.assertIn("FileNotFoundError", report["stderr"])

    def test_deadman_is_transient_and_bounded(self) -> None:
        backend = FakeBackend()
        ta.arm_deadman(backend, "session", "/recovery", 900)
        script = backend.pi_calls[-1][0]
        self.assertIn("systemd-run --user", script)
        self.assertIn("--on-active=900s", script)
        self.assertIn("/recovery/restore.sh", script)
        with self.assertRaises(ta.SafetyFailure):
            ta.arm_deadman(backend, "session", "/recovery", 10)

    def test_restored_mixer_readback_cancels_deadman_despite_command_warning(
        self,
    ) -> None:
        backend = FakeBackend()

        def restore(script, *, timeout=60, stdin=None):
            backend.pi_calls.append((script, stdin))
            if script.startswith("/bin/bash"):
                return ta.CommandResult(
                    0,
                    json.dumps(
                        {
                            "restored": True,
                            "command_errors": ["a harmless failed write"],
                            "errors": [],
                        }
                    ),
                    "",
                )
            return ta.CommandResult(0, "", "")

        backend.pi = restore  # type: ignore[method-assign]
        report = ta.stop_mixer_guard(backend, "guard", "/recovery")
        self.assertTrue(report["restored"])
        self.assertTrue(
            any("deadman-guard" in script for script, _ in backend.pi_calls)
        )

    def test_cleanup_failure_reports_both_primary_and_restore_errors(self) -> None:
        with mock.patch.object(
            ta, "stop_mixer_guard", side_effect=ta.SafetyFailure("restore failed")
        ), self.assertRaisesRegex(
            ta.SafetyFailure,
            "primary failure: RigFailure: capture failed.*restore failed",
        ):
            ta.finish_mixer_guard(
                FakeBackend(), "guard", "/recovery", ta.RigFailure("capture failed")
            )

    def test_recovery_uses_full_audio_stack_and_exact_hash_verification(self) -> None:
        script = ta._recovery_script("session", "/recovery", ta.InstrumentSpec())
        self.assertIn("stop bridge-supervisor.service", script)
        self.assertIn(
            "restart pipewire.service pipewire-pulse.service wireplumber.service",
            script,
        )
        self.assertIn("matches", script)
        self.assertIn("mixer-recovery-result.json", script)
        self.assertIn("'mixer':mixer", script)
        self.assertNotIn("alsactl", script)
        self.assertNotIn("restart wireplumber.service\n", script)
        self.assertNotIn("PY || true", script)
        self.assertIn("\nset +e\npython3 - <<'PY'", script)

    def test_staging_is_volatile_and_override_points_at_complete_package(self) -> None:
        backend = FakeBackend()
        candidate = ta.Candidate(
            "source",
            Path("."),
            "revision",
            "candidate",
            "diff",
            "content",
            (),
            (),
            b"tar",
            {},
        )
        ta.apply_candidate(backend, candidate, "session", "supervisor")
        joined = "\n".join(call[0] for call in backend.pi_calls)
        self.assertIn(
            "/run/user/1000/larkbridge-dev/session/candidates/candidate", joined
        )
        self.assertIn("90-larkbridge-dev.conf", joined)
        override_match = re.search(
            r"echo ([A-Za-z0-9+/=]+) \| base64 -d > .*90-larkbridge-dev\.conf",
            joined,
        )
        self.assertIsNotNone(override_match)
        assert override_match is not None
        override = ta.base64.b64decode(override_match.group(1)).decode()
        self.assertIn(f"Environment=BRIDGE_CONFIG={ta.CONFIG_PATH}", override)
        self.assertIn("restart bridge-supervisor.service", joined)
        self.assertNotIn("restart wireplumber.service", joined)
        apply_script = backend.pi_calls[-1][0]
        self.assertTrue(apply_script.startswith("set -euo pipefail\n"))
        self.assertIn("mutation.lock", apply_script)
        self.assertNotIn("; systemctl --user", apply_script)

    def test_expired_deadman_marker_is_detected_before_an_iteration(self) -> None:
        state = snapshot()
        state["supervisor_process"]["stdout"] = "old deployed supervisor"
        session = {
            "baseline": {"boot_id": "boot-1"},
            "current_candidate": {"candidate_id": "candidate"},
        }
        with self.assertRaisesRegex(ta.SafetyFailure, "deadman may have restored"):
            ta._current_session_guard(session, state)

    def test_failed_exact_cleanup_is_not_reported_as_restored(self) -> None:
        backend = FakeBackend()

        def failed(script, *, timeout=60, stdin=None):
            return ta.CommandResult(1, '{"restored":false}', "hash mismatch")

        backend.pi = failed  # type: ignore[method-assign]
        with self.assertRaises(ta.RigFailure):
            ta.restore_remote_session(
                backend,
                {
                    "session_id": "session",
                    "recovery_root": "/recovery",
                },
            )

    def test_cached_candidate_rejects_identity_and_policy_tampering(self) -> None:
        candidate = ta.Candidate(
            "source",
            Path("."),
            "revision",
            "candidate",
            "diff",
            "content",
            (),
            (),
            b"tar",
            {"66-policy.conf": b"policy"},
        )
        with tempfile.TemporaryDirectory() as directory:
            artifacts = Path(directory)
            root = ta.cache_candidate(artifacts, candidate)
            manifest = json.loads((root / "manifest.json").read_text())
            manifest["candidate_id"] = "other"
            (root / "manifest.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ta.SafetyFailure, "identity"):
                ta.load_cached_candidate(artifacts, "candidate")
            root = ta.cache_candidate(artifacts, candidate)
            (root / "policies" / "66-policy.conf").write_bytes(b"tampered")
            with self.assertRaisesRegex(ta.SafetyFailure, "hash changed"):
                ta.load_cached_candidate(artifacts, "candidate")
            root = ta.cache_candidate(artifacts, candidate)
            (root / "policies" / "66-policy.conf").unlink()
            with self.assertRaisesRegex(ta.SafetyFailure, "policy set changed"):
                ta.load_cached_candidate(artifacts, "candidate")

    def test_new_policy_added_mid_session_gets_a_deployed_preimage_first(self) -> None:
        backend = FakeBackend()
        path = f"{ta.WP_DEPLOYED_DIR}/66-new.conf"
        updated = {ta.OVERRIDE_PATH: {"exists": False}, path: {"exists": False}}

        def extend(script, *, timeout=60, stdin=None):
            backend.pi_calls.append((script, stdin))
            return ta.CommandResult(0, json.dumps(updated), "")

        backend.pi = extend  # type: ignore[method-assign]
        session = {
            "recovery_root": "/recovery",
            "preimages": {ta.OVERRIDE_PATH: {"exists": False}},
        }
        ta.extend_preimages(backend, session, ["66-new.conf"])
        self.assertIn(path, session["preimages"])
        self.assertIn("preimages.json", backend.pi_calls[-1][0])

    def test_removed_candidate_policy_is_removed_only_inside_managed_directory(
        self,
    ) -> None:
        backend = FakeBackend()
        candidate = ta.Candidate(
            "source",
            Path("."),
            "revision",
            "candidate",
            "diff",
            "content",
            (),
            (),
            b"tar",
            {},
        )
        ta.apply_candidate(
            backend,
            candidate,
            "session",
            "audio-stack",
            remove_policy_names=("66-old.conf",),
        )
        joined = "\n".join(script for script, _stdin in backend.pi_calls)
        self.assertIn(f"rm -f {ta.WP_DEPLOYED_DIR}/66-old.conf", joined)
        with self.assertRaises(ta.SafetyFailure):
            ta.apply_candidate(
                backend,
                candidate,
                "session",
                "audio-stack",
                remove_policy_names=("../outside",),
            )

    def test_runtime_condition_wait_never_runs_full_snapshot_or_adb(self) -> None:
        backend = FakeBackend()
        with mock.patch.object(
            ta, "capture_snapshot", side_effect=AssertionError("full snapshot polled")
        ):
            result = ta.verify_runtime(
                backend,
                0.95,
                ta.FIXED_AUX_TARGET,
                "candidate",
            )
        self.assertEqual(result["status"]["wired_output_volume"]["observed"], 0.95)
        self.assertEqual(backend.adb_calls, [])
        joined = "\n".join(script for script, _stdin in backend.pi_calls)
        self.assertIn("condition_probe", joined)
        for expensive in ("pw-dump", "pw-link", "lsusb", "journalctl", "dumpsys"):
            self.assertNotIn(expensive, joined)

    def test_phone_transport_wait_reads_only_atomic_status(self) -> None:
        backend = FakeBackend()
        with mock.patch.object(
            ta, "capture_snapshot", side_effect=AssertionError("full snapshot polled")
        ):
            phone, elapsed = ta.wait_phone_transport(backend, "MEDIA_ACTIVE", timeout=3)
        self.assertEqual(phone["transport"], "MEDIA_ACTIVE")
        self.assertLess(elapsed, 1)
        self.assertEqual(backend.adb_calls, [])
        self.assertEqual(backend.pi_calls, [(f"cat {ta.STATUS_PATH}", None)])

    def test_runtime_rejects_missing_volume_truth(self) -> None:
        backend = FakeBackend()
        del backend.snapshot["status"]["phone"]["target_volume"]
        with self.assertRaisesRegex(ta.RigFailure, "status is missing"):
            ta.verify_runtime(backend, 0.95, ta.FIXED_AUX_TARGET, "candidate")

    def test_runtime_rejects_wrong_aux_target_or_volume(self) -> None:
        backend = FakeBackend()
        phone = backend.snapshot["status"]["phone"]
        phone["expected_target"] = "alsa_output.usb-GeneralPlus"
        phone["target_volume"]["observed"] = 0.5
        with self.assertRaisesRegex(ta.RigFailure, "expected fixed AUX"):
            ta.verify_runtime(backend, 0.95, ta.FIXED_AUX_TARGET, "candidate")

    def test_runtime_rejects_unverified_volume_and_error(self) -> None:
        backend = FakeBackend()
        block = backend.snapshot["status"]["phone"]["target_volume"]
        block.update(verified=False, error="readback failed")
        with self.assertRaisesRegex(ta.RigFailure, "not verified"):
            ta.verify_runtime(backend, 0.95, ta.FIXED_AUX_TARGET, "candidate")

    def test_runtime_rejects_old_process_and_stale_status(self) -> None:
        backend = FakeBackend()
        backend.snapshot["supervisor_process"][
            "stdout"
        ] = "LARKBRIDGE_DEV_CANDIDATE=old\n--CMDLINE--\n/deployed/bridge_supervisor.py"
        with self.assertRaisesRegex(ta.RigFailure, "running supervisor process"):
            ta.verify_runtime(backend, 0.95, ta.FIXED_AUX_TARGET, "candidate")
        backend = FakeBackend()
        backend.snapshot["status"]["timestamp"] -= 30
        with self.assertRaisesRegex(ta.RigFailure, "status is stale"):
            ta.verify_runtime(backend, 0.95, ta.FIXED_AUX_TARGET, "candidate")

    def test_call_runtime_verifies_fixed_aux_independently_of_dynamic_output(
        self,
    ) -> None:
        backend = FakeBackend()
        backend.snapshot = snapshot("CALL", "ACTIVE")
        backend.snapshot["status"]["wired_output_volume"].update(
            target="alsa_output.usb-GeneralPlus", verified=True
        )
        ta.verify_runtime(
            backend,
            0.95,
            ta.FIXED_AUX_TARGET,
            "candidate",
            mode="call",
        )
        self.assertTrue(
            any("wpctl','get-volume" in script for script, _stdin in backend.pi_calls)
        )

        failed_probe = mock.Mock(return_value={"verified": False, "error": "missing"})
        with mock.patch.object(
            ta, "probe_fixed_aux_volume", failed_probe
        ), self.assertRaisesRegex(ta.RigFailure, "independent fixed-AUX"):
            ta.verify_runtime(
                backend,
                0.95,
                ta.FIXED_AUX_TARGET,
                "candidate",
                mode="call",
            )

    def test_call_runtime_accepts_fixed_aux_call_output_directly(self) -> None:
        backend = FakeBackend()
        backend.snapshot = snapshot("CALL", "ACTIVE")
        backend.snapshot["status"]["wired_output_volume"][
            "target"
        ] = ta.FIXED_AUX_TARGET
        with mock.patch.object(ta, "probe_fixed_aux_volume") as probe:
            ta.verify_runtime(
                backend,
                0.95,
                ta.FIXED_AUX_TARGET,
                "candidate",
                mode="call",
            )
        probe.assert_not_called()

    def test_nested_probe_failure_and_restart_delta_fail_closed(self) -> None:
        before = snapshot()
        for probe in (
            "services",
            "supervisor_process",
            "system_services",
            "bluetooth",
            "graph",
            "links",
            "usb",
            "usb_topology",
            "kernel_errors",
            "watchdog",
        ):
            with self.subTest(probe=probe):
                failed = snapshot()
                failed[probe]["returncode"] = 1
                self.assertTrue(ta.required_snapshot_evidence_failures(failed))
        for probe in ("adb_devices", "audio", "bluetooth_manager"):
            with self.subTest(android_probe=probe):
                failed = snapshot()
                failed["android"][probe]["returncode"] = 1
                self.assertTrue(ta.required_snapshot_evidence_failures(failed))
        for missing in ("deployed_head", "deployed_hashes"):
            with self.subTest(missing=missing):
                failed = snapshot()
                del failed[missing]
                self.assertTrue(ta.required_snapshot_evidence_failures(failed))
        failed = snapshot()
        failed["status"] = {}
        self.assertTrue(ta.required_snapshot_evidence_failures(failed))
        after = snapshot()
        after["services"]["stdout"] = after["services"]["stdout"].replace(
            "Id=bridge-supervisor.service\nActiveState=active\nNRestarts=0",
            "Id=bridge-supervisor.service\nActiveState=active\nNRestarts=1",
        )
        deltas, failures = ta.restart_counter_failures(before, after)
        self.assertEqual(deltas["bridge-supervisor.service"], 1)
        self.assertTrue(failures)

    def test_restart_parser_is_independent_of_systemctl_property_order(self) -> None:
        state = snapshot()
        state["services"]["stdout"] = """NRestarts=7
Id=bridge-supervisor.service
ActiveState=active

NRestarts=3
Id=pipewire.service
ActiveState=active
"""
        self.assertEqual(
            ta.service_restarts(state),
            {
                "bridge-supervisor.service": 7,
                "pipewire.service": 3,
                "bluetooth.service": 0,
                "bridge-btwatchdog@call.service": 0,
            },
        )

    def test_empty_deployed_hash_manifest_is_not_baseline_evidence(self) -> None:
        state = snapshot()
        state["deployed_hashes"] = {}
        self.assertTrue(
            any(
                "hash manifest" in failure
                for failure in ta.required_snapshot_evidence_failures(state)
            )
        )

    def test_unavailable_full_journal_is_recorded_but_not_a_rapid_loop_gate(
        self,
    ) -> None:
        state = snapshot()
        state["journals"] = {
            "returncode": 1,
            "stdout": "",
            "stderr": "No journal files were opened due to insufficient permissions",
        }
        self.assertEqual(ta.required_snapshot_evidence_failures(state), [])

    def test_session_lock_contention_fails_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with ta.session_lock(root), self.assertRaisesRegex(
                ta.SafetyFailure, "holds the session lock"
            ), ta.session_lock(root):
                pass

    def test_recovery_and_deadman_cleanup_verify_every_unit(self) -> None:
        script = ta._recovery_script("session", "/recovery", ta.InstrumentSpec())
        self.assertIn("for unit in ('pipewire.service'", script)
        self.assertIn("'services':services", script)
        backend = FakeBackend()

        def failed(script, *, timeout=60, stdin=None):
            return ta.CommandResult(1, "active", "stop failed")

        backend.pi = failed  # type: ignore[method-assign]
        with self.assertRaises(ta.RigFailure):
            ta.cancel_deadman(backend, "session")

    def test_recovery_installer_is_atomic_and_hash_verified(self) -> None:
        backend = FakeBackend()
        ta.install_recovery_script(backend, "session", "/recovery", ta.InstrumentSpec())
        script = backend.pi_calls[-1][0]
        self.assertTrue(script.startswith("set -euo pipefail"))
        self.assertIn(".e19-new", script)
        self.assertGreaterEqual(script.count("sha256sum"), 2)


class ScoringAndRoutingTests(unittest.TestCase):
    def test_clean_media_capture_passes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.wav"
            write_sine(path)
            result = ta.score_media(
                path,
                calibrated_noise_floor_dbfs=-65,
                thresholds=ta.Thresholds(),
            )
        self.assertEqual(result["verdict"], "PASS")

    def test_media_gate_detects_clipping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.wav"
            rate = ta.GENERALPLUS_RATE
            with wave.open(str(path), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(rate)
                handle.writeframes(
                    b"".join(
                        struct.pack("<h", 32767 if index % 2 else -32768)
                        for index in range(rate * 3)
                    )
                )
            result = ta.score_media(
                path,
                calibrated_noise_floor_dbfs=-65,
                thresholds=ta.Thresholds(),
            )
        self.assertEqual(result["verdict"], "FAIL")
        self.assertTrue(any("clipped" in failure for failure in result["failures"]))

    def test_media_gate_rejects_wrong_tone_at_healthy_level(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.wav"
            write_sine(path, frequency=500.0)
            result = ta.score_media(
                path,
                calibrated_noise_floor_dbfs=-65,
                thresholds=ta.Thresholds(),
            )
        self.assertEqual(result["verdict"], "FAIL")
        self.assertTrue(any("does not match" in item for item in result["failures"]))

    def test_generated_media_stimulus_passes_after_delayed_route_arrival(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "stimulus.wav"
            capture = root / "delayed-loopback.wav"
            ta.generate_stimulus(source, mode="sine", seconds=5, channels=1)
            with wave.open(str(source), "rb") as handle:
                params = handle.getparams()
                frames = handle.readframes(handle.getnframes())
            with wave.open(str(capture), "wb") as handle:
                handle.setparams(params)
                handle.writeframes(b"\0\0" * ta.GENERALPLUS_RATE * 2)
                handle.writeframes(frames)
            result = ta.score_media(
                capture,
                calibrated_noise_floor_dbfs=-65,
                thresholds=ta.Thresholds(),
                minimum_stable_s=4,
            )
        self.assertEqual(result["verdict"], "PASS")
        self.assertGreaterEqual(result["steady_window"]["start_s"], 2.5)
        self.assertEqual(
            result["discontinuities"],
            {"hp_burst": [], "step": [], "inactive_gaps": []},
        )

    def test_media_gate_rejects_interior_dropout_instead_of_trimming_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.wav"
            rate = ta.GENERALPLUS_RATE
            frames = bytearray()
            for index in range(rate * 25):
                value = int(8000 * math.sin(2 * math.pi * 1000 * index / rate))
                if rate * 22 <= index < rate * 23:
                    value = 0
                frames.extend(struct.pack("<h", value))
            with wave.open(str(path), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(rate)
                handle.writeframes(frames)
            result = ta.score_media(
                path,
                calibrated_noise_floor_dbfs=-65,
                thresholds=ta.Thresholds(),
            )
        self.assertEqual(result["verdict"], "FAIL")
        self.assertTrue(result["discontinuities"]["inactive_gaps"])

    def test_call_score_preserves_known_near_end_without_claiming_echo_suppression(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stimulus = root / "stimulus.wav"
            ta.generate_stimulus(stimulus, mode="speech", seconds=5, channels=1)
            reference = root / "reference.wav"
            raw = root / "raw.wav"
            clean = root / "clean.wav"
            for target in (reference, raw, clean):
                target.write_bytes(stimulus.read_bytes())
            result = ta.score_call(
                stimulus=stimulus,
                reference=reference,
                raw=raw,
                clean=clean,
                thresholds=ta.Thresholds(),
            )
        self.assertEqual(result["verdict"], "PASS")
        self.assertEqual(
            result["echo_suppression"],
            "NOT_MEASURED_USE_SEPARATE_SPEAKER_MODE_FIXTURE",
        )
        self.assertGreaterEqual(result["raw_correlation"], 0.3)

    def test_route_gate_rejects_decoy_and_duplicate(self) -> None:
        state = snapshot()
        graph = json.loads(state["graph"]["stdout"])
        graph.append(
            {
                "id": 4,
                "type": "PipeWire:Interface:Node",
                "info": {"props": {"node.name": "alsa_output.usb-GeneralPlus"}},
            }
        )
        graph.append(
            {
                "id": 5,
                "type": "PipeWire:Interface:Link",
                "info": {"props": {"link.output.node": 1, "link.input.node": 4}},
            }
        )
        state["graph"]["stdout"] = json.dumps(graph)
        inventory = mock.Mock(
            aux_target=ta.FIXED_AUX_TARGET, pixel_bt_mac="AA:BB:CC:DD:EE:FF"
        )
        failures = ta._route_failures("media", state, inventory)
        self.assertTrue(
            any("only the configured AUX" in failure for failure in failures)
        )

    def test_route_gate_accepts_stereo_channel_links_to_same_aux(self) -> None:
        state = snapshot()
        graph = json.loads(state["graph"]["stdout"])
        graph.append(
            {
                "id": 4,
                "type": "PipeWire:Interface:Link",
                "info": {"props": {"link.output.node": 1, "link.input.node": 2}},
            }
        )
        state["graph"]["stdout"] = json.dumps(graph)
        inventory = mock.Mock(
            aux_target=ta.FIXED_AUX_TARGET, pixel_bt_mac="AA:BB:CC:DD:EE:FF"
        )
        failures = ta._route_failures("media", state, inventory)
        self.assertEqual(failures, [])

    def test_call_gate_rejects_physical_microphone_bypass(self) -> None:
        state = snapshot("CALL", "ACTIVE")
        graph = json.loads(state["graph"]["stdout"])
        graph.extend(
            [
                {
                    "id": 4,
                    "type": "PipeWire:Interface:Node",
                    "info": {"props": {"node.name": "alsa_input.usb-Lark"}},
                },
                {
                    "id": 5,
                    "type": "PipeWire:Interface:Link",
                    "info": {"props": {"link.output.node": 4, "link.input.node": 2}},
                },
            ]
        )
        state["graph"]["stdout"] = json.dumps(graph)
        inventory = mock.Mock(
            aux_target=ta.FIXED_AUX_TARGET, pixel_bt_mac="AA:BB:CC:DD:EE:FF"
        )
        self.assertTrue(
            any(
                "bypass" in value
                for value in ta._route_failures("call", state, inventory)
            )
        )


class CommandTests(unittest.TestCase):
    def test_without_live_no_backend_action_occurs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = root / "inventory.toml"
            write_inventory(inventory)
            code = ta.main(
                [
                    "--inventory",
                    str(inventory),
                    "--artifacts",
                    str(root / "artifacts"),
                    "baseline",
                ]
            )
        self.assertEqual(code, ta.EXIT_HARDWARE)

    def test_baseline_records_dynamic_snapshot_and_instrument(self) -> None:
        backend = FakeBackend()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = root / "inventory.toml"
            artifacts = root / "artifacts"
            write_inventory(inventory)
            code = ta.main(
                [
                    "--inventory",
                    str(inventory),
                    "--artifacts",
                    str(artifacts),
                    "baseline",
                    "--live",
                ],
                backend=backend,
            )
            document = json.loads((artifacts / "latest-baseline.json").read_text())
        self.assertEqual(code, 0)
        self.assertEqual(document["snapshot"]["boot_id"], "boot-1")
        self.assertEqual(document["instrument"]["alsa_id"], "GeneralPlus")

    def test_adb_evidence_loss_fails_snapshot(self) -> None:
        backend = FakeBackend()

        def failed_adb(args, *, timeout=60):
            return ta.CommandResult(1, "", "device offline")

        backend.adb = failed_adb  # type: ignore[method-assign]
        with self.assertRaisesRegex(ta.RigFailure, "ADB device inventory"):
            ta.capture_snapshot(backend)

    def test_ssh_evidence_loss_fails_snapshot(self) -> None:
        backend = FakeBackend()

        def failed_pi(script, *, timeout=60, stdin=None):
            return ta.CommandResult(255, "", "connection lost")

        backend.pi = failed_pi  # type: ignore[method-assign]
        with self.assertRaisesRegex(ta.RigFailure, "Pi snapshot"):
            ta.capture_snapshot(backend)

    def test_media_smoke_launches_installed_vlc_explicitly(self) -> None:
        backend = FakeBackend()
        quick_calibration = {"metrics": {"noise_floor_dbfs": -65}}
        with tempfile.TemporaryDirectory() as directory:
            inventory_file = Path(directory) / "inventory.toml"
            write_inventory(inventory_file)
            inventory = ta.Inventory.load(inventory_file)
            result = ta.media_smoke(
                backend,
                inventory,
                good_instrument(),
                Path(directory) / "artifact",
                seconds=3,
                quick_calibration=quick_calibration,
            )
        launch = next(
            call for call in backend.adb_calls if "android.intent.action.VIEW" in call
        )
        self.assertIn("org.videolan.vlc", launch)
        self.assertTrue(any("pw-top -b" in script for script, _ in backend.pi_calls))
        volume_sets = [call for call in backend.adb_calls if "--set" in call]
        self.assertEqual(volume_sets[0][-1], "25")
        self.assertEqual(volume_sets[-1][-1], "16")
        self.assertEqual(backend.music_volume, 16)
        self.assertEqual(
            result["android_music_volume"]["during"]["observed"]["value"], 25
        )
        self.assertEqual(result["metrics"]["verdict"], "PASS")

    def test_failed_media_launch_stops_units_and_restores_android_volume(self) -> None:
        backend = FakeBackend()
        normal_adb = backend.adb

        def adb(args, *, timeout=60):
            values = tuple(str(item) for item in args)
            if "android.intent.action.VIEW" in values:
                backend.adb_calls.append(values)
                return ta.CommandResult(1, "", "launch failed")
            return normal_adb(args, timeout=timeout)

        backend.adb = adb  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as directory:
            inventory_file = Path(directory) / "inventory.toml"
            write_inventory(inventory_file)
            inventory = ta.Inventory.load(inventory_file)
            with self.assertRaisesRegex(ta.RigFailure, "launch the installed VLC"):
                ta.media_smoke(
                    backend,
                    inventory,
                    good_instrument(),
                    Path(directory) / "artifact",
                    seconds=3,
                    quick_calibration={"metrics": {"noise_floor_dbfs": -65}},
                )
        self.assertEqual(backend.music_volume, 16)
        cleanup = "\n".join(script for script, _stdin in backend.pi_calls)
        self.assertIn(
            "for unit in larkbridge-e19-media-capture.service "
            "larkbridge-e19-media-pw-top.service",
            cleanup,
        )
        self.assertIn('systemctl --user stop "$unit"', cleanup)

    def test_failed_call_stimulus_stops_both_call_units(self) -> None:
        backend = FakeBackend()
        backend.snapshot = snapshot("CALL", "ACTIVE")
        normal_pi = backend.pi
        normal_adb = backend.adb

        def adb(args, *, timeout=60):
            values = tuple(str(item) for item in args)
            if values == ("shell", "dumpsys", "audio"):
                backend.adb_calls.append(values)
                return ta.CommandResult(0, "MODE_IN_COMMUNICATION", "")
            return normal_adb(args, timeout=timeout)

        def pi(script, *, timeout=60, stdin=None):
            if "larkbridge-e19-call-stimulus" in script and "systemd-run" in script:
                backend.pi_calls.append((script, stdin))
                return ta.CommandResult(1, "", "speaker failed")
            return normal_pi(script, timeout=timeout, stdin=stdin)

        backend.adb = adb  # type: ignore[method-assign]
        backend.pi = pi  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as directory:
            inventory_file = Path(directory) / "inventory.toml"
            write_inventory(inventory_file)
            inventory = ta.Inventory.load(inventory_file)
            with self.assertRaisesRegex(ta.RigFailure, "near-end acoustic stimulus"):
                ta.call_smoke(
                    backend,
                    inventory,
                    good_instrument(),
                    Path(directory) / "artifact",
                    seconds=3,
                )
        cleanup = "\n".join(script for script, _stdin in backend.pi_calls)
        self.assertIn(
            "for unit in larkbridge-e19-call-capture.service "
            "larkbridge-e19-call-stimulus.service",
            cleanup,
        )
        self.assertIn('systemctl --user stop "$unit"', cleanup)

    def test_session_start_never_caches_candidate_when_focused_tests_fail(self) -> None:
        candidate = mock.Mock(candidate_id="candidate")
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            ta, "resolve_candidate", return_value=candidate
        ), mock.patch.object(
            ta, "run_focused_tests", side_effect=ta.RigFailure("focused tests failed")
        ), mock.patch.object(
            ta, "cache_candidate"
        ) as cache:
            args = mock.Mock(
                artifacts=Path(directory),
                candidate=".",
                deadman=900,
            )
            with self.assertRaisesRegex(ta.RigFailure, "focused tests failed"):
                ta.command_session_start(args, mock.Mock(), FakeBackend())
        cache.assert_not_called()

    def test_call_iteration_requests_discord_before_candidate_mutation(self) -> None:
        backend = FakeBackend()
        before = snapshot("MEDIA_ACTIVE", "CALL_DOWN")
        with mock.patch.object(
            ta,
            "load_session",
            return_value={
                "baseline": {"boot_id": "boot-1"},
                "instrument_fingerprint": "fingerprint",
            },
        ), mock.patch.object(ta, "resolve_candidate") as resolve, mock.patch.object(
            ta, "run_focused_tests", return_value={}
        ), mock.patch.object(
            ta, "cache_candidate"
        ), mock.patch.object(
            ta, "capture_snapshot", return_value=before
        ):
            resolve.return_value = mock.Mock(candidate_id="candidate")
            args = mock.Mock(artifacts=Path("artifacts"), candidate=".", mode="call")
            inventory = mock.Mock()
            with self.assertRaises(ta.HardwareRequired):
                ta.command_iterate(args, inventory, backend)
        self.assertFalse(
            any("90-larkbridge-dev.conf" in script for script, _ in backend.pi_calls)
        )

    def test_transition_times_media_to_call_edge(self) -> None:
        before = snapshot("MEDIA_ACTIVE", "CALL_DOWN")
        after = snapshot("CALL", "ACTIVE")
        session = {
            "status": "active",
            "session_id": "session",
            "recovery_root": "/recovery",
            "baseline": {"boot_id": "boot-1"},
            "current_candidate": {"candidate_id": "candidate"},
        }
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            ta, "load_session", return_value=session
        ), mock.patch.object(ta, "arm_deadman"), mock.patch.object(
            ta, "capture_snapshot", side_effect=(before, after)
        ), mock.patch.object(
            ta,
            "wait_phone_transport",
            return_value=(after["status"]["phone"], 2.75),
        ):
            args = mock.Mock(artifacts=Path(directory), expect="call", timeout=12.0)
            code = ta.command_transition(args, mock.Mock(), FakeBackend())
            record = next((Path(directory) / "transitions").glob("*/transition.json"))
            document = json.loads(record.read_text())
        self.assertEqual(code, 0)
        self.assertEqual(
            document["before"]["status"]["phone"]["transport"], "MEDIA_ACTIVE"
        )
        self.assertEqual(document["after"]["status"]["phone"]["transport"], "CALL")
        self.assertEqual(document["elapsed_s"], 2.75)

    def test_capture_failure_rolls_back_to_cached_last_good_candidate(self) -> None:
        backend = FakeBackend()
        candidate = mock.Mock(
            candidate_id="new",
            content_sha256="new-content",
            policy_files={},
            manifest=lambda: {
                "candidate_id": "new",
                "content_sha256": "new-content",
                "policy_files": {},
            },
        )
        last_good = mock.Mock(
            candidate_id="good",
            content_sha256="good-content",
            policy_files={},
            manifest=lambda: {
                "candidate_id": "good",
                "content_sha256": "good-content",
                "policy_files": {},
            },
        )
        session = {
            "status": "active",
            "session_id": "session",
            "recovery_root": "/recovery",
            "baseline": {"boot_id": "boot-1"},
            "instrument": good_instrument(),
            "instrument_fingerprint": "fingerprint",
            "current_candidate": {
                "candidate_id": "good",
                "content_sha256": "good-content",
                "policy_files": {},
            },
            "last_good_candidate": "good",
            "iterations": [],
        }
        quick_calibration = {
            "instrument_fingerprint": "fingerprint",
            "capture_gain_request": "0%",
            "metrics": {
                "noise_floor_dbfs": -65,
                "above_floor_db": 45,
                "clipped_pct": 0,
            },
        }
        session["quick_calibration_sha256"] = ta.sha256_bytes(
            ta.canonical_json(quick_calibration)
        )
        state = snapshot(candidate_id="good")
        backend.snapshot = state
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            ta, "load_session", return_value=session
        ), mock.patch.object(
            ta, "resolve_candidate", return_value=candidate
        ), mock.patch.object(
            ta, "run_focused_tests", return_value={}
        ), mock.patch.object(
            ta, "confirm_candidate_still_current"
        ), mock.patch.object(
            ta, "cache_candidate"
        ), mock.patch.object(
            ta, "capture_snapshot", return_value=state
        ), mock.patch.object(
            ta, "load_quick_calibration", return_value=quick_calibration
        ), mock.patch.object(
            ta, "validate_quick_calibration"
        ), mock.patch.object(
            ta,
            "load_calibration",
            side_effect=AssertionError("full gate was consulted"),
        ), mock.patch.object(
            ta, "probe_instrument", return_value=good_instrument()
        ), mock.patch.object(
            ta, "instrument_fingerprint", return_value="fingerprint"
        ), mock.patch.object(
            ta, "validate_prepared_mixer_state"
        ), mock.patch.object(
            ta, "extend_preimages"
        ), mock.patch.object(
            ta, "arm_deadman"
        ), mock.patch.object(
            ta, "apply_candidate"
        ) as apply, mock.patch.object(
            ta, "verify_runtime", return_value=state
        ), mock.patch.object(
            ta, "media_smoke", side_effect=ta.RigFailure("capture failed")
        ), mock.patch.object(
            ta, "load_cached_candidate", return_value=last_good
        ):
            args = mock.Mock(
                artifacts=Path(directory),
                candidate=".",
                mode="media",
                deadman=900,
                seconds=3,
            )
            inventory = mock.Mock(
                instrument=ta.InstrumentSpec(),
                thresholds=ta.Thresholds(),
                aux_volume=0.95,
            )
            with self.assertRaisesRegex(ta.RigFailure, "capture failed"):
                ta.command_iterate(args, inventory, backend)
        self.assertEqual(apply.call_count, 2)
        self.assertIs(apply.call_args_list[-1].args[1], last_good)

    def test_first_failed_same_candidate_restores_deployed_baseline(self) -> None:
        backend = FakeBackend()
        candidate = mock.Mock(
            candidate_id="candidate",
            content_sha256="content",
            policy_files={},
            manifest=lambda: {
                "candidate_id": "candidate",
                "content_sha256": "content",
                "policy_files": {},
            },
        )
        quick = {
            "instrument_fingerprint": "fingerprint",
            "instrument": {"cable_id": "cable-a"},
            "capture_gain_request": "0%",
            "metrics": {
                "noise_floor_dbfs": -65,
                "above_floor_db": 45,
                "clipped_pct": 0,
            },
        }
        state = snapshot(candidate_id="candidate")
        session = {
            "status": "active",
            "session_id": "session",
            "recovery_root": "/recovery",
            "baseline": {"boot_id": "boot-1"},
            "instrument": good_instrument(),
            "instrument_fingerprint": "fingerprint",
            "quick_calibration_sha256": ta.sha256_bytes(ta.canonical_json(quick)),
            "current_candidate": candidate.manifest(),
            "last_good_candidate": None,
            "iterations": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            replacements = {
                "load_session": mock.Mock(return_value=session),
                "resolve_candidate": mock.Mock(return_value=candidate),
                "run_focused_tests": mock.Mock(return_value={}),
                "confirm_candidate_still_current": mock.Mock(),
                "cache_candidate": mock.Mock(),
                "capture_snapshot": mock.Mock(return_value=state),
                "load_quick_calibration": mock.Mock(return_value=quick),
                "validate_quick_calibration": mock.Mock(),
                "probe_instrument": mock.Mock(return_value=good_instrument()),
                "instrument_fingerprint": mock.Mock(return_value="fingerprint"),
                "validate_prepared_mixer_state": mock.Mock(),
                "classify_restart": mock.Mock(return_value="supervisor"),
                "extend_preimages": mock.Mock(),
                "arm_deadman": mock.Mock(),
                "capture_condition_snapshot": mock.Mock(return_value=state),
                "apply_candidate": mock.Mock(),
                "verify_runtime": mock.Mock(return_value=state),
                "media_smoke": mock.Mock(side_effect=ta.RigFailure("smoke failed")),
                "load_cached_candidate": mock.Mock(
                    side_effect=AssertionError("untested candidate used as last-good")
                ),
                "restore_remote_session": mock.Mock(return_value={"restored": True}),
            }
            with mock.patch.multiple(ta, **replacements), self.assertRaisesRegex(
                ta.RigFailure, "smoke failed"
            ):
                args = mock.Mock(
                    artifacts=Path(directory),
                    candidate=".",
                    mode="media",
                    deadman=900,
                    seconds=3,
                )
                inventory = mock.Mock(
                    instrument=ta.InstrumentSpec(),
                    thresholds=ta.Thresholds(),
                    aux_volume=0.95,
                    aux_target=ta.FIXED_AUX_TARGET,
                )
                ta.command_iterate(args, inventory, backend)
            checkpoint = json.loads((Path(directory) / ta.SESSION_FILE).read_text())
        self.assertEqual(checkpoint["status"], "iteration-failed-baseline-restored")
        self.assertIsNone(checkpoint["current_candidate"])
        self.assertIsNone(checkpoint["last_good_candidate"])
        replacements["restore_remote_session"].assert_called_once()
        replacements["load_cached_candidate"].assert_not_called()

    def test_failed_rollback_and_exact_restore_remain_retryable(self) -> None:
        session = {
            "status": "active",
            "session_id": "session",
            "recovery_root": "/recovery",
            "current_candidate": {"candidate_id": "failed"},
            "last_good_candidate": "missing",
            "iterations": [],
        }
        primary = ta.RigFailure("smoke failed")
        rollback = ta.SafetyFailure("cached candidate missing")
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(ta, "arm_deadman"), mock.patch.object(
                ta,
                "restore_remote_session",
                side_effect=ta.RigFailure("exact restore lost SSH"),
            ), self.assertRaisesRegex(
                ta.SafetyFailure,
                "smoke failed.*cached candidate missing.*exact restore lost SSH",
            ):
                checkpoint = Path(directory) / ta.SESSION_FILE
                ta._restore_iteration_baseline(
                    FakeBackend(),
                    session,
                    checkpoint,
                    {"mode": "media", "candidate_id": "failed"},
                    primary,
                    (rollback,),
                )
            restored = json.loads(checkpoint.read_text())
        self.assertEqual(restored["status"], "restoring")
        self.assertIn("cached candidate missing", restored["rollback_failures"][0])
        self.assertIn("exact restore lost SSH", restored["restore_failure"])

    def test_milestone_requires_explicit_muted_input_confirmation(self) -> None:
        backend = FakeBackend()
        backend.snapshot = snapshot("CALL", "ACTIVE")
        with mock.patch.object(
            ta,
            "load_session",
            return_value={
                "session_id": "session",
                "recovery_root": "/recovery",
                "baseline": {"boot_id": "boot-1"},
                "current_candidate": {"candidate_id": "candidate"},
            },
        ):
            args = mock.Mock(
                artifacts=Path("artifacts"),
                operator_confirmed_input_muted=False,
                ffmpeg_device="loopback",
                seconds=10,
            )
            inventory = mock.Mock()
            with self.assertRaisesRegex(ta.HardwareRequired, "mute the Windows"):
                ta.command_milestone(args, inventory, backend)

    def test_quick_calibration_prompts_before_any_bench_action(self) -> None:
        backend = FakeBackend()
        with tempfile.TemporaryDirectory() as directory:
            args = mock.Mock(hardware_ready=False, artifacts=Path(directory))
            with self.assertRaisesRegex(ta.HardwareRequired, "connect Pi AUX"):
                ta.command_quick_calibrate(args, mock.Mock(aux_volume=0.95), backend)
        self.assertEqual(backend.pi_calls, [])

    def test_quick_calibration_touches_minimum_then_records_explicit_gain(self) -> None:
        backend = FakeBackend()
        prepared = good_instrument()
        prepared["prepared_capture_gain_value"] = "12"

        def measured(_backend, _inventory, _instrument, artifact):
            artifact.mkdir(parents=True)
            return {
                "noise_floor_dbfs": -65,
                "tone_dbfs": -15,
                "above_floor_db": 50,
                "clipped_pct": 0,
            }

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            ta, "start_mixer_guard", return_value=("guard", "/recovery")
        ), mock.patch.object(
            ta, "prepare_mixer", return_value=prepared
        ) as prepare, mock.patch.object(
            ta, "perform_quick_calibration", side_effect=measured
        ), mock.patch.object(
            ta, "stop_mixer_guard", return_value={"restored": True}
        ):
            root = Path(directory)
            inventory_file = root / "inventory.toml"
            write_inventory(inventory_file)
            inventory = ta.Inventory.load(inventory_file)
            args = mock.Mock(
                hardware_ready=True,
                capture_gain="12%",
                artifacts=root / "artifacts",
            )
            code = ta.command_quick_calibrate(args, inventory, backend)
            document = json.loads(
                (args.artifacts / ta.QUICK_CALIBRATION_FILE).read_text()
            )
        self.assertEqual(code, 0)
        self.assertEqual(prepare.call_args_list[0].kwargs["capture_gain"], "0%")
        self.assertEqual(prepare.call_args_list[1].kwargs["capture_gain"], "12%")
        self.assertEqual(document["verdict"], "PASS")
        self.assertEqual(document["capture_gain_request"], "12%")

    def test_quick_calibration_failure_still_restores_mixer_preimage(self) -> None:
        backend = FakeBackend()
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            ta, "start_mixer_guard", return_value=("guard", "/recovery")
        ), mock.patch.object(
            ta, "prepare_mixer", return_value=good_instrument()
        ), mock.patch.object(
            ta, "perform_quick_calibration", side_effect=ta.RigFailure("capture failed")
        ), mock.patch.object(
            ta, "stop_mixer_guard", return_value={"restored": True}
        ) as stop:
            inventory_file = Path(directory) / "inventory.toml"
            write_inventory(inventory_file)
            inventory = ta.Inventory.load(inventory_file)
            args = mock.Mock(
                hardware_ready=True,
                capture_gain=None,
                artifacts=Path(directory) / "artifacts",
            )
            with self.assertRaisesRegex(ta.RigFailure, "capture failed"):
                ta.command_quick_calibrate(args, inventory, backend)
        stop.assert_called_once_with(backend, "guard", "/recovery")

    def test_session_start_uses_quick_gate_not_full_promotion_calibration(self) -> None:
        backend = FakeBackend()
        candidate = mock.Mock(
            candidate_id="candidate",
            policy_files={},
            manifest=lambda: {
                "candidate_id": "candidate",
                "content_sha256": "content",
                "policy_files": {},
            },
        )
        quick = {
            "instrument_fingerprint": "fingerprint",
            "capture_gain_request": "0%",
            "metrics": {
                "noise_floor_dbfs": -65,
                "above_floor_db": 45,
                "clipped_pct": 0,
            },
        }
        start_order: list[str] = []
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            ta, "resolve_candidate", return_value=candidate
        ), mock.patch.object(
            ta, "run_focused_tests", return_value={"returncode": 0}
        ), mock.patch.object(
            ta, "confirm_candidate_still_current"
        ), mock.patch.object(
            ta, "cache_candidate"
        ), mock.patch.object(
            ta, "capture_snapshot", return_value=snapshot()
        ), mock.patch.object(
            ta, "probe_instrument", return_value=good_instrument()
        ), mock.patch.object(
            ta, "instrument_fingerprint", return_value="fingerprint"
        ), mock.patch.object(
            ta, "load_quick_calibration", return_value=quick
        ), mock.patch.object(
            ta, "validate_quick_calibration"
        ), mock.patch.object(
            ta,
            "load_calibration",
            side_effect=AssertionError("full gate was consulted"),
        ), mock.patch.object(
            ta, "capture_preimages", return_value=({}, "/recovery")
        ), mock.patch.object(
            ta, "install_recovery_script"
        ), mock.patch.object(
            ta, "arm_deadman"
        ), mock.patch.object(
            ta,
            "prepare_mixer",
            side_effect=lambda *_args, **_kwargs: (
                start_order.append("prepare_mixer") or good_instrument()
            ),
        ) as prepare, mock.patch.object(
            ta, "policy_restart_from_snapshot", return_value="supervisor"
        ), mock.patch.object(
            ta,
            "apply_candidate",
            side_effect=lambda *_args, **_kwargs: start_order.append("apply_candidate"),
        ), mock.patch.object(
            ta,
            "verify_runtime",
            side_effect=lambda *_args, **_kwargs: (
                start_order.append("verify_runtime") or snapshot()
            ),
        ):
            args = mock.Mock(
                artifacts=Path(directory),
                candidate=".",
                deadman=900,
            )
            inventory = mock.Mock(
                aux_volume=0.95,
                instrument=ta.InstrumentSpec(),
                thresholds=ta.Thresholds(),
            )
            code = ta.command_session_start(args, inventory, backend)
            session = json.loads((Path(directory) / ta.SESSION_FILE).read_text())
        self.assertEqual(code, 0)
        self.assertIn("quick_calibration_sha256", session)
        self.assertEqual(session["focused_tests"]["returncode"], 0)
        self.assertEqual(prepare.call_args.kwargs["capture_gain"], "0%")
        self.assertEqual(
            start_order, ["apply_candidate", "verify_runtime", "prepare_mixer"]
        )

    def test_session_start_restore_failure_is_reported_and_retryable(self) -> None:
        backend = FakeBackend()
        candidate = mock.Mock(
            candidate_id="candidate",
            policy_files={},
            manifest=lambda: {
                "candidate_id": "candidate",
                "content_sha256": "content",
                "policy_files": {},
            },
        )
        quick = {
            "instrument_fingerprint": "fingerprint",
            "capture_gain_request": "0%",
            "metrics": {
                "noise_floor_dbfs": -65,
                "above_floor_db": 45,
                "clipped_pct": 0,
            },
        }
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            ta, "resolve_candidate", return_value=candidate
        ), mock.patch.object(
            ta, "run_focused_tests", return_value={"returncode": 0}
        ), mock.patch.object(
            ta, "confirm_candidate_still_current"
        ), mock.patch.object(
            ta, "cache_candidate"
        ), mock.patch.object(
            ta, "capture_snapshot", return_value=snapshot()
        ), mock.patch.object(
            ta, "probe_instrument", return_value=good_instrument()
        ), mock.patch.object(
            ta, "instrument_fingerprint", return_value="fingerprint"
        ), mock.patch.object(
            ta, "load_quick_calibration", return_value=quick
        ), mock.patch.object(
            ta, "validate_quick_calibration"
        ), mock.patch.object(
            ta, "capture_preimages", return_value=({}, "/recovery")
        ), mock.patch.object(
            ta, "install_recovery_script"
        ), mock.patch.object(
            ta, "arm_deadman"
        ), mock.patch.object(
            ta, "prepare_mixer", return_value=good_instrument()
        ), mock.patch.object(
            ta, "policy_restart_from_snapshot", return_value="supervisor"
        ), mock.patch.object(
            ta, "apply_candidate", side_effect=ta.RigFailure("candidate start failed")
        ), mock.patch.object(
            ta,
            "restore_remote_session",
            side_effect=ta.RigFailure("SSH connection lost during exact restore"),
        ) as restore:
            artifacts = Path(directory)
            args = mock.Mock(artifacts=artifacts, candidate=".", deadman=900)
            inventory = mock.Mock(
                aux_volume=0.95,
                instrument=ta.InstrumentSpec(),
                thresholds=ta.Thresholds(),
            )
            with self.assertRaisesRegex(
                ta.SafetyFailure,
                "primary failure: RigFailure: candidate start failed; cleanup failure: "
                "RigFailure: SSH connection lost during exact restore",
            ):
                ta.command_session_start(args, inventory, backend)
            checkpoint = json.loads((artifacts / ta.SESSION_FILE).read_text())
            with self.assertRaises(ta.RigFailure):
                ta.load_session(artifacts)
            retryable = ta.load_session(artifacts, allow_restoring=True)
        restore.assert_called_once()
        self.assertEqual(checkpoint["status"], "restoring")
        self.assertIn("candidate start failed", checkpoint["start_failure"])
        self.assertIn("SSH connection lost", checkpoint["restore_failure"])
        self.assertEqual(retryable["status"], "restoring")

    def test_session_start_install_or_arm_failure_restores_exact_baseline(self) -> None:
        candidate = mock.Mock(
            candidate_id="candidate",
            policy_files={},
            manifest=lambda: {
                "candidate_id": "candidate",
                "content_sha256": "content",
                "policy_files": {},
            },
        )
        quick = {
            "instrument_fingerprint": "fingerprint",
            "instrument": {"cable_id": "cable-a"},
            "capture_gain_request": "0%",
            "metrics": {
                "noise_floor_dbfs": -65,
                "above_floor_db": 45,
                "clipped_pct": 0,
            },
        }
        for failed_name in ("install_recovery_script", "arm_deadman"):
            with self.subTest(
                failed_name=failed_name
            ), tempfile.TemporaryDirectory() as directory:
                replacements = {
                    "resolve_candidate": mock.Mock(return_value=candidate),
                    "run_focused_tests": mock.Mock(return_value={"returncode": 0}),
                    "confirm_candidate_still_current": mock.Mock(),
                    "cache_candidate": mock.Mock(),
                    "capture_snapshot": mock.Mock(return_value=snapshot()),
                    "probe_instrument": mock.Mock(return_value=good_instrument()),
                    "instrument_fingerprint": mock.Mock(return_value="fingerprint"),
                    "load_quick_calibration": mock.Mock(return_value=quick),
                    "validate_quick_calibration": mock.Mock(),
                    "capture_preimages": mock.Mock(return_value=({}, "/recovery")),
                    "install_recovery_script": mock.Mock(),
                    "arm_deadman": mock.Mock(),
                    "prepare_mixer": mock.Mock(return_value=good_instrument()),
                    "policy_restart_from_snapshot": mock.Mock(
                        return_value="supervisor"
                    ),
                    "apply_candidate": mock.Mock(),
                    "verify_runtime": mock.Mock(return_value=snapshot()),
                    "restore_remote_session": mock.Mock(
                        return_value={"restored": True}
                    ),
                }
                replacements[failed_name].side_effect = ta.RigFailure(
                    f"{failed_name} failed"
                )
                with mock.patch.multiple(ta, **replacements):
                    artifacts = Path(directory)
                    args = mock.Mock(artifacts=artifacts, candidate=".", deadman=900)
                    inventory = mock.Mock(
                        aux_volume=0.95,
                        aux_target=ta.FIXED_AUX_TARGET,
                        instrument=ta.InstrumentSpec(),
                        thresholds=ta.Thresholds(),
                    )
                    with self.assertRaisesRegex(ta.RigFailure, failed_name):
                        ta.command_session_start(args, inventory, FakeBackend())
                    checkpoint = json.loads((artifacts / ta.SESSION_FILE).read_text())
                self.assertEqual(checkpoint["status"], "start-failed-restored")
                self.assertTrue(checkpoint["restore"]["restored"])
                replacements["restore_remote_session"].assert_called_once()

    def test_calibration_failure_still_restores_mixer_preimage(self) -> None:
        backend = FakeBackend()
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            ta, "start_mixer_guard", return_value=("guard", "/recovery")
        ), mock.patch.object(
            ta, "prepare_mixer", return_value=good_instrument()
        ), mock.patch.object(
            ta, "perform_calibration_stage", side_effect=ta.RigFailure("capture failed")
        ), mock.patch.object(
            ta, "stop_mixer_guard", return_value={"restored": True}
        ) as stop:
            inventory_file = Path(directory) / "inventory.toml"
            write_inventory(inventory_file)
            inventory = ta.Inventory.load(inventory_file)
            args = mock.Mock(
                hardware_ready=True,
                stage="self-loop",
                capture_gain=None,
                artifacts=Path(directory) / "artifacts",
            )
            with self.assertRaisesRegex(ta.RigFailure, "capture failed"):
                ta.command_calibrate(args, inventory, backend)
        stop.assert_called_once_with(backend, "guard", "/recovery")


if __name__ == "__main__":
    unittest.main()
