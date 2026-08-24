#!/usr/bin/env python3
"""bt-watchdog — detect a wedged Bluetooth controller and recover it unattended.

STATUS: WRITTEN, NOT YET DEPLOYED OR TESTED. Do not enable this until it has been run
against a deliberately wedged controller. See "Validation still owed" at the bottom.

WHY THIS EXISTS
---------------
E07 recorded four occurrences of the BCM43438 wedging mid-call. The H4 stream between the SoC
and the controller loses byte alignment, and nothing host-side can resynchronise it — only a
firmware reload (rung 6 of scripts/bt-reset.sh) recovers. Moving Mode 1W's output off USB
(E07 runs 11-12) made it far rarer, but "rarer" is not "gone", and the product lives in a car
where nobody can type a command.

WHY DETECTION IS THE HARD PART
------------------------------
Every passive signal lies. Measured during occurrence 4, on a controller that was already dead:

    hciconfig hci0            UP RUNNING PSCAN ISCAN      <- healthy
    HCI error counters        errors:0 both directions    <- healthy
    SCO frame flow            still transmitting, nominal <- healthy
    bridge-supervisor         both legs "verified"        <- healthy
    bluetoothctl info         Connected: yes              <- healthy

A watchdog built on any of those reports green on a dead radio. Worse, `bluetoothctl` reports
its own bookkeeping: during occurrence 3 a `disconnect` returned "Disconnection successful"
while the kernel logged `command 0x0406 tx timeout` — the command never reached the controller.

The only signal that discriminated was **issuing a command and seeing whether it is answered**.
That is what this does: `hciconfig <call adapter> version` (Read Local Version). One HCI round
trip per probe against SCO's ~133 frames/s, i.e. not a meaningful addition to UART load.

The adapter is resolved per probe from the phone's bond, never hardcoded: since 2026-08-23 there
are two controllers, and the one carrying the call is whichever holds the bond.

`Frame reassembly failed` is deliberately NOT used as a trigger: in occurrence 4 it was logged
**3 minutes 26 seconds** after the actual desync. It is a lagging indicator.

FALSE POSITIVES ARE WORSE THAN LATE DETECTION
---------------------------------------------
An unnecessary reset drops a live call and takes ~20 s to come back. So:
  - require CONSECUTIVE probe failures, not one;
  - back off exponentially after each recovery, so a genuinely broken controller cannot put
    the machine in a reset loop;
  - never act while a recovery is already in flight.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time

import btadapters

REPO = os.environ.get("BRIDGE_REPO", "/home/admin/rpi-lark-bridge")
BT_RESET = os.path.join(REPO, "scripts", "bt-reset.sh")

# Timing is derived from the acceptance bar: a wedge must self-heal within ~90 s, measured from
# the fault to call audio being back.
#
# The awkward case is a WEDGED controller (as opposed to an absent one): the probe does not fail
# fast, it BLOCKS until its timeout, because the command is accepted and simply never answered.
# So detection latency is (timeout + interval + timeout), not (interval x failures).
#
#     worst case detect = 20 + 15 + 20 = 55 s
#     bt-reset.sh       ~= 30 s   (measured: rungs 1-6 plus endpoint re-registration)
#     total             ~= 85 s   <- inside the 90 s bar
#
# The earlier defaults (30 s interval, 25 s timeout) gave 80 s detect + 30 s recovery = 110 s,
# which missed the bar. Do not raise these without redoing that arithmetic.
PROBE_INTERVAL = float(os.environ.get("BRIDGE_WD_INTERVAL", "15"))
FAILURES_TO_ACT = int(os.environ.get("BRIDGE_WD_FAILURES", "2"))
PROBE_TIMEOUT = float(os.environ.get("BRIDGE_WD_PROBE_TIMEOUT", "20"))

PHONE_MAC = os.environ.get("BRIDGE_PHONE_MAC", "5C:33:7B:CB:BF:C5")

# Which controller carries the call. Resolved from the phone's own bond rather than
# configured, because the bond already records the answer and a second source of truth is
# a second thing to get wrong. BRIDGE_CALL_ADAPTER overrides it by BD address for the
# coexistence experiment, which deliberately moves the call to the other radio.
#
# This is never `hci0`: with a dongle present, the index that carries the call is whatever
# holds the bond, and index order is not stable across boots.
CALL_ADAPTER = os.environ.get("BRIDGE_CALL_ADAPTER", "")


def call_adapter() -> btadapters.Adapter | None:
    """The controller the phone is bonded on, re-resolved every time it is needed.

    Deliberately not cached: a controller that is unbound and rebound during recovery can
    come back as a different hciX, and a stale handle would then point the next probe --
    or the next reset -- at the wrong radio. With two controllers that is not a harmless
    mistake; resetting the radio carrying the speaker link during a call would drop it.
    """
    if CALL_ADAPTER:
        return btadapters.adapter_by_address(CALL_ADAPTER)
    owner = btadapters.adapter_for_device(PHONE_MAC)
    if owner is not None:
        return owner
    # No bond visible: bluetoothd may not be up yet, or the pairing database did not
    # mount. Fall back to the onboard controller, which is where every bond has lived
    # historically and which is the one this recovery ladder was written for.
    for adapter in btadapters.adapters():
        if adapter.bus == "UART":
            return adapter
    return None
# Wait before initiating to the phone ourselves, so we do not race Android's own reconnect.
# Trap 5: the Pi should normally let Android initiate -- but measured 2026-08-17, the Pixel
# sometimes will NOT re-initiate after a Bluetooth toggle, and in a car nobody can tap it.
RECONNECT_DELAY = float(os.environ.get("BRIDGE_WD_RECONNECT_DELAY", "20"))
RECONNECT = os.environ.get("BRIDGE_WD_RECONNECT", "1") not in ("0", "false", "no")
# Bounded, because an unbounded reconnector fights the operator. If the phone was
# deliberately taken elsewhere -- Bluetooth switched off, or the call moved to another
# device -- pulling it back repeatedly is the same class of bug as e6f4139, where the
# bridge fought another app for the communication route. The budget resets only when the
# phone is actually connected, so a deliberate departure costs a few failed attempts and
# then silence.
RECONNECT_ATTEMPTS = int(os.environ.get("BRIDGE_WD_RECONNECT_ATTEMPTS", "3"))

# ---------------------------------------------------------------- the speaker
# Measured 2026-08-23: the Monoprice Boombox drops its A2DP link after a couple of minutes
# with no audio flowing, and NOTHING on the Pi paged it back. The supervisor handled it
# correctly -- the user's choice was remembered and the output fell back to the wired jack --
# but in a car that means the speaker silently leaves mid-drive and stays gone, which makes
# the whole of Mode 1 unusable however well the radios coexist.
#
# Two ways this budget differs from the phone's, both deliberate:
#
#   * More attempts. A phone has an owner who can tap it; a speaker in the boot does not, and
#     the failure is invisible until someone tries to listen.
#   * Attempts are spent on a TIMER, not on absence. A speaker that is switched off should
#     cost a few quiet retries and then silence, not a page every 15 s forever -- pages are
#     ACL traffic and E03 is explicit about what ACL traffic near an active call costs.
SPEAKER_RECONNECT = os.environ.get("BRIDGE_WD_SPEAKER_RECONNECT", "1") not in ("0", "false", "no")
SPEAKER_ATTEMPTS = int(os.environ.get("BRIDGE_WD_SPEAKER_ATTEMPTS", "5"))
SPEAKER_RETRY_SECONDS = float(os.environ.get("BRIDGE_WD_SPEAKER_RETRY", "30"))

# The supervisor is user-scoped and this watchdog is root, so the status file has to be named
# rather than derived: default_status_path() would resolve to /run/user/0 under root.
STATUS_PATH = os.environ.get("BRIDGE_WD_STATUS", "/run/user/1000/bridge-status.json")

BACKOFF_START = 60.0
BACKOFF_MAX = 900.0

log = logging.getLogger("bt-watchdog")


def controller_answers() -> bool:
    """Active liveness probe. True only if the controller ANSWERED.

    Read Local Version Information via hciconfig. On a wedged controller this returns
    "Can't read version info hci0: Connection timed out (110)" and a non-zero exit.
    """
    adapter = call_adapter()
    if adapter is None:
        # We cannot even name the controller to probe. That is our problem or bluetoothd's,
        # not evidence of a wedge, and resetting on it would be a guess with a live call as
        # the stake. Same reasoning as the missing-hciconfig case below.
        log.error("no call adapter could be resolved — treating as UNKNOWN, not failed")
        return True
    try:
        r = subprocess.run(
            ["hciconfig", adapter.hci, "version"],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT, check=False,
        )
    except subprocess.TimeoutExpired:
        log.warning("probe timed out after %.0fs", PROBE_TIMEOUT)
        return False
    except OSError as exc:
        log.error("probe could not run (%s) — treating as UNKNOWN, not failed", exc)
        # A missing hciconfig is our bug, not the controller's. Do not reset on it.
        return True
    if r.returncode != 0 or "Connection timed out" in (r.stdout + r.stderr):
        return False
    return "HCI Version" in r.stdout


def recover() -> bool:
    """Run the established recovery ladder. Returns True if it reports success."""
    if not os.path.exists(BT_RESET):
        log.error("%s not found — cannot recover", BT_RESET)
        return False
    adapter = call_adapter()
    if adapter is None:
        log.error("no call adapter could be resolved — refusing to reset a guessed radio")
        return False
    # Name the target explicitly. bt-reset.sh defaults to hci0, which stopped being a safe
    # default the moment a second controller existed: resetting the wrong radio mid-call
    # would drop the link it was not even trying to fix.
    environment = dict(os.environ, BRIDGE_HCI=adapter.hci)
    log.warning("running bt-reset.sh against %s (%s)", adapter.hci, adapter.address)
    try:
        r = subprocess.run(
            ["/bin/bash", BT_RESET], capture_output=True, text=True, timeout=300,
            check=False, env=environment,
        )
    except subprocess.SubprocessError as exc:
        log.error("bt-reset.sh failed to run: %s", exc)
        return False
    for line in (r.stdout or "").splitlines()[-8:]:
        log.info("  %s", line)
    ok = "RECOVERED" in (r.stdout or "")
    log.warning("bt-reset.sh %s", "succeeded" if ok else "did NOT report recovery")
    return ok


def phone_connected() -> bool:
    # Reads the Connected property of the phone's own D-Bus object. `bluetoothctl info`
    # resolved the MAC against whichever adapter bluetoothd last called default, which with
    # a dongle present is the one holding no bonds -- so it reported "not connected" for a
    # phone that was connected perfectly well, and the reconnect logic below acted on that.
    #
    # Deliberately only trusted for the POSITIVE case; a wedge is detected by the active
    # probe above, never by this.
    return btadapters.is_connected(PHONE_MAC)


def reconnect_phone() -> None:
    """Initiate to the phone if it has not come back on its own."""
    if phone_connected():
        log.info("phone reconnected on its own")
        return
    # Deliberately does not restate a duration: callers know how long the phone has been
    # gone, this function does not, and printing RECONNECT_DELAY here reported a number
    # that was simply wrong when called from the absence path.
    adapter = call_adapter()
    if adapter is None:
        log.error("no call adapter could be resolved — not initiating")
        return
    log.info("initiating connect to %s from %s", PHONE_MAC, adapter.hci)
    ok, detail = btadapters.connect(PHONE_MAC, adapter)
    log.info("connect %s: %s", "succeeded" if ok else "failed", detail)


def desired_speaker() -> tuple[str, str | None, str | None] | None:
    """The chosen speaker as (device address, adapter address, diagnostic hci), or None.

    Read from the supervisor's published status rather than recomputed here. The supervisor
    already resolves config defaults, the runtime override and the candidate list every poll,
    and a second implementation of that policy in the watchdog is a second thing to disagree
    with. If the supervisor is not publishing, this returns None and the watchdog does
    nothing -- which is correct: with no idea what the user wants, paging a guess is worse
    than waiting.
    """
    try:
        with open(STATUS_PATH, encoding="utf-8") as handle:
            status = json.load(handle)
    except (OSError, ValueError):
        return None
    block = status.get("output") or {}
    desired_id = block.get("desired_id")
    if not desired_id or not str(desired_id).startswith("a2dp:"):
        return None  # the wired jack needs no paging, and an unset choice is not ours to guess
    for candidate in block.get("candidates") or []:
        if candidate.get("id") == desired_id:
            if candidate.get("connected"):
                return None
            return (
                str(candidate.get("address") or ""),
                candidate.get("adapter_address"),
                candidate.get("adapter"),
            )
    # Chosen but not in the candidate list at all: the bond is gone, not merely asleep.
    return None


def reconnect_speaker(
    address: str, adapter_address: str | None, adapter_hci: str | None
) -> bool:
    """Page the chosen speaker on ITS OWN adapter, A2DP profile only."""
    adapter = (
        btadapters.adapter_by_address(adapter_address) if adapter_address else None
    )
    if adapter is None and not adapter_address and adapter_hci:
        adapter = next((a for a in btadapters.adapters() if a.hci == adapter_hci), None)
    if adapter is None:
        log.error(
            "speaker adapter unavailable (address=%s, last hci=%s)",
            adapter_address or "<legacy status>",
            adapter_hci or "<none>",
        )
        return False
    powered, detail = btadapters.power_on(adapter)
    if not powered:
        log.error("speaker adapter power recovery failed: %s", detail)
        return False
    # Idempotent, and it costs no LARKDATA write when already correct. Without it a
    # speaker can be paged successfully and then have its own reconnection refused.
    pin = btadapters.pin_to_adapter(address, adapter)
    if pin.changed:
        log.warning("speaker trust corrected: %s", ", ".join(pin.changed))
    if not pin.ok:
        log.error("speaker trust pinning failed: %s", "; ".join(pin.failures))
        return False
    ok, detail = btadapters.connect_profile(address, adapter)
    log.info("speaker connect %s: %s", "succeeded" if ok else "failed", detail)
    return ok


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("BRIDGE_LOG", "INFO").upper(),
        format="%(levelname)s %(message)s",
        stream=sys.stdout,
    )

    stopping = False

    def on_signal(signum, _frame):
        nonlocal stopping
        log.info("signal %s — shutting down", signum)
        stopping = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    adapter = call_adapter()
    log.info(
        "watching %s: probe every %.0fs, act after %d consecutive failures",
        f"{adapter.hci} ({adapter.address})" if adapter else "<unresolved call adapter>",
        PROBE_INTERVAL, FAILURES_TO_ACT,
    )
    for other in btadapters.adapters():
        if adapter is None or other.hci != adapter.hci:
            log.info("other controller present: %s %s on %s", other.hci, other.address, other.bus)

    failures = 0
    backoff = BACKOFF_START
    last_recovery = 0.0

    phone_present = False
    phone_absent_since: float | None = None
    phone_attempts = 0

    speaker_attempts = 0
    speaker_next_try = 0.0

    while not stopping:
        if controller_answers():
            if failures:
                log.info("controller answered again after %d failure(s)", failures)
            failures = 0
            # A clean probe well past the last recovery means we are stable again.
            if last_recovery and time.monotonic() - last_recovery > BACKOFF_MAX:
                backoff = BACKOFF_START

            # A healthy controller with no phone on it. This is the ordinary in-car case
            # and it is NOT a wedge: the recovery ladder below never runs, because the
            # controller is answering perfectly well. Before this branch existed,
            # reconnect_phone() was reachable only after recover(), so a phone that simply
            # went out of range was never re-established.
            #
            # Measured 2026-08-23 on the hardened card: after a power cut the Pixel did not
            # re-initiate within 130 s, while a Pi-initiated connect succeeded in 20 s.
            # In a car there is nobody to tap the phone, so the Pi has to make the call.
            if RECONNECT:
                if phone_connected():
                    if not phone_present:
                        log.info("phone present")
                    phone_present = True
                    phone_absent_since = None
                    phone_attempts = 0
                else:
                    if phone_present:
                        log.warning("phone dropped")
                        phone_present = False
                    if phone_absent_since is None:
                        phone_absent_since = time.monotonic()
                    absent = time.monotonic() - phone_absent_since
                    # Hold off briefly so we do not race Android when it *does* choose to
                    # re-initiate; only then take over.
                    if absent >= RECONNECT_DELAY and phone_attempts < RECONNECT_ATTEMPTS:
                        phone_attempts += 1
                        log.warning(
                            "phone absent %.0fs — initiating (attempt %d/%d)",
                            absent, phone_attempts, RECONNECT_ATTEMPTS,
                        )
                        reconnect_phone()

            # The speaker, independently of the phone. Deliberately outside the RECONNECT
            # branch above: losing the speaker and losing the phone are different faults with
            # different budgets, and a phone that is present must not stop the speaker being
            # recovered.
            #
            # This runs during a live call. E07 measured paging a device during active SCO as
            # its own failure mode -- but that was ONE controller, where the page and the
            # voice link competed for the same radio. Here the speaker is on hci1 and SCO is
            # on hci0, so the premise does not hold. It is nonetheless UNMEASURED, which is
            # why the retry interval is 30 s rather than the 15 s probe cadence: if a mid-call
            # page does disturb SCO, this errs towards disturbing it rarely.
            if SPEAKER_RECONNECT:
                wanted = desired_speaker()
                if wanted is None or not wanted[0]:
                    # Connected, not chosen, or no bond at all. All three mean there is
                    # nothing to page, so the budget is fresh for the next real absence.
                    if speaker_attempts:
                        log.info("chosen speaker no longer needs paging")
                    speaker_attempts = 0
                else:
                    now = time.monotonic()
                    if now >= speaker_next_try and speaker_attempts < SPEAKER_ATTEMPTS:
                        speaker_attempts += 1
                        log.warning(
                            "chosen speaker %s is absent — paging (attempt %d/%d)",
                            wanted[0], speaker_attempts, SPEAKER_ATTEMPTS,
                        )
                        if reconnect_speaker(*wanted):
                            speaker_attempts = 0
                        speaker_next_try = time.monotonic() + SPEAKER_RETRY_SECONDS
        else:
            failures += 1
            log.warning("controller did not answer (%d/%d)", failures, FAILURES_TO_ACT)

            if failures >= FAILURES_TO_ACT:
                since = time.monotonic() - last_recovery
                if last_recovery and since < backoff:
                    log.warning(
                        "wedged, but only %.0fs since last recovery (backoff %.0fs) — waiting",
                        since, backoff,
                    )
                else:
                    if recover() and RECONNECT:
                        time.sleep(RECONNECT_DELAY)
                        reconnect_phone()
                    last_recovery = time.monotonic()
                    backoff = min(backoff * 2, BACKOFF_MAX)
                    failures = 0

        time.sleep(PROBE_INTERVAL)

    log.info("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# VALIDATION STATUS — measured 2026-08-17
# ---------------------------------------
# PASSED, no call active:
#   * No false positive across `systemctl restart bluetooth` or `hciconfig hci0 down/up`.
#     Zero bt-reset invocations over both perturbations.
#   * Detection and recovery end to end: unbinding hci_uart_bcm (so hci0 vanishes) produced
#         controller did not answer (1/2)
#         controller did not answer (2/2)
#         running bt-reset.sh  ->  RECOVERED at rung 6
#     and hci0 returned UP RUNNING PSCAN ISCAN with the version probe answering.
#
# STILL OWED — needs a live call and therefore the operator:
#   * The acceptance criterion itself: after recovery, does **call audio return unaided**?
#     The phone logged `resetBluetoothSco` and fell back to its earpiece during occurrence 5,
#     so Android may not re-route on its own. If it does not, this must do more than reconnect
#     — and the fix has to work with no PC present, so `adb` is not an option.
#   * Detection latency against a REAL wedge rather than an absent adapter. An unbound driver
#     fails the probe quickly; a wedged controller blocks until PROBE_TIMEOUT. The timing above
#     assumes the latter; confirm it.
