from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import btadapters

SPEAKER = "C9:5C:FD:6E:28:46"
TARGET = btadapters.Adapter("hci1", "A0:AD:9F:73:6C:24", "USB", 1)


def device(trusted: bool) -> dict:
    return {"org.bluez.Device1": {"Trusted": {"data": trusted}}}


def path(hci: str) -> str:
    return f"/org/bluez/{hci}/dev_{SPEAKER.replace(':', '_')}"


class TrustPinTests(unittest.TestCase):
    def test_target_is_trusted_before_duplicates_are_untrusted(self) -> None:
        tree = {path("hci0"): device(True), path("hci1"): device(False)}
        calls: list[tuple[str, bool]] = []

        def set_trusted(
            _mac: str,
            trusted: bool,
            adapter: btadapters.Adapter | None = None,
            **_kwargs,
        ):
            assert adapter is not None
            calls.append((adapter.hci, trusted))
            return True, adapter.path

        with (
            mock.patch.object(btadapters, "managed_objects", return_value=tree),
            mock.patch.object(btadapters, "set_trusted", side_effect=set_trusted),
        ):
            result = btadapters.pin_to_adapter(SPEAKER, TARGET)

        self.assertTrue(result.ok)
        self.assertEqual(calls, [("hci1", True), ("hci0", False)])
        self.assertEqual(
            result.changed,
            ("hci1:trusted=True", "hci0:trusted=False"),
        )
        self.assertEqual(result.failures, ())

    def test_already_pinned_is_idempotent(self) -> None:
        tree = {path("hci0"): device(False), path("hci1"): device(True)}
        with (
            mock.patch.object(btadapters, "managed_objects", return_value=tree),
            mock.patch.object(btadapters, "set_trusted") as setter,
        ):
            result = btadapters.pin_to_adapter(SPEAKER, TARGET)
        self.assertTrue(result.ok)
        self.assertEqual(result.changed, ())
        setter.assert_not_called()

    def test_missing_target_bond_fails_without_untrusting_the_duplicate(self) -> None:
        tree = {path("hci0"): device(True)}
        with (
            mock.patch.object(btadapters, "managed_objects", return_value=tree),
            mock.patch.object(btadapters, "set_trusted") as setter,
        ):
            result = btadapters.pin_to_adapter(SPEAKER, TARGET)
        self.assertFalse(result.ok)
        self.assertIn("no bond", result.failures[0])
        setter.assert_not_called()


class IncomingPairingWindowTests(unittest.TestCase):
    @staticmethod
    def paired_tree(*addresses: str, trusted: bool = False) -> dict:
        return {
            btadapters.path_for(TARGET, address): {
                "org.bluez.Device1": {
                    "Address": {"data": address},
                    "Paired": {"data": True},
                    "Bonded": {"data": True},
                    "Trusted": {"data": trusted},
                }
            }
            for address in addresses
        }

    def test_adapter_property_write_is_exact_and_verified(self) -> None:
        tree = {TARGET.path: {"org.bluez.Adapter1": {"Pairable": {"data": True}}}}
        with (
            mock.patch.object(btadapters, "_run", return_value=(0, "", "")) as run,
            mock.patch.object(btadapters, "managed_objects", return_value=tree),
        ):
            ok, detail = btadapters.set_adapter_property(TARGET, "Pairable", True)

        self.assertTrue(ok, detail)
        command = run.call_args.args[0]
        self.assertIn(TARGET.path, command)
        self.assertEqual(command[-3:], ["Pairable", "b", "true"])

    def test_accepts_only_configured_phone_and_removes_new_unexpected_bond(
        self,
    ) -> None:
        unexpected = "00:11:22:33:44:55"
        children = []

        class Process:
            def poll(self):
                return None

        class Agent:
            def __init__(self, command):
                self.command = command
                self.process = Process()
                self.sent = []
                self.stopped = ()
                self.lines = iter(
                    (
                        (0.0, "Agent registered"),
                        (0.0, "Default agent request successful"),
                    )
                )
                children.append(self)

            def send(self, command):
                self.sent.append(command)

            def get(self, _timeout):
                return next(self.lines, None)

            def stop(self, *commands):
                self.stopped = commands

        observed = self.paired_tree(SPEAKER, unexpected)
        verified = self.paired_tree(SPEAKER, trusted=True)
        with (
            mock.patch.object(btadapters, "_LineProcess", Agent),
            mock.patch.object(
                btadapters, "set_adapter_property", return_value=(True, "ok")
            ) as adapter_property,
            mock.patch.object(
                btadapters, "managed_objects", side_effect=[observed, verified]
            ),
            mock.patch.object(
                btadapters, "remove_device", return_value=(True, "removed")
            ) as remove,
            mock.patch.object(
                btadapters, "set_trusted", return_value=(True, "trusted")
            ) as trust,
        ):
            result = btadapters.incoming_pairing_window(
                SPEAKER,
                TARGET,
                timeout=1.0,
                preexisting_paired=set(),
            )

        self.assertTrue(result.ok, result.detail)
        self.assertEqual(result.unexpected_removed, (unexpected,))
        remove.assert_called_once_with(unexpected, TARGET)
        trust.assert_called_once_with(SPEAKER, True, TARGET, cancelled=None)
        self.assertEqual(
            children[0].command, ["bluetoothctl", "--agent", "NoInputNoOutput"]
        )
        self.assertEqual(children[0].sent, ["default-agent"])
        self.assertEqual(children[0].stopped, ("agent off", "quit"))
        self.assertEqual(
            adapter_property.call_args_list,
            [
                mock.call(TARGET, "Pairable", True, cancelled=None),
                mock.call(TARGET, "Discoverable", True, cancelled=None),
                mock.call(TARGET, "Discoverable", False),
                mock.call(TARGET, "Pairable", False),
            ],
        )

    def test_timeout_always_closes_pairability_and_unregisters_agent(self) -> None:
        children = []

        class Process:
            def poll(self):
                return None

        class Agent:
            def __init__(self, _command):
                self.process = Process()
                self.stopped = ()
                self.lines = iter(
                    (
                        (0.0, "Agent registered"),
                        (0.0, "Default agent request successful"),
                    )
                )
                children.append(self)

            def send(self, _command):
                pass

            def get(self, _timeout):
                return next(self.lines, None)

            def stop(self, *commands):
                self.stopped = commands

        with (
            mock.patch.object(btadapters, "_LineProcess", Agent),
            mock.patch.object(
                btadapters, "set_adapter_property", return_value=(True, "ok")
            ) as adapter_property,
            mock.patch.object(btadapters, "managed_objects", return_value={}),
        ):
            result = btadapters.incoming_pairing_window(SPEAKER, TARGET, timeout=0.02)

        self.assertFalse(result.ok)
        self.assertIn("deadline", result.detail)
        self.assertEqual(children[0].stopped, ("agent off", "quit"))
        self.assertEqual(
            adapter_property.call_args_list[-2:],
            [
                mock.call(TARGET, "Discoverable", False),
                mock.call(TARGET, "Pairable", False),
            ],
        )

    def test_target_write_failure_preserves_the_trusted_duplicate(self) -> None:
        tree = {path("hci0"): device(True), path("hci1"): device(False)}
        with (
            mock.patch.object(btadapters, "managed_objects", return_value=tree),
            mock.patch.object(
                btadapters,
                "set_trusted",
                return_value=(False, "target D-Bus write failed"),
            ) as setter,
        ):
            result = btadapters.pin_to_adapter(SPEAKER, TARGET)
        self.assertFalse(result.ok)
        self.assertEqual(result.failures, ("target D-Bus write failed",))
        self.assertEqual(setter.call_count, 1)
        self.assertTrue(setter.call_args.args[1])

    def test_duplicate_write_failure_is_reported(self) -> None:
        tree = {path("hci0"): device(True), path("hci1"): device(True)}
        with (
            mock.patch.object(btadapters, "managed_objects", return_value=tree),
            mock.patch.object(
                btadapters,
                "set_trusted",
                return_value=(False, "duplicate D-Bus write failed"),
            ),
        ):
            result = btadapters.pin_to_adapter(SPEAKER, TARGET)
        self.assertFalse(result.ok)
        self.assertEqual(result.failures, ("duplicate D-Bus write failed",))


class PowerTests(unittest.TestCase):
    def test_stale_soft_block_is_cleared_before_powering(self) -> None:
        with (
            mock.patch.object(btadapters, "is_blocked", return_value=True),
            mock.patch.object(btadapters, "unblock", return_value=True) as unblock,
            mock.patch.object(btadapters, "is_powered", side_effect=[False, True]),
            mock.patch.object(
                btadapters, "_run", return_value=(1, "", "transition reply")
            ) as run,
        ):
            ok, _detail = btadapters.power_on(TARGET)
        self.assertTrue(
            ok, "verified Powered=true owns the outcome, not busctl's reply"
        )
        unblock.assert_called_once_with(TARGET, cancelled=None)
        self.assertIn(TARGET.path, run.call_args.args[0])

    def test_unblock_failure_prevents_a_power_write(self) -> None:
        with (
            mock.patch.object(btadapters, "is_blocked", return_value=True),
            mock.patch.object(btadapters, "unblock", return_value=False),
            mock.patch.object(btadapters, "_run") as run,
        ):
            ok, detail = btadapters.power_on(TARGET)
        self.assertFalse(ok)
        self.assertIn("rfkill", detail)
        run.assert_not_called()

    def test_already_powered_is_idempotent(self) -> None:
        with (
            mock.patch.object(btadapters, "is_blocked", return_value=False),
            mock.patch.object(btadapters, "is_powered", return_value=True),
            mock.patch.object(btadapters, "_run") as run,
        ):
            ok, detail = btadapters.power_on(TARGET)
        self.assertTrue(ok)
        self.assertIn("already powered", detail)
        run.assert_not_called()

    def test_shutdown_is_checked_before_each_power_mutation(self) -> None:
        checks = iter((False, True))
        with (
            mock.patch.object(btadapters, "is_blocked", return_value=True),
            mock.patch.object(
                btadapters, "_run", return_value=(1, "", "failed")
            ) as run,
            mock.patch.object(Path, "write_text") as write_text,
            self.assertRaises(btadapters.BluetoothOperationCancelled),
        ):
            btadapters.unblock(TARGET, cancelled=lambda: next(checks))
        run.assert_called_once_with(["rfkill", "unblock", str(TARGET.rfkill_index)])
        write_text.assert_not_called()


class DiscoveryTests(unittest.TestCase):
    def test_usb_identity_comes_from_stable_device_metadata_not_port_number(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            device = root / "1-1.4"
            # A colon is illegal in a Windows temporary filename.  bInterfaceClass is the
            # kernel signal used by the parser; the production sysfs name remains 1-1.4:1.0.
            interface = device / "1-1.4-interface-1.0"
            hci = interface / "bluetooth" / "hci7"
            hci.mkdir(parents=True)
            (device / "idVendor").write_text("0b05\n", encoding="utf-8")
            (device / "idProduct").write_text("1bf6\n", encoding="utf-8")
            (device / "product").write_text("ASUS USB-BT500\n", encoding="utf-8")
            (interface / "bInterfaceClass").write_text("e0\n", encoding="utf-8")
            (interface / "uevent").write_text("DRIVER=btusb\n", encoding="utf-8")

            identity = btadapters._sysfs_identity(hci)

        self.assertEqual(identity["bus"], "USB")
        self.assertEqual(identity["usb_vendor_id"], "0b05")
        self.assertEqual(identity["usb_product_id"], "1bf6")
        self.assertEqual(identity["usb_parent"], "1-1.4")
        self.assertEqual(identity["usb_interface"], "1-1.4-interface-1.0")
        self.assertEqual(identity["driver"], "btusb")

    def test_monitor_parser_keeps_only_exact_adapter_window_and_strongest_rssi(
        self,
    ) -> None:
        accumulator = btadapters.DiscoveryAccumulator(TARGET.path, 10.0, 22.0)
        target = f'{TARGET.path}/dev_{SPEAKER.replace(":", "_")}'
        event = lambda path_value, rssi: (
            f'{{"path":"{path_value}","member":"PropertiesChanged",'
            f'"RSSI":{{"data":{rssi}}}}}'
        )

        accumulator.add(10.0, event(target, -20))  # exactly at start is pre-window
        accumulator.add(11.0, event(target, -72))
        accumulator.add(12.0, event(target, -48))
        accumulator.add(13.0, event(target, -60))
        accumulator.add(14.0, event(target.replace("hci1", "hci0"), -10))
        accumulator.add(15.0, f'{{"path":"{target}","Connected":{{"data":true}}}}')
        accumulator.add(22.0, event(target, -5))  # deadline is exclusive

        self.assertEqual(accumulator.observations, {SPEAKER: -48})

    def test_cancelled_scan_always_stops_owned_discovery_and_monitor(self) -> None:
        instances = []

        class Process:
            def poll(self):
                return None

        class Child:
            def __init__(self, command):
                self.command = command
                self.process = Process()
                self.sent = []
                self.stopped = ()
                self.responses = (
                    []
                    if command[0] == "busctl"
                    else [
                        (1.0, f"Controller {TARGET.address} speaker [default]"),
                        (2.0, "Discovery started"),
                    ]
                )
                instances.append(self)

            def send(self, command):
                self.sent.append(command)

            def get(self, _timeout):
                return self.responses.pop(0) if self.responses else None

            def stop(self, *commands):
                self.stopped = commands

        checks = 0

        def cancelled():
            nonlocal checks
            checks += 1
            return checks >= 3

        with (
            mock.patch.object(btadapters, "_LineProcess", Child),
            self.assertRaises(btadapters.BluetoothOperationCancelled),
        ):
            btadapters.discover_bredr(TARGET, cancelled=cancelled)

        monitor, owner = instances
        self.assertEqual(
            monitor.command,
            [
                "sudo",
                "-n",
                "busctl",
                "--system",
                "--json=short",
                "monitor",
                btadapters.BLUEZ,
            ],
        )
        self.assertEqual(owner.sent[:2], [f"select {TARGET.address}", "scan bredr"])
        self.assertEqual(owner.stopped, ("scan off", "quit"))
        self.assertEqual(monitor.stopped, ())


class ExplicitDeviceOperationTests(unittest.TestCase):
    def test_precancelled_command_is_never_spawned(self) -> None:
        with (
            mock.patch.object(btadapters.subprocess, "Popen") as spawn,
            self.assertRaises(btadapters.BluetoothOperationCancelled),
        ):
            btadapters._run_cancellable(
                ["busctl", "mutating-command"], timeout=1, cancelled=lambda: True
            )
        spawn.assert_not_called()

    def test_connect_classifies_in_progress_and_preserves_explicit_path(self) -> None:
        cases = (
            (
                "Call failed: org.bluez.Error.InProgress: Operation already in progress",
                True,
            ),
            ("Call failed: In Progress", True),
            (
                "Call failed: org.bluez.Error.Failed: Connection timed out",
                False,
            ),
        )
        for error, expected in cases:
            with (
                self.subTest(error=error),
                mock.patch.object(
                    btadapters, "_run", return_value=(1, "", error)
                ) as run,
            ):
                ok, detail = btadapters.connect(SPEAKER, TARGET)

            self.assertFalse(ok)
            self.assertIs(btadapters.connect_in_progress(ok, detail), expected)
            self.assertIn(btadapters.path_for(TARGET, SPEAKER), detail)
            self.assertIn(btadapters.path_for(TARGET, SPEAKER), run.call_args.args[0])

    def test_a2dp_connect_profile_uses_only_explicit_device_path(self) -> None:
        with mock.patch.object(btadapters, "_run", return_value=(0, "", "")) as run:
            ok, detail = btadapters.connect_profile(SPEAKER, TARGET)
        self.assertTrue(ok)
        self.assertEqual(detail, btadapters.path_for(TARGET, SPEAKER))
        command = run.call_args.args[0]
        self.assertIn(btadapters.path_for(TARGET, SPEAKER), command)
        self.assertIn("ConnectProfile", command)
        self.assertIn(btadapters.A2DP_SINK_UUID, command)
        self.assertNotIn("Connect", command)

    def test_blocking_radio_lock_can_be_cancelled_while_waiting(self) -> None:
        btadapters._host_radio_lock.acquire()
        try:
            with (
                mock.patch.object(btadapters, "fcntl", None),
                self.assertRaises(btadapters.BluetoothOperationCancelled),
                btadapters.speaker_radio_lock(cancelled=lambda: True),
            ):
                self.fail("cancelled lock acquisition must not enter the transaction")
        finally:
            btadapters._host_radio_lock.release()

    def test_pair_uses_temporary_noinput_agent_and_reaps_it(self) -> None:
        children = []
        test_case = self

        class Process:
            def poll(self):
                return None

        class Agent:
            def __init__(self, command):
                self.command = command
                self.process = Process()
                self.sent = []
                self.stopped = ()
                self.registration_observed = False
                children.append(self)

            def send(self, command):
                if command == "default-agent":
                    test_case.assertTrue(self.registration_observed)
                self.sent.append(command)

            def get(self, _timeout):
                if not self.registration_observed:
                    self.registration_observed = True
                    return (0.0, "Agent registered")
                return (0.0, "Default agent request successful")

            def drain(self):
                return []

            def stop(self, *commands):
                self.stopped = commands

        with (
            mock.patch.object(btadapters, "_LineProcess", Agent),
            mock.patch.object(
                btadapters, "_run_cancellable", return_value=(0, "", "")
            ) as run,
            mock.patch.object(btadapters, "paired_on", return_value=True),
        ):
            result = btadapters.pair_device(SPEAKER, TARGET)

        self.assertTrue(result.ok)
        agent = children[0]
        self.assertEqual(
            agent.command,
            ["sudo", "-n", "bluetoothctl", "--agent", "NoInputNoOutput"],
        )
        self.assertEqual(agent.sent, ["default-agent"])
        self.assertNotIn("agent NoInputNoOutput", agent.sent)
        self.assertEqual(agent.stopped, ("agent off", "quit"))
        self.assertEqual(run.call_args.args[0][-2:], ["org.bluez.Device1", "Pair"])
        self.assertIn(btadapters.path_for(TARGET, SPEAKER), run.call_args.args[0])

    def test_pin_prompt_is_declined_without_starting_pair_command(self) -> None:
        class Process:
            def poll(self):
                return None

        class Agent:
            def __init__(self, _command):
                self.process = Process()

            def send(self, _command):
                pass

            def get(self, _timeout):
                return (0.0, "Enter PIN code")

            def drain(self):
                return []

            def stop(self, *_commands):
                pass

        with (
            mock.patch.object(btadapters, "_LineProcess", Agent),
            mock.patch.object(btadapters, "_run_cancellable") as run,
            mock.patch.object(btadapters, "cancel_pairing") as cancel,
        ):
            result = btadapters.pair_device(SPEAKER, TARGET)
        self.assertFalse(result.ok)
        self.assertTrue(result.pin_requested)
        run.assert_not_called()
        cancel.assert_called_once_with(SPEAKER, TARGET)

    def test_pin_prompt_during_pair_is_not_misreported_as_disconnect(self) -> None:
        active_agent = None

        class Process:
            def poll(self):
                return None

        class Agent:
            def __init__(self, _command):
                nonlocal active_agent
                active_agent = self
                self.process = Process()
                self.registration_observed = False

            def send(self, _command):
                pass

            def get(self, _timeout):
                if not self.registration_observed:
                    self.registration_observed = True
                    return (0.0, "Agent registered")
                return (0.0, "Default agent request successful")

            def drain(self):
                return [(0.0, "Confirm passkey 123456")]

            def stop(self, *_commands):
                pass

        def run(_command, **kwargs):
            self.assertTrue(kwargs["cancelled"]())
            raise btadapters.BluetoothOperationCancelled("cancelled")

        with (
            mock.patch.object(btadapters, "_LineProcess", Agent),
            mock.patch.object(btadapters, "_run_cancellable", side_effect=run),
            mock.patch.object(btadapters, "cancel_pairing") as cancel,
        ):
            result = btadapters.pair_device(SPEAKER, TARGET)
        self.assertIsNotNone(active_agent)
        self.assertFalse(result.ok)
        self.assertTrue(result.pin_requested)
        cancel.assert_called_once_with(SPEAKER, TARGET)


if __name__ == "__main__":
    unittest.main()
