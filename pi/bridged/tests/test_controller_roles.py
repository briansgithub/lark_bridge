from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import btadapters
import controller_roles

CALL_ADDRESS = "B0:82:E2:25:18:36"
OUTPUT_ADDRESS = "A0:AD:9F:73:6C:24"
PHONE_ADDRESS = "5C:33:7B:CB:BF:C5"


def document() -> dict:
    return {
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


def adapter(
    hci: str,
    address: str,
    product_id: str,
    *,
    usb_parent: str,
    product: str = "runtime label",
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
        product=product,
        manufacturer="ASUSTek Computer, Inc.",
    )


class ConfigTests(unittest.TestCase):
    def test_exact_target_roles_parse(self) -> None:
        roles = controller_roles.parse_controller_roles(document())
        self.assertEqual(roles.phone_address, PHONE_ADDRESS)
        self.assertEqual(roles.call.expected_usb_id, "0b05:1d70")
        self.assertEqual(roles.output.expected_usb_id, "0b05:1bf6")
        self.assertFalse(roles.transitional)

    def test_permanent_addresses_must_be_uppercase_and_distinct(self) -> None:
        lower = document()
        lower["devices"]["phone"]["adapter"] = CALL_ADDRESS.lower()
        with self.assertRaisesRegex(controller_roles.ControllerConfigError, "uppercase"):
            controller_roles.parse_controller_roles(lower)

        duplicate = document()
        duplicate["devices"]["output"]["adapter"] = CALL_ADDRESS
        with self.assertRaisesRegex(controller_roles.ControllerConfigError, "distinct"):
            controller_roles.parse_controller_roles(duplicate)

    def test_uart_transition_requires_explicit_opt_in_and_omitted_usb_ids(self) -> None:
        transitional = document()
        phone = transitional["devices"]["phone"]
        phone.update(
            {
                "adapter": "B8:27:EB:43:8D:51",
                "adapter_product": "Raspberry Pi 3B onboard BCM43438",
                "adapter_bus": "UART",
            }
        )
        del phone["adapter_usb_vendor_id"]
        del phone["adapter_usb_product_id"]
        with self.assertRaisesRegex(controller_roles.ControllerConfigError, "requires"):
            controller_roles.parse_controller_roles(transitional)

        transitional["bluetooth"]["allow_transitional_uart_call"] = True
        roles = controller_roles.parse_controller_roles(transitional)
        self.assertTrue(roles.transitional)
        self.assertIsNone(roles.call.expected_usb_id)

    def test_stale_uart_opt_in_is_rejected_for_usb_call_config(self) -> None:
        stale = document()
        stale["bluetooth"]["allow_transitional_uart_call"] = True
        with self.assertRaisesRegex(controller_roles.ControllerConfigError, "stale"):
            controller_roles.parse_controller_roles(stale)


class ResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.roles = controller_roles.parse_controller_roles(document())

    def test_hci_renumbering_reversed_order_and_port_move_do_not_change_roles(self) -> None:
        first = [
            adapter("hci2", CALL_ADDRESS, "1d70", usb_parent="1-1.4"),
            adapter("hci0", OUTPUT_ADDRESS, "1bf6", usb_parent="1-1.2"),
        ]
        reversed_and_moved = [
            adapter("hci1", OUTPUT_ADDRESS, "1bf6", usb_parent="1-1.4"),
            adapter("hci0", CALL_ADDRESS, "1d70", usb_parent="1-1.2"),
        ]
        first_result = controller_roles.resolve_controllers(self.roles, first)
        second_result = controller_roles.resolve_controllers(self.roles, reversed_and_moved)
        self.assertEqual(first_result["call"].address, CALL_ADDRESS)
        self.assertEqual(first_result["output"].address, OUTPUT_ADDRESS)
        self.assertEqual(second_result["call"].address, CALL_ADDRESS)
        self.assertEqual(second_result["output"].address, OUTPUT_ADDRESS)
        self.assertNotEqual(first_result["call"].hci, second_result["call"].hci)
        self.assertNotEqual(first_result["call"].usb_parent, second_result["call"].usb_parent)

    def test_missing_duplicate_wrong_bus_and_wrong_usb_id_are_typed(self) -> None:
        with self.assertRaises(controller_roles.ControllerMissingError):
            controller_roles.resolve_controller(self.roles.call, [])

        duplicate = [
            adapter("hci0", CALL_ADDRESS, "1d70", usb_parent="1-1.1"),
            adapter("hci3", CALL_ADDRESS, "1d70", usb_parent="1-1.4"),
        ]
        with self.assertRaises(controller_roles.ControllerDuplicateError):
            controller_roles.resolve_controller(self.roles.call, duplicate)

        wrong_bus = btadapters.Adapter("hci0", CALL_ADDRESS, "UART", 0)
        with self.assertRaises(controller_roles.ControllerIdentityMismatchError):
            controller_roles.resolve_controller(self.roles.call, [wrong_bus])

        wrong_id = adapter("hci0", CALL_ADDRESS, "1bf6", usb_parent="1-1.1")
        with self.assertRaises(controller_roles.ControllerIdentityMismatchError):
            controller_roles.resolve_controller(self.roles.call, [wrong_id])

    def test_product_is_diagnostic_not_trusted_identity(self) -> None:
        observed = adapter(
            "hci0", CALL_ADDRESS, "1d70", usb_parent="1-1.1", product="unexpected label"
        )
        self.assertIs(
            controller_roles.resolve_controller(self.roles.call, [observed]), observed
        )

    def test_status_is_additive_and_reports_diagnostics(self) -> None:
        inventory = [
            adapter("hci2", CALL_ADDRESS, "1d70", usb_parent="1-1.4"),
            adapter("hci0", OUTPUT_ADDRESS, "1bf6", usb_parent="1-1.2"),
        ]
        status = controller_roles.controllers_status(
            self.roles, inventory, policy=controller_roles.ReadinessPolicy.FINAL_USB
        )
        self.assertTrue(status["ready"])
        self.assertEqual(status["policy"], "final-usb")
        self.assertEqual(status["call"]["configured_address"], CALL_ADDRESS)
        self.assertEqual(status["call"]["expected_bus"], "USB")
        self.assertEqual(status["call"]["expected_usb_id"], "0b05:1d70")
        self.assertEqual(status["call"]["observed_usb_id"], "0b05:1d70")
        self.assertEqual(status["call"]["hci"], "hci2")
        self.assertEqual(status["call"]["driver"], "btusb")
        self.assertIsNone(status["call"]["error"])

    def test_final_readiness_rejects_valid_transitional_uart_role(self) -> None:
        transitional = document()
        phone = transitional["devices"]["phone"]
        phone.update(
            {
                "adapter": "B8:27:EB:43:8D:51",
                "adapter_product": "Raspberry Pi 3B onboard BCM43438",
                "adapter_bus": "UART",
            }
        )
        del phone["adapter_usb_vendor_id"]
        del phone["adapter_usb_product_id"]
        transitional["bluetooth"]["allow_transitional_uart_call"] = True
        roles = controller_roles.parse_controller_roles(transitional)
        inventory = [
            btadapters.Adapter("hci0", roles.call.address, "UART", 0),
            adapter("hci1", OUTPUT_ADDRESS, "1bf6", usb_parent="1-1.2"),
        ]
        accepted = controller_roles.controllers_status(
            roles, inventory, policy=controller_roles.ReadinessPolicy.TRANSITIONAL
        )
        rejected = controller_roles.controllers_status(
            roles, inventory, policy=controller_roles.ReadinessPolicy.FINAL_USB
        )
        self.assertTrue(accepted["ready"])
        self.assertTrue(accepted["transitional_uart_call"])
        self.assertFalse(rejected["ready"])
        self.assertIn("controller_policy_mismatch", rejected["call"]["error"])


class SysfsTests(unittest.TestCase):
    def test_usb_physical_diagnostics_are_derived_from_ancestors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            usb = Path(directory) / "usb1" / "1-1.4"
            # Windows rejects ':' in a fixture path; bInterfaceClass is the production
            # identity signal and exercises the same ancestor selection.
            interface = usb / "1-1.4-interface-1.0"
            hci = interface / "bluetooth" / "hci7"
            hci.mkdir(parents=True)
            (usb / "idVendor").write_text("0B05\n", encoding="utf-8")
            (usb / "idProduct").write_text("1D70\n", encoding="utf-8")
            (usb / "product").write_text("ASUS USB-BT600\n", encoding="utf-8")
            (usb / "manufacturer").write_text("ASUSTek\n", encoding="utf-8")
            (interface / "bInterfaceClass").write_text("e0\n", encoding="utf-8")
            (interface / "uevent").write_text("DRIVER=btusb\n", encoding="utf-8")

            identity = btadapters._sysfs_identity(hci)

        self.assertEqual(identity["bus"], "USB")
        self.assertEqual(identity["usb_vendor_id"], "0b05")
        self.assertEqual(identity["usb_product_id"], "1d70")
        self.assertEqual(identity["usb_parent"], "1-1.4")
        self.assertEqual(identity["usb_interface"], "1-1.4-interface-1.0")
        self.assertEqual(identity["driver"], "btusb")
        self.assertEqual(identity["product"], "ASUS USB-BT600")
        self.assertEqual(identity["manufacturer"], "ASUSTek")


class CommandLineTests(unittest.TestCase):
    def write_config(self, directory: str, value: dict | None = None) -> Path:
        source = value or document()
        phone = source["devices"]["phone"]
        output = source["devices"]["output"]
        bluetooth = source["bluetooth"]
        path = Path(directory) / "bridge.toml"
        path.write_text(
            "\n".join(
                (
                    "[devices.phone]",
                    f'address = "{phone["address"]}"',
                    f'adapter = "{phone["adapter"]}"',
                    f'adapter_product = "{phone["adapter_product"]}"',
                    f'adapter_bus = "{phone["adapter_bus"]}"',
                    *(
                        (
                            f'adapter_usb_vendor_id = "{phone["adapter_usb_vendor_id"]}"',
                            f'adapter_usb_product_id = "{phone["adapter_usb_product_id"]}"',
                        )
                        if phone["adapter_bus"] == "USB"
                        else ()
                    ),
                    "",
                    "[devices.output]",
                    f'adapter = "{output["adapter"]}"',
                    f'adapter_product = "{output["adapter_product"]}"',
                    f'adapter_bus = "{output["adapter_bus"]}"',
                    f'adapter_usb_vendor_id = "{output["adapter_usb_vendor_id"]}"',
                    f'adapter_usb_product_id = "{output["adapter_usb_product_id"]}"',
                    "",
                    "[bluetooth]",
                    "allow_transitional_uart_call = "
                    + str(bluetooth["allow_transitional_uart_call"]).lower(),
                    "",
                )
            ),
            encoding="utf-8",
        )
        return path

    def test_status_and_resolve_share_the_strict_final_inventory(self) -> None:
        inventory = [
            adapter("hci9", OUTPUT_ADDRESS, "1bf6", usb_parent="1-1.4"),
            adapter("hci2", CALL_ADDRESS, "1d70", usb_parent="1-1.2"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            config = self.write_config(directory)
            output = io.StringIO()
            with (
                mock.patch.object(controller_roles, "live_inventory", return_value=inventory),
                redirect_stdout(output),
            ):
                result = controller_roles.main(
                    ["--config", str(config), "--policy", "final-usb", "status"]
                )
            status = json.loads(output.getvalue())
            self.assertEqual(result, 0)
            self.assertTrue(status["ready"])
            self.assertEqual(status["call"]["hci"], "hci2")

            output = io.StringIO()
            with (
                mock.patch.object(controller_roles, "live_inventory", return_value=inventory),
                redirect_stdout(output),
            ):
                result = controller_roles.main(
                    [
                        "--config",
                        str(config),
                        "--policy",
                        "final-usb",
                        "resolve",
                        "output",
                        "--field",
                        "hci",
                    ]
                )
            self.assertEqual(result, 0)
            self.assertEqual(output.getvalue().strip(), "hci9")

    def test_final_cli_rejects_a_valid_uart_transition(self) -> None:
        transitional = document()
        phone = transitional["devices"]["phone"]
        phone.update(
            {
                "adapter": "B8:27:EB:43:8D:51",
                "adapter_product": "Raspberry Pi 3B onboard BCM43438",
                "adapter_bus": "UART",
            }
        )
        del phone["adapter_usb_vendor_id"]
        del phone["adapter_usb_product_id"]
        transitional["bluetooth"]["allow_transitional_uart_call"] = True
        inventory = [
            btadapters.Adapter("hci0", phone["adapter"], "UART", 0),
            adapter("hci1", OUTPUT_ADDRESS, "1bf6", usb_parent="1-1.2"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            config = self.write_config(directory, transitional)
            output = io.StringIO()
            with (
                mock.patch.object(controller_roles, "live_inventory", return_value=inventory),
                redirect_stdout(output),
            ):
                result = controller_roles.main(
                    ["--config", str(config), "--policy", "final-usb", "status"]
                )
        self.assertEqual(result, 1)
        status = json.loads(output.getvalue())
        self.assertFalse(status["ready"])
        self.assertIn("controller_policy_mismatch", status["call"]["error"])


if __name__ == "__main__":
    unittest.main()
