from __future__ import annotations

import unittest
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

        def set_trusted(_mac: str, trusted: bool, adapter: btadapters.Adapter | None = None):
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
        self.assertTrue(ok, "verified Powered=true owns the outcome, not busctl's reply")
        unblock.assert_called_once_with(TARGET)
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


if __name__ == "__main__":
    unittest.main()
