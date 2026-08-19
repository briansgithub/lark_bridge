import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE))

import lark_state

CONFIG_A = b'[bridge]\nname = "one"\n'
CONFIG_B = b'[bridge]\nname = "two"\n'


class ConfigTransactions(unittest.TestCase):
    def test_selects_alternate_when_current_slot_is_corrupt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.toml"
            source.write_bytes(CONFIG_A)
            self.assertEqual(lark_state.write_config(root, source), "a")
            source.write_bytes(CONFIG_B)
            self.assertEqual(lark_state.write_config(root, source), "b")
            (root / "config/slot-b/bridge.toml").write_bytes(b"broken = [")

            selected, slot, failures = lark_state.select_config(root)

            self.assertEqual(slot, "a")
            self.assertEqual(selected.read_bytes(), CONFIG_A)
            self.assertEqual(len(failures), 1)

    def test_uncommitted_inactive_slot_never_becomes_current(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.toml"
            source.write_bytes(CONFIG_A)
            lark_state.write_config(root, source)
            incomplete = root / "config/slot-b"
            incomplete.mkdir(parents=True)
            (incomplete / "bridge.toml").write_bytes(CONFIG_B)

            selected, slot, _ = lark_state.select_config(root)

            self.assertEqual(slot, "a")
            self.assertEqual(selected.read_bytes(), CONFIG_A)


class PairingTransactions(unittest.TestCase):
    @staticmethod
    def bluez_tree(path: Path, key: str) -> None:
        device = path / "adapter" / "device"
        device.mkdir(parents=True)
        (device / "info").write_text(
            f"[LinkKey]\nKey={key}\nType=4\nPINLength=0\n", encoding="utf-8"
        )

    def test_seals_two_slots_and_restores_latest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "state"
            live = base / "live"
            restored = base / "restored"
            self.bluez_tree(live, "A" * 32)
            self.assertEqual(lark_state.seal_pairing(root, live), "a")
            (live / "adapter/device/info").write_text(
                "[LinkKey]\nKey=" + "B" * 32 + "\n", encoding="utf-8"
            )
            self.assertEqual(lark_state.seal_pairing(root, live), "b")

            snapshot, slot, failures = lark_state.select_pairing_snapshot(root)
            lark_state.restore_pairing(snapshot, restored)

            self.assertEqual(slot, "b")
            self.assertEqual(failures, [])
            info = restored / "adapter/device/info"
            self.assertIn("B" * 32, info.read_text(encoding="utf-8"))
            self.assertFalse((restored / ".larkbridge-manifest.json").exists())

    def test_manifest_detects_snapshot_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "state"
            live = base / "live"
            self.bluez_tree(live, "A" * 32)
            lark_state.seal_pairing(root, live)
            info = root / "bluetooth/snapshot-a/adapter/device/info"
            info.write_text("[LinkKey]\nKey=corrupt\n", encoding="utf-8")

            with self.assertRaises(lark_state.StateError):
                lark_state.validate_pairing_slot(root, "a")

    @unittest.skipIf(os.name == "nt", "ordinary Windows users cannot create symlinks")
    def test_restore_preserves_live_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            target = base / "persistent/live"
            alias = base / "bluetooth"
            self.bluez_tree(source, "A" * 32)
            target.parent.mkdir(parents=True)
            alias.symlink_to(target, target_is_directory=True)

            lark_state.restore_pairing(source, alias)

            self.assertTrue(alias.is_symlink())
            self.assertTrue((target / "adapter/device/info").is_file())


class Ledger(unittest.TestCase):
    def test_appends_individually_parseable_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lark_state.append_ledger(root, {"event": "one"})
            lark_state.append_ledger(root, {"event": "two"})
            records = [
                json.loads(line)
                for line in (root / "recovery/ledger.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual([record["event"] for record in records], ["one", "two"])

    def test_repairs_an_interrupted_final_append(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lark_state.append_ledger(root, {"event": "complete"})
            ledger = root / "recovery/ledger.jsonl"
            with ledger.open("ab") as handle:
                handle.write(b'{"event":"interrupted"')

            detail = lark_state.repair_ledger(root)
            records = [
                json.loads(line)
                for line in ledger.read_text(encoding="utf-8").splitlines()
            ]

            self.assertIn("discarded", detail)
            self.assertEqual([record["event"] for record in records], ["complete"])


if __name__ == "__main__":
    unittest.main()
