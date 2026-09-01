#!/usr/bin/env python3
"""bridgectl -- ask the bridge what it is doing, and tell it where to play.

    bridgectl output              # list the outputs, marking live and chosen
    bridgectl output set 2        # by list index
    bridgectl output set boombox  # by name, case-insensitive substring
    bridgectl output set boombox --remember  # also make it the next-boot default
    bridgectl output set wired
    bridgectl output rename 2 "Car stereo"
    bridgectl output clear        # revert to the configured default
    bridgectl output status       # one line, or --json
    bridgectl microphone list     # ordered candidates and live diagnostics
    bridgectl microphone status   # selected microphone, or --json
    bridgectl phone status        # phone media/microphone transport, or --json
    sudo bridgectl phone repair   # open a bounded Pixel re-pairing window

This is the first slice of the CLI PLAN.md 4.2 has specified since the beginning and which
never existed. It is deliberately the FIRST front-end rather than the phone app, because it
is the one that proves the selection engine independently of any Bluetooth transport: if
`bridgectl output set` cannot move the audio, no amount of Kotlin will.

WHY IT ACCEPTS AN INDEX OR A NAME
---------------------------------
The canonical id is `a2dp:C9:5C:FD:6E:28:46`. Nobody types that, and an interface that
requires it invites copy-paste mistakes into an operation that changes where a live call is
audible. Indexes and name fragments resolve against the list the bridge itself just
published, and an ambiguous fragment is refused rather than guessed.

WHAT IT DOES NOT DO
-------------------
It does not restart the supervisor. Selection is a file the supervisor re-reads on its normal
poll, so a switch costs no rebuild of anything except the audio graph -- and restarting the
supervisor during active SCO is the suspected trigger for the E08 controller wedge, which is
exactly the situation a user changing outputs mid-call is in.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import struct
import subprocess
import sys
import tempfile
import time
import tomllib
import wave
from pathlib import Path
from typing import Any

import bridge_supervisor as supervisor

try:  # Optional: only needed to power a speaker on, not to select one.
    import btadapters
except (
    ImportError
):  # pragma: no cover - present on the appliance, absent in odd contexts
    btadapters = None  # type: ignore[assignment]


PHONE_WATCHDOG_STATE = Path(
    os.environ.get("BRIDGE_WD_CALL_STATE", "/run/larkbridge/bt-watchdog/call.json")
)
PHONE_WATCHDOG_UNIT = "bridge-btwatchdog@call.service"


def read_status(path: Path | None = None) -> dict[str, Any]:
    target = path or supervisor.default_status_path()
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SystemExit(
            f"cannot read {target}: {exc}\nIs bridge-supervisor running?"
        ) from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{target} is not valid JSON: {exc}") from exc


def outputs_of(status: dict[str, Any]) -> list[dict[str, Any]]:
    block = status.get("output") or {}
    candidates = block.get("candidates")
    if not candidates:
        raise SystemExit(
            "the supervisor published no output candidates.\n"
            "Its build may predate output selection, or pi/bridged/outputs.py is not deployed."
        )
    return list(candidates)


def resolve_selector(selector: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Turn a human's argument into exactly one candidate, or refuse.

    Order matters: an exact id first so a scripted caller is never surprised by fuzzy
    matching, then a list index, then a name fragment.
    """
    wanted = selector.strip()

    for candidate in candidates:
        if candidate["id"] == wanted:
            return candidate

    if wanted.isdigit():
        index = int(wanted)
        if 1 <= index <= len(candidates):
            return candidates[index - 1]
        raise SystemExit(f"no output {index}; there are {len(candidates)}")

    lowered = wanted.lower()
    matches = [
        c
        for c in candidates
        if lowered in str(c["label"]).lower()
        or lowered in str(c["id"]).lower()
        or (lowered == "wired" and c["kind"] == "wired")
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SystemExit(
            f"nothing matches {selector!r}. Run 'bridgectl output' for the list."
        )
    names = ", ".join(str(c["label"]) for c in matches)
    raise SystemExit(f"{selector!r} is ambiguous: {names}")


def do_list(args: argparse.Namespace) -> int:
    status = read_status()
    block = status.get("output") or {}
    candidates = outputs_of(status)
    chosen = (block.get("chosen") or {}).get("id")
    desired = block.get("desired_id")

    if args.json:
        print(json.dumps(block, indent=2))
        return 0

    print(f"  mode: {status.get('mode', '?')}    state: {status.get('state', '?')}")
    print()
    print("   #  output                     status")
    for index, candidate in enumerate(candidates, start=1):
        marks = []
        if candidate["id"] == chosen:
            marks.append("PLAYING")
        if candidate["id"] == desired:
            marks.append("chosen")
        if not candidate["present"]:
            marks.append("off or out of range")
        elif candidate["kind"] == "a2dp":
            marks.append("connected")
        pointer = "->" if candidate["id"] == chosen else "  "
        print(
            f" {pointer}{index:2}  {str(candidate['label'])[:24]:24}   {', '.join(marks)}"
        )
    print()
    if desired and desired != chosen:
        print(
            f"  note: {desired} is chosen but not available; {block.get('reason', '')}"
        )
    elif not desired:
        print("  no explicit choice; following the mode default")
    return 0


def do_status(args: argparse.Namespace) -> int:
    status = read_status()
    block = status.get("output") or {}
    if args.json:
        print(json.dumps(block, indent=2))
        return 0
    chosen = block.get("chosen") or {}
    print(
        f"{chosen.get('label') or '<none>'}  [{chosen.get('id') or '-'}]  {block.get('reason', '')}"
    )
    return 0


def microphones_of(
    status: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    block = status.get("microphone")
    if not isinstance(block, dict) or not isinstance(block.get("candidates"), list):
        raise SystemExit(
            "the supervisor published no microphone inventory.\n"
            "Its build may predate ordered microphone selection."
        )
    return block, list(block["candidates"])


def do_microphone_list(args: argparse.Namespace) -> int:
    block, candidates = microphones_of(read_status())
    if args.json:
        print(json.dumps(block, indent=2))
        return 0
    selected_id = (block.get("selected") or {}).get("id")
    print("   #  microphone                 status")
    for index, candidate in enumerate(candidates, start=1):
        marker = "->" if candidate.get("id") == selected_id else "  "
        state = str(candidate.get("state") or "unknown")
        nodes = candidate.get("matched_nodes") or []
        detail = f"{state}: {candidate.get('reason') or ''}"
        if nodes:
            detail += f" ({', '.join(str(node) for node in nodes)})"
        print(
            f" {marker}{index:2}  {str(candidate.get('label') or candidate.get('id'))[:24]:24}   {detail}"
        )
    print()
    print(f"  {block.get('selection_reason') or ''}")
    return 0


def do_microphone_status(args: argparse.Namespace) -> int:
    block, _candidates = microphones_of(read_status())
    if args.json:
        print(json.dumps(block, indent=2))
        return 0
    selected = block.get("selected") or {}
    print(
        f"{selected.get('label') or '<none>'}  "
        f"[{selected.get('id') or '-'}]  {block.get('selection_reason') or ''}"
    )
    return 0


def phone_of(status: dict[str, Any]) -> dict[str, Any]:
    block = status.get("phone")
    if not isinstance(block, dict):
        raise SystemExit(
            "the supervisor published no phone transport status.\n"
            "Its build may predate transparent phone audio."
        )
    return block


def read_phone_watchdog_state(path: Path | None = None) -> dict[str, Any]:
    """Read reconnect truth without making phone status depend on watchdog availability."""
    target = path or PHONE_WATCHDOG_STATE
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def phone_status_view(status: dict[str, Any]) -> dict[str, Any]:
    block = dict(phone_of(status))
    watchdog = read_phone_watchdog_state()
    repair_state = str(watchdog.get("repair_state") or "unavailable")
    bond_state = str(watchdog.get("bond_state") or "unknown")
    deadline = watchdog.get("repair_deadline_monotonic")
    reconnect_next = watchdog.get("reconnect_next_monotonic")

    if repair_state == "pairing_window":
        instructions = (
            "On the Pixel, open Bluetooth, tap LarkBridge BT500, and approve Pair."
        )
    elif repair_state == "pairing_required":
        instructions = (
            "Run 'sudo bridgectl phone repair', then approve pairing on the Pixel."
        )
    elif repair_state in {"requested", "preparing"}:
        instructions = "Pairing repair is starting; keep the Pixel unlocked and nearby."
    elif block.get("connected"):
        instructions = "No action required."
    else:
        instructions = "Keep Pixel Bluetooth enabled and the phone in range."

    block.update(
        {
            "bond_state": bond_state,
            "repair_state": repair_state,
            "repair_trigger": watchdog.get("repair_trigger"),
            "repair_deadline_monotonic": deadline,
            "repair_deadline_remaining_seconds": (
                max(0.0, float(deadline) - time.monotonic())
                if isinstance(deadline, (int, float))
                else None
            ),
            "watchdog_action": watchdog.get("last_action"),
            "startup": {
                "phase": watchdog.get("startup_phase"),
                "connect_attempts": watchdog.get("startup_connect_attempts"),
                "first_connect_request_monotonic": watchdog.get(
                    "first_connect_request_monotonic"
                ),
                "local_profiles_ready_monotonic": watchdog.get(
                    "startup_profile_ready_monotonic"
                ),
                "profile_wait_deadline_monotonic": watchdog.get(
                    "startup_profile_deadline_monotonic"
                ),
                "missing_local_profile_uuids": watchdog.get(
                    "startup_missing_local_uuids"
                ),
            },
            "reconnect_timing": {
                "attempts": watchdog.get("reconnect_attempts"),
                "connected_monotonic": watchdog.get("connected_monotonic"),
                "next_monotonic": reconnect_next,
                "next_in_seconds": (
                    max(0.0, float(reconnect_next) - time.monotonic())
                    if isinstance(reconnect_next, (int, float)) and reconnect_next > 0
                    else 0.0 if reconnect_next == 0 else None
                ),
                "pending_since_monotonic": watchdog.get(
                    "connect_pending_since_monotonic"
                ),
                "pending_deadline_monotonic": watchdog.get(
                    "connect_pending_deadline_monotonic"
                ),
            },
            "instructions": instructions,
        }
    )
    return block


def do_phone_status(args: argparse.Namespace) -> int:
    """Report transport truth without attempting to connect or change the phone."""
    block = phone_status_view(read_status())
    if args.json:
        print(json.dumps(block, indent=2))
        return 0

    connected = "phone connected" if block.get("connected") else "phone disconnected"
    media = "media routed" if block.get("media_routed") else "media not routed"
    if block.get("android_microphone_transport"):
        microphone = "Android microphone transport open"
    else:
        reason = str(
            block.get("microphone_transport_reason")
            or "Android has not opened a microphone transport"
        )
        microphone = f"Android microphone transport closed: {reason}"
    failure = block.get("failure_reason")
    failure_text = f"  failure: {failure}" if failure else ""
    print(
        f"{block.get('transport') or 'UNKNOWN'}  {connected}  {media}  "
        f"{microphone}{failure_text}"
    )
    print(
        f"bond: {block['bond_state']}  repair: {block['repair_state']}  "
        f"action: {block.get('watchdog_action') or '-'}"
    )
    startup = block.get("startup") or {}
    print(
        f"startup: {startup.get('phase') or '-'}  "
        f"connect attempts: {startup.get('connect_attempts') or 0}"
    )
    print(str(block["instructions"]))
    return 0


def do_phone_repair(_args: argparse.Namespace) -> int:
    """Ask the running call watchdog to own one exact, bounded repair transaction."""
    getuid = getattr(os, "geteuid", None)
    if getuid is None or getuid() != 0:
        raise SystemExit("phone repair requires root; run: sudo bridgectl phone repair")
    try:
        result = subprocess.run(
            [
                "systemctl",
                "kill",
                "--kill-whom=main",
                "--signal=SIGUSR1",
                PHONE_WATCHDOG_UNIT,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(f"could not request phone repair: {exc}") from exc
    if result.returncode != 0:
        detail = (
            result.stderr.strip()
            or result.stdout.strip()
            or f"exit {result.returncode}"
        )
        raise SystemExit(f"could not request phone repair: {detail}")
    print(
        "Pixel repair requested. Open Bluetooth on the Pixel and approve pairing when prompted."
    )
    return 0


def _call_is_up(status: dict[str, Any]) -> bool:
    return bool((status.get("call") or {}).get("hfp_nodes_present"))


def _call_adapter(status: dict[str, Any]) -> str | None:
    """Which controller carries the call, so a page can be judged against it."""
    if btadapters is None:
        return None
    sink = (status.get("endpoints") or {}).get("hfp_sink")
    if not sink:
        return None
    # bluez_output.5C_33_7B_CB_BF_C5.1 -> 5C:33:7B:CB:BF:C5
    parts = str(sink).split(".")
    if len(parts) < 2:
        return None
    adapter = btadapters.adapter_for_device(parts[1].replace("_", ":"))
    return adapter.hci if adapter else None


CHIME_HZ = (784.0, 1047.0)  # G5 then C6: a rising two-note figure, obviously deliberate
CHIME_NOTE_S = 0.16
CHIME_DBFS = -14.0


def chime_path() -> Path:
    """A short confirmation tone, generated once into tmpfs.

    WHY AUDIBLE CONFIRMATION EXISTS AT ALL
    --------------------------------------
    The appliance is headless and in a car nobody can look at anything. A chime played OUT OF
    THE NEWLY SELECTED OUTPUT is the only confirmation that demonstrates rather than claims:
    the user hears which box it came from, so a stale list, a wrong MAC or a silent failure
    cannot mislead them. A message on a screen they are not looking at cannot do that.

    Two notes rather than one so it cannot be mistaken for call audio, hum, or a glitch.
    Generated rather than shipped because there is no TTS on the appliance and a committed WAV
    would be a binary blob in the repo for 0.3 s of sound.
    """
    # Derived from the supervisor's own runtime dir rather than rebuilding /run/user/<uid>:
    # os.getuid() does not exist on the control PC, where the host test suite runs.
    target = supervisor.default_status_path().parent / "bridge-chime.wav"
    if target.exists():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    rate = 48000
    amplitude = 10 ** (CHIME_DBFS / 20.0)
    frames = bytearray()
    for note in CHIME_HZ:
        count = int(rate * CHIME_NOTE_S)
        for index in range(count):
            # Raised-cosine envelope: a hard edge on a Bluetooth sink is a click, and a click
            # is exactly the artefact the dropout detector is built to find.
            envelope = 0.5 - 0.5 * math.cos(2 * math.pi * index / max(count - 1, 1))
            value = int(
                32767
                * amplitude
                * envelope
                * math.sin(2 * math.pi * note * index / rate)
            )
            frames += struct.pack("<hh", value, value)
    with wave.open(str(target), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(frames))
    return target


def wait_for_node(target: dict[str, Any], seconds: float = 6.0) -> str | None:
    """Wait briefly for a just-paged speaker's graph node to appear.

    Polls the PipeWire graph directly, not the status file. A page can finish between status
    publications, so reading the file loses a race it did not need to enter and can suppress
    the one confirmation the user actually gets.
    """
    if target["kind"] != "a2dp" or not target.get("address"):
        return None
    try:
        import outputs as outputs_module
    except ImportError:
        return None
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        node = outputs_module.find_a2dp_node(
            supervisor.pw_nodes() or {}, target["address"]
        )
        if node:
            return node
        time.sleep(0.3)
    return None


def play_chime(node: str) -> bool:
    """Play the confirmation into ONE explicit node. Never the default sink.

    --target is not optional here. Letting PipeWire pick would play the confirmation
    somewhere other than the thing being confirmed, which is worse than no confirmation.
    """
    try:
        result = subprocess.run(
            ["pw-play", "--target", node, str(chime_path())],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def target_adapter(target: dict[str, Any]):
    """Resolve a status candidate only by its permanent controller address."""
    if btadapters is None:
        return None
    address = target.get("adapter_address")
    canonical = btadapters.canonical_mac(address)
    if canonical is None or canonical != address:
        return None
    return btadapters.adapter_by_address(canonical)


def _toml_value(value: str | bool) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    # JSON's double-quoted string syntax is valid TOML for the identifiers stored here.
    return json.dumps(value)


def patch_toml_table(
    text: str,
    table: str,
    values: dict[str, str | bool | None],
) -> str:
    """Update one TOML table without rewriting unrelated comments or formatting.

    The hardened appliance deliberately ships without a TOML writer dependency.  A full
    parse-and-reserialize would either add one to the boot-critical image or discard the
    operator's comments.  These tables contain only simple scalar keys, so a narrow line
    patch is both easier to audit and less destructive.  The completed document is parsed
    again before it can reach the persistent slot.
    """
    lines = text.splitlines(keepends=True)
    header = f"[{table}]"
    start = next(
        (index for index, line in enumerate(lines) if line.strip() == header),
        None,
    )
    if start is None:
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] += "\n"
        if lines and lines[-1].strip():
            lines.append("\n")
        lines.append(header + "\n")
        start = len(lines) - 1
        end = len(lines)
    else:
        end = next(
            (
                index
                for index in range(start + 1, len(lines))
                if lines[index].lstrip().startswith("[")
            ),
            len(lines),
        )

    for key, value in values.items():
        match = next(
            (
                index
                for index in range(start + 1, end)
                if lines[index].lstrip().startswith(f"{key} ")
                or lines[index].lstrip().startswith(f"{key}=")
            ),
            None,
        )
        if value is None:
            if match is not None:
                del lines[match]
                end -= 1
            continue
        replacement = f"{key} = {_toml_value(value)}\n"
        if match is None:
            lines.insert(end, replacement)
            end += 1
        else:
            lines[match] = replacement

    candidate = "".join(lines)
    tomllib.loads(candidate)
    return candidate


def startup_config_for(target: dict[str, Any], current: str) -> str:
    """Return a validated config that makes *target* the next-boot default."""
    kind = str(target.get("kind") or "")
    output_id = str(target.get("id") or "")
    if kind not in {"wired", "a2dp"} or not output_id:
        raise ValueError("output has no persistable identity")
    if len(output_id) > 512 or not output_id.startswith(f"{kind}:"):
        raise ValueError("output has an invalid stable id")
    candidate = patch_toml_table(
        current,
        "bridge",
        {
            "mode": "bluetooth" if kind == "a2dp" else "bluetooth-wired",
            "fallback_to_wired": True,
        },
    )
    current_document = tomllib.loads(current)
    current_adapter = str(
        (((current_document.get("devices") or {}).get("output") or {}).get("adapter"))
        or ""
    ).strip()
    output_values: dict[str, str | bool | None] = {
        "id": output_id,
        "address": None,
        # This is the appliance's dedicated radio identity, not a property of whichever
        # output is selected. Keep it when the user switches back to the wire.
        "adapter": current_adapter or None,
        "reconnect": None,
    }
    if kind == "a2dp":
        address = str(target.get("address") or "").upper()
        adapter = str(target.get("adapter_address") or "").upper()
        valid_address = (
            btadapters is not None and btadapters.canonical_mac(address) == address
        )
        valid_adapter = (
            btadapters is not None and btadapters.canonical_mac(adapter) == adapter
        )
        if not valid_address or not valid_adapter or output_id != f"a2dp:{address}":
            raise ValueError(
                "Bluetooth startup choices require the speaker and controller addresses"
            )
        output_values.update(
            {"address": address, "adapter": adapter, "reconnect": True}
        )
    return patch_toml_table(candidate, "devices.output", output_values)


def state_tool_path() -> Path:
    installed = Path("/usr/local/lib/rpi-lark-bridge/powerloss/lark_state.py")
    if installed.exists():
        return installed
    return Path(__file__).resolve().parents[1] / "powerloss" / "lark_state.py"


def _commit_startup_payload(
    payload: bytes,
    config_path: Path,
    *,
    tool_path: Path | None = None,
) -> tuple[bool, str]:
    """Commit exact bytes to the durable slot and active mirror as one recoverable step."""
    try:
        tomllib.loads(payload.decode("utf-8"))
        config_path.read_bytes()
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return False, f"cannot prepare startup configuration: {exc}"
    runtime = supervisor.default_status_path().parent
    runtime.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    active_temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb",
            dir=runtime,
            prefix=".bridge-startup-output-",
            suffix=".toml",
            delete=False,
        ) as handle:
            handle.write(payload)
            temporary = Path(handle.name)

        # Prepare the live mirror before committing the A/B slot. After config-write returns,
        # only one same-filesystem rename remains; this keeps the verifier and any later
        # supervisor restart aligned with the slot that the next boot will restore.
        with tempfile.NamedTemporaryFile(
            "wb",
            dir=config_path.parent,
            prefix=f".{config_path.name}.",
            suffix=".new",
            delete=False,
        ) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            active_temporary = Path(handle.name)
        active_temporary.chmod(config_path.stat().st_mode & 0o777)

        state_tool = str(tool_path or state_tool_path())
        result = subprocess.run(
            [
                "sudo",
                "-n",
                "python3",
                state_tool,
                "config-write",
                "--source",
                str(temporary),
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if result.returncode != 0:
            detail = (
                result.stderr or result.stdout
            ).strip() or f"exit {result.returncode}"
            return False, f"persistent configuration rejected: {detail}"
        slot = result.stdout.strip()
        try:
            os.replace(active_temporary, config_path)
        except OSError as exc:
            # config-write has already advanced the durable pointer. Restore the old active
            # file into a fresh slot so a failed live rename cannot create a split-brain boot.
            rollback = subprocess.run(
                [
                    "sudo",
                    "-n",
                    "python3",
                    state_tool,
                    "config-write",
                    "--source",
                    str(config_path),
                ],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            if rollback.returncode == 0:
                return (
                    False,
                    f"active configuration update failed and was rolled back: {exc}",
                )
            detail = (
                rollback.stderr or rollback.stdout
            ).strip() or f"exit {rollback.returncode}"
            return False, (
                f"choice reached slot {slot or '?'} but the active mirror failed: {exc}; "
                f"rollback also failed: {detail}"
            )
        active_temporary = None
        return True, f"saved in recovery-safe configuration slot {slot or '?'}"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"persistent configuration failed: {exc}"
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if active_temporary is not None:
            active_temporary.unlink(missing_ok=True)


def remember_startup_output(
    target: dict[str, Any],
    config_path: Path,
    *,
    tool_path: Path | None = None,
) -> tuple[bool, str]:
    """Commit a next-boot default through LARKDATA's checksummed A/B slots."""
    try:
        candidate = startup_config_for(target, config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        return False, f"cannot prepare startup configuration: {exc}"
    return _commit_startup_payload(
        candidate.encode("utf-8"), config_path, tool_path=tool_path
    )


def restore_startup_config(
    snapshot: bytes,
    config_path: Path,
    *,
    tool_path: Path | None = None,
) -> tuple[bool, str]:
    """Restore the exact pre-transaction config through the same A/B commit path."""
    return _commit_startup_payload(snapshot, config_path, tool_path=tool_path)


def do_set(args: argparse.Namespace) -> int:
    status = read_status()
    candidates = outputs_of(status)
    target = resolve_selector(args.selector, candidates)

    if target.get("setup_state", "ready") != "ready":
        print(
            f"cannot select {target['label']}: speaker setup is required on the dedicated radio",
            file=sys.stderr,
        )
        return 1

    # Powering a speaker on is a page. E07 measured paging during active SCO as its own
    # failure mode -- but that was ONE controller, where the page and the voice link competed
    # for the same radio. It is only a hazard here when the speaker shares an adapter with the
    # call, and this gate says so rather than refusing every mid-call page.
    #
    # Measured 2026-08-23 on two radios: bt-watchdog paged the Boombox on hci1 during a live
    # call and SCO on hci0 held at 135 frames/s, exactly nominal. n=1, so the same-adapter
    # case stays gated and only the cross-adapter case is allowed. This previously refused
    # unconditionally while the watchdog paged anyway -- two components disagreeing about one
    # safety question, which is worse than either answer alone.
    # Trust hygiene runs on SELECTION, not only when a page is needed. An already-connected
    # but untrusted speaker is precisely the churn case: BlueZ refuses its next incoming
    # connection with `a2dp.c:auth_cb() Access denied`, so it drops and cannot come back. An
    # earlier version pinned trust only inside the paging branch, which skipped the connected
    # speaker that actually had the wrong flag.
    speaker_adapter = None
    if target["kind"] == "a2dp" and btadapters is not None:
        speaker_adapter = target_adapter(target)
        if speaker_adapter is None:
            print(
                f"cannot select {target['label']}: permanent speaker controller is unavailable",
                file=sys.stderr,
            )
            return 1
        pin = btadapters.pin_to_adapter(target["address"], speaker_adapter)
        if pin.changed:
            print(f"trust: {', '.join(pin.changed)}", file=sys.stderr)
        if not pin.ok:
            print(
                f"cannot select {target['label']}: trust pinning failed: "
                f"{'; '.join(pin.failures)}",
                file=sys.stderr,
            )
            return 1

    if getattr(args, "remember", False):
        config_path = Path(
            status.get("config_path") or supervisor.default_config_path()
        )
        saved, detail = remember_startup_output(target, config_path)
        if not saved:
            print(f"cannot remember {target['label']}: {detail}", file=sys.stderr)
            return 1
        print(f"startup: {detail}", file=sys.stderr)

    needs_connect = target["kind"] == "a2dp" and not target["present"]
    shares_call_radio = bool(
        target.get("adapter") and target["adapter"] == _call_adapter(status)
    )
    if needs_connect and args.connect:
        if _call_is_up(status) and shares_call_radio and not args.force:
            print(
                f"{target['label']} is not connected, a call is active, and the speaker is\n"
                f"bonded on {target['adapter']} -- the same radio carrying the call. Paging\n"
                "there during active SCO is a measured failure mode (E07), so it was NOT\n"
                "attempted. The choice is recorded and takes effect when the speaker\n"
                "connects. Use --force to page anyway, or bond it on the other adapter.",
                file=sys.stderr,
            )
        elif btadapters is None:
            print(
                "btadapters unavailable; recording the choice without connecting.",
                file=sys.stderr,
            )
        else:
            ok, detail = btadapters.connect_profile(target["address"], speaker_adapter)
            print(
                f"connect {target['label']}: {'ok' if ok else 'failed'} ({detail})",
                file=sys.stderr,
            )

    supervisor.write_desire(target["id"], source="bridgectl")
    print(f"chose {target['label']}  [{target['id']}]")

    node = target["node"] or wait_for_node(target, seconds=6.0)

    if args.chime and node:
        heard = play_chime(node)
        print(f"chime -> {target['label']}: {'played' if heard else 'FAILED'}")
    elif args.chime:
        print(
            f"{target['label']} is not available yet; no chime to play", file=sys.stderr
        )

    print("the supervisor applies this in under 1s")
    return 0


def do_rename(args: argparse.Namespace) -> int:
    """Give an output a name the owner would actually recognise.

    Motivated by running the list for real: the Boombox reports itself as "MP43247", so
    `bridgectl output set boombox` matched nothing. An interface the owner cannot address in
    their own words is not a usable selector.
    """
    status = read_status()
    target = resolve_selector(args.selector, outputs_of(status))
    if target["kind"] != "a2dp":
        raise SystemExit(
            "only Bluetooth outputs can be renamed; the wired jack is named by ALSA"
        )
    if btadapters is None:
        raise SystemExit("btadapters unavailable; cannot rename")
    adapter = target_adapter(target)
    ok, detail = btadapters.set_alias(target["address"], args.name, adapter)
    if not ok:
        raise SystemExit(f"rename failed: {detail}")
    print(f"renamed {target['label']} -> {args.name}")
    return 0


def do_clear(_args: argparse.Namespace) -> int:
    supervisor.write_desire(None, source="bridgectl")
    print("cleared; following the configured default again")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bridgectl",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    top = parser.add_subparsers(dest="group", required=True)

    output = top.add_parser("output", help="where call audio is played")
    actions = output.add_subparsers(dest="action")

    listing = actions.add_parser("list", help="list the outputs (default)")
    listing.add_argument("--json", action="store_true")
    listing.set_defaults(func=do_list)

    setter = actions.add_parser("set", help="choose an output by id, index or name")
    setter.add_argument("selector")
    setter.add_argument(
        "--connect",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="power on a bonded speaker that is currently off (default: yes)",
    )
    setter.add_argument(
        "--force", action="store_true", help="page a speaker even during a live call"
    )
    setter.add_argument(
        "--chime",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="play a confirmation tone out of the selected output (default: yes)",
    )
    setter.add_argument(
        "--remember",
        action="store_true",
        help="also use this output after a restart (commits one recovery-safe config slot)",
    )
    setter.set_defaults(func=do_set)

    renamer = actions.add_parser("rename", help="give an output a friendly name")
    renamer.add_argument("selector")
    renamer.add_argument("name")
    renamer.set_defaults(func=do_rename)

    clearer = actions.add_parser("clear", help="revert to the configured default")
    clearer.set_defaults(func=do_clear)

    reporter = actions.add_parser("status", help="one line about the live output")
    reporter.add_argument("--json", action="store_true")
    reporter.set_defaults(func=do_status)

    # `bridgectl output` with no action lists, because listing is the harmless one.
    output.set_defaults(func=do_list, json=False)

    microphone = top.add_parser(
        "microphone", help="which configured microphone is active"
    )
    microphone_actions = microphone.add_subparsers(dest="action")

    microphone_listing = microphone_actions.add_parser(
        "list", help="list ordered microphone candidates (default)"
    )
    microphone_listing.add_argument("--json", action="store_true")
    microphone_listing.set_defaults(func=do_microphone_list)

    microphone_status = microphone_actions.add_parser(
        "status", help="one line about the selected microphone"
    )
    microphone_status.add_argument("--json", action="store_true")
    microphone_status.set_defaults(func=do_microphone_status)

    # Read-only by design: configured order is authoritative and there is no runtime set.
    microphone.set_defaults(func=do_microphone_list, json=False)

    phone = top.add_parser("phone", help="phone media and microphone transport state")
    phone_actions = phone.add_subparsers(dest="action")

    phone_status = phone_actions.add_parser(
        "status", help="one line about the phone's live transports"
    )
    phone_status.add_argument("--json", action="store_true")
    phone_status.set_defaults(func=do_phone_status)

    phone_repair = phone_actions.add_parser(
        "repair", help="open a 120-second Pixel-only pairing repair window"
    )
    phone_repair.set_defaults(func=do_phone_repair)

    # Status is the harmless default; repair must be explicit and root-owned.
    phone.set_defaults(func=do_phone_status, json=False)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
