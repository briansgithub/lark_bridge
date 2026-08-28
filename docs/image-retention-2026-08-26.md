# LarkBridge image retention review — 2026-08-26

## Promoted transparent-audio image — 2026-08-28

The accepted transparent media/call appliance is release commit
`6f08cbc78911780ebba75f1c88bc7bb7d631f8bf`. Its exact install archive is
`archive/LarkBridge-bt500-aux-6f08cbc78911-20260828T054454Z.zip`, SHA-256
`ed679819305c7336ce45fe1b3df45183c6901138780054a85bc22be769b60757`.

The guarded full-card capture is
`E:\larkbridge-images\20260828T060759Z\LarkBridge-bt500-aux-6f08cbc-20260828T060759Z.img`,
exactly `16088301568` bytes, SHA-256
`d77ac682157b80406982a8d689152e4be61a5c3024c6c5f8146723ed2db65d16`. The
capture streamed `/dev/mmcblk0` with persistent storage read-only and Bluetooth/audio state
writers stopped, then restored the data filesystem and all previously active services. Before
capture, the exact installed release, fresh Pixel bond, AUX volume 0.95, media -> Discord call ->
media transition, read-only boot/lower-root mounts, and `powerloss_verify.py` `ready: true` were
confirmed. External `capture-metadata.json` beside the image records the source and partition
identity without committing unit secrets.

Independent restore/boot verification on a spare equal-or-larger card remains pending, as does
the optional repeated physical power-cut campaign. Retain all older rollback images until the
spare-card test passes.

## Previous image retained

The preceding appliance state was the deployment built from commit
`03df47e8486b99ba741d65949b83557f983d4e33` (`feat(microphone): prefer live Lark
transmitters`). Its exact install archive is:

- filename: `LarkBridge-bt500-aux-03df47e8486b-20260826T174103Z.zip`
- bytes: `5027213`
- SHA-256: `4fa9e8a73c7bc95921fd735bfb2506ce05fca5963a6a96da815d144785ea3e97`

The archive and Git commit preserve the versioned software, but they are not a bootable
card image and do not contain all per-unit configuration, Bluetooth pairing state, or SSH
identity. The required full-card capture is now complete; its independent spare-card
restore/boot test is not. The raw image remains outside Git because it is roughly 15 GiB
and contains device secrets.

Status updated 2026-08-27: **full-card capture complete and hashed**. The guarded read produced
`E:\larkbridge-images\20260827T204627Z\LarkBridge-bt500-aux-03df47e-20260827T204627Z.img`,
exactly `16088301568` bytes, SHA-256
`18349d99672237cc50f9aa28a9511f8f60b83e80f3bb821bd4b7843742019493`. External
`capture-metadata.json` beside the image records the source identity, partition table, consistency
steps, and source-Pi restoration result without putting device secrets in Git. Capture ran from
`2026-08-27T20:46:27Z` through `2026-08-27T21:09:28Z` from `/dev/mmcblk0`. The recorded partition
table is: partition 1 start `16384`, size `1048576`, type `c`; partition 2 start `1064960`, size
`28260352`, type `83`; partition 3 start `29325312`, size `2097152`, type `83`.

The capture stopped the previously active Bluetooth/watchdog/pairing and user audio services,
synced, remounted `LARKDATA` read-only, streamed `/dev/mmcblk0`, then restored `LARKDATA` read-write
and every recorded service. The boot ID remained
`7f23ba7d-2d8a-447b-a94a-4834c69d343a`; deployed-manifest and configuration hashes remained
`e57414034b3a7b04f968456ba411e7d77f280bb2018c01a2edc6b3bdb4f2f1f6` and
`73aa5203ab5cb23500e940d101fbffc2b87dcc74996e621fbc378d7c88b21361`. The supervisor returned
`CALL_DOWN`, the controller was ready, and AUX volume was verified at 0.95. The Pixel remained
paired/bonded/trusted; its first reconnect burst timed out after the deliberate Bluetooth stop,
then the connection was re-established and the watchdog returned to `device-connected` with no
error.

The operator explicitly waived a spare-card restore/boot test only as a gate for volatile,
disposable-Pi rapid development. That waiver does not apply to promotion, reboot/install
qualification, or image retirement. The test remains **pending**, and the older `c63c823` image
must still not be retired until it is eventually completed.

Do not retire the older `c63c823` image until the new image has been captured, hashed, and
successfully restored and booted at least once.

## Full-card image inventory

All observed images are exactly `16088301568` bytes (14.9834 GiB). Their hashes differ, so
there are no byte-for-byte duplicate boot images.

| Image | SHA-256 | Purpose | Decision |
|---|---|---|---|
| `E:\larkbridge-images\20260828T060759Z\LarkBridge-bt500-aux-6f08cbc-20260828T060759Z.img` | `d77ac682157b80406982a8d689152e4be61a5c3024c6c5f8146723ed2db65d16` | Guarded full-card capture of accepted transparent A2DP media / HFP call release `6f08cbc`, including the fresh Pixel bond and persistent AUX 0.95 configuration | **Keep as current candidate.** Capture/hash and source-Pi restoration passed; independent spare-card restore/boot remains pending. |
| `E:\larkbridge-images\20260827T204627Z\LarkBridge-bt500-aux-03df47e-20260827T204627Z.img` | `18349d99672237cc50f9aa28a9511f8f60b83e80f3bb821bd4b7843742019493` | Consistent full-card capture of deployed `03df47e`, including current configuration and unit identity | **Keep.** Capture/hash and source-Pi state restoration passed. Independent spare-card restore/boot remains pending; it is waived only for rapid development. |
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
