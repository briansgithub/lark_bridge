# LarkBridge image retention review — 2026-08-26

## Current image to preserve

The desired appliance state is the deployment built from commit
`03df47e8486b99ba741d65949b83557f983d4e33` (`feat(microphone): prefer live Lark
transmitters`). Its exact install archive is:

- filename: `LarkBridge-bt500-aux-03df47e8486b-20260826T174103Z.zip`
- bytes: `5027213`
- SHA-256: `4fa9e8a73c7bc95921fd735bfb2506ce05fca5963a6a96da815d144785ea3e97`

The archive and Git commit preserve the versioned software, but they are not a bootable
card image and do not contain all per-unit configuration, Bluetooth pairing state, or SSH
identity. A new full-card image of the deployed Pi is therefore required. Track its
filename, byte size, SHA-256, capture time, boot ID, deployed manifest, configuration hash,
partition table, capture consistency steps, and post-capture readiness result here after
capture. Store the raw image outside Git: it is roughly 15 GiB and contains device secrets.

Status at review time: **full-card capture pending**. The Pi was not reachable by its saved
hostname, last IPv4 address, or last link-local IPv6 address, so no current image or hash is
claimed by this record.

Do not retire the older `c63c823` image until the new image has been captured, hashed, and
successfully restored and booted at least once.

## Full-card image inventory

All observed images are exactly `16088301568` bytes (14.9834 GiB). Their hashes differ, so
there are no byte-for-byte duplicate boot images.

| Image | SHA-256 | Purpose | Decision |
|---|---|---|---|
| `E:\larkbridge-source-20260823.img` | `847b4d34d112cbef497304885494780a15a5104e29ff69e1339ede306202f6bc` | Clean-shutdown, pre-hardening source state; its recovery card was physically boot-tested | **Keep permanently.** This is the documented last-resort rollback and preserves a hybrid deployed state that no Git commit fully represents. Keep `host-safety-evidence.json` and the recovery card with it. |
| `rpi_lark_mic_bridge-mode1\artifacts\phase5\source-PhysicalDrive3-0123456789ABCDE.img` | `d61a145b91498df38973806eed6bf2aa1b76dc49bc72ef8c9cb02e9795c313db` | Verified pre-mutation rollback for the unfinished dual-USB/BT600 experiment | **Keep while that experiment may resume.** If the experiment is formally abandoned, it can be retired after retaining its evidence because the production rollback above remains available. |
| `rpi_lark_mic_bridge-mode1\artifacts\phase5\candidate-preboot-PhysicalDrive3-0123456789ABCDE.img` | `c3fae02c5216f68f5179e8ff2592f67540882540deaf0922635d891f0a3a5d9e` | Mutated intermediate dual-USB/BT600 candidate from a non-production branch | **Best immediate retirement candidate.** It is not the source rollback, is not production, and its smaller evidence and Git history preserve the experiment. Reclaims 14.9834 GiB. |
| `rpi_lark_mic_bridge\artifacts\pi-images\20260825T214343Z\LarkBridge-pi3-bt500-aux-c63c823-20260825T214343Z.img` | `8ab6af1dcacdd40851e8ef9919cda8cec1ec69e786d768dfb3e7caac8a7c7dec` | Consistent hardened BT500/AUX image at commit `c63c823`; qualification was incomplete | **Keep temporarily.** Retire only after the current `03df47e` image passes capture/hash/restore/boot verification. Reclaims another 14.9834 GiB. |

Removing the BT600 candidate now and the `c63c823` image after replacement would reclaim
`32176603136` bytes (29.9668 GiB).

## Other saved artifacts

- The three tracked `archive/LarkBridge-*-Benchmark-*.zip` files are tiny benchmark logs,
  not boot images. Keep them in Git as historical evidence.
- Older install ZIPs (`64f5533`, `c63c823`, `b76e201`, `78b5d64`, `1ab2798`, and
  `5ae3d94`) are all ancestors of `03df47e`. They are hash-unique but operationally
  superseded. They may be removed if standalone rollback installers are not wanted; keep
  the current `03df47e` ZIP and checksum. The total saving is only about 24 MiB.
- `artifacts/bt500-aux-evidence-stage-20260825` is fully represented by the verified
  `LarkBridge-bt500-aux-evidence-c63c823ab9d6-20260825T213549Z.zip`. The uncompressed
  staging directory is a safe cleanup candidate, saving about 175.9 MiB, if the ZIP and
  checksum remain.
- Git tags, experiment reports, release/source archives, and compact evidence should remain
  even when a superseded raw image is retired. They preserve history at a small storage cost.

No files were deleted as part of this review.
