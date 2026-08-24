# High-risk untested areas

- **Owner / date:** Claude + Codex / 2026-08-24
- **Scope:** the in-car call bridge as deployed on the hardened (read-only root) card

A consolidated list of what could still bite, ranked by risk to the in-car use case rather than by
how interesting it is. "Untested" here means *never exercised on hardware* — not "believed to work"
and not "covered by host tests".

The bar this list is written against: the appliance runs unattended in a car, with no SSH, no
screen, and nobody who can tap the phone.

---

## Tier 1 — untested code on a recovery path

These are the sharpest, because the code only ever runs when something has already gone wrong. A
latent bug here is invisible until the exact moment it is needed.

### 1. The A/B config slot fallback has never executed

The Mode 1 startup choice has now been written to slot B and validated by `lark_state.py status`,
including stable speaker/controller identities and the existing AEC settings. But the Pi has not
booted from that new slot, and choosing the older valid slot after corruption has **never run
outside host tests**. Both the new default and the fallback path remain unproven at boot.

*Test:* first boot and assert slot B materializes Mode 1. Then deliberately corrupt the newest slot,
boot, assert the guard selects the older valid slot and the appliance still takes a call.

### 2. A power cut during early boot

A cut landing 1-5 s in, while `bridge-storage-guard` is choosing a config slot and BlueZ is
restoring pairing, is the window the design most fears. Both physical cuts performed so far landed
on a fully-booted system.

*Test:* `powerlossctl` already models early-boot delays (1, 3, 5, 10, 20 s). Operator pulls power on
cue N seconds after restore.

### 3. A power cut during a persistent write

Cuts so far landed while LARKDATA was idle or carrying a call. Neither hit the moment the config or
pairing slots were actually being rewritten — which is the only moment those files are vulnerable.

*Test:* `powerlossctl`'s `persistent-write` context.

### 4. LARKDATA unmountable

If the data partition fails to mount at all, the documented behaviour is a fallback. That has never
been forced.

*Test:* point the fstab entry at a bad UUID, boot, assert the appliance still comes up and takes a
call in a degraded mode rather than failing closed.

---

## Tier 2 — known product defects, characterised but unfixed

### 5. A PipeWire or WirePlumber restart permanently kills call audio

**E13 Finding 2, reproduced and unfixed.** The Bluetooth ACL survives; the SCO voice link does not,
and nothing re-establishes it. Worse, the damage is phone-side: **Android falls back to its own
earpiece and stickily stays there**, so the call continues while bypassing the bridge entirely. The
user hears audio from the phone and has no reason to connect that to anything on the Pi.

`bluetoothctl connect` restores the ACL; restoring SCO is not automatable from the Pi, because
Android has already decided where the call belongs.

The AudioInputRouter app is now installed on the Pixel and its one-action LarkBridge route,
background operation, process restart and interrupted-mute recovery pass without a call. That is a
promising mitigation, not closure: its re-assertion has not yet been exercised against Discord
after a live audio-stack restart.

Risk in a car: any crash or restart of the audio stack mid-drive silently removes the bridge from
the call. Arguably not the supervisor's defect, but it is the product's.

### 6. The `bridge-btfw` retry budget

`30 x 0.10 s = 3 s` under `TimeoutStartSec=5` — short for a controller settling after a firmware
reload, and the most likely reason a recovery reapply would fail twice. **Deferred by the operator**;
it needs a forced firmware reload, which costs the call.

### 7. The E08 controller wedge is unexplained

Observed once, never reproduced. The churn hypothesis was tested directly and **refuted** (8
restarts at 12 s and 14 at 4 s produced no wedge). Recorded as unreproduced at n=1 rather than
attributed to the nearest available cause. A wedge in a car means no call until a power cycle.

---

## Tier 3 — gaps in what the reconnect work covers

### 8. A reconnect budget can exhaust while the Pi stays powered

The phone has 3 attempts and the speaker has a separate 5-attempt budget; each resets **only on a
successful connection**. In a car this is normally fine, because the Pi power-cycles with the
engine and a fresh boot resets the budgets.

**But if the Pi is on power that stays live with the engine off**, a phone that leaves for the day
exhausts three attempts and the Pi then never tries again — so the phone will not reconnect when the
driver returns, and there is no SSH to intervene. Untested, and the failure is silent.

*Test:* leave the Pi powered, take each peer out of range past its budget, return, and see whether
both links are re-established without intervention.

### 9. Intentional-versus-unintentional disconnects are not distinguished

The btmon HCI reason-code consumer was designed but not built; the bounded budget approximates it.
A deliberate disconnection still costs three attempts. Harmless today, but it means the Pi cannot
currently tell "drove out of range" from "user chose another device".

---

### 10. Mode 1 AEC and speaker return are not yet repeatable results

The first real-call AEC measurement passed at **13.26 dB** and the operator heard clean playback,
but the corrected synthetic pair measured **7.88 dB** and **0.01 dB**. Before a third trial, the
Boombox dropped A2DP; acoustic preflight detected no sound and correctly stopped the series. A
single good call plus a contradictory pair is not a shippable distribution.

*Test:* repeat real-call AEC captures and complete the speaker out-of-range/return sequence, always
gating each trial on sound measured back at the Lark.

### 11. The phone output selector has not yet changed hardware output

RFCOMM discovery, candidate listing and app force-stop/reconnect pass on the real Pi and Pixel.
The mutating request is unit tested through the durable CLI path, but was not sent while the
operator was away because a real Bluetooth selection is speaker-dependent and must first pass the
acoustic playback gate.

*Test:* with verified Boombox playback at the Lark, select Aux and then Boombox from the app during
a call. Assert the far end keeps hearing the Lark, the selected output changes, and the final A/B
slot records Boombox without a supervisor restart.

## Tier 4 — durability and provenance

### 12. Cumulative power cuts

**n=2.** Two cuts say nothing about drift over dozens of engine cycles, which is exactly what a car
does. Pairing survival, slot integrity and filesystem health have only been checked across two
events.

### 13. Brownouts and rapid cycling

Both cuts were clean ~10 s outages. A sagging supply or a fast off/on — the realistic behaviour of a
car's electrical system on cranking — is untested and is a harder case than a clean cut.

### 14. A clean re-conversion has never been run end to end

E14 records that fixes 1-4 were applied to the card's lower filesystem **by hand as well as to the
scripts**. So the current card is correct, but nobody has proven the fixed scripts alone reproduce
it. This matters the moment a second unit is built, or this card is ever re-imaged.

### 15. The BlueZ bind-mount ordering has had no stress

The `x-systemd.requires-mounts-for` entry works, but no reboot-ordering stress was applied. If it
ever loses the race, pairing is not where BlueZ expects it at start.

---

## Deliberately accepted, not untested

Recorded here so they are not mistaken for gaps:

- **No logs survive a power cut.** The journal is in RAM by design. Post-cut forensics rely on the
  storage guard's verdict and `invariants.py`. Reversible by flipping `Storage` in the journald
  drop-in, where the persistent sizing is retained and marked inert for that purpose.
- **~0.9 s of raw un-cancelled uplink** when the Lark appears mid-call. Measured, reduced 9.2x from
  8.19 s, bounded by the 2 s poll interval. Eliminating it needs a WirePlumber policy rule.
- **AEC efficiency work is paused** on `codex/aec-efficiency`, with the near/far-end mic coupling
  question unresolved. The deployed crackle fix is independent of it and is verified.
