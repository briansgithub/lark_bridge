from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "build_manifest.py"
SPEC = importlib.util.spec_from_file_location("build_manifest", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
build_manifest = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_manifest)


class BuildManifestTests(unittest.TestCase):
    def test_artifacts_exclude_cmake_compiler_probes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            build = project / "build"
            (build / "bootloader").mkdir(parents=True)
            (build / "partition_table").mkdir()
            (build / "CMakeFiles" / "4.0.3").mkdir(parents=True)
            (build / "gateway.bin").write_bytes(b"app")
            (build / "gateway.elf").write_bytes(b"elf")
            (build / "bootloader" / "bootloader.bin").write_bytes(b"boot")
            (build / "partition_table" / "partition-table.bin").write_bytes(b"parts")
            (build / "CMakeFiles" / "4.0.3" / "CMakeDetermineCompilerABI_C.bin").write_bytes(
                b"probe"
            )

            paths = {record["path"] for record in build_manifest.artifact_records(project)}

            self.assertEqual(
                paths,
                {
                    "build/gateway.bin",
                    "build/gateway.elf",
                    "build/bootloader/bootloader.bin",
                    "build/partition_table/partition-table.bin",
                },
            )


if __name__ == "__main__":
    unittest.main()
