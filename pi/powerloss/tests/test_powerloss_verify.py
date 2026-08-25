from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

POWERLOSS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POWERLOSS))
SPEC = importlib.util.spec_from_file_location(
    "powerloss_verify_tested", POWERLOSS / "powerloss_verify.py"
)
assert SPEC is not None and SPEC.loader is not None
powerloss_verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(powerloss_verify)


def config_text(*, uart: bool = False) -> str:
    if uart:
        call = '''adapter = "B8:27:EB:43:8D:51"
adapter_product = "Raspberry Pi 3B onboard BCM43438"
adapter_bus = "UART"'''
    else:
        call = '''adapter = "B0:82:E2:25:18:36"
adapter_product = "ASUS USB-BT600"
adapter_bus = "USB"
adapter_usb_vendor_id = "0b05"
adapter_usb_product_id = "1d70"'''
    return f"""[devices.phone]
address = "5C:33:7B:CB:BF:C5"
{call}

[devices.output]
adapter = "A0:AD:9F:73:6C:24"
adapter_product = "ASUS USB-BT500"
adapter_bus = "USB"
adapter_usb_vendor_id = "0b05"
adapter_usb_product_id = "1bf6"

[bluetooth]
allow_transitional_uart_call = {str(uart).lower()}
"""


def usb_adapter(hci: str, address: str, product_id: str):
    return powerloss_verify.btadapters.Adapter(
        hci,
        address,
        "USB",
        int(hci.removeprefix("hci")),
        usb_vendor_id="0b05",
        usb_product_id=product_id,
        usb_parent="1-1.2",
        usb_interface="1-1.2:1.0",
        driver="btusb",
    )


class FinalControllerReadinessTests(unittest.TestCase):
    def test_reversed_enumeration_is_final_ready(self) -> None:
        inventory = [
            usb_adapter("hci9", "A0:AD:9F:73:6C:24", "1bf6"),
            usb_adapter("hci2", "B0:82:E2:25:18:36", "1d70"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "bridge.toml"
            config.write_text(config_text(), encoding="utf-8")
            status = powerloss_verify.final_controller_status(config, inventory)
        self.assertEqual(powerloss_verify.controller_failures(status), [])
        self.assertEqual(status["call"]["hci"], "hci2")
        self.assertEqual(status["output"]["hci"], "hci9")

    def test_explicit_uart_opt_in_never_passes_powerloss_readiness(self) -> None:
        inventory = [
            powerloss_verify.btadapters.Adapter(
                "hci0", "B8:27:EB:43:8D:51", "UART", 0
            ),
            usb_adapter("hci1", "A0:AD:9F:73:6C:24", "1bf6"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "bridge.toml"
            config.write_text(config_text(uart=True), encoding="utf-8")
            status = powerloss_verify.final_controller_status(config, inventory)
        failures = powerloss_verify.controller_failures(status)
        self.assertFalse(status["ready"])
        self.assertTrue(any("transitional UART" in failure for failure in failures))
        self.assertTrue(any("call controller" in failure for failure in failures))

    def test_published_runtime_controller_status_must_match_fresh_resolution(self) -> None:
        final = {
            "call": {
                "configured_address": "B0:82:E2:25:18:36",
                "observed_address": "B0:82:E2:25:18:36",
                "observed_usb_id": "0b05:1d70",
                "hci": "hci2",
            },
            "output": {
                "configured_address": "A0:AD:9F:73:6C:24",
                "observed_address": "A0:AD:9F:73:6C:24",
                "observed_usb_id": "0b05:1bf6",
                "hci": "hci9",
            },
        }
        published = {
            "ready": True,
            "transitional_uart_call": False,
            "call": dict(final["call"]),
            "output": dict(final["output"]),
        }
        self.assertEqual(
            powerloss_verify.published_controller_failures(published, final), []
        )
        published["call"]["hci"] = "hci0"
        self.assertIn(
            "bridge call controller hci is stale or mismatched",
            powerloss_verify.published_controller_failures(published, final),
        )

    def test_required_units_are_the_two_role_instances(self) -> None:
        self.assertIn("bridge-btwatchdog@call.service", powerloss_verify.SYSTEM_UNITS)
        self.assertIn("bridge-btwatchdog@output.service", powerloss_verify.SYSTEM_UNITS)
        self.assertNotIn("bridge-btwatchdog.service", powerloss_verify.SYSTEM_UNITS)


class RestartReadinessTests(unittest.TestCase):
    def test_clean_boot_requires_zero_restart_records(self) -> None:
        self.assertEqual(powerloss_verify.service_restart_failures("normal boot"), (0, []))

        count, failures = powerloss_verify.service_restart_failures(
            "Scheduled restart job\nservice recovered"
        )
        self.assertEqual(count, 1)
        self.assertEqual(
            failures, ["current boot contains 1 service restart record(s)"]
        )

    def test_multiple_restart_records_do_not_pass_as_bounded(self) -> None:
        count, failures = powerloss_verify.service_restart_failures(
            "Scheduled restart job\nScheduled restart job"
        )
        self.assertEqual(count, 2)
        self.assertTrue(failures)


if __name__ == "__main__":
    unittest.main()
