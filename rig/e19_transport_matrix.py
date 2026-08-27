#!/usr/bin/env python3
"""Drive and capture the E19 phone-transport matrix. Runs on the control PC.

THIS ONE IS ACTIVE, AND THAT IS THE POINT
------------------------------------------
`rig/e16_live_capture.py` says of itself: "deliberately passive: it starts read-only Pi/Android
monitors, records before/after snapshots, and never sends a Bluetooth command or Android input
event." That was right for E16, which measured a baseline. E19 measures *transitions* -- what
happens to the A2DP stream when a communication transport opens, and whether an idle phone ever
opens one at all -- and a transition cannot be observed without causing it. So this script sends
Android input events and Bluetooth commands, and the operator is asked for a physical action only
where no programmatic trigger exists.

WHAT IT MUTATES, AND HOW IT PUTS IT BACK
-----------------------------------------
The appliance does not advertise A2DP Sink; `bluez5.roles` omits `a2dp_sink` deliberately, so
the Pixel cannot present media to it. Measuring the media path therefore requires enabling that
role, which is a real change to a deployed appliance living on the persistence overlay -- it
survives a reboot. Three guards, in order of how much they are trusted:

  1. every exit path reverts, including exceptions and Ctrl-C (`finally`);
  2. a **deadman** job on the Pi restores the file and restarts WirePlumber after a timeout
     regardless of what happens to this process or the network -- because a dead control host
     must not be able to strand the appliance off-baseline;
  3. both apply and revert verify by SHA-256 against the recorded deployed hash, and refuse to
     proceed on a mismatch rather than guessing.

    python rig/e19_transport_matrix.py --seconds 40
    python rig/e19_transport_matrix.py --no-spike        # HFP/idle states only, no mutation
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RESULTS = REPO / "docs" / "experiments" / "results" / "E19"

PI_HOST = os.environ.get("E19_PI_HOST", "larkbridge")
PHONE_MAC = os.environ.get("E19_PHONE_MAC", "5C:33:7B:CB:BF:C5")

CONF = "/home/admin/.config/wireplumber/wireplumber.conf.d/50-bridge-bluez.conf"
BACKUP = "/tmp/e19-conf.orig"
DEADMAN_CANCEL = "/tmp/e19-deadman-cancel"
# The deployed content of 50-bridge-bluez.conf. Recorded 2026-08-26 from the running
# appliance at commit 03df47e. A mismatch means the appliance is not what this script was
# written against, and it refuses rather than editing something it does not recognise.
BASELINE_SHA = "9024a5ac8e3c463bdf7316d8f46c5251882432fc8c7a8703871e5bfdf1467b34"
BASE_ROLES = "  bluez5.roles = [ a2dp_source hfp_hf hsp_hs ]"
SPIKE_ROLES = "  bluez5.roles = [ a2dp_sink a2dp_source hfp_hf hsp_hs ]"

A2DP_SINK_UUID_FRAGMENT = "0000110b"

ADB_CANDIDATES = (
    Path(os.environ.get("ANDROID_HOME", "")) / "platform-tools" / "adb.exe",
    Path(os.environ.get("ANDROID_SDK_ROOT", "")) / "platform-tools" / "adb.exe",
    Path.home() / "AppData" / "Local" / "Android" / "Sdk" / "platform-tools" / "adb.exe",
    Path("adb"),
)

KEY_MEDIA_PLAY = "126"
KEY_MEDIA_PAUSE = "127"
KEY_VOICE_ASSIST = "231"


def stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def locate_adb() -> str:
    for candidate in ADB_CANDIDATES:
        if candidate.name == "adb":
            from shutil import which

            found = which("adb")
            if found:
                return found
            continue
        if candidate.is_file():
            return str(candidate)
    raise SystemExit("adb not found - set ANDROID_SDK_ROOT or run: rig setup-adb")


class Rig:
    """Thin transport layer. Everything that touches hardware goes through here."""

    def __init__(self, adb_path: str, verbose: bool = True) -> None:
        self.adb_path = adb_path
        self.verbose = verbose

    def _run(self, cmd: list[str], timeout: float) -> str:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
            return proc.stdout
        except subprocess.TimeoutExpired:
            return "<TIMEOUT>"
        except OSError as exc:
            return f"<ERROR {exc}>"

    def pi(self, script: str, timeout: float = 45.0) -> str:
        return self._run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", PI_HOST, script], timeout
        )

    def adb(self, *args: str, timeout: float = 45.0) -> str:
        return self._run([self.adb_path, *args], timeout)

    def shell(self, command: str, timeout: float = 45.0) -> str:
        return self.adb("shell", command, timeout=timeout).replace("\r", "")

    def say(self, message: str) -> None:
        if self.verbose:
            print(message, flush=True)


# --------------------------------------------------------------------------- spike control


def conf_sha(rig: Rig) -> str:
    return rig.pi(f"sha256sum {CONF} | cut -d' ' -f1").strip()


def arm_deadman(rig: Rig, seconds: int) -> None:
    """Restore the baseline even if this process or the network dies."""
    rig.pi(f"rm -f {DEADMAN_CANCEL}")
    script = (
        f"nohup setsid bash -c 'for i in $(seq {seconds}); do "
        f"[ -f {DEADMAN_CANCEL} ] && exit 0; sleep 1; done; "
        f"sudo cp -a {BACKUP} {CONF}; "
        f"XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart wireplumber' "
        f">/dev/null 2>&1 < /dev/null &"
    )
    rig.pi(script)
    rig.say(f"  deadman armed: baseline restored unconditionally in {seconds}s")


def cancel_deadman(rig: Rig) -> None:
    rig.pi(f"touch {DEADMAN_CANCEL}")


def apply_spike(rig: Rig, deadman_seconds: int) -> None:
    got = conf_sha(rig)
    if got != BASELINE_SHA:
        raise SystemExit(
            f"refusing to edit: {CONF} is sha {got}, expected the deployed {BASELINE_SHA}"
        )
    rig.say("  guard: config matches deployed baseline")
    rig.pi(f"sudo cp -a {CONF} {BACKUP}")
    arm_deadman(rig, deadman_seconds)
    rig.pi(f"sudo sed -i 's|^  bluez5.roles = .*|{SPIKE_ROLES}|' {CONF}")
    rig.pi("XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart wireplumber; sleep 6")
    uuids = rig.pi("bluetoothctl show | grep 'UUID: Audio'")
    if A2DP_SINK_UUID_FRAGMENT not in uuids:
        raise SystemExit(f"a2dp_sink role did not take effect; adapter shows:\n{uuids}")
    rig.say("  spike applied: adapter now advertises Audio Sink 0000110b")


def revert_spike(rig: Rig) -> dict:
    rig.say("  reverting to deployed baseline ...")
    rig.pi(f"sudo cp -a {BACKUP} {CONF}")
    rig.pi("XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart wireplumber; sleep 6")
    cancel_deadman(rig)
    got = conf_sha(rig)
    uuids = rig.pi("bluetoothctl show | grep 'UUID: Audio'")
    roles = rig.pi(f"grep 'bluez5.roles = ' {CONF}").strip()
    supervisor = rig.pi(
        'python3 -c "import json;print(json.load('
        "open('/run/user/1000/bridge-status.json'))['state'])\""
    ).strip()
    result = {
        "sha256": got,
        "sha256_matches_baseline": got == BASELINE_SHA,
        "audio_sink_absent": A2DP_SINK_UUID_FRAGMENT not in uuids,
        "roles_line": roles,
        "supervisor_state": supervisor,
    }
    ok = result["sha256_matches_baseline"] and result["audio_sink_absent"]
    rig.say(f"  revert {'VERIFIED' if ok else 'FAILED -- INVESTIGATE'}: {json.dumps(result)}")
    return result


# --------------------------------------------------------------------------- observation


def snapshot(rig: Rig, out_dir: Path, label: str) -> dict:
    """One complete observation of both sides. Raw files on disk, summary returned."""
    target = out_dir / label
    target.mkdir(parents=True, exist_ok=True)

    graph = rig.pi(
        "export XDG_RUNTIME_DIR=/run/user/1000; "
        f"python3 /tmp/transport_trace.py --mac {PHONE_MAC} --seconds 0.2 "
        "--interval 0.1 --out /dev/stdout"
    )
    (target / "graph.jsonl").write_text(graph, encoding="utf-8")

    links = rig.pi("export XDG_RUNTIME_DIR=/run/user/1000; pw-link -l")
    (target / "pw-link.txt").write_text(links, encoding="utf-8")

    supervisor = rig.pi("cat /run/user/1000/bridge-status.json")
    (target / "supervisor.json").write_text(supervisor, encoding="utf-8")

    dumpsys = rig.shell("dumpsys audio")
    (target / "dumpsys-audio.txt").write_text(dumpsys, encoding="utf-8")

    android: dict = {}
    parser = REPO / "rig" / "analysis" / "audio_state.py"
    try:
        proc = subprocess.run(
            [sys.executable, str(parser)],
            input=dumpsys,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        android = json.loads(proc.stdout or "{}")
    except (subprocess.SubprocessError, json.JSONDecodeError):
        android = {"PARSE_FAILED": True}
    (target / "android.json").write_text(json.dumps(android, indent=2), encoding="utf-8")

    first_graph: dict = {}
    for line in graph.splitlines():
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if candidate.get("t") != "END":
            first_graph = candidate
            break

    try:
        supervisor_state = json.loads(supervisor)["state"]
    except (json.JSONDecodeError, KeyError, TypeError):
        supervisor_state = None

    summary = {
        "label": label,
        "t": datetime.now(UTC).isoformat(),
        "supervisor_state": supervisor_state,
        "card_profile": first_graph.get("card_profile"),
        "nodes": first_graph.get("nodes", {}),
        "transports": first_graph.get("transports", {}),
        "phone_link_count": len(first_graph.get("links", [])),
        "android_mode": android.get("audio_mode_actual"),
        "android_mode_owner": android.get("audio_mode_owner"),
        "android_sco_state": android.get("sco_audio_state"),
        "android_comm_device": android.get("active_communication_device"),
    }
    (target / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    rig.say(
        f"    [{label}] sup={summary['supervisor_state']} "
        f"nodes={list(summary['nodes'])} links={summary['phone_link_count']} "
        f"mode={summary['android_mode']} sco={summary['android_sco_state']}"
    )
    return summary


def hfp_present(summary: dict) -> bool:
    """Android has opened a microphone transport iff the HFP nodes exist. See E01."""
    return any("headset-audio-gateway" in v for v in summary.get("nodes", {}).values())


def foreground(rig: Rig) -> str:
    """Which activity is actually on screen.

    Without this, a trigger that silently failed to launch is indistinguishable from one that
    launched and was declined by Android -- and only the second is evidence about the audio
    policy. The first is evidence about adb.
    """
    raw = rig.shell(
        "dumpsys activity activities | grep -m1 -E 'topResumedActivity|mResumedActivity'"
    )
    return raw.strip() or "<unknown>"


# --------------------------------------------------------------------------- the matrix


def bt_connect(rig: Rig) -> None:
    rig.pi(f"bluetoothctl connect {PHONE_MAC}; sleep 8")


def bt_disconnect(rig: Rig) -> None:
    rig.pi(f"bluetoothctl disconnect {PHONE_MAC}; sleep 5")


def media(rig: Rig, key: str) -> None:
    rig.shell(f"input keyevent {key}")
    rig.pi("sleep 6")


MIC_TRIGGERS = (
    ("voice-assist-key", lambda rig: rig.shell(f"input keyevent {KEY_VOICE_ASSIST}")),
    (
        "recognize-speech-intent",
        lambda rig: rig.shell("am start -a android.speech.action.RECOGNIZE_SPEECH"),
    ),
    (
        "pi-transport-acquire",
        lambda rig: rig.pi(
            "for p in $(busctl --system tree org.bluez --list | grep 'dev_"
            + PHONE_MAC.replace(":", "_")
            + "' | grep /fd); do "
            "busctl --system call org.bluez $p org.bluez.MediaTransport1 Acquire; done"
        ),
    ),
)


def try_mic_triggers(rig: Rig, out_dir: Path, results: list) -> str | None:
    """Attempt each programmatic route to a microphone transport; record what happened.

    Records the outcome of every attempt rather than stopping at the first success, because a
    trigger that fails is evidence too -- it is the negative that the contract's exclusions
    rest on.
    """
    winner = None
    # A dozing screen makes every UI trigger a no-op, which would masquerade as "Android
    # refused to open a transport" when in fact nothing was ever asked. Wake first, and record
    # the wakefulness alongside the result so the negative can be trusted.
    rig.shell("input keyevent 224")  # WAKEUP
    rig.pi("sleep 2")
    wakefulness = rig.shell("dumpsys power | grep -m1 mWakefulness=").strip()
    rig.say(f"  screen: {wakefulness or '<unknown>'}")
    before = foreground(rig)
    for name, action in MIC_TRIGGERS:
        rig.say(f"  mic trigger: {name}")
        output = ""
        try:
            output = action(rig) or ""
        except Exception as exc:  # noqa: BLE001 - a failed trigger is a datum, not a crash
            output = f"<raised {exc}>"
            rig.say(f"    trigger raised: {exc}")
        rig.pi("sleep 6")
        after = foreground(rig)
        summary = snapshot(rig, out_dir, f"05-mic-trigger-{name}")
        summary["trigger_output"] = output.strip()[:2000]
        summary["foreground_before"] = before
        summary["foreground_after"] = after
        summary["trigger_changed_foreground"] = after != before
        summary["trigger_opened_transport"] = hfp_present(summary)
        # snapshot() has already written summary.json; rewrite it so the per-state file and
        # the manifest agree. Without this the trigger diagnostics live only in the manifest,
        # which is exactly where nobody looks when reading one state's evidence.
        (out_dir / summary["label"] / "summary.json").write_text(
            json.dumps(summary, indent=2), encoding="utf-8"
        )
        results.append(summary)
        rig.say(
            f"    fired={'yes' if summary['trigger_changed_foreground'] else 'no visible change'}"
            f" transport={'OPENED' if summary['trigger_opened_transport'] else 'none'}"
        )
        if summary["trigger_opened_transport"] and winner is None:
            winner = name
            rig.say(f"    *** {name} OPENED a microphone transport ***")
        rig.shell("input keyevent 4")  # BACK, dismiss whatever the trigger opened
        rig.pi("sleep 3")
        before = foreground(rig)
    return winner


def run_matrix(rig: Rig, out_dir: Path, use_spike: bool) -> list[dict]:
    results: list[dict] = []

    results.append(snapshot(rig, out_dir, "00-start"))

    bt_disconnect(rig)
    results.append(snapshot(rig, out_dir, "01-phone-disconnected"))

    bt_connect(rig)
    results.append(snapshot(rig, out_dir, "02-connected-idle"))

    if use_spike:
        media(rig, KEY_MEDIA_PLAY)
        results.append(snapshot(rig, out_dir, "03-media-playing"))

        media(rig, KEY_MEDIA_PAUSE)
        results.append(snapshot(rig, out_dir, "04-media-paused"))

        media(rig, KEY_MEDIA_PLAY)
        results.append(snapshot(rig, out_dir, "04b-media-resumed"))

    winner = try_mic_triggers(rig, out_dir, results)

    if winner:
        results.append(snapshot(rig, out_dir, "06-transport-open"))
        rig.pi("sleep 4")
        results.append(snapshot(rig, out_dir, "07-transport-settled"))

    bt_disconnect(rig)
    results.append(snapshot(rig, out_dir, "08-bt-disconnected"))
    bt_connect(rig)
    results.append(snapshot(rig, out_dir, "09-bt-reconnected"))

    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=40.0, help="trace window per phase")
    parser.add_argument("--no-spike", action="store_true", help="do not enable a2dp_sink")
    parser.add_argument("--deadman", type=int, default=900, help="unconditional revert timeout")
    parser.add_argument("--label", default="transport-matrix")
    args = parser.parse_args(argv)

    rig = Rig(locate_adb())
    out_dir = RESULTS / f"{stamp()}-{args.label}"
    out_dir.mkdir(parents=True, exist_ok=True)
    rig.say(f"results -> {out_dir}")

    tracer = REPO / "rig" / "pi" / "measure" / "transport_trace.py"
    subprocess.run(
        ["ssh", "-o", "BatchMode=yes", PI_HOST, "cat > /tmp/transport_trace.py"],
        input=tracer.read_text(encoding="utf-8"),
        text=True,
        timeout=60,
        check=False,
    )

    use_spike = not args.no_spike
    manifest: dict = {
        "started": datetime.now(UTC).isoformat(),
        "phone_mac": PHONE_MAC,
        "pi_host": PI_HOST,
        "spike_applied": use_spike,
        "baseline_sha_expected": BASELINE_SHA,
    }

    try:
        if use_spike:
            rig.say("applying a2dp_sink spike ...")
            apply_spike(rig, args.deadman)
        manifest["states"] = run_matrix(rig, out_dir, use_spike)
    finally:
        if use_spike:
            manifest["revert"] = revert_spike(rig)
        else:
            cancel_deadman(rig)
        manifest["finished"] = datetime.now(UTC).isoformat()
        (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        rig.say(f"manifest -> {out_dir / 'manifest.json'}")

    revert = manifest.get("revert") or {}
    if use_spike and not revert.get("sha256_matches_baseline"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
