from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import unittest


SUPPORT_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("bt600_build", SUPPORT_ROOT / "build_package.py")
assert SPEC is not None and SPEC.loader is not None
BUILD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILD)


class PackageContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = json.loads((SUPPORT_ROOT / "sources.json").read_text(encoding="utf-8"))

    def test_exact_kernel_and_toolchain_pins(self) -> None:
        self.assertEqual("6.18.34+rpt-rpi-v8", self.manifest["kernel"]["release"])
        self.assertEqual("linux_6.18.34-1+rpt1", self.manifest["kernel"]["source_package"])
        self.assertEqual(
            "c8c7494100e99ee05b11aaa4f0588a223a63d1af",
            self.manifest["kernel"]["source_commit"],
        )
        self.assertEqual("14.2.0-19", self.manifest["build"]["gcc_package_version"])
        self.assertEqual("2.44", self.manifest["build"]["binutils_version"])

    def test_changelog_stanza_includes_blank_line_separated_bullets(self) -> None:
        changelog = (
            "linux (1:6.18.34-1+rpt1) trixie; urgency=medium\n\n"
            "  * Raspberry Pi OS release\n"
            "  * Linux commit: c8c7494100e99ee05b11aaa4f0588a223a63d1af\n\n"
            " -- Maintainer <root@localhost>  Tue, 09 Jun 2026 14:55:28 +0100\n\n"
            "linux (1:6.18.33-1+rpt1) trixie; urgency=medium\n"
        )
        stanza = BUILD.first_debian_changelog_stanza(changelog)
        self.assertIn("Linux commit: c8c7494", stanza)
        self.assertNotIn("6.18.33", stanza)

    def test_labelled_dpkg_control_fields_are_parsed(self) -> None:
        output = (
            "Package: larkbridge-bt600-support\n"
            "Version: 1.0.0-1\n"
            "Architecture: arm64\n"
            "Depends: linux-image-6.18.34+rpt-rpi-v8 (= 1:6.18.34-1+rpt1)\n"
        )
        self.assertEqual(
            {
                "Package": "larkbridge-bt600-support",
                "Version": "1.0.0-1",
                "Architecture": "arm64",
                "Depends": "linux-image-6.18.34+rpt-rpi-v8 (= 1:6.18.34-1+rpt1)",
            },
            BUILD.parse_control_field_lines(output),
        )

    def test_exact_package_dependency(self) -> None:
        control = BUILD.package_control(self.manifest)
        self.assertIn(
            "Depends: linux-image-6.18.34+rpt-rpi-v8 (= 1:6.18.34-1+rpt1)\n",
            control,
        )
        self.assertNotIn("initramfs", control.lower())
        self.assertEqual("0b05:1d70", self.manifest["package"]["bt600_usb_id"])
        self.assertTrue(
            self.manifest["package"]["bt600_modalias_probe"].startswith("usb:v0B05p1D70")
        )

    def test_maintainer_scripts_are_depmod_only(self) -> None:
        expected = "#!/bin/sh\nset -e\ndepmod -a 6.18.34+rpt-rpi-v8\n"
        self.assertEqual(expected, BUILD.maintainer_script("6.18.34+rpt-rpi-v8"))

    def test_depmod_overrides_are_exact_release_only(self) -> None:
        lines = BUILD.depmod_config("6.18.34+rpt-rpi-v8").splitlines()
        self.assertEqual(
            [
                "override btrtl 6.18.34+rpt-rpi-v8 updates/larkbridge",
                "override btusb 6.18.34+rpt-rpi-v8 updates/larkbridge",
            ],
            [line for line in lines if not line.startswith("#")],
        )

    def test_firmware_set_and_hashes_are_exact(self) -> None:
        firmware = {item["name"]: item for item in self.manifest["firmware"]["files"]}
        self.assertEqual({"rtl8761cu_fw.bin", "rtl8761cu_config.bin"}, set(firmware))
        self.assertEqual(
            "525bfa5ab9a3ed74b788f9ac494467c522a436c1bc5290ebcb05c7f920533c09",
            firmware["rtl8761cu_fw.bin"]["sha256"],
        )
        self.assertEqual(
            "3142c2732470e4edcdb68edf58cc505b581e38d56b3ec09c64eddcb1d5099507",
            firmware["rtl8761cu_config.bin"]["sha256"],
        )
        self.assertTrue(firmware["rtl8761cu_fw.bin"]["required_for_initialization"])
        self.assertFalse(firmware["rtl8761cu_config.bin"]["required_for_initialization"])

    def test_btusb_patch_adds_only_the_requested_entry(self) -> None:
        patch = (SUPPORT_ROOT / "patches/0002-btusb-add-asus-usb-bt600.patch").read_text(
            encoding="utf-8"
        )
        added_code = [
            line[1:]
            for line in patch.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        ]
        self.assertEqual(
            [
                "\t{ USB_DEVICE(0x0b05, 0x1d70), .driver_info = BTUSB_REALTEK |",
                "\t\t\t\t\t\t     BTUSB_WIDEBAND_SPEECH },",
            ],
            added_code,
        )
        self.assertIn("@@ -800,6 +800,8 @@", patch)

    def test_btrtl_patch_has_complete_upstream_semantics(self) -> None:
        patch = (SUPPORT_ROOT / "patches/0001-btrtl-add-rtl8761cuv-support.patch").read_text(
            encoding="utf-8"
        )
        for token in (
            "+\tCHIP_ID_8761C = 51,",
            "+\t  .config_needed = false,",
            "+\t  .fw_name  = \"rtl_bt/rtl8761cu_fw\",",
            "+\t  .cfg_name = \"rtl_bt/rtl8761cu_config\",",
            "+\tcase CHIP_ID_8761C:",
            "+MODULE_FIRMWARE(\"rtl_bt/rtl8761cu_fw.bin\");",
            "+MODULE_FIRMWARE(\"rtl_bt/rtl8761cu_config.bin\");",
        ):
            self.assertIn(token, patch)

    def test_container_is_arm64_and_snapshot_pinned(self) -> None:
        dockerfile = (SUPPORT_ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("FROM --platform=linux/arm64 debian@sha256:c94f5ddd", dockerfile)
        self.assertIn("archive/debian/20260803T000000Z", dockerfile)
        self.assertIn("gcc-14=14.2.0-19", dockerfile)
        self.assertIn("binutils=2.44-3", dockerfile)
        self.assertIn("python3=3.13.5-1", dockerfile)


if __name__ == "__main__":
    unittest.main()
