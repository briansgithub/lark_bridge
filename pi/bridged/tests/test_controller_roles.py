from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

import btadapters
import controller_roles

CALL_ADDRESS = "A0:AD:9F:73:6C:24"
OUTPUT_ADDRESS = "B0:82:E2:25:18:36"
PHONE_ADDRESS = "5C:33:7B:CB:BF:C5"


def wired_document() -> dict:
    return {
        "bridge": {"mode": "bluetooth-wired", "fallback_to_wired": True},
        "devices": {
            "phone": {
                "address": PHONE_ADDRESS,
                "adapter": CALL_ADDRESS,
                "adapter_product": "ASUS USB-BT500",
                "adapter_bus": "USB",
                "adapter_usb_vendor_id": "0b05",
                "adapter_usb_product_id": "1bf6",
            },
            "output": {"id": "wired:alsa_output.platform-test.stereo"},
        },
    }


def adapter(
    hci: str,
    address: str = CALL_ADDRESS,
    product_id: str = "1bf6",
    *,
    usb_parent: str = "1-1.2",
) -> btadapters.Adapter:
    return btadapters.Adapter(
        hci=hci,
        address=address,
        bus="USB",
        rfkill_index=int(hci.removeprefix("hci")),
        usb_vendor_id="0b05",
        usb_product_id=product_id,
        usb_parent=usb_parent,
        usb_interface=f"{usb_parent}:1.0",
        driver="btusb",
        product="runtime label",
        manufacturer="ASUSTek",
    )


class ConfigTests(unittest.TestCase):
    def test_wired_output_has_no_controller_role(self) -> None:
        roles = controller_roles.parse_controller_roles(wired_document())
        self.assertEqual(roles.phone_address, PHONE_ADDRESS)
        self.assertEqual(roles.call.address, CALL_ADDRESS)
        self.assertEqual(roles.call.expected_usb_id, "0b05:1bf6")
        self.assertIsNone(roles.output)

    def test_a2dp_output_requires_a_complete_distinct_usb_role(self) -> None:
        missing = wired_document()
        missing["devices"]["output"]["id"] = "a2dp:C9:5C:FD:6E:28:46"
        with self.assertRaisesRegex(controller_roles.ControllerConfigError, "requires"):
            controller_roles.parse_controller_roles(missing)

        configured = wired_document()
        configured["bridge"]["mode"] = "bluetooth"
        configured["devices"]["output"] = {
            "id": "a2dp:C9:5C:FD:6E:28:46",
            "adapter": OUTPUT_ADDRESS,
            "adapter_product": "ASUS USB-BT600",
            "adapter_bus": "USB",
            "adapter_usb_vendor_id": "0b05",
            "adapter_usb_product_id": "1d70",
        }
        roles = controller_roles.parse_controller_roles(configured)
        self.assertIsNotNone(roles.output)
        assert roles.output is not None
        self.assertEqual(roles.output.expected_usb_id, "0b05:1d70")

        configured["devices"]["output"]["adapter"] = CALL_ADDRESS
        with self.assertRaisesRegex(controller_roles.ControllerConfigError, "distinct"):
            controller_roles.parse_controller_roles(configured)

    def test_partial_output_identity_is_rejected_even_for_wired_mode(self) -> None:
        partial = wired_document()
        partial["devices"]["output"]["adapter"] = OUTPUT_ADDRESS
        with self.assertRaisesRegex(controller_roles.ControllerConfigError, "adapter_product"):
            controller_roles.parse_controller_roles(partial)

    def test_addresses_and_usb_ids_are_canonical(self) -> None:
        lower = wired_document()
        lower["devices"]["phone"]["adapter"] = CALL_ADDRESS.lower()
        with self.assertRaisesRegex(controller_roles.ControllerConfigError, "uppercase"):
            controller_roles.parse_controller_roles(lower)

        upper_usb = wired_document()
        upper_usb["devices"]["phone"]["adapter_usb_vendor_id"] = "0B05"
        with self.assertRaisesRegex(controller_roles.ControllerConfigError, "lowercase"):
            controller_roles.parse_controller_roles(upper_usb)


class ResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.roles = controller_roles.parse_controller_roles(wired_document())

    def test_hci_renumbering_and_usb_port_moves_do_not_change_identity(self) -> None:
        first = controller_roles.resolve_controllers(
            self.roles, [adapter("hci2", usb_parent="1-1.2")]
        )
        moved = controller_roles.resolve_controllers(
            self.roles, [adapter("hci0", usb_parent="1-1.4")]
        )
        self.assertEqual(first["call"].address, CALL_ADDRESS)
        self.assertEqual(moved["call"].address, CALL_ADDRESS)
        self.assertNotIn("output", first)
        self.assertNotEqual(first["call"].hci, moved["call"].hci)

    def test_missing_duplicate_wrong_bus_and_wrong_usb_id_fail_closed(self) -> None:
        with self.assertRaises(controller_roles.ControllerMissingError):
            controller_roles.resolve_controller(self.roles.call, [])

        with self.assertRaises(controller_roles.ControllerDuplicateError):
            controller_roles.resolve_controller(self.roles.call, [adapter("hci0"), adapter("hci3")])

        with self.assertRaises(controller_roles.ControllerIdentityMismatchError):
            controller_roles.resolve_controller(
                self.roles.call,
                [btadapters.Adapter("hci0", CALL_ADDRESS, "UART", 0)],
            )

        with self.assertRaises(controller_roles.ControllerIdentityMismatchError):
            controller_roles.resolve_controller(
                self.roles.call, [adapter("hci0", product_id="1d70")]
            )

    def test_wired_output_status_has_a_stable_ready_shape(self) -> None:
        status = controller_roles.controllers_status(
            self.roles,
            [adapter("hci7")],
            policy=controller_roles.ReadinessPolicy.FINAL_USB,
        )
        self.assertTrue(status["ready"])
        self.assertEqual(status["call"]["hci"], "hci7")
        self.assertEqual(status["call"]["sysfs_path"], "1-1.2")
        self.assertEqual(
            status["output"],
            {
                "required": False,
                "configured": False,
                "configured_address": None,
                "expected_bus": None,
                "expected_usb_id": None,
                "expected_product": None,
                "observed_address": None,
                "observed_bus": None,
                "observed_usb_id": None,
                "hci": None,
                "bluez_path": None,
                "sysfs_path": None,
                "usb_parent": None,
                "usb_interface": None,
                "driver": None,
                "product": None,
                "manufacturer": None,
                "rfkill_index": None,
                "ready": True,
                "reason": "wired-output",
                "error": None,
            },
        )

    def test_absent_output_role_is_typed_and_cli_never_guesses(self) -> None:
        with self.assertRaises(controller_roles.ControllerRoleNotConfiguredError) as caught:
            controller_roles.role_spec(self.roles, "output")
        self.assertEqual(caught.exception.code, "controller_role_not_configured")

        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "bridge.toml"
            config.write_text(
                """
[bridge]
mode = "bluetooth-wired"

[devices.phone]
address = "5C:33:7B:CB:BF:C5"
adapter = "A0:AD:9F:73:6C:24"
adapter_product = "ASUS USB-BT500"
adapter_bus = "USB"
adapter_usb_vendor_id = "0b05"
adapter_usb_product_id = "1bf6"

[devices.output]
id = "wired:alsa_output.platform-test.stereo"
""",
                encoding="utf-8",
            )
            stderr = io.StringIO()
            with (
                mock.patch.object(
                    controller_roles, "live_inventory", return_value=[adapter("hci9")]
                ),
                redirect_stderr(stderr),
            ):
                result = controller_roles.main(
                    ["--config", str(config), "resolve", "output", "--field", "hci"]
                )
        self.assertEqual(result, 1)
        self.assertIn("controller_role_not_configured", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
