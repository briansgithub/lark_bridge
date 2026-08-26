from __future__ import annotations

import json
import subprocess
import tempfile
import tomllib
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

import bridgectl
import btadapters


def candidate(output_id: str, label: str, kind: str = "a2dp", present: bool = True) -> dict:
    return {
        "id": output_id,
        "kind": kind,
        "label": label,
        "node": "node" if present else None,
        "present": present,
        "connected": present,
        "adapter": "hci1" if kind == "a2dp" else None,
        "adapter_address": "A0:AD:9F:73:6C:24" if kind == "a2dp" else None,
        "address": output_id.split(":", 1)[1] if kind == "a2dp" else None,
    }


WIRED = candidate("wired:alsa_output.platform-x.mailbox", "Built-in Audio Stereo", "wired")
BOOMBOX = candidate("a2dp:C9:5C:FD:6E:28:46", "Boombox")
IWORLD = candidate("a2dp:50:D7:1B:74:34:D6", "iWorld", present=False)
SOUNDCORE = candidate("a2dp:98:47:44:CD:73:DE", "Soundcore Space A40", present=False)
ALL = [WIRED, BOOMBOX, IWORLD, SOUNDCORE]


class SelectorTests(unittest.TestCase):
    """A selector a person cannot address in their own words is not a usable selector."""

    def test_exact_id_wins_before_any_fuzzy_matching(self) -> None:
        self.assertEqual(bridgectl.resolve_selector(BOOMBOX["id"], ALL), BOOMBOX)

    def test_list_index_is_one_based_like_the_printed_list(self) -> None:
        self.assertEqual(bridgectl.resolve_selector("2", ALL), BOOMBOX)

    def test_index_out_of_range_is_refused_with_the_count(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            bridgectl.resolve_selector("9", ALL)
        self.assertIn("there are 4", str(caught.exception))

    def test_name_fragment_is_case_insensitive(self) -> None:
        self.assertEqual(bridgectl.resolve_selector("boombox", ALL), BOOMBOX)
        self.assertEqual(bridgectl.resolve_selector("BOOM", ALL), BOOMBOX)

    def test_wired_is_a_reserved_word_for_the_jack(self) -> None:
        """The onboard sink's ALSA name contains nothing a person would type."""
        self.assertEqual(bridgectl.resolve_selector("wired", ALL), WIRED)

    def test_ambiguity_is_refused_rather_than_guessed(self) -> None:
        pair = [candidate("a2dp:AA:AA:AA:AA:AA:AA", "Kitchen speaker"),
                candidate("a2dp:BB:BB:BB:BB:BB:BB", "Bedroom speaker")]
        with self.assertRaises(SystemExit) as caught:
            bridgectl.resolve_selector("speaker", pair)
        self.assertIn("ambiguous", str(caught.exception))

    def test_no_match_names_the_help_command(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            bridgectl.resolve_selector("garage", ALL)
        self.assertIn("bridgectl output", str(caught.exception))

    def test_an_offline_speaker_is_still_selectable(self) -> None:
        """Choosing something switched off is the whole point of listing it."""
        self.assertEqual(bridgectl.resolve_selector("iworld", ALL), IWORLD)

    def test_matching_by_mac_fragment_works_for_scripts(self) -> None:
        self.assertEqual(bridgectl.resolve_selector("C9:5C", ALL), BOOMBOX)


class StatusParsingTests(unittest.TestCase):
    def test_missing_output_block_explains_the_likely_cause(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            bridgectl.outputs_of({"state": "ACTIVE"})
        self.assertIn("outputs.py", str(caught.exception))

    def test_call_detection_reads_the_hfp_flag(self) -> None:
        self.assertTrue(bridgectl._call_is_up({"call": {"hfp_nodes_present": True}}))
        self.assertFalse(bridgectl._call_is_up({"call": {"hfp_nodes_present": False}}))
        self.assertFalse(bridgectl._call_is_up({}))

    def test_missing_microphone_inventory_is_explained(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            bridgectl.microphones_of({"state": "ACTIVE"})
        self.assertIn("ordered microphone selection", str(caught.exception))

    def test_microphone_status_reports_selected_candidate(self) -> None:
        status = {
            "microphone": {
                "selected": {"id": "fifine-k054", "label": "FIFINE K054"},
                "selection_reason": "lark-a1 absent; using fifine-k054",
                "candidates": [],
            }
        }
        output = StringIO()
        with (
            mock.patch.object(bridgectl, "read_status", return_value=status),
            redirect_stdout(output),
        ):
            self.assertEqual(
                bridgectl.do_microphone_status(Namespace(json=False)),
                0,
            )
        self.assertIn("FIFINE K054", output.getvalue())
        self.assertIn("lark-a1 absent", output.getvalue())

    def test_microphone_cli_is_read_only_and_supports_json(self) -> None:
        block = {
            "selected": None,
            "selection_reason": "no configured microphone is usable",
            "candidates": [{"id": "lark-a1", "label": "Lark", "state": "absent"}],
        }
        output = StringIO()
        with (
            mock.patch.object(bridgectl, "read_status", return_value={"microphone": block}),
            redirect_stdout(output),
        ):
            self.assertEqual(bridgectl.main(["microphone", "list", "--json"]), 0)
        self.assertEqual(json.loads(output.getvalue()), block)
        with self.assertRaises(SystemExit):
            bridgectl.main(["microphone", "set"])


class SelectionSafetyTests(unittest.TestCase):
    def test_target_adapter_prefers_permanent_address_over_stale_hci(self) -> None:
        adapter = btadapters.Adapter("hci7", "A0:AD:9F:73:6C:24", "USB", 4)
        target = dict(BOOMBOX, adapter="hci1", adapter_address=adapter.address)
        with mock.patch.object(
            bridgectl.btadapters, "adapter_by_address", return_value=adapter
        ) as resolver:
            self.assertEqual(bridgectl.target_adapter(target), adapter)
        resolver.assert_called_once_with(adapter.address)

    def test_target_adapter_never_falls_back_to_hci_index(self) -> None:
        with mock.patch.object(bridgectl.btadapters, "adapters") as adapters:
            self.assertIsNone(
                bridgectl.target_adapter(dict(BOOMBOX, adapter_address=None, adapter="hci1"))
            )
        adapters.assert_not_called()

    def test_needs_setup_is_rejected_before_trust_connect_or_desire(self) -> None:
        target = dict(BOOMBOX, setup_state="needs_setup", present=False, node=None)
        status = {"call": {"hfp_nodes_present": False}, "output": {"candidates": [target]}}
        args = Namespace(selector=target["id"], connect=True, force=False, chime=False)
        with (
            mock.patch.object(bridgectl, "read_status", return_value=status),
            mock.patch.object(bridgectl.btadapters, "pin_to_adapter") as pin,
            mock.patch.object(bridgectl.btadapters, "connect_profile") as connect,
            mock.patch.object(bridgectl.supervisor, "write_desire") as desire,
        ):
            self.assertEqual(bridgectl.do_set(args), 1)
        pin.assert_not_called()
        connect.assert_not_called()
        desire.assert_not_called()

    def test_trust_failure_does_not_record_the_selection(self) -> None:
        adapter = btadapters.Adapter("hci1", "A0:AD:9F:73:6C:24", "USB", 1)
        failed = btadapters.TrustPinResult(False, failures=("D-Bus refused",))
        status = {
            "call": {"hfp_nodes_present": False},
            "output": {"candidates": [BOOMBOX]},
        }
        args = Namespace(selector="boombox", connect=True, force=False, chime=False)
        with (
            mock.patch.object(bridgectl, "read_status", return_value=status),
            mock.patch.object(
                bridgectl.btadapters, "adapter_by_address", return_value=adapter
            ),
            mock.patch.object(
                bridgectl.btadapters, "pin_to_adapter", return_value=failed
            ),
            mock.patch.object(bridgectl.supervisor, "write_desire") as write_desire,
        ):
            self.assertEqual(bridgectl.do_set(args), 1)
        write_desire.assert_not_called()

    def test_failed_startup_commit_does_not_change_the_live_selection(self) -> None:
        status = {
            "call": {"hfp_nodes_present": False},
            "config_path": "/tmp/bridge.toml",
            "output": {"candidates": [WIRED]},
        }
        args = Namespace(
            selector="wired",
            connect=True,
            force=False,
            chime=False,
            remember=True,
        )
        with (
            mock.patch.object(bridgectl, "read_status", return_value=status),
            mock.patch.object(
                bridgectl,
                "remember_startup_output",
                return_value=(False, "slot unavailable"),
            ),
            mock.patch.object(bridgectl.supervisor, "write_desire") as write_desire,
        ):
            self.assertEqual(bridgectl.do_set(args), 1)
        write_desire.assert_not_called()


class StartupConfigTests(unittest.TestCase):
    BASE = """# operator comment
[audio.aec]
enabled = true
node_latency_frames = 1920
"""

    def test_bluetooth_default_preserves_audio_and_uses_stable_adapter_address(self) -> None:
        candidate = bridgectl.startup_config_for(BOOMBOX, self.BASE)
        document = tomllib.loads(candidate)

        self.assertTrue(document["audio"]["aec"]["enabled"])
        self.assertEqual(document["audio"]["aec"]["node_latency_frames"], 1920)
        self.assertEqual(document["bridge"]["mode"], "bluetooth")
        self.assertTrue(document["bridge"]["fallback_to_wired"])
        self.assertEqual(document["devices"]["output"]["id"], BOOMBOX["id"])
        self.assertEqual(document["devices"]["output"]["address"], BOOMBOX["address"])
        self.assertEqual(
            document["devices"]["output"]["adapter"],
            BOOMBOX["adapter_address"],
        )
        self.assertTrue(document["devices"]["output"]["reconnect"])
        self.assertIn("# operator comment", candidate)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bridge.toml"
            path.write_text(candidate, encoding="utf-8")
            settings = bridgectl.supervisor.load_settings(path)
        self.assertEqual(settings.mode, "bluetooth")
        self.assertEqual(settings.desired_output, BOOMBOX["id"])
        self.assertEqual(settings.speaker_adapter, BOOMBOX["adapter_address"])
        self.assertTrue(settings.aec.enabled)

    def test_wired_default_preserves_dedicated_speaker_controller_identity(self) -> None:
        current = bridgectl.startup_config_for(BOOMBOX, self.BASE)
        candidate = bridgectl.startup_config_for(WIRED, current)
        document = tomllib.loads(candidate)
        output = document["devices"]["output"]

        self.assertEqual(document["bridge"]["mode"], "bluetooth-wired")
        self.assertEqual(
            output,
            {"id": WIRED["id"], "adapter": BOOMBOX["adapter_address"]},
        )

    def test_missing_permanent_adapter_address_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "controller addresses"):
            bridgectl.startup_config_for(
                dict(BOOMBOX, adapter_address=None),
                self.BASE,
            )

    def test_malformed_device_or_controller_identity_is_refused(self) -> None:
        for changed in (
            dict(BOOMBOX, address="not-a-mac"),
            dict(BOOMBOX, adapter_address="hci1"),
            dict(BOOMBOX, id="a2dp:00:00:00:00:00:00"),
        ):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                bridgectl.startup_config_for(changed, self.BASE)

    def test_commit_uses_the_existing_ab_slot_writer(self) -> None:
        captured: dict[str, str] = {}

        def commit(command, **_kwargs):
            source = Path(command[-1])
            captured["candidate"] = source.read_text(encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="b\n", stderr="")

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = base / "bridge.toml"
            config.write_text(self.BASE, encoding="utf-8")
            with (
                mock.patch.object(
                    bridgectl.supervisor,
                    "default_status_path",
                    return_value=base / "runtime" / "bridge-status.json",
                ),
                mock.patch.object(bridgectl.subprocess, "run", side_effect=commit) as run,
            ):
                ok, detail = bridgectl.remember_startup_output(
                    BOOMBOX,
                    config,
                    tool_path=Path("/installed/lark_state.py"),
                )

            active = tomllib.loads(config.read_text(encoding="utf-8"))

        self.assertTrue(ok)
        self.assertIn("slot b", detail)
        self.assertEqual(active["devices"]["output"]["id"], BOOMBOX["id"])
        self.assertEqual(
            tomllib.loads(captured["candidate"])["devices"]["output"]["id"],
            BOOMBOX["id"],
        )
        command = run.call_args.args[0]
        self.assertEqual(command[:5], [
            "sudo", "-n", "python3", str(Path("/installed/lark_state.py")), "config-write"
        ])

    def test_failed_live_mirror_rolls_the_persistent_pointer_back(self) -> None:
        commits = []

        def commit(command, **_kwargs):
            commits.append(command)
            return subprocess.CompletedProcess(command, 0, stdout="b\n", stderr="")

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = base / "bridge.toml"
            config.write_text(self.BASE, encoding="utf-8")
            with (
                mock.patch.object(
                    bridgectl.supervisor,
                    "default_status_path",
                    return_value=base / "runtime" / "bridge-status.json",
                ),
                mock.patch.object(bridgectl.subprocess, "run", side_effect=commit),
                mock.patch.object(bridgectl.os, "replace", side_effect=OSError("rename refused")),
            ):
                ok, detail = bridgectl.remember_startup_output(
                    BOOMBOX,
                    config,
                    tool_path=Path("/installed/lark_state.py"),
                )

            self.assertEqual(config.read_text(encoding="utf-8"), self.BASE)

        self.assertFalse(ok)
        self.assertIn("rolled back", detail)
        self.assertEqual(len(commits), 2)
        self.assertEqual(commits[1][-1], str(config))

    def test_transaction_rollback_restores_exact_config_bytes_through_ab_writer(self) -> None:
        original = b'# exact operator bytes\r\n[bridge]\r\nmode = "bluetooth-wired"\r\n'
        captured = b""

        def commit(command, **_kwargs):
            nonlocal captured
            captured = Path(command[-1]).read_bytes()
            return subprocess.CompletedProcess(command, 0, stdout="a\n", stderr="")

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = base / "bridge.toml"
            config.write_text(bridgectl.startup_config_for(BOOMBOX, self.BASE), encoding="utf-8")
            with (
                mock.patch.object(
                    bridgectl.supervisor,
                    "default_status_path",
                    return_value=base / "runtime" / "bridge-status.json",
                ),
                mock.patch.object(bridgectl.subprocess, "run", side_effect=commit),
            ):
                ok, _detail = bridgectl.restore_startup_config(
                    original, config, tool_path=Path("/installed/lark_state.py")
                )
            active = config.read_bytes()
        self.assertTrue(ok)
        self.assertEqual(captured, original)
        self.assertEqual(active, original)


if __name__ == "__main__":
    unittest.main()
