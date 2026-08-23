# High-risk untested areas

- **Owner / date:** Claude / 2026-08-23
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

`config_slot: a` has been valid on every single boot ever observed, so the fallback to slot B has
**never run outside host tests**. The entire point of the alternating checksummed slots is to
survive a corrupted primary, and that path is unproven on real storage.

*Test:* deliberately corrupt slot A, boot, assert the guard selects B and the appliance still takes
a call. Cheap and entirely automatable — no operator needed.

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

### 8. The reconnect budget can exhaust while the Pi stays powered

`RECONNECT_ATTEMPTS=3`, and the budget resets **only on a successful connection**. In a car this is
normally fine, because the Pi power-cycles with the engine and a fresh boot resets the budget.

**But if the Pi is on power that stays live with the engine off**, a phone that leaves for the day
exhausts three attempts and the Pi then never tries again — so the phone will not reconnect when the
driver returns, and there is no SSH to intervene. Untested, and the failure is silent.

*Test:* leave the Pi powered, take the phone out of range past the budget, return, and see whether
the link is re-established.

### 9. Intentional-versus-unintentional disconnects are not distinguished

The btmon HCI reason-code consumer was designed but not built; the bounded budget approximates it.
A deliberate disconnection still costs three attempts. Harmless today, but it means the Pi cannot
currently tell "drove out of range" from "user chose another device".

---

## Tier 4 — durability and provenance

### 10. Cumulative power cuts

**n=2.** Two cuts say nothing about drift over dozens of engine cycles, which is exactly what a car
does. Pairing survival, slot integrity and filesystem health have only been checked across two
events.

### 11. Brownouts and rapid cycling

Both cuts were clean ~10 s outages. A sagging supply or a fast off/on — the realistic behaviour of a
car's electrical system on cranking — is untested and is a harder case than a clean cut.

### 12. A clean re-conversion has never been run end to end

E14 records that fixes 1-4 were applied to the card's lower filesystem **by hand as well as to the
scripts**. So the current card is correct, but nobody has proven the fixed scripts alone reproduce
it. This matters the moment a second unit is built, or this card is ever re-imaged.

### 13. The BlueZ bind-mount ordering has had no stress

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
