from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

import bt_watchdog
import btadapters
import controller_roles

CALL_ADDRESS = "B0:82:E2:25:18:36"
OUTPUT_ADDRESS = "A0:AD:9F:73:6C:24"
ONBOARD_ADDRESS = "B8:27:EB:43:8D:51"
PHONE_ADDRESS = "5C:33:7B:CB:BF:C5"
SPEAKER_ADDRESS = "C9:5C:FD:6E:28:46"


def roles() -> controller_roles.ControllerRoles:
    return controller_roles.parse_controller_roles(
        {
            "devices": {
                "phone": {
                    "address": PHONE_ADDRESS,
                    "adapter": CALL_ADDRESS,
                    "adapter_product": "ASUS USB-BT600",
                    "adapter_bus": "USB",
                    "adapter_usb_vendor_id": "0b05",
                    "adapter_usb_product_id": "1d70",
                },
                "output": {
                    "adapter": OUTPUT_ADDRESS,
                    "adapter_product": "ASUS USB-BT500",
                    "adapter_bus": "USB",
                    "adapter_usb_vendor_id": "0b05",
                    "adapter_usb_product_id": "1bf6",
                },
            },
            "bluetooth": {"allow_transitional_uart_call": False},
        }
    )


def adapter(
    hci: str, address: str, product_id: str, rfkill_index: int
) -> btadapters.Adapter:
    return btadapters.Adapter(
        hci,
        address,
        "USB",
        rfkill_index,
        usb_vendor_id="0b05",
        usb_product_id=product_id,
        usb_parent="1-1.4",
        usb_interface="1-1.4:1.0",
        driver="btusb",
    )


CALL = adapter("hci7", CALL_ADDRESS, "1d70", 7)
OUTPUT = adapter("hci2", OUTPUT_ADDRESS, "1bf6", 2)


class ResolutionTests(unittest.TestCase):
    def test_each_role_resolves_independently_of_order_or_hci_number(self) -> None:
        configured = roles()
        inventory = [OUTPUT, CALL]
        self.assertIs(
            bt_watchdog.resolve_role(configured, "call", objects={}, inventory=inventory),
            CALL,
        )
        self.assertIs(
            bt_watchdog.resolve_role(configured, "output", objects={}, inventory=inventory),
            OUTPUT,
        )

    def test_onboard_phone_bond_is_not_inferred_as_the_call_controller(self) -> None:
        onboard = btadapters.Adapter("hci0", ONBOARD_ADDRESS, "UART", 0)
        with self.assertRaises(controller_roles.ControllerMissingError):
            bt_watchdog.resolve_role(
                roles(), "call", objects={}, inventory=[onboard, OUTPUT]
            )


class IndependentStateTests(unittest.TestCase):
    def test_role_state_lock_and_backoff_are_independent(self) -> None:
        call = bt_watchdog.RecoveryState("call", failures=2)
        output = bt_watchdog.RecoveryState("output", failures=1)

        recovered = bt_watchdog.attempt_recovery(
            call,
            lambda: bt_watchdog.RecoveryResult(True, "hci-down-up"),
            now=500.0,
        )

        self.assertTrue(recovered)
        self.assertEqual(call.failures, 0)
        self.assertEqual(call.recoveries, 1)
        self.assertEqual(call.backoff, 120.0)
        self.assertEqual(output.failures, 1)
        self.assertEqual(output.recoveries, 0)
        self.assertEqual(output.backoff, 60.0)
        self.assertNotEqual(
            bt_watchdog.role_lock_path("call"), bt_watchdog.role_lock_path("output")
        )
        self.assertNotEqual(
            bt_watchdog.role_state_path("call"), bt_watchdog.role_state_path("output")
        )

    def test_failed_recovery_keeps_only_that_roles_failure_state(self) -> None:
        call = bt_watchdog.RecoveryState("call", failures=3, backoff=240.0)
        output = bt_watchdog.RecoveryState("output", failures=2)
        recover = mock.Mock(
            return_value=bt_watchdog.RecoveryResult(False, "exhausted", "still wedged")
        )
        self.assertFalse(
            bt_watchdog.attempt_recovery(
                call,
                recover,
                now=500.0,
            )
        )
        self.assertFalse(bt_watchdog.attempt_recovery(call, recover, now=501.0))
        self.assertEqual((call.failures, call.backoff), (3, 240.0))
        self.assertEqual(call.last_attempt, 0.0)
        self.assertEqual(recover.call_count, 2)
        self.assertEqual((output.failures, output.backoff), (2, 60.0))

    def test_state_files_are_separate(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            bt_watchdog, "STATE_DIR", Path(directory)
        ):
            bt_watchdog.write_state(bt_watchdog.RecoveryState("call"), CALL)
            bt_watchdog.write_state(bt_watchdog.RecoveryState("output"), OUTPUT)
            call = json.loads((Path(directory) / "call.json").read_text(encoding="utf-8"))
            output = json.loads(
                (Path(directory) / "output.json").read_text(encoding="utf-8")
            )
        self.assertEqual(call["role"], "call")
        self.assertEqual(call["controller"]["address"], CALL_ADDRESS)
        self.assertEqual(output["role"], "output")
        self.assertEqual(output["controller"]["address"], OUTPUT_ADDRESS)


class TargetedRecoveryTests(unittest.TestCase):
    def test_output_transaction_lock_covers_the_complete_destructive_ladder(self) -> None:
        state = bt_watchdog.RecoveryState("output", failures=1)
        lock_active = False

        class Lock:
            def __enter__(self):
                nonlocal lock_active
                lock_active = True
                return True

            def __exit__(self, *_args):
                nonlocal lock_active
                lock_active = False

        def recover() -> bt_watchdog.RecoveryResult:
            self.assertTrue(lock_active)
            return bt_watchdog.RecoveryResult(True, "hci-down-up")

        with mock.patch.object(
            bt_watchdog.btadapters, "speaker_radio_lock", return_value=Lock()
        ) as lock:
            self.assertTrue(
                bt_watchdog.record_failure_and_recover(
                    roles(), "output", state, recover=recover
                )
            )
        lock.assert_called_once_with(bt_watchdog.OUTPUT_RADIO_LOCK, blocking=False)
        self.assertFalse(lock_active)
        self.assertEqual(state.failures, 0)

    def test_busy_output_transaction_spends_no_failure_or_recovery_attempt(self) -> None:
        state = bt_watchdog.RecoveryState("output", failures=1, backoff=240.0)
        recover = mock.Mock()
        with mock.patch.object(
            bt_watchdog.btadapters,
            "speaker_radio_lock",
            return_value=nullcontext(False),
        ):
            self.assertFalse(
                bt_watchdog.record_failure_and_recover(
                    roles(), "output", state, recover=recover
                )
            )
        self.assertEqual((state.failures, state.backoff), (1, 240.0))
        self.assertEqual(state.last_action, "output-radio-busy")
        recover.assert_not_called()

    def test_full_ladder_uses_only_the_call_controller_exact_targets(self) -> None:
        commands: list[list[str]] = []

        def run(command, **_kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        with tempfile.TemporaryDirectory() as directory:
            driver = Path(directory) / "btusb"
            driver.mkdir()
            (driver / "unbind").touch()
            (driver / "bind").touch()
            with (
                mock.patch.object(bt_watchdog, "SYS_USB_DRIVERS", Path(directory)),
                mock.patch.object(bt_watchdog.subprocess, "run", side_effect=run),
                mock.patch.object(bt_watchdog.btadapters, "power_on", return_value=(True, "on")),
                mock.patch.object(bt_watchdog.btadapters, "disconnect") as disconnect,
                mock.patch.object(bt_watchdog.time, "sleep"),
            ):
                result = bt_watchdog.targeted_recovery(
                    roles(),
                    "call",
                    refresh=lambda: CALL,
                    verify=mock.Mock(side_effect=[False, False, False, False, True]),
                )
            unbound = (driver / "unbind").read_text(encoding="utf-8")
            rebound = (driver / "bind").read_text(encoding="utf-8")

        self.assertTrue(result.ok)
        self.assertEqual(result.action, "usb-interface-rebind")
        disconnect.assert_called_once_with(PHONE_ADDRESS, CALL, cancelled=None)
        self.assertIn(["hciconfig", CALL.hci, "down"], commands)
        self.assertIn(["hciconfig", CALL.hci, "up"], commands)
        self.assertIn(["rfkill", "block", str(CALL.rfkill_index)], commands)
        self.assertIn(["rfkill", "unblock", str(CALL.rfkill_index)], commands)
        self.assertFalse(any("systemctl" in command for command in commands))
        self.assertFalse(any("bluetooth" in command for command in commands))
        self.assertFalse(any(OUTPUT.hci in command for command in commands))
        self.assertEqual(unbound.strip(), CALL.usb_interface)
        self.assertEqual(rebound.strip(), CALL.usb_interface)

    def test_output_recovery_disconnects_only_the_desired_speaker_on_bt500(self) -> None:
        with (
            mock.patch.object(bt_watchdog.btadapters, "power_on", return_value=(True, "on")),
            mock.patch.object(bt_watchdog, "recovery_device", return_value=SPEAKER_ADDRESS),
            mock.patch.object(bt_watchdog.btadapters, "disconnect") as disconnect,
            mock.patch.object(bt_watchdog.time, "sleep"),
        ):
            result = bt_watchdog.targeted_recovery(
                roles(),
                "output",
                refresh=lambda: OUTPUT,
                verify=mock.Mock(side_effect=[False, True]),
            )
        self.assertTrue(result.ok)
        self.assertEqual(result.action, "device-disconnect")
        disconnect.assert_called_once_with(SPEAKER_ADDRESS, OUTPUT, cancelled=None)

    def test_shutdown_is_checked_immediately_before_every_recovery_mutation(self) -> None:
        checks = 0

        def cancelled() -> bool:
            nonlocal checks
            checks += 1
            # Allow resolve and power-on, then stop before the device disconnect.
            return checks >= 3

        with (
            mock.patch.object(
                bt_watchdog.btadapters, "power_on", return_value=(True, "on")
            ) as power_on,
            mock.patch.object(
                bt_watchdog, "recovery_device", return_value=SPEAKER_ADDRESS
            ),
            mock.patch.object(bt_watchdog.btadapters, "disconnect") as disconnect,
        ):
            result = bt_watchdog.targeted_recovery(
                roles(),
                "output",
                refresh=lambda: OUTPUT,
                verify=lambda: False,
                cancelled=cancelled,
            )
        self.assertEqual(result.action, "cancelled")
        power_on.assert_called_once_with(OUTPUT, cancelled=cancelled)
        disconnect.assert_not_called()

    def test_hci_rfkill_and_usb_helpers_recheck_shutdown_between_commands(self) -> None:
        calls: list[list[str]] = []

        def run(command, **_kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch.object(bt_watchdog.subprocess, "run", side_effect=run):
            checks = iter((False, False, True))
            with self.assertRaises(bt_watchdog.RecoveryCancelled):
                bt_watchdog.cycle_hci(CALL, cancelled=lambda: next(checks))
        self.assertEqual(calls, [["hciconfig", CALL.hci, "down"]])

        calls.clear()
        with mock.patch.object(bt_watchdog.subprocess, "run", side_effect=run):
            checks = iter((False, False, True))
            with self.assertRaises(bt_watchdog.RecoveryCancelled):
                bt_watchdog.cycle_rfkill(CALL, cancelled=lambda: next(checks))
        self.assertEqual(calls, [["rfkill", "block", str(CALL.rfkill_index)]])

        with tempfile.TemporaryDirectory() as directory:
            driver = Path(directory) / "btusb"
            driver.mkdir()
            (driver / "unbind").touch()
            (driver / "bind").touch()
            checks = iter((False, False, True))
            with (
                mock.patch.object(bt_watchdog, "SYS_USB_DRIVERS", Path(directory)),
                self.assertRaises(bt_watchdog.RecoveryCancelled),
            ):
                bt_watchdog.rebind_usb(CALL, cancelled=lambda: next(checks))
            self.assertEqual((driver / "unbind").read_text().strip(), CALL.usb_interface)
            self.assertEqual((driver / "bind").read_text(), "")

    def test_automatic_source_contains_no_global_recovery_action(self) -> None:
        source = Path(bt_watchdog.__file__).read_text(encoding="utf-8")
        self.assertNotIn("bt-reset.sh", source)
        self.assertNotIn("systemctl restart", source)
        self.assertNotIn("restart wireplumber", source.lower())
        self.assertNotIn('["rfkill", "block", "bluetooth"]', source)


class ReconnectTests(unittest.TestCase):
    def status(self, adapter_address: str = OUTPUT_ADDRESS) -> dict:
        return {
            "output": {
                "desired_id": f"a2dp:{SPEAKER_ADDRESS}",
                "candidates": [
                    {
                        "id": f"a2dp:{SPEAKER_ADDRESS}",
                        "address": SPEAKER_ADDRESS,
                        "adapter_address": adapter_address,
                    }
                ],
            }
        }

    def test_desired_speaker_rejects_wrong_controller_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            path.write_text(json.dumps(self.status(CALL_ADDRESS)), encoding="utf-8")
            with mock.patch.object(bt_watchdog, "STATUS_PATH", path):
                self.assertIsNone(bt_watchdog.desired_speaker_address(roles()))

    def test_output_reconnect_uses_bt500_and_the_speaker_radio_lock(self) -> None:
        state = bt_watchdog.RecoveryState("output")
        with (
            mock.patch.object(bt_watchdog, "recovery_device", return_value=SPEAKER_ADDRESS),
            mock.patch.object(bt_watchdog.btadapters, "managed_objects", return_value={}),
            mock.patch.object(bt_watchdog, "resolve_role", return_value=OUTPUT) as resolve,
            mock.patch.object(bt_watchdog.btadapters, "connected_on", return_value=False),
            mock.patch.object(
                bt_watchdog.btadapters,
                "speaker_radio_lock",
                return_value=nullcontext(True),
            ) as lock,
            mock.patch.object(bt_watchdog.btadapters, "power_on", return_value=(True, "on")),
            mock.patch.object(
                bt_watchdog.btadapters,
                "pin_to_adapter",
                return_value=btadapters.TrustPinResult(True),
            ) as pin,
            mock.patch.object(
                bt_watchdog.btadapters, "connect_profile", return_value=(True, "connected")
            ) as connect,
        ):
            self.assertTrue(bt_watchdog.service_reconnect(roles(), "output", state, now=100.0))
        self.assertTrue(all(call.args[1] == "output" for call in resolve.call_args_list))
        lock.assert_called_once_with(bt_watchdog.OUTPUT_RADIO_LOCK, blocking=False)
        pin.assert_called_once_with(SPEAKER_ADDRESS, OUTPUT, cancelled=None)
        connect.assert_called_once_with(SPEAKER_ADDRESS, OUTPUT, cancelled=None)

    def test_call_reconnect_uses_bt600_without_the_speaker_transaction(self) -> None:
        state = bt_watchdog.RecoveryState("call")
        with (
            mock.patch.object(bt_watchdog.btadapters, "managed_objects", return_value={}),
            mock.patch.object(bt_watchdog, "resolve_role", return_value=CALL),
            mock.patch.object(bt_watchdog.btadapters, "connected_on", return_value=False),
            mock.patch.object(bt_watchdog.btadapters, "speaker_radio_lock") as output_lock,
            mock.patch.object(bt_watchdog.btadapters, "power_on", return_value=(True, "on")),
            mock.patch.object(
                bt_watchdog.btadapters, "connect", return_value=(True, "connected")
            ) as connect,
            mock.patch.object(bt_watchdog.btadapters, "pin_to_adapter") as pin,
        ):
            self.assertTrue(bt_watchdog.service_reconnect(roles(), "call", state, now=100.0))
        output_lock.assert_not_called()
        pin.assert_not_called()
        connect.assert_called_once_with(PHONE_ADDRESS, CALL, cancelled=None)

    def test_shutdown_between_power_and_connect_stops_reconnect(self) -> None:
        state = bt_watchdog.RecoveryState("call")
        stopping = False

        def power_on(_adapter, **_kwargs):
            nonlocal stopping
            stopping = True
            return True, "on"

        with (
            mock.patch.object(bt_watchdog.btadapters, "managed_objects", return_value={}),
            mock.patch.object(bt_watchdog, "resolve_role", return_value=CALL),
            mock.patch.object(bt_watchdog.btadapters, "connected_on", return_value=False),
            mock.patch.object(bt_watchdog.btadapters, "power_on", side_effect=power_on),
            mock.patch.object(bt_watchdog.btadapters, "connect") as connect,
        ):
            self.assertFalse(
                bt_watchdog.service_reconnect(
                    roles(), "call", state, now=100.0, cancelled=lambda: stopping
                )
            )
        connect.assert_not_called()
        self.assertEqual(state.last_action, "cancelled")

    def test_busy_output_radio_spends_no_attempt(self) -> None:
        state = bt_watchdog.RecoveryState("output", reconnect_attempts=2)
        with (
            mock.patch.object(bt_watchdog, "recovery_device", return_value=SPEAKER_ADDRESS),
            mock.patch.object(bt_watchdog.btadapters, "managed_objects", return_value={}),
            mock.patch.object(bt_watchdog, "resolve_role", return_value=OUTPUT),
            mock.patch.object(bt_watchdog.btadapters, "connected_on", return_value=False),
            mock.patch.object(
                bt_watchdog.btadapters,
                "speaker_radio_lock",
                return_value=nullcontext(False),
            ),
            mock.patch.object(bt_watchdog.btadapters, "connect_profile") as connect,
        ):
            self.assertFalse(bt_watchdog.service_reconnect(roles(), "output", state, now=100.0))
        self.assertEqual(state.reconnect_attempts, 2)
        connect.assert_not_called()


class ProbeTests(unittest.TestCase):
    def test_probe_targets_only_the_resolved_runtime_hci(self) -> None:
        result = subprocess.CompletedProcess([], 0, "HCI Version: 6.0\n", "")
        with mock.patch.object(bt_watchdog.subprocess, "run", return_value=result) as run:
            self.assertIs(bt_watchdog.probe_controller(CALL), bt_watchdog.ProbeStatus.ANSWERED)
        self.assertEqual(run.call_args.args[0], ["hciconfig", CALL.hci, "version"])


class UnitTests(unittest.TestCase):
    def test_template_runs_one_explicit_role_and_legacy_name_is_call_only(self) -> None:
        units = Path(bt_watchdog.__file__).resolve().parents[1] / "systemd" / "system"
        template = (units / "bridge-btwatchdog@.service").read_text(encoding="utf-8")
        legacy = (units / "bridge-btwatchdog.service").read_text(encoding="utf-8")
        self.assertIn("bt_watchdog.py --role %i", template)
        self.assertIn("bt_watchdog.py --role call", legacy)
        self.assertNotIn("bt-reset.sh", template)
        self.assertNotIn("bluetooth.service restart", template)
        self.assertNotIn("wireplumber", template.lower())


if __name__ == "__main__":
    unittest.main()
