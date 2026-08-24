#!/usr/bin/env python3
"""bridgectl -- ask the bridge what it is doing, and tell it where to play.

    bridgectl output              # list the outputs, marking live and chosen
    bridgectl output set 2        # by list index
    bridgectl output set boombox  # by name, case-insensitive substring
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
import sys
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


def do_set(args: argparse.Namespace) -> int:
    status = read_status()
    candidates = outputs_of(status)
    target = resolve_selector(args.selector, candidates)

    # Powering a speaker on is a page, and E07 measured paging a device during active SCO as
    # its own failure mode -- distinct from, and additional to, E03's coexistence problem.
    # Selecting is always allowed; only the page is gated.
    needs_connect = target["kind"] == "a2dp" and not target["present"]
    if needs_connect and args.connect:
        if _call_is_up(status) and not args.force:
            print(
                f"{target['label']} is not connected, and a call is active.\n"
                "Paging a speaker during a live call is a measured failure mode (E07), so it\n"
                "was NOT attempted. The choice is recorded and will take effect when the\n"
                "speaker connects. Use --force to page anyway.",
                file=sys.stderr,
            )
        elif btadapters is None:
            print("btadapters unavailable; recording the choice without connecting.", file=sys.stderr)
        else:
            adapter = None
            if target.get("adapter"):
                adapter = next(
                    (a for a in btadapters.adapters() if a.hci == target["adapter"]), None
                )
            ok, detail = btadapters.connect_profile(target["address"], adapter)
            print(f"connect {target['label']}: {'ok' if ok else 'failed'} ({detail})", file=sys.stderr)

    supervisor.write_desire(target["id"], source="bridgectl")
    print(f"chose {target['label']}  [{target['id']}]")
    print(f"the supervisor applies this within ~{supervisor.POLL_SECONDS:.0f}s")
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
    adapter = None
    if target.get("adapter"):
        adapter = next((a for a in btadapters.adapters() if a.hci == target["adapter"]), None)
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
