from __future__ import annotations

import json
import math
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
  Format: S16_LE
  Channels: 2
  Rates: 48000
Capture:
  Format: S16_LE
  Channels: 1
  Rates: 48000
""",
    }


def snapshot(transport: str = "MEDIA_ACTIVE", state: str = "CALL_DOWN") -> dict:
    return {
        "boot_id": "boot-1",
        "deployed_hashes": {},
        "services": {
            "stdout": """Id=pipewire.service
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
Environment=LARKBRIDGE_DEV_CANDIDATE=candidate"""
        },
        "system_services": {"stdout": "Id=bluetooth.service\nActiveState=active\nNRestarts=0"},
        "bluetooth": {"stdout": "UUID: Audio Sink (0000110b-0000)"},
        "status": {
            "state": state,
            "output": {"observed_volume": 0.95},
            "phone": {
                "transport": transport,
                "route_verified": transport == "MEDIA_ACTIVE",
                "route_count": 1,
                "expected_target": "alsa_output.platform-bcm2835.Headphones",
                "microphone_uplink_count": 1,
                "physical_microphone_bypass": False,
                "android_microphone_transport": transport == "CALL",
            },
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
        return ta.CommandResult(0, "tests passed", "")

    def pi(self, script, *, timeout=60, stdin=None):
        self.pi_calls.append((script, stdin))
        if "usb_id,required_port" in script:
            return ta.CommandResult(0, json.dumps(self.instrument), "")
        if "'deployed_hashes':hashes" in script:
            return ta.CommandResult(0, json.dumps(self.snapshot), "")
        if "amixer -D" in script:
            return ta.CommandResult(0, MIXER, "")
        if "ActiveState --value" in script:
            return ta.CommandResult(0, "inactive\n", "")
        if script == f"cat {ta.STATUS_PATH}":
            return ta.CommandResult(0, json.dumps(self.snapshot["status"]), "")
        return ta.CommandResult(0, "ok\n", "")

    def adb(self, args, *, timeout=60):
        values = tuple(str(item) for item in args)
        self.adb_calls.append(values)
        if values[:3] == ("shell", "pm", "path"):
            return ta.CommandResult(0, "package:/data/app/org.videolan.vlc/base.apk\n", "")
        if values == ("shell", "dumpsys", "audio"):
            return ta.CommandResult(0, "MODE_NORMAL", "")
        if values == ("devices",):
            return ta.CommandResult(0, "serial\tdevice\n", "")
        return ta.CommandResult(0, "ok\n", "")

    def fetch(self, remote, local, *, recursive=False):
        local.parent.mkdir(parents=True, exist_ok=True)
        if recursive:
            local.mkdir(parents=True, exist_ok=True)
            for role in ("reference", "raw", "clean"):
                write_sine(local / f"{role}_quick.wav")
        elif self.fetch_wav is not None:
            local.write_bytes(self.fetch_wav.read_bytes())
        else:
            write_sine(local)

    def wait(self, seconds):
        self.clock += seconds


def write_sine(path: Path, *, seconds: float = 3.0, amplitude: float = 0.25) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rate = ta.GENERALPLUS_RATE
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        frames = []
        for index in range(int(rate * seconds)):
            value = int(32767 * amplitude * math.sin(2 * math.pi * 1000 * index / rate))
            frames.append(struct.pack("<h", value))
        handle.writeframes(b"".join(frames))


def write_inventory(path: Path) -> None:
    path.write_text(
        """pi_host = "pi"
generalplus_usb_id = "1b3f:2008"
generalplus_port_path = "1-1.5"
generalplus_cable_id = "cable-a"
generalplus_speaker_position_id = "position-a"
""",
        encoding="utf-8",
    )


class InstrumentTests(unittest.TestCase):
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
        backend.instrument.update(ready=False, reason="device is at 1-1.4, required 1-1.5")
        with self.assertRaises(ta.HardwareRequired):
            ta.probe_instrument(backend, ta.InstrumentSpec())

    def test_wrong_pcm_capability_fails_before_capture(self) -> None:
        report = good_instrument()
        report["stream_capabilities"] = report["stream_capabilities"].replace(
            "Channels: 1", "Channels: 2"
        )
        with self.assertRaisesRegex(ta.HardwareRequired, "capture 1ch"):
            ta.validate_instrument_capabilities(report, ta.InstrumentSpec())

    def test_unqualified_mixer_map_fails_closed(self) -> None:
        report = good_instrument()
        report["mixer_contents"] = "name='Mic Capture Volume',\n"
        with self.assertRaises(ta.HardwareRequired):
            ta.validate_mixer_map(report, ta.InstrumentSpec())

    def test_safe_mixer_disables_agc_and_sidetone_and_starts_at_minimum_gain(self) -> None:
        script = ta.safe_mixer_script(ta.InstrumentSpec(), "GeneralPlus")
        self.assertIn("Auto Gain Control", script)
        self.assertIn("Mic Playback Switch", script)
        self.assertIn("Mic Capture Switch", script)
        self.assertIn("Mic Capture Volume", script)
        self.assertIn("0%", script)
        self.assertNotIn("+30", script)

    def test_mixer_map_fingerprint_ignores_values_but_state_gate_does_not(self) -> None:
        drifted = MIXER.replace("values=off", "values=on", 1)
        self.assertEqual(ta.mixer_map_sha256(MIXER), ta.mixer_map_sha256(drifted))
        report = good_instrument()
        report["mixer_contents"] = drifted
        with self.assertRaisesRegex(ta.HardwareRequired, "mixer drifted"):
            ta.validate_prepared_mixer_state(report, ta.InstrumentSpec())


class CalibrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.thresholds = ta.Thresholds()
        self.good = {
            "instrument_fingerprint": "fingerprint",
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

    def test_self_loop_metrics_derive_linearity_dynamic_range_and_clipping(self) -> None:
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
        subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-qm", "baseline"], check=True)

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
            self.assertEqual(candidate.policy_files["50.conf"].replace(b"\r\n", b"\n"), b"roles\n")

    def test_restart_classification(self) -> None:
        manifest = {"content_sha256": "same", "policy_files": {"50.conf": "old"}}
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
            self.assertIsNotNone(test_root)
            self.assertNotEqual(test_root, root)
            assert test_root is not None
            self.assertFalse(test_root.exists())


class TransactionTests(unittest.TestCase):
    def test_deadman_is_transient_and_bounded(self) -> None:
        backend = FakeBackend()
        ta.arm_deadman(backend, "session", "/recovery", 900)
        script = backend.pi_calls[-1][0]
        self.assertIn("systemd-run --user", script)
        self.assertIn("--on-active=900s", script)
        self.assertIn("/recovery/restore.sh", script)
        with self.assertRaises(ta.SafetyFailure):
            ta.arm_deadman(backend, "session", "/recovery", 10)

    def test_recovery_uses_full_audio_stack_and_exact_hash_verification(self) -> None:
        script = ta._recovery_script("session", "/recovery", ta.InstrumentSpec())
        self.assertIn("stop bridge-supervisor.service", script)
        self.assertIn(
            "restart pipewire.service pipewire-pulse.service wireplumber.service", script
        )
        self.assertIn("matches", script)
        self.assertNotIn("restart wireplumber.service\n", script)

    def test_staging_is_volatile_and_override_points_at_complete_package(self) -> None:
        backend = FakeBackend()
        candidate = ta.Candidate(
            "source", Path("."), "revision", "candidate", "diff", "content", (), (), b"tar", {}
        )
        ta.apply_candidate(backend, candidate, "session", "supervisor")
        joined = "\n".join(call[0] for call in backend.pi_calls)
        self.assertIn("/run/user/1000/larkbridge-dev/session/candidates/candidate", joined)
        self.assertIn("90-larkbridge-dev.conf", joined)
        self.assertIn("restart bridge-supervisor.service", joined)
        self.assertNotIn("restart wireplumber.service", joined)

    def test_expired_deadman_marker_is_detected_before_an_iteration(self) -> None:
        state = snapshot()
        state["services"]["stdout"] = state["services"]["stdout"].replace(
            "Environment=LARKBRIDGE_DEV_CANDIDATE=candidate", "Environment="
        )
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

    def test_removed_candidate_policy_is_removed_only_inside_managed_directory(self) -> None:
        backend = FakeBackend()
        candidate = ta.Candidate(
            "source", Path("."), "revision", "candidate", "diff", "content", (), (), b"tar", {}
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
                handle.writeframes(struct.pack("<h", 32767) * rate * 3)
            result = ta.score_media(
                path,
                calibrated_noise_floor_dbfs=-65,
                thresholds=ta.Thresholds(),
            )
        self.assertEqual(result["verdict"], "FAIL")
        self.assertTrue(any("clipped" in failure for failure in result["failures"]))

    def test_route_gate_rejects_decoy_and_duplicate(self) -> None:
        state = snapshot()
        state["status"]["phone"].update(
            expected_target="alsa_output.usb-GeneralPlus", route_count=2
        )
        failures = ta._route_failures("media", state)
        self.assertTrue(any("GeneralPlus" in failure for failure in failures))
        self.assertTrue(any("exactly one" in failure for failure in failures))

    def test_call_gate_rejects_physical_microphone_bypass(self) -> None:
        state = snapshot("CALL", "ACTIVE")
        state["status"]["phone"]["physical_microphone_bypass"] = True
        self.assertTrue(any("bypasses" in value for value in ta._route_failures("call", state)))


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
        calibration = {
            "stages": {"self-loop": {"noise_floor_dbfs": -65}}
        }
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
                calibration=calibration,
            )
        launch = next(call for call in backend.adb_calls if "android.intent.action.VIEW" in call)
        self.assertIn("org.videolan.vlc", launch)
        self.assertEqual(result["metrics"]["verdict"], "PASS")

    def test_call_iteration_requests_discord_before_candidate_mutation(self) -> None:
        backend = FakeBackend()
        before = snapshot("MEDIA_ACTIVE", "CALL_DOWN")
        with mock.patch.object(ta, "load_session", return_value={
            "baseline": {"boot_id": "boot-1"},
            "instrument_fingerprint": "fingerprint",
        }), mock.patch.object(ta, "resolve_candidate") as resolve, mock.patch.object(
            ta, "run_focused_tests", return_value={}
        ), mock.patch.object(ta, "cache_candidate"), mock.patch.object(
            ta, "capture_snapshot", return_value=before
        ):
            resolve.return_value = mock.Mock(candidate_id="candidate")
            args = mock.Mock(
                artifacts=Path("artifacts"), candidate=".", mode="call"
            )
            inventory = mock.Mock()
            with self.assertRaises(ta.HardwareRequired):
                ta.command_iterate(args, inventory, backend)
        self.assertFalse(any("90-larkbridge-dev.conf" in script for script, _ in backend.pi_calls))

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
        calibration = {
            "instrument_fingerprint": "fingerprint",
            "stages": {
                "self-loop": {
                    "noise_floor_dbfs": -65,
                    "linearity_error_db": 1,
                    "dynamic_range_db": 55,
                    "clipped_pct": 0,
                },
                "aux-loop": {"above_floor_db": 45, "clipped_pct": 0},
                "acoustic": {"snr_db": 25, "clipped_pct": 0},
            },
        }
        state = snapshot()
        state["services"]["stdout"] = state["services"]["stdout"].replace(
            "LARKBRIDGE_DEV_CANDIDATE=candidate", "LARKBRIDGE_DEV_CANDIDATE=good"
        )
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            ta, "load_session", return_value=session
        ), mock.patch.object(ta, "resolve_candidate", return_value=candidate), mock.patch.object(
            ta, "run_focused_tests", return_value={}
        ), mock.patch.object(ta, "confirm_candidate_still_current"), mock.patch.object(
            ta, "cache_candidate"
        ), mock.patch.object(ta, "capture_snapshot", return_value=state), mock.patch.object(
            ta, "load_calibration", return_value=calibration
        ), mock.patch.object(ta, "validate_calibration"), mock.patch.object(
            ta, "probe_instrument", return_value=good_instrument()
        ), mock.patch.object(ta, "instrument_fingerprint", return_value="fingerprint"), mock.patch.object(
            ta, "validate_prepared_mixer_state"
        ), mock.patch.object(
            ta, "extend_preimages"
        ), mock.patch.object(ta, "arm_deadman"), mock.patch.object(
            ta, "apply_candidate"
        ) as apply, mock.patch.object(ta, "verify_runtime", return_value=state), mock.patch.object(
            ta, "media_smoke", side_effect=ta.RigFailure("capture failed")
        ), mock.patch.object(ta, "load_cached_candidate", return_value=last_good):
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

    def test_milestone_requires_explicit_muted_input_confirmation(self) -> None:
        backend = FakeBackend()
        backend.snapshot = snapshot("CALL", "ACTIVE")
        with mock.patch.object(
            ta,
            "load_session",
            return_value={
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
