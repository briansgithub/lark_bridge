# E14 — Converting the appliance to a read-only root, and merging the power-loss work

- **Status:** MERGE DONE. Conversion works after four fixes. **Defect 5 resolved 2026-08-23** by
  moving the journal to RAM; the campaign is unblocked. `codex/power-loss-hardening` was **not**
  ready to merge as written.
- **Gates milestone:** power-loss hardening release
- **Owner / date:** Claude / 2026-08-23

## Question

`codex/power-loss-hardening` was implemented and host-tested but had never touched real storage —
its own doc said "SD conversion and physical-cut validation have not started". Does it work, and
can it merge?

## The merge was safe; the code was not

The prompt warned that power-loss carried an older supervisor and a naive merge would regress the
AEC fix. Measured rather than assumed: merge base `f0344a3`, 18 commits against 11, **29 changed
files against 61, zero overlap, zero conflict markers**. Power-loss never touched
`bridge_supervisor.py`, so the E11–E13 fixes carried through untouched.

The merge also resolved a long-standing oddity: the unit had been running its system layer from
`codex/boot-optimization` and its user layer from the AEC lineage, **a combination that existed in
no commit**. Nothing needed deploying to fix that — the merge created the commit that describes
what the unit was already running. A full drift check afterwards showed all ten tracked files
matching byte for byte.

28 tests green. Worth stating plainly: the power-loss host suites are **two tests each**, for
roughly a thousand lines of destructive storage tooling.

## Four defects, none visible from reading the code

Every one was found by running the conversion and booting the result.

### 1. parted -s refuses a shrinking resizepart

`repartition-offline-device.sh` drove the table with `parted -s ... resizepart`. `-s` is documented
as script mode, but parted 3.6 still prints "Warning: Shrinking a partition can cause data loss,
are you sure you want to continue?" and exits.

The ext4 shrink had already completed and the table edit had not, leaving a 14.47 GB filesystem
inside an unchanged 15.54 GB partition. That is the harmless direction and the card stayed
mountable — but had the order been reversed, the partition would have been cut beneath a live
filesystem. **Fixed:** drive the table with `sfdisk`, which is genuinely non-interactive.

### 2. ro on the kernel cmdline disables the overlay it configures

`configure-offline-boot.py` added `ro` to `cmdline.txt`. overlayroot reads the cmdline and, finding
`ro`, **deliberately remounts the assembled overlay read-only** — `init-bottom/overlayroot` lines
703 and 865-869, "just to be more normal". An overlayfs cannot be reconfigured afterwards; the
kernel answers `No changes allowed in reconfigure`.

So the tmpfs overlay that exists to absorb runtime writes absorbed none. `bridge-storage-guard`
died on first boot with EROFS writing `bridge.toml`, and nothing on the system could write
anywhere. **Fixed:** do not set `ro`. The card is still protected — overlayroot mounts the real
device read-only at `/media/root-ro`, verified on hardware: a write to `/` appears in the tmpfs
upper and is **absent from the card**.

### 3. Symlinking /var/lib/bluetooth kills Bluetooth entirely

`prepare-offline-root.sh` symlinked `/var/lib/bluetooth` into LARKDATA. `bluetoothd` declares
`StateDirectory=bluetooth`, and **systemd refuses to set up a StateDirectory whose path is a
symlink** — it exits `238/STATE_DIRECTORY` and the service never starts.

On a Bluetooth call bridge that means the converted appliance could not do its job at all.
**Fixed:** a real directory plus an fstab bind mount, which is what systemd expects.

### 4. systemd-remount-fs fails every boot

It exists to remount `/` per fstab, and overlayfs rejects any reconfigure, so it can only fail.
Harmless in itself, but `powerlossctl`'s pre-cut acceptance refuses *any* failed unit — so it is
not cosmetic in practice. **Fixed:** masked, since the initramfs has already assembled `/`.

## Defect 5 — RESOLVED 2026-08-23: the journal corrupts itself every boot

The appliance is permanently DEGRADED, and `powerlossctl arm` correctly refuses to cut power:

```
pre-cut acceptance failed: ['DEGRADED state has an unexpected persistent mount',
                            'systemd reports failed units']
```

`storage_guard.verify_or_reset_journal` runs `journalctl --verify` over the journal on LARKDATA and
quarantines it on failure. It fails **every boot**:

```
Data object references invalid entry at 107cf8
File corruption detected at .../system.journal:1080000 (of 8388608 bytes, 12%)
```

The corruption is real, not a false positive. The mechanism appears self-perpetuating: the guard
does `os.replace(journal, quarantine)` — renaming the directory **while journald holds it open** —
so journald keeps writing through its open descriptor into the renamed directory. Next boot the
guard finds corrupt files, quarantines them, and the cycle repeats. Observed alongside it:
**journald ends up writing nothing at all** (`journalctl --disk-usage` reports 0 B, "No journal
files were found"), so the hardened appliance has no logs.

Note the shape is the same as defect 3: `/var/log/journal` is also a **symlink** into LARKDATA.
Two of five defects come from symlinking a daemon's state directory into the persistent partition.

**This blocks the campaign.** The acceptance check is right to refuse: cutting power to a system
already reporting DEGRADED would produce results nobody could attribute.

## What was proven

The conversion, after fixes 1-4, produces a working hardened appliance. Across reboots:

| Property | Result |
|---|---|
| `/` | overlay, **rw** |
| `/media/root-ro` (the card) | **ro — never written** |
| A write to `/` | lands in tmpfs upper, **absent from the card** |
| Bluetooth | active, **Pixel pairing intact in LARKDATA** |
| Supervisor | ACTIVE, AEC verified, graph quantum 1920 |
| **Audio end to end** | downlink -20.4 dBFS, **speaker -17.9 dBFS** |
| Failed units | 0 |

**Stage 4 passed**: the read-only root does not break the audio stack. That gate mattered — a
failure discovered after a power cut would have been unattributable.

## Verdict

**Do not merge `codex/power-loss-hardening` to master as written.** It produced a non-functional
appliance in four independent ways, and a fifth defect still blocks its own validation. The merge
onto `codex/integration` is worth keeping, because the fixes live there and the branch is now much
closer to correct.

## Caveats

- **No power cut was ever performed.** Everything here is conversion and boot validation.
- Fixes 1-4 are verified on hardware; **defect 5 is diagnosed but unfixed**, and its mechanism
  (rename-under-open-fd) is inferred from behaviour, not proven by instrumenting journald.
- Fixes were applied to the card's lower filesystem by hand as well as to the scripts. A clean
  re-conversion from the fixed scripts has **not** been run end to end.
- The `x-systemd.requires-mounts-for` bind entry for BlueZ works, but no reboot-ordering stress
  was applied to it.

## Next action

Fix defect 5 — most likely by stopping journald before touching its directory, or by not
symlinking `/var/log/journal` at all — then re-run the conversion from clean scripts and start the
power-cut campaign. The rig is otherwise ready: `rig/inventory.toml` written, host-side safety
evidence created and verified, campaign initialised at
`artifacts/powerloss/campaign-20260823T140824Z`.

## Defect 5 — resolution, and a correction to the mechanism above

**The mechanism recorded above is wrong.** This document inferred rename-under-open-fd: the guard
calling `os.replace()` on the journal directory while journald held it open. Direct evidence
refutes it. journald's fd 28 resolves to inode 102, which is the *live*
`journal/<machine-id>/system.journal` — not a stale handle into the quarantined directory. The
guard's unit already orders itself `Before=systemd-journal-flush.service`, so journald has not
opened persistent storage when it runs. The corruption's cause was never established, and this
document implied more was known than was.

What *was* established, by measurement:

- The corruption is **ongoing, not inherited from the source image**. A freshly created journal
  failed `journalctl --verify` at 45% of an 8 MB file within ~90 minutes, **with no power cut ever
  performed**. The guard was correctly detecting real, recurring damage.
- An earlier reading of "journald is storing nothing at all" was a **transient state sampled inside
  the window right after the guard quarantines at boot**. Steady state was healthy and `journalctl`
  read the corrupt journal perfectly well. But records emitted during that window are genuinely
  lost — worst precisely when a post-power-cut record matters most.
- The dominant cost was never corruption but **write churn**: `rtkit-daemon` logging
  `Supervising 0 threads of 0 processes of 0 users.` at `PRIORITY=7` about three times a second,
  15374 of 15600 records in a 90 minute boot, driving 1.69 MB/min to LARKDATA while idle.
- Storage `DEGRADED` **gates nothing functional**. It is read only by `powerloss_verify.py` (which
  accepts `DEGRADED` as valid) and `rig/boot/bootctl.py`. The `DEGRADED` in `bridge_supervisor.py`
  is an unrelated enum. Calls were never affected.

**Resolution: the journal moved to RAM** (`d016aa1`), removing the failure class rather than
chasing a cause that could have been journald, ext4 under `commit=1`, or the card itself. This is
also correct on its own merits for an appliance that runs in a car: a power cut can only corrupt a
write in flight, and the safest journal is one that never touches the card.

Required alongside it: the stale journal files had to be deleted from LARKDATA. `verify_or_reset_journal`
(`pi/powerloss/storage_guard.py:124`) returns clean only when it finds no `.journal` files, so
switching to volatile alone would have left the guard re-quarantining 32 MB on every boot.

Verified on hardware after reboot:

| | Before | After |
|---|---|---|
| Storage state | `DEGRADED` every boot | **`READY`, `reasons: []`** |
| Journal location | 24 MB on LARKDATA | **RAM only**, card dir empty |
| LARKDATA written / 120 s idle | 3,375,104 B | **65,536 B** |
| LARKDATA used | ~28 MB | **632 K** |
| rootfs written | 0 B | 0 B |
| Boot / storage guard | 19.34 s / 1.616 s | 18.84 s / 1.536 s |
| Pixel pairing | intact | **intact** |
| Failed units | 0 | 0 |

**A 98% reduction in idle writes** (~2.3 GB/day to ~46 MB/day). The change is on the sealed card,
written by remounting `/media/root-ro` rw and re-sealing it, and survives reboot.

Trade accepted deliberately: **no logs survive a power cut.** For a car appliance diagnosed over
SSH while parked that is the right side of the trade, and it can be reversed by flipping `Storage`
back — the persistent sizing is retained in the drop-in, marked inert, for exactly that reason.

## Power cuts, at last — and the in-car cycle end to end (2026-08-23)

With defect 5 resolved the campaign was no longer blocked. Two physical cuts were performed by the
operator, each verified by a changed `boot_id` so a cut could not be confused with a reboot.

### Cut 1 — idle

| Check | Result |
|---|---|
| Genuine cut | `boot_id` changed |
| Unclean shutdown | `EXT4-fs (mmcblk0p2): orphan cleanup on readonly fs` |
| LARKDATA | mounted clean `r/w`, no errors |
| Storage guard | **`READY`, `reasons: []`** |
| Pairing | intact; all four slots present |
| Config slot | `a`, `pairing: live-valid` |
| Failed units | 0 |
| Back on SSH | ~30 s |

### Cut 2 — **mid-call**, the worst case

Power pulled with the call up, eSCO carrying audio, and LARKDATA live.

| Check | Result |
|---|---|
| Genuine cut | `boot_id` changed |
| LARKDATA (live at the moment power died) | **`READY`, `reasons: []`** |
| Pairing | **intact**, all four slots |
| Failed units | 0 |
| Phone reconnected | **unaided, ~20 s**, no human involvement |
| Fresh call afterwards | **works** — see below |

The fresh call is the assertion that matters. Surviving a cut is worthless if the appliance cannot
take the next call:

```
healthy: True          state: ACTIVE
aec_enabled: True      aec_verified: True
call_up: True          lark_present: True
graph_quantum: 1920    quantum_below_configured: False
bluetooth: acl True, sco True
resources: modules 57, loopback 2, fds 54, RSS 13888 KiB
```

Resource figures are identical to E13's healthy baseline, and `quantum_below_configured: False` is
the direct assertion that the crackle regression has not returned under a read-only root.

### The phone will not reconnect itself — measured, then fixed

The in-car assumption was that Android auto-connects to a trusted paired HFP device. **It does
not.** After a power cut the Pixel was observed for **130 s** with the Pi discoverable, bonded and
trusted, and it never re-initiated. A Pi-initiated connect succeeded in 20 s.

This was already predicted in `bt_watchdog.py`'s Trap 5 note from 2026-08-17, but the code never
acted on it: `reconnect_phone()` was reachable only after `recover()`, which runs only when the
*controller* stops answering. A phone that merely went out of range left the controller answering
perfectly well, so nothing ever re-established the link.

Fixed in `cd5dbd4` with a bounded reconnect on the healthy-controller path. The budget resets only
on a successful connection, so an operator who deliberately moves the call elsewhere costs three
failed attempts and then silence — deliberately avoiding the failure mode of `e6f4139`, where the
bridge fought another app for the communication route.

**The complete in-car cycle now runs with nobody in the loop:**

```
power on -> boot ~19 s -> watchdog holds 30 s -> connects -> call works
         -> power cut mid-call -> recovers -> reconnects unaided -> next call works
```

Roughly 60-70 s from power-on to phone connected.

### What these cuts do not cover

- **n=2.** Two cuts, one idle and one mid-call. No cumulative-cut testing.
- **No early-boot cut.** A cut landing a few seconds into boot, while the guard is choosing a
  config slot, is the window the design most fears and was not exercised.
- **No cut during a persistent write** to the config or pairing slots.
- **A/B slot recovery was never forced.** `config_slot: a` was valid throughout, so the fallback
  path to slot B has still never executed.
- Cuts were ~10 s off. Brownouts and rapid off/on cycles are untested.
- **No logs survive a cut**, by design now. Post-cut forensics rely on the guard's verdict and
  `invariants.py`, not the journal.
