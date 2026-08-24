#!/usr/bin/env python3
"""Serve the phone's output picker over the existing paired Bluetooth link.

Android is the RFCOMM server because the sealed Pi image has no practical way to publish an
SDP service without a long-lived helper.  The Pi connects to the phone's service, receives one
JSON request per line, and answers on the same socket.  Selection is still owned by the Pi:
the phone never caches policy and every explicit choice goes through ``bridgectl --remember``.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

import bridge_supervisor as supervisor


SERVICE_UUID = "6e0e6e72-3f13-4f7e-9d3f-87b6f5a43c11"
MAX_LINE_BYTES = 64 * 1024
RETRY_SECONDS = 5.0
log = logging.getLogger("bridge-output-remote")


def parse_rfcomm_channel(text: str) -> int | None:
    marker = "Service Name: LarkBridge Output Control"
    if marker not in text:
        return None
    section = text.split(marker, 1)[1].split("Service Name:", 1)[0]
    if SERVICE_UUID not in section.lower():
        return None
    match = re.search(r"^\s*Channel:\s*(\d+)\s*$", section, re.MULTILINE)
    if not match:
        return None
    channel = int(match.group(1))
    return channel if 1 <= channel <= 30 else None


def discover_channel(phone: str) -> int | None:
    try:
        result = subprocess.run(
            ["sdptool", "browse", phone],
            capture_output=True,
            text=True,
            timeout=12,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_rfcomm_channel(result.stdout) if result.returncode == 0 else None


def read_status(path: Path | None = None) -> dict[str, Any]:
    target = path or supervisor.default_status_path()
    return json.loads(target.read_text(encoding="utf-8"))


def public_output_state(status: dict[str, Any]) -> dict[str, Any]:
    block = status.get("output") or {}
    chosen = block.get("chosen") or {}
    candidates = []
    for candidate in block.get("candidates") or []:
        candidates.append(
            {
                "id": candidate.get("id"),
                "label": candidate.get("label"),
                "kind": candidate.get("kind"),
                "available": bool(candidate.get("present")),
                "connected": bool(candidate.get("connected")),
            }
        )
    return {
        "outputs": candidates,
        "desired_id": block.get("desired_id"),
        "chosen_id": chosen.get("id"),
        "reason": block.get("reason") or "",
    }


def handle_request(
    request: dict[str, Any],
    *,
    status_path: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    bridgectl_path: Path | None = None,
) -> dict[str, Any]:
    request_id = request.get("id")
    op = request.get("op")
    base: dict[str, Any] = {"id": request_id}
    try:
        status = read_status(status_path)
    except (OSError, json.JSONDecodeError) as exc:
        return {**base, "ok": False, "error": f"bridge status unavailable: {exc}"}

    if op in {"list", "status"}:
        return {**base, "ok": True, **public_output_state(status)}

    if op != "set":
        return {**base, "ok": False, "error": "unsupported operation"}

    output_id = str(request.get("output_id") or "")
    candidates = (status.get("output") or {}).get("candidates") or []
    target = next((item for item in candidates if item.get("id") == output_id), None)
    if target is None:
        return {**base, "ok": False, "error": "output is no longer listed"}

    command = [
        sys.executable,
        str(bridgectl_path or Path(__file__).with_name("bridgectl.py")),
        "output",
        "set",
        output_id,
        "--remember",
        "--no-chime",
    ]
    try:
        result = runner(
            command,
            capture_output=True,
            text=True,
            timeout=35,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {**base, "ok": False, "error": f"selection failed: {exc}"}
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
        return {**base, "ok": False, "error": detail}

    # The supervisor applies the desire asynchronously. Wait briefly so the first response the
    # phone renders already distinguishes the requested device from a wired fallback.
    refreshed = status
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        try:
            refreshed = read_status(status_path)
        except (OSError, json.JSONDecodeError):
            break
        if refreshed.get("output", {}).get("desired_id") == output_id:
            break
        time.sleep(0.1)
    return {
        **base,
        "ok": True,
        "accepted_id": output_id,
        "accepted_label": target.get("label"),
        "message": (result.stderr or result.stdout).strip(),
        **public_output_state(refreshed),
    }


def serve_connection(sock: socket.socket, status_path: Path | None = None) -> None:
    stream = sock.makefile("rwb", buffering=0)
    while True:
        line = stream.readline(MAX_LINE_BYTES + 1)
        if not line:
            return
        if len(line) > MAX_LINE_BYTES or not line.endswith(b"\n"):
            response = {"id": None, "ok": False, "error": "request is too large"}
        else:
            try:
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise ValueError("request must be an object")
                response = handle_request(request, status_path=status_path)
            except (json.JSONDecodeError, ValueError) as exc:
                response = {"id": None, "ok": False, "error": str(exc)}
        stream.write(json.dumps(response, separators=(",", ":")).encode() + b"\n")


def run(phone: str, status_path: Path | None = None) -> None:
    while True:
        channel = discover_channel(phone)
        if channel is None:
            time.sleep(RETRY_SECONDS)
            continue
        try:
            with socket.socket(
                socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM
            ) as client:
                client.settimeout(15)
                client.connect((phone, channel))
                client.settimeout(None)
                log.info("phone output control connected on RFCOMM channel %s", channel)
                serve_connection(client, status_path)
        except OSError as exc:
            log.info("phone output control disconnected: %s", exc)
        time.sleep(RETRY_SECONDS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phone", help="override the configured phone Bluetooth address")
    parser.add_argument("--status", type=Path, help="override bridge-status.json (tests)")
    args = parser.parse_args(argv)
    settings = supervisor.load_settings()
    phone = (args.phone or settings.phone_mac).strip().upper()
    if not phone:
        raise SystemExit("no phone Bluetooth address configured")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run(phone, args.status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
