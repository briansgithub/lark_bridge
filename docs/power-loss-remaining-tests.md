# Power-loss hardening: what still needs testing

Status: E14 converted the card and fixed four defects; a fifth blocks the cut campaign.
Date: 2026-08-23

## Measured on 2026-08-23 — the premise holds, but the exposure window does not

Write sectors over 60 s **idle**, from `/sys/block/mmcblk0/*/stat`:

| Partition | Written in 60 s |
|---|---|
| `rootfs` (p2) | **0 bytes** |
| `LARKDATA` (p3) | **1,945,600 bytes** |

The read-only root works exactly as designed and had never been directly verified: the
card is genuinely not written during normal operation.

**But LARKDATA takes ~1.9 MB/min while idle** — roughly 114 MB/hour, 2.7 GB/day, on a
1 GiB partition. Two consequences, and the second is the important one:

1. Flash wear on a partition sized for bounded state.
2. **You can only be corrupted while writing.** Constant background writes maximise the
   very exposure window the hardening exists to shrink.

Almost certainly the journal churn of E14 defect 5, which raises that defect from
"blocks the campaign" to "actively undermines the design".

## Still untested

- **Cumulative cuts.** `powerlossctl` performs one cut per context; corruption usually
  appears after N, not one.
- **The A/B slot mechanism.** Deliberately corrupt config slot A and confirm recovery
  from slot B. This is the design's core safety claim and nothing has exercised it.
- **LARKDATA unmountable.** The design says it falls back to a RAM overlay and reports
  DEGRADED. Never provoked.
- **Pairing survival across N cuts** — the failure a user would actually notice.
- **Write volume during an active call**, as opposed to idle, so the exposure window is
  known for the state that matters.

## Order

Fix defect 5 first. It blocks `powerlossctl arm` (pre-cut acceptance correctly refuses a
DEGRADED system) and it is the likely source of the idle write churn, so fixing it should
be visible as a drop in the LARKDATA figure above. Re-measure to confirm before cutting
power to anything.

## Update 2026-08-23 — idle write churn found and cut by 78%

The idle write figure recorded above was investigated rather than left as a note.

**Correction to the first measurement.** A 20 s sample suggested ~500 KB/min and I briefly
reported the original figure as an overstatement. That was wrong: the writes are bursty, and a
120 s sample gives **1.69 MB/min**, corroborating the original ~1.9 MB/min. Short windows are not
trustworthy here. The `rootfs` figure of **0 bytes** has now been confirmed three times, so the
read-only root premise genuinely holds.

**Cause.** `rtkit-daemon` emits `Supervising 0 threads of 0 processes of 0 users.` at `PRIORITY=7`
about three times a second. It accounted for **15374 of 15600 records** in a 90 minute boot --
98.6% of all logging -- with the unit idle and no call up.

The configured rate limiter never applied: `RateLimitBurst=2000` per 30 s is far above rtkit's
~90 records per 30 s, so every record passed through untouched.

**Why this mattered more than card wear.** A power cut can only corrupt a write that is in flight.
Continuous background writing maximises the exact exposure window this hardening exists to shrink,
so the appliance was working against its own design goal while doing nothing.

**Fix** (`993dd0e`): `MaxLevelStore=info` in the journald drop-in, dropping debug records for every
unit rather than special-casing rtkit.

| Over 120 s idle | Before | After |
|---|---:|---:|
| LARKDATA written | 3,375,104 B | **753,664 B** |
| Journal records | 177 | **6** |
| rootfs written | 0 B | 0 B |

**78% reduction** (~2.3 GB/day to ~0.5 GB/day). Verified by emitting paired debug/info markers:
the debug record was dropped, the info record survived. The residual is not log content -- at 6
records per 120 s it is ext4 committing its own journal every second under `commit=1`, which is a
deliberate hardening trade and not waste.

Applied to the running Pi through the tmpfs overlay, so it reverts on reboot and is **not yet on
the card**. It reaches the card only through the normal install path.

## Defect 5 is narrower and worse than recorded

Two corrections to the earlier characterisation.

**Logging is not dead.** An earlier read showed journald storing nothing at all. That was a
transient state sampled inside the window right after the guard quarantines the journal at boot.
Steady state is healthy: 19.7 MB of journals and fresh records landing immediately.

**But records emitted during that window are silently lost.** A marker logged during it never
appeared, while an identical marker minutes later did. Small, but it means the moments just after
boot -- exactly when a post-power-cut forensic record matters most -- are the moments least likely
to be recorded.

**The corruption is ongoing, not inherited.** This is the sharp finding. `journalctl --verify`
fails on the *freshly created* journal:

```
File corruption detected at .../system.journal:3792088 (of 8388608 bytes, 45%)
```

The guard quarantines a corrupt journal at boot, journald creates a clean one, and that clean one
is corrupt again within ~90 minutes -- **with no power cut having been performed at any point**.
So this is not damage carried in the source image, and the appliance re-enters `DEGRADED` every
boot by correctly detecting real corruption.

Cause unknown. The write-volume fix reduces rotation pressure on 8 MB files but does not explain
corruption appearing without power loss; that points at journald, ext4 under `commit=1`, or the
card itself, and those are three different products. `Seal=yes` is configured and untested as a
factor.

**This still blocks the power-cut campaign, and now for a stronger reason than "arm refuses":** a
campaign whose forensic record is a journal that corrupts itself unprompted cannot distinguish
damage caused by a power cut from damage that was going to happen anyway.
