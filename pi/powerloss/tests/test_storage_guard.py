import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE))

import lark_state
import storage_guard


def arguments(base: Path, *, persistent: bool) -> argparse.Namespace:
    immutable_bluez = base / "immutable-bluez"
    immutable_bluez.mkdir()
    immutable_config = base / "immutable.toml"
    immutable_config.write_text('[bridge]\nname = "immutable"\n', encoding="utf-8")
    return argparse.Namespace(
        active_config=base / "active/bridge.toml",
        assume_persistent=persistent,
        immutable_bluez=immutable_bluez,
        immutable_config=immutable_config,
        live_bluez=base / "state/bluetooth/live",
        mountinfo=base / "mountinfo",
        root=base / "state",
        status=base / "run/storage-health.json",
    )


class StorageGuard(unittest.TestCase):
    def test_healthy_persistent_state_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            options = arguments(base, persistent=True)
            source = base / "candidate.toml"
            source.write_text('[bridge]\nname = "persistent"\n', encoding="utf-8")
            lark_state.write_config(options.root, source)
            options.live_bluez.mkdir(parents=True)

            status = storage_guard.guard(options)

            self.assertEqual(status["state"], "READY")
            self.assertEqual(status["config_source"], "slot-a")
            self.assertIn(
                "persistent", options.active_config.read_text(encoding="utf-8")
            )
            self.assertEqual((options.root / "random-seed").stat().st_size, 512)

    def test_missing_mount_uses_immutable_state_and_reports_degraded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            options = arguments(base, persistent=False)
            options.mountinfo.write_text("", encoding="utf-8")

            status = storage_guard.guard(options)

            self.assertEqual(status["state"], "DEGRADED")
            self.assertFalse(status["persistent"])
            self.assertEqual(status["config_source"], "immutable")
            self.assertTrue(options.live_bluez.is_dir())
            written = json.loads(options.status.read_text(encoding="utf-8"))
            self.assertEqual(written["state"], "DEGRADED")

    def test_corrupt_live_pairing_recovers_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            options = arguments(base, persistent=True)
            source = base / "candidate.toml"
            source.write_text('[bridge]\nname = "persistent"\n', encoding="utf-8")
            lark_state.write_config(options.root, source)
            device = base / "pairing/adapter/device"
            device.mkdir(parents=True)
            (device / "info").write_text(
                "[LinkKey]\nKey=" + "A" * 32 + "\n", encoding="utf-8"
            )
            lark_state.seal_pairing(options.root, base / "pairing")
            options.live_bluez.mkdir(parents=True)
            (options.live_bluez / "info").write_text("not ini", encoding="utf-8")

            status = storage_guard.guard(options)

            self.assertEqual(status["state"], "DEGRADED")
            self.assertEqual(status["pairing_action"], "snapshot-restored")
            self.assertTrue((options.live_bluez / "adapter/device/info").is_file())

    def test_corrupt_current_config_repairs_pointer_to_alternate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            options = arguments(base, persistent=True)
            source = base / "candidate.toml"
            source.write_text('[bridge]\nname = "first"\n', encoding="utf-8")
            lark_state.write_config(options.root, source)
            source.write_text('[bridge]\nname = "second"\n', encoding="utf-8")
            lark_state.write_config(options.root, source)
            (options.root / "config/slot-b/bridge.toml").write_text(
                "broken = [", encoding="utf-8"
            )
            options.live_bluez.mkdir(parents=True)

            recovered = storage_guard.guard(options)
            next_boot = storage_guard.guard(options)

            self.assertEqual(recovered["state"], "DEGRADED")
            self.assertEqual(recovered["config_slot"], "a")
            self.assertEqual(
                (options.root / "config/current").read_text(encoding="ascii"), "a\n"
            )
            self.assertEqual(next_boot["state"], "READY")

    def test_corrupt_journal_is_quarantined(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = root / "journal/machine"
            journal.mkdir(parents=True)
            (journal / "system.journal").write_bytes(b"corrupt")
            failed = mock.Mock(returncode=1, stderr="invalid", stdout="")

            with mock.patch.object(
                storage_guard.subprocess, "run", return_value=failed
            ):
                detail = storage_guard.verify_or_reset_journal(root)

            self.assertIn("quarantined", detail)
            self.assertTrue((root / "journal").is_dir())
            self.assertTrue((root / "journal-corrupt/machine/system.journal").is_file())


class MountInfo(unittest.TestCase):
    def test_requires_distinct_rw_ext4_mount(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "state"
            root.mkdir()
            table = base / "mountinfo"
            table.write_text(
                f"36 25 179:3 / {root} rw,nosuid - ext4 /dev/mmcblk0p3 rw,commit=1\n",
                encoding="utf-8",
            )
            present, detail = storage_guard.persistent_mount_state(root, table)
            self.assertTrue(present)
            self.assertIn("/dev/mmcblk0p3", detail)

    def test_detaches_read_only_mount_before_ram_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "state"
            root.mkdir()
            table = base / "mountinfo"
            table.write_text(
                f"36 25 179:3 / {root} ro,nosuid - ext4 /dev/mmcblk0p3 ro\n",
                encoding="utf-8",
            )

            def stop_mount(*_args, **_kwargs):
                table.write_text("", encoding="utf-8")
                return mock.Mock(returncode=0, stderr="", stdout="")

            with mock.patch.object(
                storage_guard.subprocess, "run", side_effect=stop_mount
            ):
                detail = storage_guard.detach_unusable_mount(root, table)

            self.assertIn("RAM fallback", detail)
            self.assertFalse(storage_guard.is_exact_mount(root, table))


if __name__ == "__main__":
    unittest.main()
