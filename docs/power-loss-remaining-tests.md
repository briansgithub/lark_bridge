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
