#!/usr/bin/env python3
"""Compatibility entry point for the generalized transparent-audio rig.

The original script performed a one-off, hash-pinned WirePlumber spike. Its work is
now represented by guarded baseline/session/iterate/transition commands, so keeping a
second mutating implementation would bypass the exact-preimage and deadman contract.
"""

from __future__ import annotations

from transparent_audio import main

if __name__ == "__main__":
    raise SystemExit(main())
