from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


class DeploymentRoleTests(unittest.TestCase):
    def text(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_transactional_install_replaces_singleton_with_both_instances(self) -> None:
        source = self.text("scripts/install.sh")
        self.assertIn('"pi/systemd/system/bridge-btwatchdog@.service"', source)
        self.assertIn('bridge-btwatchdog@call.service', source)
        self.assertIn('bridge-btwatchdog@output.service', source)
        self.assertIn('systemctl disable --now bridge-btwatchdog.service', source)
        self.assertIn("active=inactive", source)
        self.assertIn("printf '%s\\t%s\\t%s\\n'", source)

    def test_rollback_accepts_old_manifests_and_restores_new_runtime_state(self) -> None:
        source = self.text("pi/scripts/boot-transaction.sh")
        self.assertIn("read -r unit enabled active", source)
        self.assertIn("bridge-btwatchdog.service", source)
        self.assertIn("bridge-btwatchdog@call.service", source)
        self.assertIn("bridge-btwatchdog@output.service", source)
        self.assertIn('if [ "$active" = "active" ]', source)
        self.assertIn('elif [ "$active" = "inactive" ]', source)

    def test_install_verification_is_visibly_transitional_not_official(self) -> None:
        source = self.text("scripts/bootstrap/70-verify.sh")
        self.assertIn("--policy transitional status", source)
        self.assertIn("explicitly transitional install policy", source)
        self.assertIn("bridge-btwatchdog@call.service", source)
        self.assertIn("bridge-btwatchdog@output.service", source)
        self.assertIn('ok "$unit active"', source)
        self.assertIn("transitional singleton watchdog inactive", source)

    def test_early_units_order_before_both_watchdogs(self) -> None:
        for relative in (
            "pi/systemd/system/bridge-tuning.service",
            "pi/systemd/system/bridge-storage-guard.service",
        ):
            with self.subTest(relative=relative):
                source = self.text(relative)
                self.assertIn("bridge-btwatchdog@call.service", source)
                self.assertIn("bridge-btwatchdog@output.service", source)

    def test_remote_boot_deployer_stages_every_new_install_dependency(self) -> None:
        source = self.text("rig/boot/apply_variant.py")
        for relative in (
            "pi/systemd/system/bridge-btwatchdog@.service",
            "pi/systemd/system/bridge-btwatchdog.service",
            "pi/rpi/swap.conf.d/90-larkbridge-pure-zram.conf",
            "pi/bridged/controller_roles.py",
            "pi/bridged/btadapters.py",
        ):
            with self.subTest(relative=relative):
                self.assertIn(f'"{relative}"', source)

    def test_active_e17_tools_use_final_roles_without_hci_overrides_or_bond_inference(self) -> None:
        for relative in (
            "rig/pi/measure/a2dp-survival.sh",
            "rig/pi/measure/a2dp-dropouts.sh",
            "rig/pi/soak/sco-sampler.sh",
            "rig/pi/soak/soak-monitor.sh",
            "rig/tests/unit/U20.sh",
            "rig/tests/unit/U21.sh",
        ):
            with self.subTest(relative=relative):
                source = self.text(relative)
                self.assertIn("--policy final-usb", source)
                self.assertNotIn("adapter_for_device", source)
                self.assertNotIn('bluetoothctl show', source)

    def test_e16_capture_uses_the_strict_output_role_during_transition(self) -> None:
        source = self.text("rig/e16_live_capture.py")
        self.assertIn("--policy transitional resolve output", source)
        self.assertIn("bridge-btwatchdog@call.service", source)
        self.assertIn("bridge-btwatchdog@output.service", source)
        self.assertNotIn("/org/bluez/hci0", source)
        self.assertNotIn("/org/bluez/hci1", source)

    def test_remaining_global_reset_is_clearly_manual_onboard_rollback_only(self) -> None:
        source = self.text("scripts/bt-reset.sh")
        self.assertIn("MANUAL ONBOARD-ROLLBACK ONLY", source)
        self.assertIn("those actions are not", source)
        self.assertIn("permitted in automatic dual-controller recovery", source)


if __name__ == "__main__":
    unittest.main()
