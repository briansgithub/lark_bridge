from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "build_firmware.py"
SPEC = importlib.util.spec_from_file_location("build_firmware", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
build_firmware = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_firmware)


class BuildFirmwareTests(unittest.TestCase):
    def test_output_command_is_pinned(self) -> None:
        lock = build_firmware.load_lock()
        command = build_firmware.docker_command("output", lock, "docker-test", pull=False)
        rendered = " ".join(command)
        self.assertEqual(command[:3], ["docker-test", "run", "--rm"])
        self.assertIn("espressif/idf:v6.0.2", command)
        self.assertIn("7101770dc6db2667b3c477cc31365dd1acd6db4e", rendered)
        self.assertIn("idf.py set-target esp32", rendered)
        self.assertNotIn("--preview set-target esp32", rendered)

    def test_s31_command_uses_preview_target(self) -> None:
        lock = build_firmware.load_lock()
        command = build_firmware.docker_command("s31", lock, pull=True)
        rendered = " ".join(command)
        self.assertIn("--pull always", rendered)
        self.assertIn("espressif/idf:v6.1-rc1", command)
        self.assertIn("44f0c59f7c81a72a5868a52d5f6dfbbf88829704", rendered)
        self.assertIn("idf.py --preview set-target esp32s31", rendered)

    def test_lock_schema_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_firmware.target_config({"schema": 1}, "output")


if __name__ == "__main__":
    unittest.main()
