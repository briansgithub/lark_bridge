from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile


MODULE_PATH = Path(__file__).resolve().parents[1] / "arrival.py"
SPEC = importlib.util.spec_from_file_location("arrival", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
arrival = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(arrival)


class FakeSerial:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.lines = [b"identity model=ESP32\r\n", b""]
        self.closed = False

    def readline(self):
        if self.lines:
            return self.lines.pop(0)
        return b""

    def close(self):
        self.closed = True


class ArrivalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def new_run(self) -> Path:
        return arrival.create_run(
            "H3",
            "heartbeat recovery",
            "test operator",
            self.root / "results",
            source={"commit": "a" * 40, "dirty": False, "dirty_paths": []},
            started_at="2026-08-25T12:00:00+00:00",
        )

    def test_new_record_and_finalize_run(self):
        run = self.new_run()
        artifact = self.root / "monitor.log"
        artifact.write_text("boot okay\n", encoding="utf-8")
        expected_digest = arrival.sha256(artifact)

        evidence = arrival.record_artifact(run, artifact, "cold boot")
        result_path = arrival.finalize_run(run, "PASS", "thresholds met")
        result = json.loads(result_path.read_text(encoding="utf-8"))

        self.assertEqual("H3", result["gate"])
        self.assertEqual("PASS", result["status"])
        self.assertFalse(result["hard_failure"])
        self.assertEqual("cold boot", evidence["note"])
        self.assertEqual(expected_digest, evidence["sha256"])
        self.assertEqual("thresholds met", result["notes"][0]["text"])

    def test_failed_run_is_a_hard_failure_and_cannot_be_refinalized(self):
        run = self.new_run()
        result_path = arrival.finalize_run(run, "FAIL")
        result = json.loads(result_path.read_text(encoding="utf-8"))
        self.assertTrue(result["hard_failure"])
        with self.assertRaises(arrival.ArrivalError):
            arrival.finalize_run(run, "PASS")

    def test_capture_is_timestamped_and_attached(self):
        run = self.new_run()
        instances = []

        def factory(**kwargs):
            instance = FakeSerial(**kwargs)
            instances.append(instance)
            return instance

        destination, evidence = arrival.capture_serial(
            run,
            "COM9",
            0.01,
            name="s31-boot.log",
            serial_factory=factory,
        )
        text = destination.read_text(encoding="utf-8")
        result = json.loads((run / "result.json").read_text(encoding="utf-8"))
        self.assertIn("CAPTURE_START port=COM9 baud=115200", text)
        self.assertIn("identity model=ESP32", text)
        self.assertIn("CAPTURE_END", text)
        self.assertEqual("serial_log", evidence["kind"])
        self.assertEqual(evidence["sha256"], result["evidence"][0]["sha256"])
        self.assertTrue(instances[0].closed)

    def make_build(self, target: str = "output") -> tuple[Path, dict]:
        firmware = self.root / "firmware"
        project = firmware / target
        build = project / "build"
        files = {
            build / "bootloader" / "bootloader.bin": b"boot",
            build / "partition_table" / "partition-table.bin": b"partitions",
            build / f"larkbridge_{target}.bin": b"application",
            build / "flash_args": b"0x0 bootloader/bootloader.bin\n",
            build / "flasher_args.json": json.dumps(
                {
                    "extra_esptool_args": {
                        "chip": "esp32s31" if target == "s31" else "esp32",
                        "before": "default-reset",
                        "after": "hard-reset",
                        "stub": target != "s31",
                    }
                }
            ).encode("utf-8"),
        }
        for path, content in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        (firmware / "toolchain.lock.json").write_text("{}\n", encoding="utf-8")
        (project / "dependencies.lock").write_text("components: []\n", encoding="utf-8")
        (project / "sdkconfig.defaults").write_text("CONFIG_BT_ENABLED=y\n", encoding="utf-8")
        records = [
            {
                "path": path.relative_to(project).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": arrival.sha256(path),
            }
            for path in files
        ]
        manifest = {
            "schema": 1,
            "target": target,
            "source_commit": "b" * 40,
            "source_dirty": False,
            "toolchain_lock_sha256": arrival.sha256(firmware / "toolchain.lock.json"),
            "component_lock_sha256": arrival.sha256(project / "dependencies.lock"),
            "artifacts": records,
        }
        manifest_path = build / "larkbridge-build-manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        return firmware, manifest

    def test_manifest_validation_and_flash_command(self):
        firmware, _ = self.make_build()
        manifest, artifacts = arrival.validate_build_manifest("output", firmware)
        commands = arrival.flash_command("output", "COM12", firmware)
        self.assertEqual("output", manifest["target"])
        self.assertEqual(5, len(artifacts))
        self.assertIn("python -m esptool --chip esp32 --port COM12", commands[1])
        self.assertEqual("idf.py -p COM12 monitor", commands[3])

        artifacts[0].write_bytes(b"changed")
        with self.assertRaisesRegex(arrival.ArrivalError, "hash mismatch"):
            arrival.validate_build_manifest("output", firmware)

    def test_dirty_manifest_requires_explicit_override(self):
        firmware, _ = self.make_build()
        path = firmware / "output" / "build" / "larkbridge-build-manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["source_dirty"] = True
        path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(arrival.ArrivalError, "dirty source tree"):
            arrival.validate_build_manifest("output", firmware)
        arrival.validate_build_manifest("output", firmware, allow_dirty=True)

    def test_bundle_contains_flash_inputs_and_checksum(self):
        firmware, _ = self.make_build("s31")
        output = self.root / "dist" / "s31.zip"
        bundle, checksum = arrival.create_bundle(
            "s31", output, firmware_root=firmware
        )
        with zipfile.ZipFile(bundle) as archive:
            names = set(archive.namelist())
            info = json.loads(archive.read("BUNDLE_INFO.json"))
        self.assertIn("firmware/s31/build/larkbridge_s31.bin", names)
        self.assertIn("firmware/s31/build/larkbridge-build-manifest.json", names)
        self.assertIn("firmware/s31/sdkconfig.defaults", names)
        self.assertIn("firmware/s31/dependencies.lock", names)
        self.assertIn("firmware/toolchain.lock.json", names)
        self.assertIn("FLASH.ps1", names)
        self.assertEqual("s31", info["target"])
        self.assertEqual(".\\FLASH.ps1 -Port <SERIAL_PORT>", info["flash_command"])
        self.assertTrue(checksum.read_text(encoding="ascii").startswith(arrival.sha256(bundle)))


if __name__ == "__main__":
    unittest.main()
