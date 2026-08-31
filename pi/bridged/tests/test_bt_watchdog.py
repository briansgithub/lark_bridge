from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import bt_watchdog
import btadapters
import controller_roles

BT500_ADDRESS = "A0:AD:9F:73:6C:24"
ONBOARD_ADDRESS = "B8:27:EB:43:8D:51"
PHONE_ADDRESS = "5C:33:7B:CB:BF:C5"


def roles() -> controller_roles.ControllerRoles:
    return controller_roles.parse_controller_roles(
        {
            "bridge": {
                "mode": "bluetooth-wired",
                "fallback_to_wired": True,
            },
            "devices": {
                "phone": {
                    "address": PHONE_ADDRESS,
                    "adapter": BT500_ADDRESS,
                    "adapter_product": "ASUS USB-BT500",
                    "adapter_bus": "USB",
                    "adapter_usb_vendor_id": "0b05",
                    "adapter_usb_product_id": "1bf6",
                },
                "output": {
                    "id": "wired:alsa_output.platform-3f00b840.mailbox.stereo-fallback",
                },
            },
        }
    )


def adapter(
    hci: str = "hci7",
    *,
    address: str = BT500_ADDRESS,
    product_id: str = "1bf6",
    rfkill_index: int = 7,
    usb_interface: str = "1-1.4:1.0",
) -> btadapters.Adapter:
    return btadapters.Adapter(
        hci=hci,
        address=address,
        bus="USB",
        rfkill_index=rfkill_index,
        usb_vendor_id="0b05",
        usb_product_id=product_id,
        usb_parent=usb_interface.split(":", 1)[0],
        usb_interface=usb_interface,
        driver="btusb",
        product="ASUS USB-BT500",
        manufacturer="ASUSTek",
    )


BT500 = adapter()


def phone_tree(
    resolved: btadapters.Adapter,
    *,
    connected: bool = False,
    paired: bool = True,
    bonded: bool = True,
    trusted: bool = True,
) -> dict:
    return {
        btadapters.path_for(resolved, PHONE_ADDRESS): {
            "org.bluez.Device1": {
                "Address": {"data": PHONE_ADDRESS},
                "Connected": {"data": connected},
                "Paired": {"data": paired},
                "Bonded": {"data": bonded},
                "Trusted": {"data": trusted},
            }
        }
    }


class ResolutionTests(unittest.TestCase):
    def test_call_role_uses_permanent_identity_not_hci_order(self) -> None:
        onboard = btadapters.Adapter("hci0", ONBOARD_ADDRESS, "UART", 0)
        observed = bt_watchdog.resolve_role(
            roles(), "call", objects={}, inventory=[onboard, BT500]
        )
        self.assertIs(observed, BT500)

    def test_onboard_phone_object_never_selects_the_call_controller(self) -> None:
        onboard = btadapters.Adapter("hci0", ONBOARD_ADDRESS, "UART", 0)
        with self.assertRaises(controller_roles.ControllerMissingError):
            bt_watchdog.resolve_role(roles(), "call", objects={}, inventory=[onboard])

    def test_wrong_bt500_usb_identity_fails_closed(self) -> None:
        wrong = adapter(product_id="ffff")
        with self.assertRaises(controller_roles.ControllerIdentityMismatchError):
            bt_watchdog.resolve_role(roles(), "call", objects={}, inventory=[wrong])

    def test_absent_output_watchdog_uses_typed_error(self) -> None:
        with self.assertRaises(
            controller_roles.ControllerRoleNotConfiguredError
        ) as raised:
            bt_watchdog.role_spec(roles(), "output")
        self.assertEqual(raised.exception.code, "controller_role_not_configured")

    def test_output_cli_exits_before_taking_a_lock(self) -> None:
        with (
            mock.patch.object(
                bt_watchdog.controller_roles,
                "load_controller_roles",
                return_value=roles(),
            ),
            mock.patch.object(bt_watchdog.btadapters, "speaker_radio_lock") as lock,
        ):
            self.assertEqual(bt_watchdog.main(["--role", "output"]), 4)
        lock.assert_not_called()


class TargetedRecoveryTests(unittest.TestCase):
    def test_complete_ladder_targets_only_the_exact_bt500(self) -> None:
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
                mock.patch.object(
                    bt_watchdog.btadapters, "power_on", return_value=(True, "on")
                ) as power,
                mock.patch.object(bt_watchdog.btadapters, "disconnect") as disconnect,
                mock.patch.object(bt_watchdog.time, "sleep"),
            ):
                result = bt_watchdog.targeted_recovery(
                    roles(),
                    "call",
                    refresh=lambda: BT500,
                    verify=mock.Mock(side_effect=[False, False, False, False, True]),
                )
            unbound = (driver / "unbind").read_text(encoding="utf-8")
            rebound = (driver / "bind").read_text(encoding="utf-8")

        self.assertTrue(result.ok)
        self.assertEqual(result.action, "usb-interface-rebind")
        power.assert_called_once_with(BT500, cancelled=None)
        disconnect.assert_called_once_with(PHONE_ADDRESS, BT500, cancelled=None)
        self.assertIn(["hciconfig", BT500.hci, "down"], commands)
        self.assertIn(["hciconfig", BT500.hci, "up"], commands)
        self.assertIn(["rfkill", "block", str(BT500.rfkill_index)], commands)
        self.assertIn(["rfkill", "unblock", str(BT500.rfkill_index)], commands)
        self.assertFalse(any("systemctl" in command for command in commands))
        self.assertFalse(any("bluetooth" in command for command in commands))
        self.assertFalse(any("wireplumber" in command for command in commands))
        self.assertEqual(unbound.strip(), BT500.usb_interface)
        self.assertEqual(rebound.strip(), BT500.usb_interface)

    def test_each_destructive_rung_refreshes_runtime_hci_identity(self) -> None:
        first = adapter("hci7", rfkill_index=7, usb_interface="1-1.1:1.0")
        hci_target = adapter("hci8", rfkill_index=8, usb_interface="1-1.2:1.0")
        rfkill_target = adapter("hci9", rfkill_index=9, usb_interface="1-1.3:1.0")
        rebind_target = adapter("hci10", rfkill_index=10, usb_interface="1-1.4:1.0")
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
                mock.patch.object(
                    bt_watchdog.btadapters, "power_on", return_value=(True, "on")
                ),
                mock.patch.object(bt_watchdog.btadapters, "disconnect"),
                mock.patch.object(bt_watchdog.time, "sleep"),
            ):
                result = bt_watchdog.targeted_recovery(
                    roles(),
                    "call",
                    refresh=mock.Mock(
                        side_effect=[first, hci_target, rfkill_target, rebind_target]
                    ),
                    verify=mock.Mock(side_effect=[False, False, False, False, True]),
                )

        self.assertTrue(result.ok)
        self.assertIn(["hciconfig", hci_target.hci, "down"], commands)
        self.assertIn(["hciconfig", hci_target.hci, "up"], commands)
        self.assertIn(["rfkill", "block", str(rfkill_target.rfkill_index)], commands)
        self.assertIn(["rfkill", "unblock", str(rfkill_target.rfkill_index)], commands)

    def test_shutdown_cancels_before_the_next_mutation(self) -> None:
        stopping = False

        def power_on(_adapter, **_kwargs):
            nonlocal stopping
            stopping = True
            return True, "on"

        with (
            mock.patch.object(bt_watchdog.btadapters, "power_on", side_effect=power_on),
            mock.patch.object(bt_watchdog.btadapters, "disconnect") as disconnect,
        ):
            result = bt_watchdog.targeted_recovery(
                roles(),
                "call",
                refresh=lambda: BT500,
                verify=lambda: False,
                cancelled=lambda: stopping,
            )
        self.assertFalse(result.ok)
        self.assertEqual(result.action, "cancelled")
        disconnect.assert_not_called()

    def test_usb_rebind_rejects_non_btusb_or_ambiguous_interface(self) -> None:
        wrong_driver = adapter()
        wrong_driver = btadapters.Adapter(
            **{**wrong_driver.__dict__, "driver": "other"}
        )
        ambiguous = adapter(usb_interface="../../unbind")
        with mock.patch.object(Path, "write_text") as write:
            self.assertFalse(bt_watchdog.rebind_usb(wrong_driver))
            self.assertFalse(bt_watchdog.rebind_usb(ambiguous))
        write.assert_not_called()


class RecoveryStateTests(unittest.TestCase):
    def test_watchdog_status_is_readable_by_unprivileged_bridgectl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "watchdog"
            with (
                mock.patch.object(bt_watchdog, "STATE_DIR", state_dir),
                mock.patch.object(bt_watchdog.os, "chmod") as chmod,
            ):
                bt_watchdog.write_state(bt_watchdog.RecoveryState("call"), BT500)

            self.assertEqual(
                chmod.call_args_list,
                [
                    mock.call(state_dir, 0o755),
                    mock.call(state_dir / "call.json", 0o644),
                ],
            )

    def test_recovery_waits_for_threshold_then_applies_backoff(self) -> None:
        state = bt_watchdog.RecoveryState("call", failures=1)
        recover = mock.Mock(
            return_value=bt_watchdog.RecoveryResult(True, "hci-down-up")
        )
        self.assertFalse(bt_watchdog.attempt_recovery(state, recover, now=100.0))
        state.failures = 2
        self.assertTrue(bt_watchdog.attempt_recovery(state, recover, now=100.0))
        self.assertEqual(state.failures, 0)
        self.assertEqual(state.recoveries, 1)
        self.assertEqual(state.backoff, 120.0)
        state.failures = 2
        self.assertFalse(bt_watchdog.attempt_recovery(state, recover, now=150.0))
        self.assertEqual(state.last_action, "backoff")

    def test_controller_recovery_discards_pre_recovery_connect_collision(self) -> None:
        state = bt_watchdog.RecoveryState(
            "call",
            failures=2,
            connect_pending_since=50.0,
            connect_pending_target=bt_watchdog._adapter_runtime_target(BT500),
            connect_collision_cancellations=1,
        )

        def recover() -> bt_watchdog.RecoveryResult:
            self.assertIsNone(state.connect_pending_since)
            self.assertIsNone(state.connect_pending_target)
            self.assertEqual(state.connect_collision_cancellations, 0)
            return bt_watchdog.RecoveryResult(True, "hci-down-up")

        self.assertTrue(bt_watchdog.attempt_recovery(state, recover, now=100.0))

    def test_failed_recovery_does_not_consume_backoff_window(self) -> None:
        state = bt_watchdog.RecoveryState("call", failures=2)
        recover = mock.Mock(
            return_value=bt_watchdog.RecoveryResult(False, "exhausted", "wedged")
        )
        self.assertFalse(bt_watchdog.attempt_recovery(state, recover, now=100.0))
        self.assertFalse(bt_watchdog.attempt_recovery(state, recover, now=101.0))
        self.assertEqual(state.last_attempt, 0.0)
        self.assertEqual(state.failures, 2)
        self.assertEqual(recover.call_count, 2)

    def test_role_lock_and_state_paths_reject_output(self) -> None:
        self.assertNotEqual(
            bt_watchdog.role_lock_path("call"),
            bt_watchdog.role_state_path("call"),
        )
        with self.assertRaises(ValueError):
            bt_watchdog.role_lock_path("output")
        with self.assertRaises(ValueError):
            bt_watchdog.role_state_path("output")


class ReconnectTests(unittest.TestCase):
    def test_connected_time_records_only_connection_transitions(self) -> None:
        state = bt_watchdog.RecoveryState("call")
        with (
            mock.patch.object(
                bt_watchdog.btadapters,
                "managed_objects",
                return_value=phone_tree(BT500, connected=True),
            ),
            mock.patch.object(bt_watchdog, "resolve_role", return_value=BT500),
        ):
            self.assertTrue(
                bt_watchdog.service_reconnect(roles(), "call", state, now=18.5)
            )
            self.assertTrue(
                bt_watchdog.service_reconnect(roles(), "call", state, now=33.5)
            )

        self.assertEqual(state.connected_monotonic, 18.5)

    def test_first_exact_runtime_target_connects_before_liveness_gating(self) -> None:
        state = bt_watchdog.RecoveryState("call", reconnect_next=999.0)
        with mock.patch.object(bt_watchdog, "service_reconnect") as reconnect:
            observed = bt_watchdog.service_startup_reconnect(
                roles(), "call", state, BT500, None, now=100.0
            )
            repeated = bt_watchdog.service_startup_reconnect(
                roles(), "call", state, BT500, observed, now=101.0
            )

        self.assertEqual(observed, bt_watchdog._adapter_runtime_target(BT500))
        self.assertEqual(repeated, observed)
        self.assertEqual(
            state.reconnect_next, 100.0 + bt_watchdog.STARTUP_RECONNECT_RETRY
        )
        reconnect.assert_called_once_with(
            roles(), "call", state, now=100.0, cancelled=None
        )

    def test_disconnected_poll_wakes_for_boot_retry_deadline(self) -> None:
        state = bt_watchdog.RecoveryState(
            "call", bond_state="trusted", reconnect_next=102.0
        )
        self.assertEqual(bt_watchdog.service_sleep_interval(state, now=100.0), 2.0)

    def test_connected_poll_keeps_normal_probe_interval(self) -> None:
        state = bt_watchdog.RecoveryState(
            "call", bond_state="connected", reconnect_next=102.0
        )
        self.assertEqual(
            bt_watchdog.service_sleep_interval(state, now=100.0),
            bt_watchdog.PROBE_INTERVAL,
        )

    def test_missing_pixel_object_requires_operator_pairing_without_auto_repair(
        self,
    ) -> None:
        state = bt_watchdog.RecoveryState("call")
        with (
            mock.patch.object(
                bt_watchdog.btadapters, "managed_objects", return_value={}
            ),
            mock.patch.object(bt_watchdog, "resolve_role", return_value=BT500),
            mock.patch.object(bt_watchdog.btadapters, "power_on") as power_on,
            mock.patch.object(bt_watchdog.btadapters, "connect") as connect,
        ):
            self.assertFalse(
                bt_watchdog.service_reconnect(roles(), "call", state, now=100.0)
            )

        self.assertEqual(state.bond_state, "missing")
        self.assertEqual(state.repair_state, "pairing_required")
        self.assertIsNone(state.repair_trigger)
        self.assertEqual(state.last_action, "pairing_required")
        power_on.assert_not_called()
        connect.assert_not_called()

    def test_phone_reconnect_uses_only_the_resolved_bt500(self) -> None:
        state = bt_watchdog.RecoveryState("call")
        with (
            mock.patch.object(
                bt_watchdog.btadapters,
                "managed_objects",
                return_value=phone_tree(BT500),
            ),
            mock.patch.object(
                bt_watchdog, "resolve_role", return_value=BT500
            ) as resolve,
            mock.patch.object(
                bt_watchdog.btadapters, "connected_on", return_value=False
            ),
            mock.patch.object(
                bt_watchdog.btadapters, "power_on", return_value=(True, "on")
            ),
            mock.patch.object(
                bt_watchdog.btadapters, "connect", return_value=(True, "connected")
            ) as connect,
        ):
            self.assertTrue(
                bt_watchdog.service_reconnect(roles(), "call", state, now=100.0)
            )
        self.assertGreaterEqual(resolve.call_count, 2)
        connect.assert_called_once_with(
            PHONE_ADDRESS,
            BT500,
            timeout=bt_watchdog.CONNECT_REQUEST_TIMEOUT,
            cancelled=None,
        )
        self.assertEqual(state.reconnect_attempts, 0)

    def test_shutdown_between_power_and_connect_stops_reconnect(self) -> None:
        state = bt_watchdog.RecoveryState("call")
        stopping = False

        def power_on(_adapter, **_kwargs):
            nonlocal stopping
            stopping = True
            return True, "on"

        with (
            mock.patch.object(
                bt_watchdog.btadapters,
                "managed_objects",
                return_value=phone_tree(BT500),
            ),
            mock.patch.object(bt_watchdog, "resolve_role", return_value=BT500),
            mock.patch.object(
                bt_watchdog.btadapters, "connected_on", return_value=False
            ),
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

    def test_in_progress_waits_without_duplicate_connect_or_spending_attempt(
        self,
    ) -> None:
        state = bt_watchdog.RecoveryState("call")
        pending = (
            "Call failed: org.bluez.Error.InProgress: Operation already in progress"
        )
        with (
            mock.patch.object(
                bt_watchdog.btadapters,
                "managed_objects",
                return_value=phone_tree(BT500),
            ),
            mock.patch.object(bt_watchdog, "resolve_role", return_value=BT500),
            mock.patch.object(
                bt_watchdog.btadapters, "connected_on", return_value=False
            ),
            mock.patch.object(
                bt_watchdog.btadapters, "power_on", return_value=(True, "on")
            ),
            mock.patch.object(
                bt_watchdog.btadapters, "connect", return_value=(False, pending)
            ) as connect,
            mock.patch.object(bt_watchdog.btadapters, "disconnect") as disconnect,
        ):
            self.assertFalse(
                bt_watchdog.service_reconnect(roles(), "call", state, now=100.0)
            )
            self.assertFalse(
                bt_watchdog.service_reconnect(
                    roles(),
                    "call",
                    state,
                    now=100.0 + bt_watchdog.CONNECT_PENDING_TIMEOUT - 1,
                )
            )

        connect.assert_called_once_with(
            PHONE_ADDRESS,
            BT500,
            timeout=bt_watchdog.CONNECT_REQUEST_TIMEOUT,
            cancelled=None,
        )
        disconnect.assert_not_called()
        self.assertEqual(state.reconnect_attempts, 0)
        self.assertEqual(state.connect_pending_since, 100.0)
        self.assertEqual(
            state.reconnect_next, 100.0 + bt_watchdog.CONNECT_PENDING_TIMEOUT
        )
        self.assertEqual(state.last_action, "device-connect-pending")
        snapshot = state.as_dict(BT500)
        self.assertEqual(snapshot["connect_pending_since_monotonic"], 100.0)
        self.assertEqual(
            snapshot["connect_pending_deadline_monotonic"],
            100.0 + bt_watchdog.CONNECT_PENDING_TIMEOUT,
        )

    def test_pending_window_starts_after_connect_returns(self) -> None:
        state = bt_watchdog.RecoveryState("call")
        pending = "Call failed: In Progress"
        with (
            mock.patch.object(
                bt_watchdog.time, "monotonic", side_effect=(100.0, 140.0)
            ),
            mock.patch.object(
                bt_watchdog.btadapters,
                "managed_objects",
                return_value=phone_tree(BT500),
            ),
            mock.patch.object(bt_watchdog, "resolve_role", return_value=BT500),
            mock.patch.object(
                bt_watchdog.btadapters, "connected_on", return_value=False
            ),
            mock.patch.object(
                bt_watchdog.btadapters, "power_on", return_value=(True, "on")
            ),
            mock.patch.object(
                bt_watchdog.btadapters, "connect", return_value=(False, pending)
            ),
        ):
            self.assertFalse(bt_watchdog.service_reconnect(roles(), "call", state))

        self.assertEqual(state.connect_pending_since, 140.0)
        self.assertEqual(
            state.reconnect_next, 140.0 + bt_watchdog.CONNECT_PENDING_TIMEOUT
        )

    def test_observed_connection_during_pending_wait_completes_without_cancel(
        self,
    ) -> None:
        state = bt_watchdog.RecoveryState(
            "call",
            connect_pending_since=100.0,
            connect_pending_target=bt_watchdog._adapter_runtime_target(BT500),
            reconnect_next=100.0 + bt_watchdog.CONNECT_PENDING_TIMEOUT,
            last_error="Call failed: In Progress",
        )
        tree = phone_tree(BT500, connected=True)
        with (
            mock.patch.object(
                bt_watchdog.btadapters, "managed_objects", return_value=tree
            ),
            mock.patch.object(bt_watchdog, "resolve_role", return_value=BT500),
            mock.patch.object(bt_watchdog.btadapters, "power_on") as power_on,
            mock.patch.object(bt_watchdog.btadapters, "connect") as connect,
            mock.patch.object(bt_watchdog.btadapters, "disconnect") as disconnect,
        ):
            self.assertTrue(
                bt_watchdog.service_reconnect(roles(), "call", state, now=120.0)
            )

        self.assertIsNone(state.connect_pending_since)
        self.assertIsNone(state.connect_pending_target)
        self.assertEqual(state.connect_collision_cancellations, 0)
        self.assertEqual(state.last_action, "connected")
        self.assertIsNone(state.last_error)
        power_on.assert_not_called()
        connect.assert_not_called()
        disconnect.assert_not_called()

    def test_expired_in_progress_cancels_once_then_retries_current_target(self) -> None:
        state = bt_watchdog.RecoveryState(
            "call",
            connect_pending_since=100.0,
            connect_pending_target=bt_watchdog._adapter_runtime_target(BT500),
            reconnect_next=100.0 + bt_watchdog.CONNECT_PENDING_TIMEOUT,
        )
        tree = phone_tree(BT500)
        with (
            mock.patch.object(
                bt_watchdog.btadapters, "managed_objects", return_value=tree
            ),
            mock.patch.object(bt_watchdog, "resolve_role", return_value=BT500),
            mock.patch.object(
                bt_watchdog.btadapters,
                "disconnect",
                return_value=(True, "disconnected"),
            ) as disconnect,
            mock.patch.object(bt_watchdog.btadapters, "power_on") as power_on,
            mock.patch.object(bt_watchdog.btadapters, "connect") as connect,
        ):
            self.assertFalse(
                bt_watchdog.service_reconnect(roles(), "call", state, now=145.0)
            )

        disconnect.assert_called_once_with(PHONE_ADDRESS, BT500, cancelled=None)
        power_on.assert_not_called()
        connect.assert_not_called()
        self.assertEqual(state.connect_collision_cancellations, 1)
        self.assertEqual(
            state.reconnect_next, 145.0 + bt_watchdog.POST_CANCEL_RETRY_DELAY
        )
        self.assertEqual(state.last_action, "device-connect-cancelled")

        moved = adapter("hci9", rfkill_index=9, usb_interface="1-1.4:1.0")
        with (
            mock.patch.object(
                bt_watchdog.btadapters,
                "managed_objects",
                return_value=phone_tree(moved),
            ),
            mock.patch.object(bt_watchdog, "resolve_role", return_value=moved),
            mock.patch.object(
                bt_watchdog.btadapters, "power_on", return_value=(True, "on")
            ),
            mock.patch.object(
                bt_watchdog.btadapters, "connect", return_value=(True, "connected")
            ) as connect,
            mock.patch.object(bt_watchdog.btadapters, "disconnect") as disconnect_again,
        ):
            self.assertTrue(
                bt_watchdog.service_reconnect(roles(), "call", state, now=165.0)
            )

        connect.assert_called_once_with(
            PHONE_ADDRESS,
            moved,
            timeout=bt_watchdog.CONNECT_REQUEST_TIMEOUT,
            cancelled=None,
        )
        disconnect_again.assert_not_called()
        self.assertEqual(state.connect_collision_cancellations, 0)

    def test_changed_pending_target_settles_without_cancelling_new_adapter(
        self,
    ) -> None:
        state = bt_watchdog.RecoveryState(
            "call",
            connect_pending_since=100.0,
            connect_pending_target=bt_watchdog._adapter_runtime_target(BT500),
            reconnect_next=100.0 + bt_watchdog.CONNECT_PENDING_TIMEOUT,
        )
        moved = adapter("hci8", rfkill_index=8, usb_interface="1-1.3:1.0")
        with (
            mock.patch.object(
                bt_watchdog.btadapters,
                "managed_objects",
                return_value=phone_tree(moved),
            ),
            mock.patch.object(bt_watchdog, "resolve_role", return_value=moved),
            mock.patch.object(bt_watchdog.btadapters, "disconnect") as disconnect,
            mock.patch.object(bt_watchdog.btadapters, "power_on") as power_on,
            mock.patch.object(bt_watchdog.btadapters, "connect") as connect,
        ):
            self.assertFalse(
                bt_watchdog.service_reconnect(roles(), "call", state, now=145.0)
            )

        disconnect.assert_not_called()
        power_on.assert_not_called()
        connect.assert_not_called()
        self.assertEqual(state.last_action, "device-connect-target-refreshed")
        self.assertIsNone(state.connect_pending_since)
        self.assertEqual(state.reconnect_next, 145.0 + bt_watchdog.RECONNECT_DELAY)

    def test_second_in_progress_after_exact_cancel_requests_stale_bond_repair(
        self,
    ) -> None:
        state = bt_watchdog.RecoveryState(
            "call",
            connect_collision_cancellations=1,
        )
        pending = "Call failed: In Progress"
        with (
            mock.patch.object(
                bt_watchdog.btadapters,
                "managed_objects",
                return_value=phone_tree(BT500),
            ),
            mock.patch.object(bt_watchdog, "resolve_role", return_value=BT500),
            mock.patch.object(
                bt_watchdog.btadapters, "connected_on", return_value=False
            ),
            mock.patch.object(
                bt_watchdog.btadapters, "power_on", return_value=(True, "on")
            ),
            mock.patch.object(
                bt_watchdog.btadapters, "connect", return_value=(False, pending)
            ) as connect,
            mock.patch.object(bt_watchdog.btadapters, "disconnect") as disconnect,
        ):
            self.assertFalse(
                bt_watchdog.service_reconnect(roles(), "call", state, now=200.0)
            )
        connect.assert_called_once_with(
            PHONE_ADDRESS,
            BT500,
            timeout=bt_watchdog.CONNECT_REQUEST_TIMEOUT,
            cancelled=None,
        )
        disconnect.assert_not_called()
        self.assertEqual(state.reconnect_attempts, 1)
        self.assertEqual(state.reconnect_next, 0.0)
        self.assertEqual(state.last_action, "stale_bond_suspected")
        self.assertEqual(state.repair_state, "requested")
        self.assertEqual(state.repair_trigger, "repeated-in-progress")
        self.assertEqual(state.stale_connect_signatures, 1)
        self.assertEqual(state.connect_collision_cancellations, 1)

    def test_timeout_after_exact_cancel_is_not_stale_bond_evidence(self) -> None:
        state = bt_watchdog.RecoveryState(
            "call",
            connect_collision_cancellations=1,
        )
        with (
            mock.patch.object(
                bt_watchdog.btadapters,
                "managed_objects",
                return_value=phone_tree(BT500),
            ),
            mock.patch.object(bt_watchdog, "resolve_role", return_value=BT500),
            mock.patch.object(
                bt_watchdog.btadapters, "power_on", return_value=(True, "on")
            ),
            mock.patch.object(
                bt_watchdog.btadapters,
                "connect",
                return_value=(False, "request timed out"),
            ) as connect,
        ):
            self.assertFalse(
                bt_watchdog.service_reconnect(roles(), "call", state, now=200.0)
            )

        connect.assert_called_once_with(
            PHONE_ADDRESS,
            BT500,
            timeout=bt_watchdog.CONNECT_REQUEST_TIMEOUT,
            cancelled=None,
        )
        self.assertEqual(state.repair_state, "idle")
        self.assertIsNone(state.repair_trigger)
        self.assertEqual(state.stale_connect_signatures, 0)
        self.assertEqual(state.last_action, "device-reconnect")
        self.assertEqual(state.reconnect_next, 200.0 + bt_watchdog.RECONNECT_RETRY)

    def test_failed_pending_cancel_enters_cooldown_without_retry(self) -> None:
        state = bt_watchdog.RecoveryState(
            "call",
            connect_pending_since=100.0,
            connect_pending_target=bt_watchdog._adapter_runtime_target(BT500),
        )
        tree = phone_tree(BT500)
        with (
            mock.patch.object(
                bt_watchdog.btadapters, "managed_objects", return_value=tree
            ),
            mock.patch.object(bt_watchdog, "resolve_role", return_value=BT500),
            mock.patch.object(
                bt_watchdog.btadapters,
                "disconnect",
                return_value=(False, "Call failed: org.bluez.Error.Failed"),
            ) as disconnect,
            mock.patch.object(bt_watchdog.btadapters, "power_on") as power_on,
            mock.patch.object(bt_watchdog.btadapters, "connect") as connect,
        ):
            self.assertFalse(
                bt_watchdog.service_reconnect(roles(), "call", state, now=145.0)
            )
            self.assertFalse(
                bt_watchdog.service_reconnect(
                    roles(),
                    "call",
                    state,
                    now=145.0 + bt_watchdog.CALL_RECONNECT_COOLDOWN - 1,
                )
            )

        disconnect.assert_called_once_with(PHONE_ADDRESS, BT500, cancelled=None)
        power_on.assert_not_called()
        connect.assert_not_called()
        self.assertEqual(state.last_action, "device-connect-cancel-failed")
        self.assertEqual(state.reconnect_attempts, bt_watchdog.CALL_RECONNECT_ATTEMPTS)
        self.assertEqual(
            state.reconnect_next, 145.0 + bt_watchdog.CALL_RECONNECT_COOLDOWN
        )

    def test_missing_controller_clears_pending_but_preserves_cancel_budget(
        self,
    ) -> None:
        state = bt_watchdog.RecoveryState(
            "call",
            connect_pending_since=100.0,
            connect_pending_target=bt_watchdog._adapter_runtime_target(BT500),
            connect_collision_cancellations=1,
        )
        error = controller_roles.ControllerMissingError("call", BT500_ADDRESS)
        with (
            mock.patch.object(
                bt_watchdog.btadapters,
                "managed_objects",
                return_value=phone_tree(BT500),
            ),
            mock.patch.object(bt_watchdog, "resolve_role", side_effect=error),
            mock.patch.object(bt_watchdog.btadapters, "disconnect") as disconnect,
            mock.patch.object(bt_watchdog.btadapters, "power_on") as power_on,
            mock.patch.object(bt_watchdog.btadapters, "connect") as connect,
        ):
            self.assertFalse(
                bt_watchdog.service_reconnect(roles(), "call", state, now=120.0)
            )

        self.assertIsNone(state.connect_pending_since)
        self.assertIsNone(state.connect_pending_target)
        self.assertEqual(state.connect_collision_cancellations, 1)
        self.assertEqual(state.reconnect_next, 120.0 + bt_watchdog.RECONNECT_DELAY)
        disconnect.assert_not_called()
        power_on.assert_not_called()
        connect.assert_not_called()

    def test_disconnect_not_connected_or_unknown_object_is_safe_to_retry(self) -> None:
        for detail in (
            "Call failed: org.bluez.Error.NotConnected",
            "Call failed: org.freedesktop.DBus.Error.UnknownObject: Unknown object",
        ):
            with self.subTest(detail=detail):
                self.assertTrue(bt_watchdog._disconnect_quiesced(False, detail))
        self.assertFalse(
            bt_watchdog._disconnect_quiesced(
                False, "Call failed: org.bluez.Error.Failed"
            )
        )

    def test_pending_cancel_fails_closed_when_identity_changes(self) -> None:
        state = bt_watchdog.RecoveryState(
            "call",
            connect_pending_since=100.0,
            connect_pending_target=bt_watchdog._adapter_runtime_target(BT500),
        )
        error = controller_roles.ControllerIdentityMismatchError(
            "call", "expected USB 0b05:1bf6, observed 0b05:ffff"
        )
        with (
            mock.patch.object(
                bt_watchdog.btadapters,
                "managed_objects",
                return_value=phone_tree(BT500),
            ),
            mock.patch.object(bt_watchdog, "resolve_role", side_effect=error),
            mock.patch.object(bt_watchdog.btadapters, "disconnect") as disconnect,
            mock.patch.object(bt_watchdog.btadapters, "power_on") as power_on,
            mock.patch.object(bt_watchdog.btadapters, "connect") as connect,
        ):
            self.assertFalse(
                bt_watchdog.service_reconnect(roles(), "call", state, now=145.0)
            )

        self.assertIn("controller_identity_mismatch", state.identity_error or "")
        self.assertEqual(state.last_action, "resolve")
        disconnect.assert_not_called()
        power_on.assert_not_called()
        connect.assert_not_called()

    def test_shutdown_before_pending_cancel_performs_no_mutation(self) -> None:
        state = bt_watchdog.RecoveryState(
            "call",
            connect_pending_since=100.0,
            connect_pending_target=bt_watchdog._adapter_runtime_target(BT500),
        )
        checks = iter((False, True))
        with (
            mock.patch.object(
                bt_watchdog.btadapters,
                "managed_objects",
                return_value=phone_tree(BT500),
            ),
            mock.patch.object(bt_watchdog, "resolve_role", return_value=BT500),
            mock.patch.object(bt_watchdog.btadapters, "disconnect") as disconnect,
            mock.patch.object(bt_watchdog.btadapters, "power_on") as power_on,
            mock.patch.object(bt_watchdog.btadapters, "connect") as connect,
        ):
            self.assertFalse(
                bt_watchdog.service_reconnect(
                    roles(), "call", state, now=145.0, cancelled=lambda: next(checks)
                )
            )

        self.assertEqual(state.last_action, "cancelled")
        disconnect.assert_not_called()
        power_on.assert_not_called()
        connect.assert_not_called()

    def test_failed_burst_enters_cooldown_without_an_immediate_fourth_attempt(
        self,
    ) -> None:
        state = bt_watchdog.RecoveryState("call", reconnect_attempts=2)
        with (
            mock.patch.object(
                bt_watchdog.btadapters,
                "managed_objects",
                return_value=phone_tree(BT500),
            ),
            mock.patch.object(bt_watchdog, "resolve_role", return_value=BT500),
            mock.patch.object(
                bt_watchdog.btadapters, "connected_on", return_value=False
            ),
            mock.patch.object(
                bt_watchdog.btadapters, "power_on", return_value=(True, "on")
            ),
            mock.patch.object(
                bt_watchdog.btadapters,
                "connect",
                return_value=(
                    False,
                    "Call failed: org.bluez.Error.Failed: Host is down",
                ),
            ) as connect,
        ):
            self.assertFalse(
                bt_watchdog.service_reconnect(roles(), "call", state, now=100.0)
            )
            self.assertFalse(
                bt_watchdog.service_reconnect(
                    roles(),
                    "call",
                    state,
                    now=100.0 + bt_watchdog.CALL_RECONNECT_COOLDOWN - 1,
                )
            )

        self.assertEqual(connect.call_count, 1)
        self.assertEqual(state.reconnect_attempts, bt_watchdog.CALL_RECONNECT_ATTEMPTS)
        self.assertEqual(
            state.reconnect_next, 100.0 + bt_watchdog.CALL_RECONNECT_COOLDOWN
        )

    def test_cooldown_expiry_starts_a_new_exact_target_burst(self) -> None:
        state = bt_watchdog.RecoveryState(
            "call",
            reconnect_attempts=bt_watchdog.CALL_RECONNECT_ATTEMPTS,
            reconnect_next=220.0,
        )
        with (
            mock.patch.object(
                bt_watchdog.btadapters,
                "managed_objects",
                return_value=phone_tree(BT500),
            ),
            mock.patch.object(bt_watchdog, "resolve_role", return_value=BT500),
            mock.patch.object(
                bt_watchdog.btadapters, "connected_on", return_value=False
            ),
            mock.patch.object(
                bt_watchdog.btadapters, "power_on", return_value=(True, "on")
            ),
            mock.patch.object(
                bt_watchdog.btadapters,
                "connect",
                return_value=(
                    False,
                    "Call failed: org.bluez.Error.Failed: Host is down",
                ),
            ) as connect,
        ):
            self.assertFalse(
                bt_watchdog.service_reconnect(roles(), "call", state, now=220.0)
            )

        connect.assert_called_once_with(
            PHONE_ADDRESS,
            BT500,
            timeout=bt_watchdog.CONNECT_REQUEST_TIMEOUT,
            cancelled=None,
        )
        self.assertEqual(state.reconnect_attempts, 1)
        self.assertEqual(state.reconnect_next, 220.0 + bt_watchdog.RECONNECT_RETRY)

    def test_observed_connection_resets_the_retry_burst(self) -> None:
        state = bt_watchdog.RecoveryState(
            "call",
            reconnect_attempts=bt_watchdog.CALL_RECONNECT_ATTEMPTS,
            reconnect_next=500.0,
            identity_error="stale identity error",
            last_action="device-reconnect",
            last_error="connection timed out",
        )
        with (
            mock.patch.object(
                bt_watchdog.btadapters,
                "managed_objects",
                return_value=phone_tree(BT500, connected=True),
            ),
            mock.patch.object(bt_watchdog, "resolve_role", return_value=BT500),
            mock.patch.object(
                bt_watchdog.btadapters, "connected_on", return_value=True
            ),
            mock.patch.object(bt_watchdog.btadapters, "power_on") as power_on,
            mock.patch.object(bt_watchdog.btadapters, "connect") as connect,
        ):
            self.assertTrue(
                bt_watchdog.service_reconnect(roles(), "call", state, now=300.0)
            )

        self.assertEqual(state.reconnect_attempts, 0)
        self.assertEqual(state.reconnect_next, 0.0)
        self.assertIsNone(state.identity_error)
        self.assertEqual(state.last_action, "connected")
        self.assertIsNone(state.last_error)
        power_on.assert_not_called()
        connect.assert_not_called()

    def test_wrong_controller_identity_never_reaches_connect(self) -> None:
        state = bt_watchdog.RecoveryState(
            "call",
            reconnect_attempts=bt_watchdog.CALL_RECONNECT_ATTEMPTS,
            reconnect_next=100.0,
            connect_collision_cancellations=1,
        )
        error = controller_roles.ControllerIdentityMismatchError(
            "call", "expected USB 0b05:1bf6, observed 0b05:ffff"
        )
        with (
            mock.patch.object(
                bt_watchdog.btadapters,
                "managed_objects",
                return_value=phone_tree(BT500),
            ),
            mock.patch.object(bt_watchdog, "resolve_role", side_effect=[BT500, error]),
            mock.patch.object(
                bt_watchdog.btadapters, "connected_on", return_value=False
            ),
            mock.patch.object(bt_watchdog.btadapters, "power_on") as power_on,
            mock.patch.object(bt_watchdog.btadapters, "connect") as connect,
        ):
            self.assertFalse(
                bt_watchdog.service_reconnect(roles(), "call", state, now=100.0)
            )

        self.assertIn("controller_identity_mismatch", state.identity_error or "")
        self.assertEqual(state.reconnect_attempts, bt_watchdog.CALL_RECONNECT_ATTEMPTS)
        self.assertEqual(state.connect_collision_cancellations, 1)
        power_on.assert_not_called()
        connect.assert_not_called()


class ProbeAndUnitTests(unittest.TestCase):
    def test_production_pairing_state_is_closed_on_exact_adapter(self) -> None:
        with mock.patch.object(
            bt_watchdog.btadapters,
            "set_adapter_property",
            return_value=(True, "ok"),
        ) as setter:
            ok, detail = bt_watchdog._set_production_pairing_closed(BT500)
        self.assertTrue(ok, detail)
        self.assertEqual(
            setter.call_args_list,
            [
                mock.call(BT500, "Discoverable", False, cancelled=None),
                mock.call(BT500, "Pairable", False, cancelled=None),
            ],
        )

    def test_probe_targets_only_current_resolved_hci(self) -> None:
        result = subprocess.CompletedProcess([], 0, "HCI Version: 6.0\n", "")
        with mock.patch.object(
            bt_watchdog.subprocess, "run", return_value=result
        ) as run:
            self.assertIs(
                bt_watchdog.probe_controller(BT500),
                bt_watchdog.ProbeStatus.ANSWERED,
            )
        self.assertEqual(run.call_args.args[0], ["hciconfig", BT500.hci, "version"])

    def test_units_run_only_the_strict_call_implementation(self) -> None:
        units = Path(bt_watchdog.__file__).resolve().parents[1] / "systemd" / "system"
        template = (units / "bridge-btwatchdog@.service").read_text(encoding="utf-8")
        legacy = (units / "bridge-btwatchdog.service").read_text(encoding="utf-8")
        self.assertIn("bt_watchdog.py --role %i", template)
        self.assertIn("bt_watchdog.py --role call", legacy)
        for forbidden in (
            "bt-reset.sh",
            "systemctl restart bluetooth",
            "rfkill block bluetooth",
            "wireplumber",
        ):
            self.assertNotIn(forbidden, template.lower())
            self.assertNotIn(forbidden, legacy.lower())


class PairingRepairTests(unittest.TestCase):
    def test_pairing_seal_retries_a_transient_bluez_tree_change(self) -> None:
        changing = subprocess.CompletedProcess(
            [], 1, "", "ERROR: BlueZ state changed while the snapshot was being sealed"
        )
        stable = subprocess.CompletedProcess([], 0, "b\n", "")
        with (
            mock.patch.object(
                bt_watchdog.subprocess, "run", side_effect=[changing, stable]
            ) as run,
            mock.patch.object(bt_watchdog.time, "sleep") as sleep,
        ):
            ok, detail = bt_watchdog._pairing_seal()

        self.assertTrue(ok, detail)
        self.assertEqual(detail, "b")
        self.assertEqual(run.call_count, 2)
        sleep.assert_called_once_with(bt_watchdog.PAIRING_SEAL_RETRY_DELAY)

    def test_success_seals_before_delete_and_after_exact_pixel_pairing(self) -> None:
        state = bt_watchdog.RecoveryState(
            "call", repair_state="requested", repair_trigger="repeated-in-progress"
        )
        events: list[str] = []

        def timer(action: str) -> tuple[bool, str]:
            events.append(f"timer:{action}")
            return True, action

        def seal() -> tuple[bool, str]:
            events.append("seal")
            return True, "slot"

        def remove(address: str, resolved: btadapters.Adapter) -> tuple[bool, str]:
            events.append(f"remove:{address}")
            self.assertIs(resolved, BT500)
            return True, "removed"

        def window(*_args, **_kwargs) -> btadapters.PairingWindowResult:
            events.append("window")
            return btadapters.PairingWindowResult(True, "paired")

        with (
            mock.patch.object(bt_watchdog, "write_state"),
            mock.patch.object(bt_watchdog, "_pairing_timer", side_effect=timer),
            mock.patch.object(
                bt_watchdog, "_pairing_seal", side_effect=seal
            ) as sealing,
            mock.patch.object(
                bt_watchdog.btadapters,
                "managed_objects",
                return_value=phone_tree(BT500),
            ),
            mock.patch.object(bt_watchdog, "resolve_role", return_value=BT500),
            mock.patch.object(
                bt_watchdog.btadapters,
                "paired_addresses_on",
                return_value={PHONE_ADDRESS},
            ),
            mock.patch.object(
                bt_watchdog.btadapters, "remove_device", side_effect=remove
            ),
            mock.patch.object(
                bt_watchdog.btadapters, "incoming_pairing_window", side_effect=window
            ) as pairing,
        ):
            self.assertTrue(bt_watchdog.repair_phone_bond(roles(), state, BT500))

        self.assertEqual(
            events,
            [
                "timer:stop",
                "seal",
                f"remove:{PHONE_ADDRESS}",
                "window",
                "seal",
                "timer:start",
            ],
        )
        self.assertEqual(sealing.call_count, 2)
        pairing.assert_called_once_with(
            PHONE_ADDRESS,
            BT500,
            timeout=bt_watchdog.PAIRING_WINDOW_SECONDS,
            preexisting_paired=set(),
            cancelled=None,
            heartbeat=mock.ANY,
        )
        self.assertEqual(state.bond_state, "trusted")
        self.assertEqual(state.repair_state, "idle")
        self.assertFalse(state.pairing_timer_paused)
        self.assertEqual(state.last_action, "bond_sealed")

    def test_timeout_leaves_bondless_state_unsealed_for_reboot_rollback(self) -> None:
        state = bt_watchdog.RecoveryState(
            "call", repair_state="requested", repair_trigger="repeated-in-progress"
        )
        with (
            mock.patch.object(bt_watchdog, "write_state"),
            mock.patch.object(
                bt_watchdog, "_pairing_timer", return_value=(True, "stopped")
            ) as timer,
            mock.patch.object(
                bt_watchdog, "_pairing_seal", return_value=(True, "pre-repair")
            ) as sealing,
            mock.patch.object(
                bt_watchdog.btadapters,
                "managed_objects",
                return_value=phone_tree(BT500),
            ),
            mock.patch.object(bt_watchdog, "resolve_role", return_value=BT500),
            mock.patch.object(
                bt_watchdog.btadapters,
                "paired_addresses_on",
                return_value={PHONE_ADDRESS},
            ),
            mock.patch.object(
                bt_watchdog.btadapters, "remove_device", return_value=(True, "removed")
            ),
            mock.patch.object(
                bt_watchdog.btadapters,
                "incoming_pairing_window",
                return_value=btadapters.PairingWindowResult(False, "pairing timed out"),
            ),
        ):
            self.assertFalse(bt_watchdog.repair_phone_bond(roles(), state, BT500))

        sealing.assert_called_once_with()
        timer.assert_called_once_with("stop")
        self.assertTrue(state.pairing_timer_paused)
        self.assertEqual(state.bond_state, "missing")
        self.assertEqual(state.repair_state, "pairing_required")
        self.assertEqual(state.last_action, "pairing_required")
        self.assertEqual(state.last_error, "pairing timed out")

    def test_reopened_timeout_window_never_seals_bondless_live_state(self) -> None:
        state = bt_watchdog.RecoveryState(
            "call",
            bond_state="missing",
            repair_state="requested",
            repair_trigger="manual",
            pairing_timer_paused=True,
        )
        with (
            mock.patch.object(bt_watchdog, "write_state"),
            mock.patch.object(bt_watchdog, "_pairing_timer") as timer,
            mock.patch.object(bt_watchdog, "_pairing_seal") as sealing,
            mock.patch.object(
                bt_watchdog.btadapters, "managed_objects", return_value={}
            ),
            mock.patch.object(bt_watchdog, "resolve_role", return_value=BT500),
            mock.patch.object(
                bt_watchdog.btadapters, "paired_addresses_on", return_value=set()
            ),
            mock.patch.object(
                bt_watchdog.btadapters,
                "incoming_pairing_window",
                return_value=btadapters.PairingWindowResult(False, "still waiting"),
            ),
        ):
            self.assertFalse(bt_watchdog.repair_phone_bond(roles(), state, BT500))

        timer.assert_not_called()
        sealing.assert_not_called()
        self.assertTrue(state.pairing_timer_paused)
        self.assertEqual(state.repair_state, "pairing_required")


if __name__ == "__main__":
    unittest.main()
