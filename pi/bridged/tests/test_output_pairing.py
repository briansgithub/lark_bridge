from __future__ import annotations

import json
import subprocess
import tempfile
import time
import unittest
from contextlib import ExitStack, nullcontext
from pathlib import Path
from unittest import mock

import btadapters
import output_remote

SPEAKER = "C9:5C:FD:6E:28:46"
OUTPUT_ID = f"a2dp:{SPEAKER}"
PHONE = "5C:33:7B:CB:BF:C5"
ADAPTER = btadapters.Adapter("hci7", "A0:AD:9F:73:6C:24", "USB", 4)


def device(*, paired: bool, a2dp: bool, connected: bool = False) -> dict:
    return {
        "Address": {"data": SPEAKER},
        "Alias": {"data": "Boombox"},
        "Paired": {"data": paired},
        "Connected": {"data": connected},
        "ServicesResolved": {"data": True},
        "UUIDs": {"data": [btadapters.A2DP_SINK_UUID] if a2dp else []},
    }


def tree(*, paired: bool, a2dp: bool, connected: bool = False) -> dict:
    return {
        ADAPTER.path: {
            "org.bluez.Adapter1": {
                "Address": {"data": ADAPTER.address},
                "Powered": {"data": True},
            }
        },
        btadapters.path_for(ADAPTER, SPEAKER): {
            "org.bluez.Device1": device(paired=paired, a2dp=a2dp, connected=connected)
        },
    }


def scan_state(*, valid: bool = True) -> output_remote.RemoteState:
    now = time.monotonic()
    state = output_remote.RemoteState()
    result = {
        "output_id": OUTPUT_ID,
        "label": "Boombox",
        "rssi_dbm": -50,
        "audio_confidence": "confirmed",
        "setup_state": "needs_setup",
        "duplicate_name_discriminator": None,
    }
    state.scan = output_remote.ScanRecord(
        scan_id="scan-token",
        results={OUTPUT_ID: result},
        completed_monotonic=now - 1,
        valid_until_monotonic=now + 60 if valid else now - 1,
        started_at_ms=1,
        completed_at_ms=2,
        valid_until_ms=3,
    )
    return state


class PairSelectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.config = base / "bridge.toml"
        self.config.write_text(
            "[bridge]\nmode = \"bluetooth-wired\"\n"
            "[devices.output]\nid = \"wired:jack\"\n"
            f"adapter = \"{ADAPTER.address}\"\n",
            encoding="utf-8",
        )
        self.status_path = base / "bridge-status.json"
        self.status = {
            "config_path": str(self.config),
            "call": {"hfp_nodes_present": False},
            "output": {
                "desired_id": "wired:jack",
                "chosen": {"id": "wired:jack"},
                "reason": "desired output is available",
                "candidates": [
                    {
                        "id": "wired:jack",
                        "kind": "wired",
                        "label": "Built-in jack",
                        "present": True,
                        "connected": True,
                        "setup_state": "ready",
                    }
                ],
            },
        }
        self.status_path.write_text(json.dumps(self.status), encoding="utf-8")

    def request(self, state: output_remote.RemoteState, **kwargs):
        payload = {
            "id": 13,
            "op": "pair_select",
            "scan_id": "scan-token",
            "output_id": OUTPUT_ID,
        }
        payload.update(kwargs.pop("payload", {}))
        return output_remote.handle_request(
            payload,
            status_path=self.status_path,
            state=state,
            runner=lambda command, **_options: subprocess.CompletedProcess(command, 0, "", ""),
            **kwargs,
        )

    def common(self, stack: ExitStack, device_tree: dict) -> None:
        stack.enter_context(
            mock.patch.object(
                output_remote,
                "_resolve_speaker_controller",
                return_value=(mock.Mock(), ADAPTER, device_tree),
            )
        )
        stack.enter_context(
            mock.patch.object(
                output_remote.btadapters,
                "speaker_radio_lock",
                return_value=nullcontext(True),
            )
        )

    def test_stale_result_performs_no_bluetooth_or_persistence_mutation(self) -> None:
        with ExitStack() as stack:
            self.common(stack, tree(paired=False, a2dp=False))
            mutations = [
                stack.enter_context(mock.patch.object(output_remote.btadapters, name))
                for name in ("pair_device", "pin_to_adapter", "connect_profile", "remove_device")
            ]
            mutations.extend(
                [
                    stack.enter_context(mock.patch.object(output_remote, "_seal_pairing")),
                    stack.enter_context(
                        mock.patch.object(output_remote.bridgectl, "remember_startup_output")
                    ),
                    stack.enter_context(
                        mock.patch.object(output_remote.supervisor, "write_desire")
                    ),
                ]
            )
            response = self.request(output_remote.RemoteState())
        self.assertEqual(response["error_code"], "stale_result")
        self.assertEqual(response["phase"], "validating")
        self.assertTrue(response["done"])
        for mutation in mutations:
            mutation.assert_not_called()

    def test_expired_scan_is_stale_before_pairing(self) -> None:
        with ExitStack() as stack:
            self.common(stack, tree(paired=False, a2dp=False))
            pair = stack.enter_context(mock.patch.object(output_remote.btadapters, "pair_device"))
            response = self.request(scan_state(valid=False))
        self.assertEqual(response["error_code"], "stale_result")
        pair.assert_not_called()

    def test_token_valid_at_acceptance_survives_radio_lock_wait(self) -> None:
        state = scan_state()

        class ExpiringLock:
            def __enter__(self):
                assert state.scan is not None
                state.scan.valid_until_monotonic = 0.0
                return True

            def __exit__(self, *_args):
                return False

        with ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(
                    output_remote,
                    "_resolve_speaker_controller",
                    return_value=(mock.Mock(), ADAPTER, tree(paired=False, a2dp=False)),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    output_remote.btadapters,
                    "speaker_radio_lock",
                    return_value=ExpiringLock(),
                )
            )
            pair = stack.enter_context(
                mock.patch.object(
                    output_remote.btadapters,
                    "pair_device",
                    return_value=btadapters.PairResult(False, detail="timed out"),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    output_remote.btadapters, "remove_device", return_value=(True, "x")
                )
            )
            response = self.request(state)

        self.assertEqual(response["error_code"], "pairing_timeout")
        pair.assert_called_once()

    def test_pair_timeout_rolls_back_only_new_target_object(self) -> None:
        with ExitStack() as stack:
            self.common(stack, tree(paired=False, a2dp=False))
            pair = stack.enter_context(
                mock.patch.object(
                    output_remote.btadapters,
                    "pair_device",
                    return_value=btadapters.PairResult(False, detail="timed out"),
                )
            )
            remove = stack.enter_context(
                mock.patch.object(output_remote.btadapters, "remove_device", return_value=(True, "x"))
            )
            pin = stack.enter_context(mock.patch.object(output_remote.btadapters, "pin_to_adapter"))
            response = self.request(scan_state())
        self.assertEqual(response["error_code"], "pairing_timeout")
        pair.assert_called_once()
        self.assertEqual(pair.call_args.args[:2], (SPEAKER, ADAPTER))
        remove.assert_called_once_with(SPEAKER, ADAPTER)
        pin.assert_not_called()

    def test_pin_requirement_has_stable_error_and_new_bond_cleanup(self) -> None:
        with ExitStack() as stack:
            self.common(stack, tree(paired=False, a2dp=False))
            stack.enter_context(
                mock.patch.object(
                    output_remote.btadapters,
                    "pair_device",
                    return_value=btadapters.PairResult(False, True, "PIN requested"),
                )
            )
            remove = stack.enter_context(
                mock.patch.object(output_remote.btadapters, "remove_device", return_value=(True, "x"))
            )
            response = self.request(scan_state())
        self.assertEqual(response["error_code"], "pin_not_supported")
        remove.assert_called_once_with(SPEAKER, ADAPTER)

    def test_missing_a2dp_removes_new_bond_but_not_preexisting_bond(self) -> None:
        for paired_before, expected_removals in ((False, 1), (True, 0)):
            with self.subTest(paired_before=paired_before), ExitStack() as stack:
                self.common(stack, tree(paired=paired_before, a2dp=False))
                stack.enter_context(
                    mock.patch.object(
                        output_remote.btadapters,
                        "pair_device",
                        return_value=btadapters.PairResult(True),
                    )
                )
                stack.enter_context(
                    mock.patch.object(output_remote, "_wait_for_services", return_value=None)
                )
                remove = stack.enter_context(
                    mock.patch.object(
                        output_remote.btadapters, "remove_device", return_value=(True, "x")
                    )
                )
                response = self.request(scan_state())
            self.assertEqual(response["error_code"], "not_audio_output")
            self.assertEqual(remove.call_count, expected_removals)

    def test_validated_bond_survives_later_a2dp_connect_failure(self) -> None:
        with ExitStack() as stack:
            self.common(stack, tree(paired=False, a2dp=False))
            stack.enter_context(
                mock.patch.object(
                    output_remote.btadapters,
                    "pair_device",
                    return_value=btadapters.PairResult(True),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    output_remote,
                    "_wait_for_services",
                    return_value=device(paired=True, a2dp=True),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    output_remote.btadapters,
                    "pin_to_adapter",
                    return_value=btadapters.TrustPinResult(True),
                )
            )
            connect = stack.enter_context(
                mock.patch.object(
                    output_remote.btadapters,
                    "connect_profile",
                    return_value=(False, "refused"),
                )
            )
            remove = stack.enter_context(mock.patch.object(output_remote.btadapters, "remove_device"))
            response = self.request(scan_state())
        self.assertEqual(response["error_code"], "connection_failed")
        self.assertEqual(response["phase"], "connecting")
        self.assertEqual(connect.call_args.args[:3], (SPEAKER, ADAPTER, btadapters.A2DP_SINK_UUID))
        remove.assert_not_called()

    def test_ready_bond_is_idempotent_without_scan_and_saves_only_after_audio(self) -> None:
        events = []
        ready_tree = tree(paired=True, a2dp=True, connected=True)
        confirmed = json.loads(json.dumps(self.status))
        confirmed["output"]["desired_id"] = OUTPUT_ID
        confirmed["output"]["chosen"] = {"id": OUTPUT_ID}
        with ExitStack() as stack:
            self.common(stack, ready_tree)
            pair = stack.enter_context(mock.patch.object(output_remote.btadapters, "pair_device"))
            stack.enter_context(
                mock.patch.object(
                    output_remote, "_wait_for_services", return_value=device(paired=True, a2dp=True)
                )
            )
            stack.enter_context(
                mock.patch.object(
                    output_remote.btadapters,
                    "pin_to_adapter",
                    side_effect=lambda *_args: (
                        events.append("trust") or btadapters.TrustPinResult(True)
                    ),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    output_remote.btadapters,
                    "connect_profile",
                    side_effect=lambda *_args, **_kwargs: events.append("connect") or (True, "ok"),
                )
            )
            stack.enter_context(mock.patch.object(output_remote, "_wait_for_connected", return_value=True))
            stack.enter_context(
                mock.patch.object(
                    output_remote,
                    "_wait_for_audio",
                    side_effect=lambda *_args: events.append("audio") or "bluez_output.target.1",
                )
            )
            stack.enter_context(
                mock.patch.object(
                    output_remote,
                    "_seal_pairing",
                    side_effect=lambda *_args: events.append("seal") or (True, "slot a"),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    output_remote.bridgectl,
                    "remember_startup_output",
                    side_effect=lambda *_args: events.append("config") or (True, "slot b"),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    output_remote.supervisor,
                    "write_desire",
                    side_effect=lambda *_args, **_kwargs: events.append("desire"),
                )
            )
            stack.enter_context(
                mock.patch.object(output_remote, "_wait_status_choice", return_value=confirmed)
            )
            phases = []
            response = self.request(
                output_remote.RemoteState(),
                payload={"scan_id": None},
                progress=lambda value: phases.append(value["phase"]),
            )
        self.assertTrue(response["ok"])
        self.assertTrue(response["done"])
        self.assertEqual(response["setup_state"], "ready")
        pair.assert_not_called()
        self.assertLess(events.index("audio"), events.index("seal"))
        self.assertEqual(events[-3:], ["seal", "config", "desire"])
        self.assertEqual(
            [name for name in phases if name in {"pinning_trust", "connecting", "waiting_for_audio", "saving"}],
            ["pinning_trust", "connecting", "waiting_for_audio", "saving"],
        )

    def test_route_confirmation_failure_restores_config_and_runtime_desire(self) -> None:
        ready_tree = tree(paired=True, a2dp=True, connected=True)
        with ExitStack() as stack:
            self.common(stack, ready_tree)
            stack.enter_context(
                mock.patch.object(
                    output_remote, "_wait_for_services", return_value=device(paired=True, a2dp=True)
                )
            )
            stack.enter_context(
                mock.patch.object(
                    output_remote.btadapters,
                    "pin_to_adapter",
                    return_value=btadapters.TrustPinResult(True),
                )
            )
            stack.enter_context(
                mock.patch.object(output_remote.btadapters, "connect_profile", return_value=(True, "ok"))
            )
            stack.enter_context(mock.patch.object(output_remote, "_wait_for_connected", return_value=True))
            stack.enter_context(mock.patch.object(output_remote, "_wait_for_audio", return_value="node"))
            stack.enter_context(mock.patch.object(output_remote, "_seal_pairing", return_value=(True, "a")))
            stack.enter_context(
                mock.patch.object(
                    output_remote.bridgectl, "remember_startup_output", return_value=(True, "b")
                )
            )
            stack.enter_context(mock.patch.object(output_remote.supervisor, "write_desire"))
            stack.enter_context(mock.patch.object(output_remote, "_wait_status_choice", return_value=None))
            restore = stack.enter_context(
                mock.patch.object(output_remote, "_restore_selection", return_value=(True, ""))
            )
            response = self.request(output_remote.RemoteState(), payload={"scan_id": None})
        self.assertEqual(response["error_code"], "persistence_failed")
        restore.assert_called_once()
        self.assertTrue(restore.call_args.kwargs["restore_config"])
        self.assertTrue(restore.call_args.kwargs["restore_desire"])


class ScanProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.status_path = Path(self.temp.name) / "status.json"
        self.status_path.write_text(
            json.dumps(
                {
                    "call": {"hfp_nodes_present": True},
                    "output": {
                        "desired_id": "wired:jack",
                        "chosen": {"id": "wired:jack"},
                        "reason": "ready",
                        "candidates": [],
                    },
                }
            ),
            encoding="utf-8",
        )

    def test_scan_progress_token_supersession_and_no_selection_side_effects(self) -> None:
        observed_tree = tree(paired=False, a2dp=False)
        progress = []
        ordering = []

        class Lock:
            def __enter__(self):
                ordering.append("lock")
                return True

            def __exit__(self, *_args):
                return False

        def discover(_adapter, **kwargs):
            kwargs["progress"](0, 12_000)
            return btadapters.DiscoveryRun({SPEAKER: -44}, 10.0, 22.0)

        state = output_remote.RemoteState()
        with (
            mock.patch.object(
                output_remote,
                "_resolve_speaker_controller",
                side_effect=lambda _status, **_kwargs: (
                    ordering.append("resolve")
                    or (mock.Mock(), ADAPTER, observed_tree)
                ),
            ),
            mock.patch.object(
                output_remote.btadapters, "speaker_radio_lock", return_value=Lock()
            ),
            mock.patch.object(output_remote.btadapters, "discover_bredr", side_effect=discover),
            mock.patch.object(output_remote.btadapters, "managed_objects", return_value=observed_tree),
            mock.patch.object(output_remote.secrets, "token_urlsafe", side_effect=["one", "two"]),
            mock.patch.object(output_remote.btadapters, "pair_device") as pair,
            mock.patch.object(output_remote.btadapters, "connect_profile") as connect,
            mock.patch.object(output_remote.btadapters, "pin_to_adapter") as trust,
            mock.patch.object(output_remote.btadapters, "remove_device") as remove,
            mock.patch.object(output_remote, "_seal_pairing") as seal,
            mock.patch.object(output_remote.bridgectl, "remember_startup_output") as remember,
            mock.patch.object(output_remote.supervisor, "write_desire") as desire,
        ):
            first = output_remote.handle_request(
                {"id": 1, "op": "scan"},
                status_path=self.status_path,
                state=state,
                progress=progress.append,
            )
            second = output_remote.handle_request(
                {"id": 2, "op": "scan"},
                status_path=self.status_path,
                state=state,
            )
        self.assertEqual(first["scan_id"], "one")
        self.assertEqual(second["scan_id"], "two")
        self.assertEqual(state.scan.scan_id, "two")
        self.assertEqual(first["duration_ms"], 12_000)
        self.assertEqual(progress[0], {
            "id": 1,
            "event": "progress",
            "done": False,
            "phase": "scanning",
            "elapsed_ms": 0,
            "duration_ms": 12_000,
        })
        self.assertTrue(first["call_active"])
        self.assertEqual(ordering[:2], ["lock", "resolve"])
        pair.assert_not_called()
        connect.assert_not_called()
        trust.assert_not_called()
        remove.assert_not_called()
        seal.assert_not_called()
        remember.assert_not_called()
        desire.assert_not_called()

    def test_failed_scan_invalidates_previous_token(self) -> None:
        state = scan_state()
        with (
            mock.patch.object(
                output_remote,
                "_resolve_speaker_controller",
                return_value=(mock.Mock(), ADAPTER, tree(paired=False, a2dp=False)),
            ),
            mock.patch.object(
                output_remote.btadapters, "speaker_radio_lock", return_value=nullcontext(True)
            ),
            mock.patch.object(
                output_remote.btadapters,
                "discover_bredr",
                side_effect=btadapters.BluetoothOperationError("BlueZ unavailable"),
            ),
        ):
            response = output_remote.handle_request(
                {"id": 1, "op": "scan"}, status_path=self.status_path, state=state
            )
        self.assertEqual(response["error_code"], "speaker_adapter_unavailable")
        self.assertIsNone(state.scan)


class ControllerValidationTests(unittest.TestCase):
    def test_noncanonical_config_never_falls_back_to_any_adapter(self) -> None:
        settings = mock.Mock(speaker_adapter=ADAPTER.address.lower(), phone_mac=PHONE)
        with (
            mock.patch.object(output_remote, "_load_operation_settings", return_value=settings),
            mock.patch.object(output_remote.btadapters, "adapter_by_address") as resolve,
            self.assertRaises(btadapters.BluetoothOperationError),
        ):
            output_remote._resolve_speaker_controller({})
        resolve.assert_not_called()

    def test_controller_holding_phone_bond_is_rejected(self) -> None:
        settings = mock.Mock(speaker_adapter=ADAPTER.address, phone_mac=PHONE)
        live = {
            ADAPTER.path: {
                "org.bluez.Adapter1": {
                    "Address": {"data": ADAPTER.address},
                    "Powered": {"data": True},
                }
            },
            btadapters.path_for(ADAPTER, PHONE): {
                "org.bluez.Device1": {"Paired": {"data": True}}
            },
        }
        with (
            mock.patch.object(output_remote, "_load_operation_settings", return_value=settings),
            mock.patch.object(output_remote.btadapters, "adapter_by_address", return_value=ADAPTER),
            mock.patch.object(output_remote.btadapters, "managed_objects", return_value=live),
            mock.patch.object(output_remote.btadapters, "is_blocked", return_value=False),
            self.assertRaises(btadapters.BluetoothOperationError),
        ):
            output_remote._resolve_speaker_controller({})


if __name__ == "__main__":
    unittest.main()
