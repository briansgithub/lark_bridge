# E14 — Converting the appliance to a read-only root, and merging the power-loss work

- **Status:** MERGE DONE. Conversion works after four fixes. **Power-cut campaign BLOCKED** by a
  fifth defect. `codex/power-loss-hardening` was **not** ready to merge as written.
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

## Defect 5 — BLOCKING, unfixed: the journal corrupts itself every boot

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
