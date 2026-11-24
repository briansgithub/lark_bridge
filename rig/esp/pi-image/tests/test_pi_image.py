"""Rig Pi image tests: provision.sh against a temp sysroot, plus the helpers.

Any Linux, no root, no hardware, nothing outside temp directories.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import conf_edit  # noqa: E402
import fake_car_status  # noqa: E402

CONFIG_TXT = """\
# For more options and information see http://rptl.io/configtxt
dtparam=audio=on
camera_auto_detect=1

[cm4]
otg_mode=1

[all]
"""
CMDLINE = ("console=serial0,115200 console=tty1 root=PARTUUID=1234-02 rootfstype=ext4 "
           "fsck.repair=yes rootwait\n")
MAIN_CONF = """\
[General]
#Name = BlueZ
#Class = 0x000100

[Policy]
#AutoEnable=true
"""
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKeyOnly rig@test\n"
BT = "B8:27:EB:00:00:01"


def make_root(base: Path) -> Path:
    root = base / "root"
    (root / "boot/firmware").mkdir(parents=True)
    (root / "boot/firmware/config.txt").write_text(CONFIG_TXT)
    (root / "boot/firmware/cmdline.txt").write_text(CMDLINE)
    (root / "etc/bluetooth").mkdir(parents=True)
    (root / "etc/bluetooth/main.conf").write_text(MAIN_CONF)
    (root / "etc/passwd").write_text("root:x:0:0::/root:/bin/bash\nadmin:x:1000:1000::/home/admin:/bin/bash\n")
    (root / "etc/hostname").write_text("raspberrypi\n")
    (root / "etc/hosts").write_text("127.0.0.1\tlocalhost\n127.0.1.1\t\traspberrypi\n")
    (root / "home/admin").mkdir(parents=True)
    return root


def snapshot(root: Path) -> dict:
    out = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        if path.is_symlink():
            out[rel] = "-> " + os.readlink(path)
        elif path.is_file():
            out[rel] = path.read_bytes()
    return out


class ProvisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = make_root(self.base)
        self.key = self.base / "rig.pub"
        self.key.write_text(KEY)

    def tearDown(self):
        self.tmp.cleanup()

    def provision(self, *args):
        env = dict(os.environ, RIG_SYSROOT=str(self.root), NO_COLOR="1",
                   RIG_CONF=str(self.base / "absent.conf"))
        return subprocess.run(["bash", str(HERE / "provision.sh"), *args],
                              env=env, capture_output=True, text=True, timeout=60)

    def full(self, *extra):
        return self.provision("--files-only", "--user", "admin", "--bt-addr", BT.lower(),
                              "--ssh-key", str(self.key), *extra)

    def test_files_only_provision_writes_the_rig_config(self):
        proc = self.full()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        r = self.root
        config = (r / "boot/firmware/config.txt").read_text()
        self.assertTrue(config.rstrip().endswith("enable_uart=1\n# END larkbridge-rig"))
        self.assertTrue(config.startswith(CONFIG_TXT.rstrip("\n")))
        self.assertNotIn("console=serial0", (r / "boot/firmware/cmdline.txt").read_text())
        self.assertIn("console=tty1", (r / "boot/firmware/cmdline.txt").read_text())
        self.assertEqual((r / "home/admin/.ssh/authorized_keys").read_text(), KEY)
        self.assertIn("PasswordAuthentication no",
                      (r / "etc/ssh/sshd_config.d/10-larkbridge-rig.conf").read_text())
        main = (r / "etc/bluetooth/main.conf").read_text()
        general = main.split("[Policy]")[0].splitlines()
        for line in ("Name = LarkRig Fake Car", "Class = 0x000420", "ControllerMode = bredr"):
            self.assertIn(line, general)
        self.assertNotIn("#Class = 0x000100", main)
        rig_conf = (r / "etc/larkbridge-rig/rig.conf").read_text()
        self.assertIn(f"RIG_BT_ADDR={BT}\n", rig_conf)
        self.assertIn("RIG_USER=admin\n", rig_conf)
        self.assertIn("a2dp_sink", (r / "etc/wireplumber/wireplumber.conf.d/"
                                    "50-larkbridge-fake-car.conf").read_text())
        self.assertTrue((r / "etc/pipewire/pipewire.conf.d/50-larkbridge-fake-car.conf").is_file())
        self.assertTrue((r / "etc/udev/rules.d/70-larkbridge-rig-no-usb-bt.rules").is_file())
        self.assertEqual((r / "etc/tmpfiles.d/larkbridge-rig.conf").read_text(),
                         "f /run/larkbridge-rig.lease 0664 admin admin -\n")
        self.assertEqual(os.readlink(r / "usr/local/bin/rig-lease"),
                         "/usr/local/lib/larkbridge-rig/rig_lease.py")
        self.assertTrue(os.access(r / "usr/local/lib/larkbridge-rig/fake-car.sh", os.X_OK))
        for unit in ("larkbridge-rig-bt-pin.service", "larkbridge-rig-pico-idle.service"):
            self.assertEqual(os.readlink(r / "etc/systemd/system/multi-user.target.wants" / unit),
                             f"/etc/systemd/system/{unit}")
        self.assertEqual(os.readlink(r / "etc/systemd/system/serial-getty@ttyS0.service"), "/dev/null")
        self.assertTrue((r / "var/lib/systemd/linger/admin").is_file())
        self.assertEqual((r / "etc/hostname").read_text(), "larkbridge-rig\n")
        self.assertIn("127.0.1.1\t\tlarkbridge-rig", (r / "etc/hosts").read_text())

    def test_second_run_changes_nothing(self):
        self.assertEqual(self.full().returncode, 0)
        before = snapshot(self.root)
        proc = self.full()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("provisioned: 0 change(s)", proc.stderr)
        self.assertEqual(snapshot(self.root), before)

    def test_rerun_keeps_the_pinned_address(self):
        self.full()
        proc = self.provision("--files-only", "--user", "admin")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"RIG_BT_ADDR={BT}\n",
                      (self.root / "etc/larkbridge-rig/rig.conf").read_text())

    def test_sysroot_refuses_live_provisioning(self):
        before = snapshot(self.root)
        proc = self.provision("--user", "admin")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("RIG_SYSROOT requires --files-only", proc.stderr)
        self.assertEqual(snapshot(self.root), before)

    def test_dry_run_writes_nothing(self):
        before = snapshot(self.root)
        proc = self.full("--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertRegex(proc.stderr, r"dry run: [1-9][0-9]* change\(s\) would be made")
        self.assertEqual(snapshot(self.root), before)

    def test_refuses_to_lock_out_password_login_without_a_key(self):
        proc = self.provision("--files-only", "--user", "admin", "--bt-addr", BT)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("refusing to disable SSH password login", proc.stderr)
        self.assertFalse((self.root / "etc/ssh/sshd_config.d/10-larkbridge-rig.conf").exists())

    def test_rejects_bad_address_and_product_hostname(self):
        self.assertNotEqual(self.provision("--files-only", "--bt-addr", "nope").returncode, 0)
        proc = self.full("--hostname", "larkbridge")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("product Pi's name", proc.stderr)

    def test_refuses_config_that_moves_bluetooth_off_the_uart(self):
        with open(self.root / "boot/firmware/config.txt", "a") as handle:
            handle.write("dtoverlay=disable-bt\n")
        proc = self.full()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("disable-bt", proc.stderr)

    def assert_refused(self, marker: str):
        before = snapshot(self.root)
        for args in ((), ("--dry-run",)):
            proc = self.full(*args)
            self.assertEqual(proc.returncode, 4, f"{marker}: {proc.stderr}")
            self.assertIn("Refusing", proc.stderr)
        self.assertEqual(snapshot(self.root), before)

    def test_refuses_product_and_overlayroot_markers(self):
        markers = {
            "etc/overlayroot.local.conf": "file",
            "usr/share/initramfs-tools/scripts/init-bottom/overlayroot": "file",
            "usr/local/lib/rpi-lark-bridge": "dir",
            "var/lib/larkbridge-persist": "dir",
            "var/lib/rpi-lark-bridge": "dir",
            "etc/bluetooth/main.conf.d/10-bridge.conf": "file",
            "etc/systemd/system/bridge-btwatchdog.service": "file",
            "home/admin/.config/systemd/user/bridge-supervisor.service": "file",
        }
        for rel, kind in markers.items():
            with self.subTest(marker=rel):
                path = self.root / rel
                if kind == "dir":
                    path.mkdir(parents=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("x\n")
                self.assert_refused(rel)
                if kind == "dir":
                    path.rmdir()
                else:
                    path.unlink()

    def test_refuses_overlay_settings(self):
        cases = [
            ("etc/overlayroot.conf", 'overlayroot="tmpfs"\n'),
            ("etc/hostname", "larkbridge\n"),
            ("boot/firmware/cmdline.txt", "console=tty1 root=/dev/x boot=overlay\n"),
            ("boot/firmware/cmdline.txt", "console=tty1 overlayroot=device:dev=/dev/sda1\n"),
        ]
        for rel, text in cases:
            with self.subTest(file=rel, text=text):
                path = self.root / rel
                original = path.read_text() if path.exists() else None
                path.write_text(text)
                self.assert_refused(rel)
                if original is None:
                    path.unlink()
                else:
                    path.write_text(original)

    def test_disabled_overlayroot_package_config_is_allowed(self):
        (self.root / "etc/overlayroot.conf").write_text('overlayroot=""\n')
        self.assertEqual(self.full().returncode, 0)


class ConfEditTests(unittest.TestCase):
    def test_config_txt_is_idempotent_and_moves_block_last(self):
        once = conf_edit.edit_config_txt(CONFIG_TXT)
        self.assertEqual(conf_edit.edit_config_txt(once), once)
        moved = conf_edit.edit_config_txt(once + "[pi4]\narm_boost=1\n")
        self.assertTrue(moved.endswith("enable_uart=1\n# END larkbridge-rig\n"))
        self.assertEqual(moved.count("BEGIN larkbridge-rig"), 1)

    def test_config_txt_refusals(self):
        for bad in ("dtoverlay=miniuart-bt\n", "enable_uart=0\n",
                    conf_edit.BEGIN + "\n" + conf_edit.BEGIN + "\n"):
            with self.assertRaises(conf_edit.Refused):
                conf_edit.edit_config_txt(CONFIG_TXT + bad)

    def test_cmdline(self):
        self.assertEqual(conf_edit.edit_cmdline("console=ttyAMA0,115200 root=x quiet\n"),
                         "root=x quiet\n")
        self.assertEqual(conf_edit.edit_cmdline("console=tty1 root=x\n"), "console=tty1 root=x\n")

    def test_ini_set(self):
        out = conf_edit.edit_ini(MAIN_CONF, "General", [("Class", "0x000420"), ("Name", "X")])
        self.assertIn("[General]\nName = X\nClass = 0x000420\n", out)
        self.assertEqual(conf_edit.edit_ini(out, "General", [("Name", "X")]), out)
        new = conf_edit.edit_ini("", "General", [("A", "1")])
        self.assertEqual(new, "[General]\nA = 1\n")
        # A key in another section is not touched.
        out = conf_edit.edit_ini("[Policy]\nName = keep\n", "General", [("Name", "X")])
        self.assertIn("[Policy]\nName = keep\n", out)


class FakeCarStatusTests(unittest.TestCase):
    OBJECTS = {"type": "a{oa{sa{sv}}}", "data": [{
        "/org/bluez": {"org.bluez.AgentManager1": {}},
        "/org/bluez/hci0": {"org.bluez.Adapter1": {
            "Address": {"type": "s", "data": BT},
            "Alias": {"type": "s", "data": "LarkRig Fake Car"},
            "Class": {"type": "u", "data": 0x240420},
            "Powered": {"type": "b", "data": True},
            "Discoverable": {"type": "b", "data": False},
            "Pairable": {"type": "b", "data": False}}},
        "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF": {"org.bluez.Device1": {
            "Address": {"type": "s", "data": "AA:BB:CC:DD:EE:FF"},
            "Alias": {"type": "s", "data": "LarkBridge-32UE"},
            "Paired": {"type": "b", "data": True},
            "Trusted": {"type": "b", "data": True},
            "Connected": {"type": "b", "data": True},
            "UUIDs": {"type": "as", "data": ["0000110A-0000-1000-8000-00805F9B34FB"]}}},
        "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF/player0": {"org.bluez.MediaPlayer1": {}},
        "/org/bluez/hci1/dev_11_22_33_44_55_66": {"org.bluez.Device1": {
            "Address": {"type": "s", "data": "11:22:33:44:55:66"}}},
    }]}
    PW = [
        {"type": "PipeWire:Interface:Node", "info": {"props": {"node.name": "larkbridge_fake_car"}}},
        {"type": "PipeWire:Interface:Node",
         "info": {"props": {"node.name": "bluez_input.AA_BB_CC_DD_EE_FF.1"}}},
        {"type": "PipeWire:Interface:Port", "info": {"props": {}}},
    ]

    def test_reduce(self):
        objects = fake_car_status.managed_objects(self.OBJECTS)
        out = fake_car_status.reduce(objects, "/org/bluez/hci0", self.PW, "larkbridge_fake_car")
        self.assertTrue(out["ok"])
        self.assertEqual(out["controller"]["address"], BT)
        self.assertEqual(out["controller"]["class"], "0x240420")
        self.assertEqual(out["connected"], ["AA:BB:CC:DD:EE:FF"])
        [device] = out["devices"]  # hci1's device is not ours
        self.assertTrue(device["a2dp_source"])
        self.assertEqual(device["avrcp_player"], "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF/player0")
        self.assertEqual(out["audio"]["a2dp_streams"], ["bluez_input.AA_BB_CC_DD_EE_FF.1"])

    def test_not_ok_without_sink_or_adapter(self):
        objects = fake_car_status.managed_objects(self.OBJECTS)
        self.assertFalse(fake_car_status.reduce(objects, "/org/bluez/hci0", [], "x")["ok"])
        self.assertFalse(fake_car_status.reduce(objects, "/org/bluez/hci9", self.PW,
                                                "larkbridge_fake_car")["ok"])
        self.assertFalse(fake_car_status.reduce({}, "/org/bluez/hci0", None, "x")["ok"])


def bash(script: str, **env) -> subprocess.CompletedProcess:
    full_env = dict(os.environ, NO_COLOR="1", RIG_CONF="/nonexistent/rig.conf")
    full_env.update(env)
    return subprocess.run(["bash", "-c", script], env=full_env, capture_output=True,
                          text=True, timeout=30)


class ShellHelperTests(unittest.TestCase):
    def test_parse_btmgmt_info(self):
        sample = ("Index list with 2 items\n"
                  "hci1:\tPrimary controller\n"
                  "\taddr 00:1a:7d:da:71:13 version 10 manufacturer 93 class 0x000000\n"
                  "\tsupported settings: powered connectable\n"
                  "hci0:\tPrimary controller\n"
                  "\taddr B8:27:EB:00:00:01 version 8 manufacturer 305 class 0x000420\n")
        proc = subprocess.run(["bash", "-c", f'. "{HERE}/lib.sh"; parse_btmgmt_info'],
                              input=sample, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.stdout, "hci1 00:1A:7D:DA:71:13\nhci0 B8:27:EB:00:00:01\n")

    def test_rig_load_conf_env_wins(self):
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as handle:
            handle.write("# c\nRIG_BT_ADDR=AA:AA:AA:AA:AA:AA\nRIG_UART=/dev/x\nbad-key=1\n")
        try:
            proc = bash(f'. "{HERE}/lib.sh"; rig_load_conf; echo "$RIG_BT_ADDR $RIG_UART"',
                        RIG_CONF=handle.name, RIG_UART="/dev/override")
            self.assertEqual(proc.stdout.strip(), "AA:AA:AA:AA:AA:AA /dev/override")
        finally:
            os.unlink(handle.name)


class PicoCtlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.elf = Path(self.tmp.name) / "fake_lark.elf"
        self.elf.write_bytes(b"\x7fELF" + b"\0" * 60)

    def tearDown(self):
        self.tmp.cleanup()

    def pico(self, *args):
        return subprocess.run(["bash", str(HERE / "pico-ctl.sh"), "--dry-run", *args],
                              env=dict(os.environ, NO_COLOR="1", RIG_CONF="/nonexistent"),
                              capture_output=True, text=True, timeout=30)

    def test_flash_uses_gpio_swd_and_leaves_pins_inputs(self):
        proc = self.pico("flash", str(self.elf))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = proc.stderr.splitlines()
        openocd = next(i for i, line in enumerate(lines) if "openocd" in line)
        self.assertIn("adapter\\ gpio\\ swclk\\ 25", lines[openocd])
        self.assertIn("adapter\\ gpio\\ swdio\\ 24", lines[openocd])
        self.assertIn("target/rp2040.cfg", lines[openocd])
        self.assertIn("verify\\ reset\\ exit", lines[openocd])
        self.assertNotIn("26", lines[openocd].split("program")[0].replace("0x3F000000", ""))
        idle = [line for line in lines[openocd + 1:] if "pinctrl set" in line]
        self.assertEqual(idle, [f"would run: pinctrl set {p} ip pn" for p in (25, 24, 26, 14)])

    def test_flash_rejects_non_elf(self):
        self.elf.write_bytes(b"not an elf")
        self.assertNotEqual(self.pico("flash", str(self.elf)).returncode, 0)

    def test_reset_pulses_run_then_releases(self):
        proc = self.pico("reset")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        sets = [line for line in proc.stderr.splitlines() if "pinctrl set" in line]
        self.assertEqual(sets[:2], ["would run: pinctrl set 26 op dl", "would run: pinctrl set 26 ip pn"])
        self.assertEqual(sets[-4:], [f"would run: pinctrl set {p} ip pn" for p in (25, 24, 26, 14)])

    def test_send_switches_tx_to_uart_and_back(self):
        proc = self.pico("send", "PLAY", "tone_1k")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        sets = [line for line in proc.stderr.splitlines() if "pinctrl set" in line]
        self.assertEqual(sets[0], "would run: pinctrl set 14 a5")
        self.assertEqual(sets[-1], "would run: pinctrl set 14 ip pn")
        self.assertIn("PLAY\\ tone_1k", proc.stderr)

    def test_status_is_json(self):
        proc = subprocess.run(["bash", str(HERE / "pico-ctl.sh"), "status"],
                              env=dict(os.environ, RIG_CONF="/nonexistent"),
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(set(json.loads(proc.stdout)["pins"]), {"25", "24", "26", "14"})


class ScriptSyntaxTests(unittest.TestCase):
    def test_bash_n(self):
        for script in sorted(HERE.glob("*.sh")):
            with self.subTest(script=script.name):
                proc = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
                self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
