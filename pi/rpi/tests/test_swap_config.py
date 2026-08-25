from __future__ import annotations

import configparser
import io
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_RELATIVE = Path("pi/rpi/swap.conf.d/90-larkbridge-pure-zram.conf")
CONFIG_TARGET = "/etc/rpi/swap.conf.d/90-larkbridge-pure-zram.conf"


class PureZramConfigTests(unittest.TestCase):
    def test_config_is_exactly_pure_zram(self) -> None:
        text = (REPO_ROOT / CONFIG_RELATIVE).read_text(encoding="utf-8")
        self.assertEqual("[Main]\nMechanism=zram\n", text)

        config = configparser.ConfigParser()
        config.read_file(io.StringIO(text))
        self.assertEqual(["Main"], config.sections())
        self.assertEqual({"mechanism": "zram"}, dict(config["Main"]))
        self.assertNotIn("writeback", text.lower())

    def test_reversible_installer_manages_the_drop_in(self) -> None:
        install = (REPO_ROOT / "scripts/install.sh").read_text(encoding="utf-8")
        install_call = (
            f'install_managed "{CONFIG_RELATIVE.as_posix()}" '
            f'"{CONFIG_TARGET}" 0644'
        )
        self.assertIn(install_call, install)

        rollback = (REPO_ROOT / "pi/scripts/boot-transaction.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn(f"        {CONFIG_TARGET}|\\\n", rollback)

    def test_change_does_not_weaken_readiness_or_reconfigure_live_swap(self) -> None:
        install = (REPO_ROOT / "scripts/install.sh").read_text(encoding="utf-8")
        self.assertNotRegex(install, r"systemctl\s+(restart|start|stop).*rpi-(swap|zram)")
        self.assertNotIn("systemd-remount-fs.service", install)


if __name__ == "__main__":
    unittest.main()
