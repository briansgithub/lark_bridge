#!/usr/bin/env python3
"""--p7-cycle-hook wrapper for rig/esp/soaks/h6_reconnect.py.
Triggers a deterministic USB replug on the Pico fake Lark via `ssh larkbridge-rig pico-ctl`.
"""
import subprocess
import sys


def main() -> int:
    # Hard-reset RP2040 via RUN pin for a clean hardware USB unplug/replug
    # (avoids RP2040 TinyUSB DPRAM exhaustion across repeated soft REPLUGs).
    cmd = [
        "wsl", "-e", "ssh", "-o", "BatchMode=yes", "larkbridge-rig",
        "pico-ctl reset",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        sys.stderr.write(r.stderr or r.stdout)
        return r.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
