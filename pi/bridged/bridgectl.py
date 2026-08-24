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
except ImportError:  # pragma: no cover - present on the appliance, absent in odd contexts
    btadapters = None  # type: ignore[assignment]


def read_status(path: Path | None = None) -> dict[str, Any]:
    target = path or supervisor.default_status_path()
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SystemExit(f"cannot read {target}: {exc}\nIs bridge-supervisor running?") from exc
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
        raise SystemExit(f"nothing matches {selector!r}. Run 'bridgectl output' for the list.")
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
        print(f" {pointer}{index:2}  {str(candidate['label'])[:24]:24}   {', '.join(marks)}")
    print()
    if desired and desired != chosen:
        print(f"  note: {desired} is chosen but not available; {block.get('reason', '')}")
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
    print(f"{chosen.get('label') or '<none>'}  [{chosen.get('id') or '-'}]  {block.get('reason', '')}")
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
            value = int(32767 * amplitude * envelope * math.sin(2 * math.pi * note * index / rate))
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
        node = outputs_module.find_a2dp_node(supervisor.pw_nodes() or {}, target["address"])
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
            capture_output=True, text=True, timeout=15, check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def target_adapter(target: dict[str, Any]):
    """Resolve a status candidate by permanent controller address, with legacy fallback."""
    if btadapters is None:
        return None
    address = target.get("adapter_address")
    if address:
        return btadapters.adapter_by_address(str(address))
    hci = target.get("adapter")
    return next((a for a in btadapters.adapters() if a.hci == hci), None) if hci else None


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
    candidate = patch_toml_table(
        current,
        "bridge",
        {
            "mode": "bluetooth" if kind == "a2dp" else "bluetooth-wired",
            "fallback_to_wired": True,
        },
    )
    output_values: dict[str, str | bool | None] = {
        "id": output_id,
        "address": None,
        "adapter": None,
        "reconnect": None,
    }
    if kind == "a2dp":
        address = str(target.get("address") or "").upper()
        adapter = str(target.get("adapter_address") or "").upper()
        if not address or not adapter:
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

    runtime = supervisor.default_status_path().parent
    runtime.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=runtime,
            prefix=".bridge-startup-output-",
            suffix=".toml",
            delete=False,
        ) as handle:
            handle.write(candidate)
            temporary = Path(handle.name)
        result = subprocess.run(
            [
                "sudo",
                "-n",
                "python3",
                str(tool_path or state_tool_path()),
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
            detail = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
            return False, f"persistent configuration rejected: {detail}"
        slot = result.stdout.strip()
        return True, f"saved in recovery-safe configuration slot {slot or '?'}"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"persistent configuration failed: {exc}"
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def do_set(args: argparse.Namespace) -> int:
    status = read_status()
    candidates = outputs_of(status)
    target = resolve_selector(args.selector, candidates)

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
        if speaker_adapter is not None:
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
            print("btadapters unavailable; recording the choice without connecting.", file=sys.stderr)
        else:
            ok, detail = btadapters.connect_profile(target["address"], speaker_adapter)
            print(f"connect {target['label']}: {'ok' if ok else 'failed'} ({detail})", file=sys.stderr)

    supervisor.write_desire(target["id"], source="bridgectl")
    print(f"chose {target['label']}  [{target['id']}]")

    node = target["node"] or wait_for_node(target, seconds=6.0)

    if args.chime and node:
        heard = play_chime(node)
        print(f"chime -> {target['label']}: {'played' if heard else 'FAILED'}")
    elif args.chime:
        print(f"{target['label']} is not available yet; no chime to play", file=sys.stderr)

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
        raise SystemExit("only Bluetooth outputs can be renamed; the wired jack is named by ALSA")
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

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
