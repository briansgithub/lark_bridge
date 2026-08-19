import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def load(name: str, path: Path):
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


configure = load(
    "configure_offline_boot", ROOT / "scripts/powerloss/configure-offline-boot.py"
)
safety = load("safety_evidence", ROOT / "scripts/powerloss/safety-evidence.py")


class OfflineBootConfiguration(unittest.TestCase):
    def test_configures_read_only_mounts_and_initramfs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "root"
            boot = base / "boot"
            (root / "etc").mkdir(parents=True)
            boot.mkdir()
            fstab = root / "etc/fstab"
            fstab.write_text(
                "PARTUUID=one /boot/firmware vfat defaults 0 2\n"
                "PARTUUID=two / ext4 defaults,noatime 0 1\n",
                encoding="utf-8",
            )
            (boot / "cmdline.txt").write_text(
                "console=tty1 root=PARTUUID=two rootfstype=ext4 rw rootwait\n",
                encoding="utf-8",
            )
            (boot / "config.txt").write_text("arm_64bit=0\n", encoding="utf-8")

            configure.configure_fstab(fstab)
            configure.configure_cmdline(boot / "cmdline.txt")
            configure.configure_config_txt(boot / "config.txt")

            result = fstab.read_text(encoding="utf-8")
            self.assertIn("LABEL=LARKDATA", result)
            root_line = next(line for line in result.splitlines() if "\t/\t" in line)
            boot_line = next(
                line for line in result.splitlines() if "\t/boot/firmware\t" in line
            )
            self.assertTrue(root_line.split()[3].endswith(",ro"))
            self.assertTrue(boot_line.split()[3].endswith(",ro"))
            cmdline = (boot / "cmdline.txt").read_text(encoding="utf-8")
            self.assertIn("root=PARTUUID=two ro rootfstype=ext4", cmdline)
            self.assertNotIn(" rw ", f" {cmdline} ")
            self.assertIn(
                "auto_initramfs=1", (boot / "config.txt").read_text(encoding="utf-8")
            )


class SafetyEvidence(unittest.TestCase):
    def test_rejects_changed_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            image = base / "backup.img"
            image.write_bytes(b"known image")
            digest = hashlib.sha256(image.read_bytes()).hexdigest()
            evidence = base / "evidence.json"
            evidence.write_text(
                json.dumps(
                    {
                        "backup": {
                            "image": str(image),
                            "sha256": digest,
                            "size": image.stat().st_size,
                        },
                        "recovery_card": {
                            "boot_id": "boot-id",
                            "card_serial": "card-serial",
                            "physically_boot_tested": True,
                            "source_image_sha256": digest,
                        },
                        "schema": 1,
                    }
                ),
                encoding="utf-8",
            )
            safety.validate_document(evidence, rehash=True)
            image.write_bytes(b"other image")
            with self.assertRaises(ValueError):
                safety.validate_document(evidence, rehash=True)


if __name__ == "__main__":
    unittest.main()
