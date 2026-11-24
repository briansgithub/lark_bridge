import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hil  # noqa: E402

MANIFEST = {
    "schema": 1,
    "target": "s31",
    "image_kind": "test",
    "source_commit": "0123456789abcdef0123",
    "source_dirty": False,
    "toolchain": {"esp_idf_tag": "v6.1-rc1"},
}


def stats(version="0123456789ab-test", image="test", up=1000, uac=None):
    record = {"t": "stats", "role": "s31", "version": version, "image": image, "up_ms": up}
    if uac is not None:
        record["uac"] = uac
    return json.dumps(record)


BOOT = json.dumps({
    "t": "boot", "role": "s31", "version": "0123456789ab-test", "image": "test",
    "idf": "v6.1-rc1", "reset": "usb", "bt": "1c:29:04:d0:61:5f", "erase_bonds": 0,
    "aec_publish": 0,
})


def log(*lines):
    return "\n".join(f"[2026-11-24T11:00:{i:02d}+00:00] {line}" for i, line in enumerate(lines))


class RegistryTests(unittest.TestCase):
    """Stage B exit test for the registry: the shipped gates.toml must be consistent."""

    def test_shipped_registry_is_consistent(self):
        gates = hil.load_gates()
        self.assertEqual(hil.validate_gates(gates), [])
        self.assertEqual(gates["stageB-exit"]["needs"], ["T2-smoke-s31", "T2-smoke-output"])

    def test_cycle_and_unknown_prerequisite_detected(self):
        gates = hil.load_gates()
        gates["H2"] = dict(gates["H2"], needs=["H3"])
        gates["H4"] = dict(gates["H4"], needs=["nope"])
        problems = hil.validate_gates(gates)
        self.assertTrue(any("cycle" in p for p in problems))
        self.assertTrue(any("unknown prerequisite nope" in p for p in problems))

    def test_stage_b_exit_follows_both_smokes(self):
        gates = hil.load_gates()
        # Independent of the live registry, where these gates may already have passed.
        for name in ("T2-smoke-s31", "T2-smoke-output", "stageB-exit"):
            gates[name]["status"] = "PENDING"
        verdicts = {"T2-smoke-s31": {"verdict": "PASS"}}
        self.assertNotEqual(hil.effective_status(gates, verdicts)["stageB-exit"], "PASS")
        verdicts["T2-smoke-output"] = {"verdict": "PASS"}
        status = hil.effective_status(gates, verdicts)
        self.assertEqual(status["stageB-exit"], "PASS")
        ready = hil.runnable(gates, status)
        self.assertIn("H6", ready)
        self.assertNotIn("H2", ready)  # still needs the harness (a person)
        self.assertNotIn("H0", ready)  # human gate excluded by default
        self.assertNotIn("T2-smoke-s31", hil.runnable(gates, hil.effective_status(gates, {}),
                                                      allow_reset=False))


class SmokeReducerTests(unittest.TestCase):
    def test_boot_matches_manifest(self):
        verdict, metrics, _ = hil.reduce_t2_smoke(
            log("ESP-ROM:esp32s31", "rst:0x17 (CHIP_USB_UART_RESET)",
                BOOT, stats(), stats(up=2000)),
            board="s31", manifest=MANIFEST)
        self.assertEqual(verdict, "PASS", metrics)

    def test_status_reply_stands_in_for_a_missed_boot(self):
        status = BOOT.replace('"t": "boot"', '"t": "status"')
        verdict, metrics, _ = hil.reduce_t2_smoke(log(stats(), status, stats(up=2000)),
                                                  board="s31", manifest=MANIFEST)
        self.assertEqual(verdict, "PASS")
        self.assertEqual(metrics["identity_source"], "status")

    def test_stats_only_is_inconclusive(self):
        verdict, _, _ = hil.reduce_t2_smoke(log(stats(), stats(up=2000)), board="s31",
                                            manifest=MANIFEST)
        self.assertEqual(verdict, "INCONCLUSIVE")

    def test_version_or_image_mismatch_fails(self):
        verdict, _, reasons = hil.reduce_t2_smoke(
            log(BOOT.replace("-test", ""), stats(), stats()), board="s31", manifest=MANIFEST)
        self.assertEqual(verdict, "FAIL")
        self.assertTrue(any("version" in r for r in reasons))
        release = dict(MANIFEST, image_kind="release")
        verdict, _, reasons = hil.reduce_t2_smoke(log(BOOT, stats(), stats()), board="s31",
                                                  manifest=release)
        self.assertEqual(verdict, "FAIL")

    def test_reboot_loop_and_erase_bonds_fail(self):
        verdict, _, _ = hil.reduce_t2_smoke(log(BOOT, "rst:0x3", BOOT, "rst:0x3", stats(), stats()),
                                            board="s31", manifest=MANIFEST)
        self.assertEqual(verdict, "FAIL")
        verdict, _, reasons = hil.reduce_t2_smoke(
            log(BOOT.replace('"erase_bonds": 0', '"erase_bonds": 1'), stats(), stats()),
            board="s31", manifest=MANIFEST)
        self.assertEqual(verdict, "FAIL")
        self.assertIn("erase_bonds is not 0", reasons)

    def test_single_crash_and_mixed_boot_stats_fail(self):
        for evidence in (
                log(BOOT, stats(), stats(up=2000), "Guru Meditation"),
                log(BOOT, stats(), stats(up=500)),
                log(BOOT, stats(version="old", up=2000))):
            with self.subTest(evidence=evidence):
                verdict, _, _ = hil.reduce_t2_smoke(evidence, board="s31", manifest=MANIFEST)
                self.assertEqual(verdict, "FAIL")

    def test_matching_release_cannot_pass_stage_b(self):
        release = dict(MANIFEST, image_kind="release")
        boot = BOOT.replace("-test", "").replace('"test"', '"release"')
        verdict, _, reasons = hil.reduce_t2_smoke(
            log(boot, stats("0123456789ab", "release"), stats("0123456789ab", "release", up=2000)),
            board="s31", manifest=release)
        self.assertEqual(verdict, "FAIL")
        self.assertIn("Stage B smoke requires a test image manifest", reasons)

    def test_old_image_without_json_fails(self):
        verdict, _, _ = hil.reduce_t2_smoke(log("I (643) s31_main: IDENTITY role=s31"),
                                            board="s31", manifest=MANIFEST)
        self.assertEqual(verdict, "FAIL")


class H6ReducerTests(unittest.TestCase):
    BOOT_LOG = "I (1103) s31_uac: LARK 3547:0407 capture ready at 48 kHz/S16/stereo\n"

    def test_clean_long_capture_is_partial_never_pass(self):
        text = ("[2026-11-24T05:00:00+00:00] E (1) s31_link: route ACK timed out\n"
                "[2026-11-24T05:40:00+00:00] E (2) s31_link: route ACK timed out\n")
        verdict, metrics, reasons = hil.reduce_h6_uac(text, boot_log=self.BOOT_LOG)
        self.assertEqual(verdict, "PARTIAL")
        self.assertEqual(metrics["capture_seconds"], 2400.0)
        self.assertTrue(any("100/100" in r for r in reasons))

    def test_transfer_error_fails(self):
        text = log("E (5) s31_uac: LARK UAC transfer error")
        verdict, _, _ = hil.reduce_h6_uac(text, boot_log=self.BOOT_LOG)
        self.assertEqual(verdict, "FAIL")

    def test_stats_counters(self):
        text = log(stats(uac={"rx": 10, "xfer_err": 0, "read_err": 0}),
                   stats(uac={"rx": 900, "xfer_err": 1, "read_err": 0}))
        verdict, metrics, _ = hil.reduce_h6_uac(text, boot_log=self.BOOT_LOG)
        self.assertEqual(verdict, "FAIL")
        self.assertEqual(metrics["uac_rx_packets_delta"], 890)


class SoakReducerTests(unittest.TestCase):
    def test_soak(self):
        good = {"polls": 3, "polls_link_up": 3, "link_drops": [], "s31_reboot_markers": 0,
                "finished_utc": "2026-11-24T11:00:00+00:00", "s31_lines": 10}
        self.assertEqual(hil.reduce_bt_soak(good)[0], "PASS")
        for missing in ({"finished_utc": None}, {"completed": False}, {"poll_errors": 1},
                        {"serial_errors": [{"error": "disconnected"}]}, {"s31_lines": 0}):
            self.assertEqual(hil.reduce_bt_soak(dict(good, **missing))[0], "INCONCLUSIVE")
        bad = dict(good, polls_link_up=2, link_drops=[{"utc": "x"}])
        self.assertEqual(hil.reduce_bt_soak(bad)[0], "FAIL")
        self.assertEqual(hil.reduce_bt_soak({"polls": 0})[0], "INCONCLUSIVE")


class LeaseTests(unittest.TestCase):
    def test_exclusive_renew_and_stale_takeover(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rig.lease"
            hil.acquire_lease(path, "a", ttl=60, now=1000)
            with self.assertRaises(hil.HilError):
                hil.acquire_lease(path, "a", ttl=60, now=1010, renew=False)
            hil.acquire_lease(path, "a", ttl=60, now=1010)  # renew
            with self.assertRaises(hil.HilError):
                hil.acquire_lease(path, "b", ttl=60, now=1020)
            with self.assertRaises(hil.HilError):
                hil.release_lease(path, "b")
            record = hil.acquire_lease(path, "b", ttl=60, now=5000)  # a's lease expired
            self.assertEqual(record["owner"], "b")
            self.assertTrue(hil.release_lease(path, "b"))
            self.assertFalse(hil.release_lease(path, "b"))


class FlashCommandTests(unittest.TestCase):
    def test_app_only_command_rejects_extra_write_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build = root / "s31" / "build"
            build.mkdir(parents=True)
            app = build / "larkbridge_s31_gateway.bin"
            definition = {
                "app": {"offset": "0x10000", "file": app.name, "encrypted": "false"},
                "extra_esptool_args": {"chip": "esp32s31", "stub": False},
                "write_flash_args": ["--flash-mode", "dio"],
            }
            path = build / "flasher_args.json"
            with patch.object(hil, "FIRMWARE_ROOT", root), patch.object(
                hil.arrival, "validate_build_manifest", return_value=(MANIFEST, [app.resolve()])
            ):
                path.write_text(json.dumps(definition))
                command = hil.app_flash_command("s31", "COM3")
                self.assertEqual(command[-5:], ["write-flash", "--flash-mode", "dio",
                                                "0x10000", str(app.resolve())])
                definition["write_flash_args"] += ["--erase-all", "--force"]
                path.write_text(json.dumps(definition))
                with self.assertRaises(hil.HilError):
                    hil.app_flash_command("s31", "COM3")


if __name__ == "__main__":
    unittest.main()
