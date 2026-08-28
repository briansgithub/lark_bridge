# E19 — Can the appliance carry Pixel media and microphone audio transparently, not only during calls?

- **Status:** In progress — rapid live media acceptance and the Discord communication-transport
  checkpoint are complete; rows 4/7, echo-suppression/far-end acoustics, soak, and promotion
  remain open
- **Resolves risk:** phone audio is usable only inside a call; media playback is not deterministically routed to the selected output
- **Gates milestone:** transparent phone audio release
- **Owner / date:** Claude (runtime model `claude-opus-5`), 2026-08-26

## Question

Can the LarkBridge appliance present the operator-selected microphone to a Pixel **while the phone
is idle** — outside a call — and simultaneously route the phone's A2DP media playback to the
configured aux output, without regressing the existing Lark/FIFINE selection, liveness, hotplug,
AEC, and break-before-make guarantees?

The question has three separable parts, and they must not be conflated:

1. Does phone **media** (A2DP) reach the configured output deterministically?
2. Does the phone accept a **microphone** during a call (HFP/SCO)? — believed yes today.
3. Does the phone accept a **microphone while idle**, with no call and no communication-mode
   application? — **unknown, and the crux of this experiment.**

## Why it cannot be answered by reading

Whether Android exposes a Bluetooth microphone transport outside a call is decided by the Android
audio policy on the handset, not by BlueZ, PipeWire, or this repository. BlueZ can report an HFP
profile as *connected* while Android never opens an SCO transport, so repository source and even
PipeWire graph state can show a microphone "selected" that the phone is not consuming. Only live
observation of the Pixel across its real states can distinguish an available transport from a
merely-advertised profile.

## User objective

Phone audio should pass through the appliance transparently in both directions: media and
notification audio out to the aux speaker, and the selected microphone available to the phone —
ideally at all times, not only when a call is in progress.

## Baseline and deployed state

| Item | Value |
|---|---|
| Development baseline | `3c119c572f5998103bc89425980f11c40c96f7da` (`docs(image): record retention review`) |
| Baseline branch | `codex/fifine-k054-compat` |
| Deployed commit on the Pi | `03df47e8486b99ba741d65949b83557f983d4e33` (`feat(microphone): prefer live Lark transmitters`) |
| Deployed branch | `codex/lark-transmitter-liveness` |
| Deployed profile | `pi3-usb-bt500-aux`, 498 manifest entries |
| Deployed archive SHA-256 | `4fa9e8a73c7bc95921fd735bfb2506ce05fca5963a6a96da815d144785ea3e97` |

`03df47e` is an ancestor of the baseline, and the baseline carries the image-retention record, so
the working tree is a strict superset of what is deployed.

Current implementation branch `codex/transparent-phone-audio` inherits the BT500 relocation fix
`7fbde86`, adds guarded capture support in `551c04c`/`a9ab9b3`, the first rapid runner checkpoint in
`7e8c649`, policy/CLI/installer integration in `9ec30d7`, and the reviewed phone transport state
machine in `b9e1b4f`. The rapid-runner follow-up and live candidate IDs are recorded below when
accepted; none of these commits changes `/etc/larkbridge/DEPLOYED.json` during volatile staging.

## Protected existing behavior

The following must survive every checkpoint unchanged. Any of them regressing is a stop condition,
not a trade-off:

- Lark transmitter **liveness** detection — a receiver present with no live transmitter is not
  eligible.
- **Lark-first, FIFINE-second** priority ordering.
- Hotplug switching, including same-name replug generation changes.
- AEC, with post-AEC `bridge.mic` ownership.
- **Break-before-make** routing.
- Fail-closed handling of ambiguous microphone identity.
- No direct physical-microphone-to-phone bypass path.

## Workspace

| Path | Role |
|---|---|
| `B:\Desktop\W\Hardware_write\rpi_lark_mic_bridge-transparent-phone-audio` | **Work location** for all 13 checkpoints; branch `codex/transparent-phone-audio` |
| `B:\Desktop\W\Hardware_write\rpi_lark_mic_bridge` | Primary checkout — holds unrelated dirty E18 evidence. **Do not touch.** |
| `B:\Desktop\W\Hardware_write\rpi_lark_mic_bridge-fifine-k054` | FIFINE worktree — holds unrelated dirty E18 evidence. **Do not touch.** |
| `...\rpi_lark_mic_bridge\.claude\worktrees\e19-transparent-phone-audio-68f529` | Harness session worktree at `59c7f89` (= `master`). **Not the work location**; it predates the baseline and lacks E15–E18 and the retention record. |

The untouchable E18 evidence is, verbatim: modified `docs/experiments/E18-fifine-k054-compat.md`
and `rig/bt500_aux/microphone_hotplug.py`, plus untracked
`docs/experiments/results/E18/field/hotplug-20260826T145218Z-e8b78232a230/` and
`docs/experiments/results/E18/field/hotplug-20260826T152038Z-823abcf04751/`.

Raw disk images and Bluetooth pairing credentials must never enter Git.

## Bluetooth feasibility gate

The requested "microphone always available" behavior may be impossible on Android. These are
treated as open gates, not assumptions, until Step 3 measures them:

- Android may expose **no** Bluetooth microphone transport outside a call or a communication-mode
  application. A selected microphone on the Pi is **not** evidence that Android is consuming it.
- A2DP and HFP may both remain *connected* while only one may be *active*, forcing a transport
  switch rather than true full duplex.
- Persistent SCO, if achievable at all, may satisfy microphone availability only by degrading media
  to narrowband and displacing A2DP.
- Android — not the Pi — decides whether paused media resumes and where ringtones and some
  notifications play. The Pi restoring a route is not the same as the phone resuming playback.
- Continuous full-duplex playback may require a different AEC reference path than the call path.

If exact transparent full-duplex Bluetooth proves impossible, Step 4 must present the alternatives
(A2DP + call-time microphone, persistent SCO, USB Audio Class, or a phone-side application) and
**stop at `DECISION_REQUIRED`**. No later checkpoint may infer that choice.

## Image-preservation requirement

The original gate allowed only Step 11 to mutate the Pi after preservation. On 2026-08-27 the
full card was captured and hashed, and the source Pi's mount/services were restored, as recorded in
[`docs/image-retention-2026-08-26.md`](../image-retention-2026-08-26.md). The operator then
explicitly designated this Pi a disposable rapid-development target and waived the spare-card
restore/boot test as a development gate. Volatile live candidates are therefore authorized before
promotion. A reachable unit is reconciled with `session-stop`; a unit wedged after a persistent
policy trial is reflashed. Reboot alone is not claimed to restore a coherent baseline because a
policy file can survive while the volatile supervisor override does not.

The preservation procedure was:

- Capture a **consistent full-card raw image** — quiesce audio, Bluetooth, watchdog and persistence
  writers, `sync`, remount `LARKDATA` read-only, stream the whole card, and restore read-write and
  the services through a guaranteed cleanup path.
- Hash the complete image (SHA-256) and record size, source device, partition layout, boot ID,
  deployed manifest, configuration hash, stopped services, and restoration status.
- Store the image **outside Git** (~15 GiB, contains device secrets) and update
  [`docs/image-retention-2026-08-26.md`](../image-retention-2026-08-26.md).
- **Delete no older image.** In particular the `c63c823` image may not be retired until the new
  image is eventually restored to a spare card and booted, even though that test no longer blocks
  development.

## Checkpoints

| # | Checkpoint | Status |
|---|---|---|
| 1 | Establish the isolated workspace | **COMPLETE** |
| 2 | Static Bluetooth and audio architecture audit | **COMPLETE** |
| 3 | Live Pixel and Pi capability characterization | **MEASURED 2026-08-27** — rows 3 and 8 confirmed; row 4 open; row 7 supported but not proven |
| 4 | Feasibility verdict and approved contract | **COMPLETE** — contract approved (provisional), ADR-0009 |
| 5 | Encode the routing contract as tests | **COMPLETE** — historical red contract, now green |
| 6 | Implement deterministic idle media routing | **IMPLEMENTED LOCALLY** — `b9e1b4f` |
| 7 | Implement safe A2DP/HFP transitions | **IMPLEMENTED LOCALLY** — `b9e1b4f`; live HFP classifier correction `7235233` |
| 8 | Integrate the approved idle microphone behavior | **IMPLEMENTED LOCALLY** — honest idle status; idle route remains unsupported |
| 9 | Status, CLI, policy, installer, and documentation | **IMPLEMENTED LOCALLY** — `9ec30d7`, docs in progress |
| 10 | Independent local regression and safety review | **COMPLETE FOR THIS CHECKPOINT** — 120 runner tests (1 host-capability skip), 330 bridge tests plus 39 subtests, Ruff, Black, and diff check pass |
| 11 | Preserve the baseline image and deploy | **PRESERVATION COMPLETE; PRODUCTION DEPLOYMENT NOT STARTED** |
| 12 | Abbreviated live acceptance test | **IN PROGRESS** — media and Discord transport/graph checkpoints measured; acoustic and promotion gates remain |
| 13 | Final audit and release verdict | Not started |

## Step 1 — Establish the isolated workspace

### Deviation from the written checkpoint

Step 1 required verifying that branch `codex/transparent-phone-audio` and the sibling worktree did
**not** already exist, and stopping if either did. **Both existed.** They were inspected rather than
reused, and found to be a pre-created but never-used workspace:

- `git log --oneline codex/fifine-k054-compat..codex/transparent-phone-audio` — empty (no unique
  commits).
- `git -C <path> status --porcelain --ignored=matching --untracked-files=all` — empty (no tracked,
  untracked, or ignored files; no `rig/inventory.toml`).
- `git reflog show codex/transparent-phone-audio` — a single entry,
  `branch: Created from codex/fifine-k054-compat`. The ref had never moved.
- Both refs resolved to the identical SHA `3c119c572f5998103bc89425980f11c40c96f7da`.

The operator was shown this evidence and directed that the stale branch and worktree be **removed
and re-created fresh** from the resolved baseline, so that Step 1 executes as written and leaves no
stale artifact. That authorization is the sole basis for the deletion below.

`git branch -d` refused with *"not fully merged"*. That refusal was an artifact of comparing against
the harness session's `HEAD` (`59c7f89`, which is an *ancestor* of the baseline), not evidence of
unique work: the branch pointed at the same commit as `codex/fifine-k054-compat`, which still
carries it. Deletion was therefore performed with `-D` behind an explicit SHA-equality guard.

A second deviation is recorded for transparency: the branch keeps the `codex/` prefix at the
operator's direction, although commit `43aba28` established that Claude-authored work belongs on
`claude/` branches. The work in this experiment is Claude-authored.

### Commands and results

| Command | Result |
|---|---|
| `git worktree remove B:/.../rpi_lark_mic_bridge-transparent-phone-audio` | removed; path no longer exists |
| `git branch -d codex/transparent-phone-audio` | **refused** — "not fully merged" (see above) |
| SHA-equality guard on both refs | passed — both `3c119c5...` |
| `git branch -D codex/transparent-phone-audio` | `Deleted branch codex/transparent-phone-audio (was 3c119c5)` |
| `git worktree add -b codex/transparent-phone-audio B:/.../rpi_lark_mic_bridge-transparent-phone-audio 3c119c5` | created; 499 files checked out |
| `git -C <new> rev-parse HEAD` | `3c119c572f5998103bc89425980f11c40c96f7da` |
| `git -C <new> merge-base --is-ancestor 03df47e HEAD` | exit 0 — deployed commit is an ancestor |
| `docs/image-retention-2026-08-26.md`, `E18-fifine-k054-compat.md`, `TEMPLATE.md` present in new worktree | all present |
| `ssh -o BatchMode=yes -o ConnectTimeout=10 larkbridge 'hostname; uptime'` | `larkbridge`, up 42 min, load 0.00 |
| `ssh larkbridge 'cat /etc/larkbridge/DEPLOYED.json'` | `commit 03df47e...`, branch `codex/lark-transmitter-liveness`, profile `pi3-usb-bt500-aux`, 498 manifest entries, archive SHA-256 `4fa9e8a7...` |
| FIFINE worktree `git status --porcelain` before and after | identical — 2 modified, 2 untracked directories; evidence untouched |

No production code, no Pi state, and no configuration were changed by this checkpoint. The only
Pi access was read-only SSH.

### Decisions

- Work proceeds in the re-created sibling worktree on `codex/transparent-phone-audio` at `3c119c5`.
- The harness session worktree is documentation-only for this experiment and is not the work
  location.
- The stale workspace was deleted under explicit operator authorization and proven-lossless guards.

### Findings that change later checkpoints

- **The Pi is reachable.** `docs/image-retention-2026-08-26.md` records that the Pi was not
  reachable by its saved hostname, last IPv4 address, or last link-local IPv6 address, so that
  record claims no current image or hash. That status is now stale: `larkbridge` answered over SSH
  on 2026-08-26 and reports the expected deployed commit. The retention record is **not** edited
  here — Step 11 owns it, and Step 11 must correct that status line when it captures the image. The
  plan's "Pi was unreachable" gate no longer blocks Step 3.
- **`rig/inventory.toml` is absent** from this worktree and from every sibling except the primary
  checkout. Step 3 must create it from `rig/inventory.toml.example` before any rig-driven live
  capture. It is gitignored, uses LF endings, and must not be converted to CRLF. Prefer the stable
  `pi_host` over the arrangement-specific `pi_ip`.
- `docs/architecture/decisions/` ends at **ADR-0008**, so Step 4 creates **ADR-0009**.

### Remaining risks

- The feasibility gate above is entirely unmeasured. Nothing in this checkpoint constitutes
  evidence that idle-state microphone transparency is achievable.
- Live Pi work in Steps 3, 11 and 12 depends on continued reachability and on the operator being
  present for physical phone interaction.

### Next action

**Step 2 — static Bluetooth and audio architecture audit.** Read-only inspection of BlueZ
configuration and advertised roles, PipeWire/WirePlumber Bluetooth policy, HFP call detection and
graph construction, A2DP handling, output selection, watchdogs and reconnection, across
`bridge_supervisor.py`, `microphones.py`, `outputs.py`, status generation, CLI, systemd units and
the release installer. Produce an architecture map and a requested-versus-possible capability
matrix. No implementation changes.

## Step 2 — Static Bluetooth and audio architecture audit

Read-only. No repository file outside this document was modified, and no Pi state was changed —
Pi access was limited to `bluetoothctl show/info/devices`, `hciconfig -a`, `wpctl status`, and
`cat` of the deployed configuration.

### Architecture map — the signal path as deployed at `03df47e`

One radio, one phone, one microphone at a time:

```
                          hci0 = ASUS USB-BT500 (Realtek, HCI 5.4, A0:AD:9F:73:6C:24)
                          the ONLY controller present; onboard BCM43438 is absent
                                        |
  Pixel 7a 5C:33:7B:CB:BF:C5            |   advertises to the phone:
  advertises to us:                     |     0000110a Audio Source   (we send to speakers)
    0000111f Handsfree AG   <-----------+     0000111e Handsfree      (we are the HF unit)
    00001112 Headset AG                 |     00001108 Headset        (HSP fallback)
    0000110a Audio Source  --- X --->   |     0000110e / 0000110c AVRCP
    (nothing here can receive it)       |     *** NO 0000110b Audio Sink ***
                                        v
                       WirePlumber bluez monitor, roles = [ a2dp_source hfp_hf hsp_hs ]
                                        |
                   ONLY while a call is up, and only then:
                     bluez_output.5C_33_7B_CB_BF_C5.1   profile headset-audio-gateway  (far end in)
                     bluez_input.5C_33_7B_CB_BF_C5.0                                   (uplink out)
                                        |
                                        v
                        bridge_supervisor.CallGraph (owns every link)
                                        |
        selected mic --> echo-cancel-capture --> bridge.aec.source --> loopback --> bluez_input
        bluez_output --> loopback --> echo-cancel-playback --> aux sink
                                        |
                                        v
               wired:alsa_output.platform-3f00b840.mailbox.stereo-fallback  @ 0.85
```

Microphones present and configured: `lark-a1` (Hollyland, `3547:0407`) then `fifine-k054`
(`0c76:161e`), both S16LE/48 kHz, `capture_only = true`. Both were live in `wpctl status`
during this audit.

**There is no phone-to-Pi media path of any kind.** The diagram has no edge for it because the
code, the roles, and the advertised UUIDs all lack one.

### The six determinations

**1. Does LarkBridge advertise A2DP Sink? No — and the omission is deliberate.**

`pi/wireplumber/wireplumber.conf.d/50-bridge-bluez.conf` sets
`bluez5.roles = [ a2dp_source hfp_hf hsp_hs ]`, with the comment: omitting `a2dp_sink` "stops
Android from pushing media at us, so the Pixel can only ever see us as a communication headset."

Verified live rather than inferred. `bluetoothctl show A0:AD:9F:73:6C:24` lists Audio Source
`0000110a`, Headset `00001108`, Handsfree `0000111e`, AVRCP `0000110e`/`0000110c` — and **no
Audio Sink `0000110b`**. `hciconfig -a` agrees: `Class: 0x680408`, `Service Classes: Capturing,
Audio, Telephony` — no *Rendering*, which is the bit an A2DP sink role would set.

PipeWire's documented default role set **includes** `a2dp_sink`. This is therefore a deliberate
removal from a working default, not a platform limitation.

**2. How do the phone's HFP and A2DP profiles appear in PipeWire?**

Only HFP appears, and only during a call: `bluez_output.<MAC>.1` and `bluez_input.<MAC>.0`, with
`api.bluez5.profile = "headset-audio-gateway"`. The naming is computed, not discovered —
`Settings.hfp_sink` / `Settings.hfp_source` build both strings from `phone_mac`.

The Pixel *does* advertise `0000110a` Audio Source, so it is capable of pushing media; there is
simply no local sink role for it to land on. `outputs.py` then uses the A2DP Sink UUID in the
opposite direction — to **exclude** the phone from the output candidate list, because the phone
also owns a `bluez_output.*` node mid-call and routing the far end back to the phone would be a
feedback loop. Measured there: "the Pixel reports `a2dp_sink=False`, every speaker reports
`True`". Enabling `a2dp_sink` on our side does not change the Pixel's own advertised UUIDs, so
that discriminator remains valid — but this must be re-checked in Step 10.

**3. Does the implementation assume only call-time phone audio? Yes, structurally.**

- Module docstring: "HFP endpoints exist only during a call, so every call-specific stream is
  built transactionally and torn down when an endpoint disappears. There is deliberately no
  default-device fallback."
- `CallGraph.tick()` opens with
  `call_up = self.settings.hfp_sink in nodes and self.settings.hfp_source in nodes`. When
  `call_up` is false it sets `State.CALL_DOWN` and returns, having done nothing but hold the aux
  sink at its configured volume.
- `CallGraph.status()` exposes exactly one call fact, `call.hfp_nodes_present`, and nulls
  `endpoints.hfp_source` / `endpoints.hfp_sink` whenever the call is down.
- The systemd unit says the same: "Creates the Mode 1W audio path only while a call is active."

There is no media, A2DP-stream, or idle-transport concept anywhere in the supervisor. Every
structure E19 needs is additive.

**4. Can one Bluetooth device expose connected A2DP and HFP concurrently?** Three layers, three
different answers, and they must not be merged:

- **BlueZ:** yes. A device can hold multiple profile UUIDs connected at once.
  `btadapters.connect_profile()` exists precisely because plain `Device1.Connect()` "brings up
  everything the remote offers" — measured on the Monoprice Boombox, which advertises Handsfree
  alongside A2DP Sink.
- **PipeWire:** expected **no**, for *active* profiles. PipeWire documents `device.profile` as
  "The initial active profile name", with "the default is to start from the 'Off' profile and
  then let session manager select the best profile based on its policy" — a single active
  profile per card, chosen by the session manager. The repo's own
  `bluetooth.autoswitch-to-headset-profile = false` exists only because a *switch* is otherwise
  required between A2DP and headset profiles. So `a2dp-sink` and `headset-audio-gateway` on the
  **same device** are expected to be mutually exclusive when active, even while both UUIDs are
  connected. **This is not yet measured for the Pixel. Step 3 must confirm it.**
- **Radio (measured, but a different topology):** E03 Step B is a clean negative control —
  "A2DP and HFP coexist perfectly with no call active", two ACL links, pips clean. The failure
  is specific to **active SCO**: SCO starves the ACL/AVDTP path, AVDTP signalling times out, and
  *our own* bluetoothd sends the Disconnect. Measured A2DP survival under SCO was 7 s to ~120 s
  across two different speakers.

  The caveat matters: E03 held A2DP and HFP against **two different peers** (speaker + phone).
  E19 needs **one peer** holding both. That configuration is untested here. Worse, E15's
  mitigation — split the profiles across two controllers — **cannot apply**, because both
  profiles belong to the same peer, only one controller is present, and
  `call_role_acceptance()` deliberately fails closed if the Pixel is seen connected on more than
  one controller.

**5. Which routes are automatic versus supervisor-owned?**

| Route | Owner | Mechanism |
|---|---|---|
| Every call link (mic->AEC, AEC->uplink, downlink->AEC->output) | **Supervisor** | `CallGraph` builds, validates, and tears down transactionally |
| Phone HFP node autoconnect | **Suppressed** | `65-bridge-hfp-no-autolink.conf` sets `node.autoconnect = false` for `api.bluez5.profile = "headset-audio-gateway"` only |
| **Everything else, including any future phone A2DP node** | **WirePlumber default policy** | That file states plainly: "A2DP and non-Bluetooth nodes retain their normal policy" |
| Profile auto-switching | Disabled | `bluetooth.autoswitch-to-headset-profile = false` |
| Device reconnection | **BlueZ + watchdog** | `AutoEnable=true`, `ReconnectAttempts=7`, exponential intervals; `connect_after_idle_seconds = 60`; `bt_watchdog` owns call-controller liveness |
| Remote volume keys | Automatic | `bluez5.enable-hw-volume = true` |

This is the single most dangerous finding for Step 6. The no-autolink rule is scoped to
`headset-audio-gateway` **only**, so the moment `a2dp_sink` is enabled the phone's media node
falls under WirePlumber's default policy and will be autoconnected to whatever the default sink
is — outside supervisor ownership. The supervisor unit already records the consequence of that
class of mistake: "a persistent loopback aimed at a transient target gets silently re-linked to
the DEFAULT device, closing an acoustic feedback loop." Step 6's requirement to not depend on
default-device state therefore requires a **new no-autolink rule covering the phone's
`a2dp-sink` profile**, not merely careful linking.

**6. What Android controls that the Pi cannot.**

- Whether a SCO / communication audio transport is opened at all. This is the crux of E19 and it
  is not ours to decide.
- Which application holds the microphone, and whether it requests communication audio.
- Whether paused media resumes after call teardown.
- Where ringtones, notifications, and assistant audio are routed.
- The per-device **"Phone calls"** and **"Media audio"** toggles in Android's Bluetooth settings.
  `Class = 0x000408` (Audio/Video, Hands-free) was chosen specifically to make the "Phone calls"
  toggle appear; a "Media audio" toggle should only appear once we advertise A2DP Sink, and the
  user can still switch it off.
- AVRCP absolute volume, and final acceptance of codec negotiation (mSBC vs CVSD; A2DP SBC
  parameters).

### Requested versus possible

| Requested behavior | Mechanism it needs | State today | Assessment |
|---|---|---|---|
| Phone media plays through aux | `a2dp_sink` role on the Pi, plus deterministic routing | Absent by design | **Plausible.** Needs the role restored, a new no-autolink rule, and supervisor-owned routing. Confirm live in Step 3. |
| Notification / second-app audio through aux | Same A2DP stream | Absent | Same as above. Android decides which streams it sends over Bluetooth. |
| Microphone to phone **during a call** | HFP HF + SCO, AEC | **Works today** | Must be preserved unchanged. |
| Microphone to phone **while idle** | Android opening a transport with no call in progress | No mechanism exists | **UNKNOWN — this is the gate.** Nothing in the repo or in the platform config decides it; only Step 3 can. |
| Media **and** microphone simultaneously (true full duplex) | `a2dp-sink` and SCO active at once on one device, one radio | No mechanism exists | **Doubtful.** PipeWire profile exclusivity points one way; E03's SCO-starves-AVDTP mechanism points the same way. Treat as unlikely until measured. |
| Ordinary recorder / camera / assistant uses the bridge mic | Android selecting a BT SCO input for a non-communication app | No mechanism exists | **Doubtful.** Outside Pi control entirely. |

### Consequences for later checkpoints

- **Step 3** must measure, at minimum: whether enabling `a2dp_sink` makes the Pixel present an
  A2DP stream; whether `a2dp-sink` and `headset-audio-gateway` can be *active* together on the
  one device; and whether any idle-state Android app opens a microphone transport.
- **Step 4** must not treat "media to aux" and "idle microphone" as one decision. The first looks
  achievable; the second is unproven. A contract that ships the first while honestly excluding
  the second is a legitimate outcome.
- **Step 4 option 4 is cheaper than it looks.** An Android companion app already exists in this
  project — E16 records `codex/larkbridge-control` with `BridgeOutputController` speaking JSON
  over RFCOMM (UUID `6e0e6e72-3f13-4f7e-9d3f-87b6f5a43c11`) for output selection. If a phone-side
  communication session is the approved route, there is an existing app to extend rather than a
  new one to build.
- **Steps 5 and 6** must add a no-autolink rule for the phone's `a2dp-sink` profile before any
  media routing is trusted.
- **Step 9** gets one break: `install_release.py` installs and tracks
  `pi/wireplumber/wireplumber.conf.d/*` by glob (both the copy at ~line 443 and the tracked-path
  set at ~line 219), so a **new** `.conf` file in that directory is already covered by the
  transactional preimage and rollback. Changing role configuration is a content edit to an
  already-tracked file, which is likewise covered.
- **Risk to flag now:** restoring `a2dp_sink` re-opens exactly what the current config was
  written to prevent. The comment "stops Android from pushing media at us" was a deliberate
  simplification. Step 10 must confirm the phone cannot become an output candidate, cannot
  become an unintended AEC reference, and cannot receive its own far-end audio back.

### External claims and sourcing

Internet access was available and used only for platform documentation.

- PipeWire `pipewire-props(7)` supplied the supported `bluez5.roles` values, the default role set
  (which includes `a2dp_sink`), and the `device.profile` semantics quoted above.
  <https://docs.pipewire.org/page_man_pipewire-props_7.html>
- The ArchWiki Bluetooth headset page was unreachable (Anubis interstitial); no content from it
  is relied upon.
- No Android primary documentation was consulted. The idle-microphone question is empirical and
  handset-specific, and Step 3 measures it directly rather than arguing it from documentation.
- Every other claim in this section is sourced to this repository, to E03/E15/E16, or to
  read-only live inspection recorded above.

### Status

Step 2 **COMPLETE**. No implementation changes were made.

**Next action — Step 3: live Pixel and Pi capability characterization.** Capture the eight
required states, with `rig/inventory.toml` created first from `rig/inventory.toml.example`.
The two measurements that decide the project are (a) whether `a2dp-sink` and
`headset-audio-gateway` can be active concurrently on the Pixel, and (b) whether any idle-state
Android application opens a microphone transport. Transient profile changes are permitted and
must be reverted; no code deployment and no persistent configuration edits.

## Step 3 — Historical pre-handset state: `BLOCKED_HARDWARE`, with a prediction

> ### This section is PREDICTION, not measurement.
>
> The Pixel 7a was not physically present on 2026-08-26, so the eight-state live matrix Step 3
> requires **was not run**. Everything below marked *predicted* is derived from platform
> documentation and from phone-free measurement on the Pi. **No later checkpoint may cite a
> prediction as evidence.** Step 12 cannot pass, and Step 13 cannot return `PASS`, on the
> strength of this section. Where a prediction is later contradicted by measurement, the
> measurement wins and this section must be corrected rather than defended.

The operator directed that work proceed as far as it can without the handset, and that the live
matrix be run later. Step 3 therefore stays open. What follows is (a) what *was* measured on the
Pi without the phone, (b) the predicted matrix with the command that will confirm or refute each
row, and (c) one architectural option the original plan did not list.

### 3a. Measured without the phone — this part is real evidence

Read-only SSH inspection of `larkbridge`, deployed commit `03df47e`:

| Property | Observed |
|---|---|
| Controllers | exactly one: `hci0`, ASUS USB-BT500, Realtek, `A0:AD:9F:73:6C:24`, HCI/LMP 5.4 |
| Onboard BCM43438 | absent from `hciconfig`/`btmgmt` — not merely unused |
| Adapter advertised UUIDs | `0000110a` Audio Source, `00001108` Headset, `0000111e` Handsfree, `0000110e`/`0000110c` AVRCP. **No `0000110b` Audio Sink** |
| `btmgmt info` supported settings | `... br/edr le ... cis-central cis-peripheral iso-broadcaster sync-receiver` |
| `btmgmt info` current settings | `powered connectable discoverable ssp br/edr le secure-conn cis-central cis-peripheral iso-broadcaster sync-receiver` |
| Stack versions | BlueZ 5.82, PipeWire 1.4.2, WirePlumber 1.4.2 |
| Pixel bond | `5C:33:7B:CB:BF:C5`, paired/bonded/trusted, **Connected: no** |
| Pixel advertised UUIDs | `0000110a` Audio Source, `0000111f` Handsfree AG, `00001112` Headset AG, AVRCP, **`00001855` Telephony and Media Audio (LE Audio TMAP)** |
| Microphones live | Lark `3547:0407` and FIFINE `0c76:161e`, both present in `wpctl status` |
| Output | `alsa_output.platform-3f00b840.mailbox.stereo-fallback` (aux) at volume 0.85 |

**The BT500 supports LE isochronous channels, and LE is currently enabled.** `cis-central`,
`cis-peripheral`, `iso-broadcaster` and `sync-receiver` are all in *current* settings. This is a
measured hardware capability, not a prediction, and it matters — see 3d.

#### Defect found in passing: `pi/bluez/main.conf.d/10-bridge.conf` is inert

Not an E19 change, recorded because it was discovered by this audit and it changes what the
platform actually is:

- `strings /usr/libexec/bluetooth/bluetoothd | grep main.conf` yields only `%*s/main.conf`.
  There is no `main.conf.d` path string in the binary, and the Debian changelog never mentions a
  drop-in directory.
- `/etc/bluetooth/main.conf` contains none of the project's keys — `ControllerMode` appears only
  as the commented default `#ControllerMode = dual` — and there is no fenced block.
- The config header promises a fallback: "If this BlueZ build does not read main.conf.d,
  `pi/bluez/apply-main-conf.sh` merges these keys into `/etc/bluetooth/main.conf` inside a fenced
  block instead." **That script does not exist.** `pi/bluez/` contains only `main.conf.d/`.
- Corroborating symptoms: the adapter `Name` is `larkbridge` (the hostname), not the configured
  `Name = LarkBridge`; and `le` is enabled despite `ControllerMode = bredr`.

So `ControllerMode`, `Name`, `FastConnectable`, `AutoEnable`, `ReconnectAttempts = 7` and
`ReconnectIntervals` are all **not in effect**. The BlueZ-level reconnect backstop the config
claims to provide does not exist; the application-level `connect_after_idle_seconds = 60` in
`config/bridge.toml` is unaffected and does work.

One contrary data point, deliberately not explained away: the observed class `0x680408` *does*
match the configured `Class = 0x000408`. The likeliest reconciliation is that BlueZ derives the
class from the registered audio profiles and independently arrives at "Audio/Video, Hands-free" —
but that is unconfirmed, and is the check that would settle this.

**Consequence for E19:** Step 9 must not assume a BlueZ `main.conf.d` change will apply. Anything
E19 needs at the BlueZ level requires a working mechanism first. Conversely, the A2DP Sink role
is owned by **WirePlumber**, not BlueZ, so the role change E19 needs is unaffected by this defect.

### 3b. Predicted eight-state matrix

Predicted only. The right-hand column is the check that will confirm or refute each row when the
handset returns.

| # | State | Predicted observation | Confidence | Confirm with |
|---|---|---|---|---|
| 1 | Paired, disconnected | `Connected: no`; no `bluez_*` nodes; supervisor `CALL_DOWN` | **Certain** — observed today | `bluetoothctl info`, `wpctl status` |
| 2 | Connected, idle | HFP AG profile connects; **no** audio nodes until a transport opens; supervisor stays `CALL_DOWN` | High | `bluetoothctl info` UUID/Connected, `pw-dump` node list |
| 3 | A2DP media playing | **Nothing today** — we advertise no Audio Sink, so the Pixel has no "Media audio" target. After enabling `a2dp_sink`: a `bluez_input.<MAC>.*` stream with `api.bluez5.profile = a2dp-sink` | High for both halves | `pw-dump`, `bluetoothctl info` after role change |
| 4 | Notification / second app | Same A2DP stream as #3; Android decides which streams it sends | Medium — Android policy, per-stream | `pw-dump` link activity while a notification fires |
| 5 | HFP call active | `bluez_output.<MAC>.1` + `bluez_input.<MAC>.0`, profile `headset-audio-gateway`, SCO up, AEC built, exactly one uplink | **Certain** — this is deployed, working behaviour | supervisor status JSON, `pw-link -l` |
| 6 | Immediately after teardown | Both HFP nodes vanish; supervisor tears down transactionally and returns to `CALL_DOWN` | **Certain** — this is deployed behaviour | supervisor status JSON |
| 7 | Ordinary recorder app, no call | **No SCO. No microphone transport. The Pi sees nothing.** | High — see 3c | `bluetoothctl info`, `pw-dump`, HCI trace showing no SCO |
| 8 | App explicitly using communication audio | SCO opens exactly as in #5; the bridge microphone is consumed | High | same as #5 |

Row 7 is the one that decides the project, and the predicted answer is **no**.

### 3c. The three decision questions, answered from documentation

**Q1 — Does phone media reach aux?** Predicted **yes, once `a2dp_sink` is enabled**, and no
before that. The Pixel already advertises `0000110a` Audio Source; the Pi advertises no Audio
Sink; PipeWire's documented default role set includes `a2dp_sink`, so this is a restoration of a
default rather than new development. *Confidence: high.* Residual risk is routing determinism,
not availability — see the Step 2 finding that a phone A2DP node would fall under WirePlumber's
default policy.

**Q2 — Microphone during a call?** **Yes — deployed and working.** Not a prediction.

**Q3 — Microphone while idle, no call, no communication app?** Predicted **no, over classic
HFP/SCO**, and the reasoning is primary-source rather than inferential.

AOSP's *Audio Managed SCO rearchitecture* states the conditions for a SCO session:

> "The AHAL starts and suspends the SCO session only if the following conditions are met: An
> active stream is patched to a SCO device. The audio mode is set, and a patch to a SCO device
> exists."

and enumerates the initiators — telecom calls (after `phoneStateChanged`), VoIP (the framework
calls `BluetoothHeadset.startScoUsingVirtualVoiceCall`), and voice recognition (the Bluetooth
stack requests SCO through `setCommunicationDevice`). Every one is an explicit communication use
case. There is no ambient or persistent SCO state.

The API history says the same thing from the other side: `startBluetoothSco()` /
`stopBluetoothSco()` are deprecated in favour of `setCommunicationDevice(AudioDeviceInfo)`, which
Android documents as selecting "the audio device that should be used for communication use cases,
for instance voice or video calls". From Android 13 (API 33) apps **must** migrate to it. A
microphone transport is something an application asks for; it is not a property of being
connected.

This is the difference the E19 question turns on, and it is exactly the trap named at the top of
this document: **the Pi can select a microphone, hold it ready, and report it — and Android will
still not consume it** until an app opens a communication session.

**Q4 — Media and microphone simultaneously (true full duplex)?** Predicted **no**, and now with
*two independent* mechanisms rather than one:

1. **Android side (new, primary source).** The same AOSP page: "The audio framework prevents an
   A2DP device from having a concurrent patch when these specific criteria are satisfied." The
   phone itself will not hold A2DP and SCO patched at once.
2. **Pi side (Step 2).** PipeWire models a BlueZ device as a card with one active profile,
   selected by the session manager; `a2dp-sink` and `headset-audio-gateway` are mutually
   exclusive as *active* profiles.

E03's radio-level finding — SCO starves AVDTP until bluetoothd disconnects A2DP — is a *third*
mechanism, but it was measured against two different peers and does not transfer directly to the
one-peer case. It does not need to: the two mechanisms above are sufficient and more direct.

Note this **reduces** the risk E03 raised rather than adding to it. If Android refuses the
concurrent patch, the appliance never reaches the state where SCO and A2DP compete for the radio
on the same link — the phone arbitrates before the radio has to.

### 3d. An option the plan did not list: LE Audio

Step 4's four alternatives omit the one path documentation actually points at for a non-call
microphone.

Android's Bluetooth **audio recording** guidance describes an ordinary `AudioRecord` selecting a
Bluetooth input directly — find an `AudioDeviceInfo.TYPE_BLE_HEADSET` among
`getDevices(GET_DEVICES_INPUTS)` and call `recorder.setPreferredDevice(bleInputDevice)` — and
states that "Bluetooth audio input can use the latest AudioManager API for nearly all use cases,
excluding phone calls". That is the inverse of the SCO situation: LE Audio input is available to
ordinary recording, and it is *calls* that are the special case.

What is already true on this hardware:

- The BT500 reports `cis-central`, `cis-peripheral`, `iso-broadcaster`, `sync-receiver` in
  **current** settings — measured today, not assumed.
- LE is enabled in practice, because `ControllerMode = bredr` is inert (3a).
- The Pixel advertises `00001855` Telephony and Media Audio, the LE Audio TMAP UUID.
- PipeWire 1.4.2 supports `bap_sink` / `bap_source` roles; the project simply does not enable
  them.

What is unknown and would have to be established: BlueZ 5.82 and PipeWire 1.4.2 LE Audio
maturity on this platform; whether the Pixel will actually select LEA for a peripheral of this
class; ISO throughput and codec negotiation on a Realtek dongle; and how LE Audio interacts with
the single-radio constraint that already governs SCO and A2DP.

This is speculative and should not be promised. But it is the **only documented Bluetooth route
to an idle-state microphone for a normal Android application**, the hardware is not the obstacle,
and Step 4 should carry it as a fifth option rather than leaving it unstated.

### 3e. Android version caveat

The Pixel 7a is eligible for **Android 17**, which began rolling out in June 2026; today is
2026-08-26, so the handset is on Android 16 or 17 and this was not verifiable without it. The
*Audio Managed SCO rearchitecture* page describes the Android 17 model, in which the audio
framework rather than the Bluetooth stack initiates SCO. The **conclusion** in Q3 holds across
both models — SCO follows a communication use case either way — but the mechanism and the exact
initiator differ. Record `getprop ro.build.version.release` and the build number as the first
action of the live run.

### 3f. What this does and does not authorize

- It does **not** satisfy Step 3. The eight-state live matrix remains required.
- Step 12 cannot pass, and Step 13 cannot return `PASS`, on predictions.
- Step 4 **may** proceed to a *provisional* contract on this basis if the operator accepts
  clearly-labelled predicted evidence, provided live confirmation of rows 3, 4, 7 and 8 is a hard
  gate before the Step 11 deployment.
- Steps 5–10 are largely unblocked either way: the test contract, the media-routing
  implementation, the transition state machine, and the status/CLI surfaces all follow from the
  contract, not from the handset.

### 3g. Sources

Platform documentation, retrieved 2026-08-26:

- *Audio Managed SCO rearchitecture*, Android Open Source Project —
  <https://source.android.com/docs/core/audio/sco-audio-mgmt>
- *Audio recording* (Bluetooth / LE Audio), Android Developers —
  <https://developer.android.com/develop/connectivity/bluetooth/ble-audio/audio-recording>
- *Audio routing API updates in Android 14 for VoIP apps*, Android Developers —
  <https://developer.android.com/develop/connectivity/telecom/voip-app/api-updates>
- *pipewire-props(7)*, PipeWire — <https://docs.pipewire.org/page_man_pipewire-props_7.html>
- Pixel update eligibility — <https://support.google.com/pixelphone/answer/4457705>

Everything in 3a is read-only measurement on `larkbridge` recorded above. No Pi state was
changed; no profile was switched; no configuration was edited.

### Historical status (superseded by the resumed measurement below)

Step 3 **`BLOCKED_HARDWARE`** — deferred pending the Pixel 7a. The predicted characterization
above is complete and is explicitly not a substitute for it.

**Next action — Step 4: feasibility verdict and approved contract.** Step 4 must present the
operator with the transport options, now including LE Audio, and stop at `DECISION_REQUIRED`.
Any contract approved on predicted evidence must be marked provisional and must carry live
confirmation of matrix rows 3, 4, 7 and 8 as a gate before Step 11.

## Step 4 — Feasibility verdict and approved contract

### Verdict on the request as asked

**The universal idle-state microphone is not achievable over classic Bluetooth.** Android opens a
SCO transport only for a communication use case; the appliance cannot manufacture one from its
side. This is a property of the handset's audio policy, not a defect in the appliance.

**Transparent full duplex — media and microphone at the same time — is not achievable either.**
The surviving reason is measured Android behavior: the Pixel withdraws its A2DP stream while SCO
owns communication audio. The earlier PipeWire-profile argument was refuted by the combined
`audio-gateway` profile measured below.

The six capabilities were assessed separately, because treating them as one question is the main
way this project could have shipped a false success:

| Capability | Verdict | Evidence class |
|---|---|---|
| A2DP media from phone to fixed AUX | **Measured; implementation validation in progress** | E19 `a2dp-source` and decoy-target trial |
| Microphone during a cellular call | **Works today** | Deployed and proven |
| Microphone during an app-created communication session | **Works today** | **Measured** — E01, `com.discord` owning `MODE_IN_COMMUNICATION`, `active comm device: bt_sco_hs larkbridge` |
| Persistent SCO/HFP outside any session | **Rejected** | Contradicted by AOSP; never demonstrated; would displace media and cap it at 16 kHz |
| Ordinary recorder / camera / plain-app microphone | **Not achievable over classic HFP** | AOSP primary documentation, plus the E01 mechanism |
| USB Audio Class | **Not achievable on this hardware** | Pi 3B `dwc_otg` is host-only; ADR-0005 / E05 route UAC through a separate Pico |
| LE Audio (BAP) ordinary-app microphone | **Unknown; hardware capable** | `cis-central`/`iso-broadcaster` measured in current settings; Pixel advertises TMAP `0x1855`; BlueZ/PipeWire maturity unproven |

**The appliance already owns transport evidence.** E01 concluded that "the HFP card materialises
only once an SCO transport exists". Raw presence of the configured phone's HFP nodes is therefore
reported as Android microphone-transport truth. Accepted controller binding and a verified
AEC/uplink graph are separate facts; the implementation does not collapse all three into the old
`call_up` boolean.

### Decision

Recorded as
[`ADR-0009`](../architecture/decisions/ADR-0009-phone-transport-media-plus-communication-mic.md).
The operator was presented with the full option set — the recommended contract, a phone-app
microphone trigger, an LE Audio probe first, and stopping for USB Audio Class — and selected the
recommended contract on 2026-08-26. The operator additionally directed that Steps 5–10 proceed on
the provisional contract, with live confirmation gated before deployment.

LE Audio is neither adopted nor foreclosed. It is the only documented Bluetooth route to an
ordinary-application microphone and the controller is capable, so it belongs in its own
experiment rather than inside this one.

### `approved_contract`

**Approved 2026-08-26; corrected from live measurements 2026-08-27. Provisional** — see the
confirmation gate below. This section is the current contract; superseded predictions later in
the experiment history are retained only as a record of what the hardware disproved.

#### Supported behavior

1. **Media.** When the Pixel is connected and presents an A2DP stream, the appliance routes it to
   the configured output — currently `wired:alsa_output.platform-3f00b840.mailbox.stereo-fallback`
   (aux) at volume 0.95 — deterministically, under supervisor ownership, never through
   default-device state or PipeWire enumeration order.
2. **Microphone.** Whenever any application on the phone opens a communication audio transport —
   cellular call, VoIP, or Assistant voice recognition — the appliance presents the
   operator-selected microphone as the uplink, AEC-protected, exactly as the deployed call path
   does today.
3. **Transition into a session.** Remove every A2DP media route and verify the removal from a fresh
   graph snapshot before constructing HFP playback, microphone ownership, and fresh AEC ownership.
   Validate exact graph ownership and unmute only after validation.
4. **Transition out.** Destroy the call graph first, report
   `MEDIA_RESTORED_APP_PAUSED`, and wait for Android to create a new A2DP stream node. Route and
   verify that node when it arrives; do not create a route to an absent stream or imply that the
   application resumed playback.
5. **Preserved unchanged.** Lark transmitter-liveness eligibility; Lark-first then FIFINE
   ordering; same-name replug generation changes; break-before-make switching; post-AEC
   `bridge.mic` ownership; verified software gain and mute; fail-closed ambiguity handling; no
   physical-microphone-to-phone bypass.
6. **Honest reporting.** With the phone connected and no communication session, status must state
   that the microphone is selected and ready **and that Android has not opened a microphone
   transport**. It must never describe such a microphone as live.
7. **Guards required by the role change.** A WirePlumber rule covering the incoming phone
   `a2dp-source` stream sets `target.object` to AUX while leaving autoconnect enabled, and the
   phone remains excluded from the output candidate list. The supervisor reconciles that
   arrival-time bootstrap to exactly one unique target.

#### Exclusions — these are not defects and must be documented as Android behavior

- No microphone for ordinary recording, camera, or any application that does not open a
  communication session.
- No simultaneous media playback and microphone uplink.
- No persistent SCO outside a session.
- No guarantee that Android resumes paused media after a call. The appliance restores the
  **route**; the application is Android's to resume.
- No control over where ringtones, notifications, or Assistant audio are routed.
- No control over the per-device "Phone calls" and "Media audio" toggles in Android's Bluetooth
  settings, either of which the user can switch off.
- Media is interrupted by calls, by Android's own refusal of a concurrent patch.

#### Expected states

| State | Meaning |
|---|---|
| `ABSENT` | The configured phone is not connected |
| `MEDIA_READY` | Phone connected and controller binding accepted; AUX and volume verified; no media stream; no post-call wait history |
| `MEDIA_ACTIVE` | One qualified A2DP stream exists, its only unique target is AUX, and fixed AUX volume is verified |
| `SWITCHING` | Transition in progress, either direction |
| `CALL` | Both HFP nodes are present, AEC is verified, and exactly one uplink exists |
| `MEDIA_RESTORED_APP_PAUSED` | Session ended; no new media node has arrived from Android |
| `DEGRADED` | Binding, AUX/volume, media identity/routing, stale cleanup, foreign-source containment, or call convergence failed verification |

These `PhoneTransport` values are orthogonal to the existing call-graph `State` values
(`CALL_DOWN`, `WAITING_MIC`, `SAFE`, and so on).

#### Acceptance criteria

1. With the phone connected and idle, media started on the phone is audible on aux, and the
   supervisor reports `MEDIA_ACTIVE` with exactly one playback link to the selected output.
2. No duplicate phone playback links, and no simultaneous stale A2DP and HFP feed to the output,
   at any point including mid-transition.
3. A call or VoIP session produces `CALL` with exactly one AEC-protected uplink and no
   inactive microphone linked to AEC.
4. Ending the session tears down the call graph and reports `MEDIA_RESTORED_APP_PAUSED` until a
   newly arriving A2DP node is routed and verified — never a claim that playback resumed.
5. No physical-microphone-to-phone bypass exists in any state.
6. Microphone selection changes — Lark promotion, FIFINE fallback, transmitter liveness loss —
   do not churn the active media output graph.
7. No profile oscillation, no supervisor restart loop, and no silent loss hidden behind a READY
   state.
8. Every existing E18 hotplug and transmitter-liveness behavior still passes.
9. Status distinguishes every state in the table above, and reports whether Android holds a
   microphone transport.

#### Confirmation gate — binding

The A2DP transport, combined BlueZ profile, `a2dp-source` node identity, node-on-pause behavior,
and `target.object` routing have now been measured on the Pixel 7a. Matrix row 3 is confirmed.
The later rapid-loop Discord checkpoint confirms row 8. Rows **4 and 7** remain binding before
promotion: second-app/notification behavior and an ordinary-recorder negative control. Step 12
cannot pass and Step 13 cannot return `PASS` until those and the remaining acceptance gates do.

### Status

Step 4 **COMPLETE** — contract approved by the operator. ADR-0009 moves to Accepted (provisional).

**Next action — Step 5: encode the routing contract as tests.** Add focused failing tests for
every state and acceptance criterion above, using existing conventions and mocks in
`pi/bridged/tests/`. Run the narrow selection and confirm failures are confined to the
unimplemented contract.

## Step 5 — The routing contract as tests (historical red checkpoint)

New file `pi/bridged/tests/test_phone_transport.py`, 30 tests. No production code changed.

```
$ PYTHONPATH=pi/bridged python -m pytest pi/bridged/tests -q
26 failed, 254 passed, 34 subtests passed in 2.30s
```

Baseline before the file was added was `250 passed, 32 subtests passed`. Every one of the 26
failures is in `test_phone_transport.py`; no pre-existing test changed behaviour, and none was
weakened to accommodate the new contract.

### Honest note on the 4 that already pass

`test_existing_link_is_not_duplicated`, `test_microphone_change_does_not_churn_the_media_graph`,
`test_ambiguous_microphone_does_not_silence_media` and
`test_no_physical_microphone_ever_reaches_the_phone` pass **vacuously**. Each asserts that
something must *not* happen, and nothing happens yet because no media routing exists. They are
guardrails that only become evidence once Steps 6–8 land. They are not counted as coverage
achieved.

### Coverage against the checkpoint's required list

| Required case | Test |
|---|---|
| Phone disconnected | `test_phone_absent_when_no_phone_nodes`, `test_disconnected_status_is_not_confused_with_idle` |
| Connected idle / media-ready | `test_connected_but_silent_is_media_ready_not_active` |
| A2DP media active, routed only to the selected output | `test_media_is_routed_to_the_selected_output`, `test_media_never_reaches_an_output_we_did_not_select`, `test_media_active_only_while_the_stream_runs` |
| Incoming call transition | `test_media_link_is_dropped_before_the_call_graph_is_built` |
| Active HFP call with AEC microphone | `test_media_and_call_never_feed_the_output_together`, `test_call_status_reports_a_live_microphone_transport`, plus the existing `CallGraphLifecycleTests` |
| Call teardown and A2DP restoration | `test_call_teardown_restores_the_media_route`, `test_restored_route_does_not_claim_playback_resumed`, `test_resumed_playback_leaves_the_paused_state` |
| Bluetooth disconnect / reconnect | `test_disconnect_clears_the_media_route`, `test_reconnect_does_not_leave_a_duplicate_link` |
| Lark/FIFINE change during idle media | `test_microphone_change_does_not_churn_the_media_graph` |
| Lark/FIFINE change during a call | existing `MicrophonePriorityLifecycleTests`, unchanged |
| Lark receiver with no live transmitter | `test_lark_present_without_a_live_transmitter_does_not_disturb_media` |
| Missing output | `test_missing_output_is_actionable_not_silent` |
| Ambiguous microphone | `test_ambiguous_microphone_does_not_silence_media` |
| Unsupported idle-microphone state | `test_idle_status_says_android_has_no_microphone_transport`, `test_idle_status_still_reports_the_microphone_as_selected_and_ready` |
| No duplicate playback or microphone links | `test_existing_link_is_not_duplicated`, `test_reconnect_does_not_leave_a_duplicate_link` |
| No raw microphone-to-phone bypass | `test_no_physical_microphone_ever_reaches_the_phone` (both media states) |

### Design decisions the tests pin

These are choices, not discoveries, and Steps 6–8 are bound by them.

1. **`PhoneTransport` is a new enum orthogonal to `State`.** `State` stays exactly what it is —
   the call-graph machine — so no E18 behaviour or existing assertion moves. The phone link gets
   its own dimension: `ABSENT`, `MEDIA_READY`, `MEDIA_ACTIVE`, `SWITCHING`, `CALL`,
   `MEDIA_RESTORED_APP_PAUSED`, `DEGRADED`. Folding these into `State` would have meant editing tests that
   currently protect deployed behaviour, which is precisely what this checkpoint forbids.

2. **The media node is found by MAC plus `api.bluez5.profile`, never by node index.** The
   phone's HFP uplink and its A2DP media stream both arrive as `bluez_input.<MAC>.<N>`;
   only the profile separates them. `outputs.find_a2dp_node` already carries this warning for
   speakers, and `test_profile_index_suffix_is_not_hardcoded` makes it structural here.
   `Settings.hfp_sink`/`hfp_source` keep their hardcoded `.1`/`.0` — changing them is outside
   this contract.

3. **Do not add `node.state` to `pw_snapshot()`.** Measurement showed that Android destroys the
   A2DP stream on pause. Node presence means active media; node absence means there is nothing to
   route. `MEDIA_READY` and `MEDIA_RESTORED_APP_PAUSED` are separated by supervisor history, and
   post-call restoration waits for a newly arriving node.

4. **A microphone problem must not silence media.** Ambiguous identity, a Lark with no live
   transmitter, and a mid-stream selection change all leave the media route untouched. Fail-closed
   exists to stop an unsafe *uplink*; there is no microphone anywhere in the media path, so there
   is nothing to fail closed on, and silencing the user's audio would be a regression dressed as
   safety.

5. **Two WirePlumber files are part of the contract**, asserted directly:
   `66-bridge-a2dp-source-target.conf` (new — the measured `target.object` bootstrap) and the
   `a2dp_sink` role restored in `50-bridge-bluez.conf`, with `a2dp_source`, `hfp_hf` and `hsp_hs`
   required to survive the edit. The new rule deliberately lives in its own file; policy tests
   prove that the HFP-only `node.autoconnect = false` rule never covers `a2dp-source` and that no
   media rule disables acquisition.

### Measured identity correction

`A2DP_SOURCE_PROFILE = "a2dp-source"` is measured on the Pixel, together with
`media.class = "Stream/Output/Audio"`. Discovery uses configured phone MAC plus those two
properties and never the observed `.2` suffix or enumeration order.

### Commands and results

| Command | Result |
|---|---|
| `PYTHONPATH=pi/bridged python -m pytest pi/bridged/tests -q` (before) | `250 passed, 32 subtests passed` |
| `... -q` (after) | `26 failed, 254 passed, 34 subtests passed` — all failures in `test_phone_transport.py` |
| `... --ignore=pi/bridged/tests/test_phone_transport.py -q` | `250 passed, 32 subtests passed` — no regression |
| `python -m ruff check pi/bridged/tests --line-length 100` | `All checks passed!` |
| `python -m black --check --line-length 100 --target-version py311 …` | unchanged |

Run with the host Python 3.13 and pytest 9.1.1 rather than the Makefile's Linux `.venv`, which
does not exist in this Windows worktree. `make test-py` remains the canonical invocation on the
Pi and in CI.

### Status

Step 5 **COMPLETE**. At this historical checkpoint, 26 focused failures defined the unimplemented
contract and no production code had changed. After `b9e1b4f`, the same transport contract is green:
`python -m pytest tests/test_phone_transport.py -q` reports **58 passed, 2 subtests passed**, and
`python -m pytest tests -q` reports **328 passed, 39 subtests passed** from `pi/bridged`; Ruff,
Black check, and `git diff --check` also pass. The old failure count is retained only as red-contract
history.

**Superseded next action.** The original Step 6 prescription used the wrong no-autolink and
`node.state` mechanisms. The measured implementation instead uses `target.object`, arrival-driven
reconciliation, and supervisor-side post-call history as specified above.

## Step 3 (resumed, 2026-08-27) — MEASURED, with the Pixel present

The predicted characterization above is left intact rather than edited, so the record shows what
was believed before the hardware arrived and what survived contact with it. **Where the two
disagree, this section wins.**

Historical characterization harness: `rig/e19_transport_matrix.py` and
`rig/pi/measure/transport_trace.py` (change-only graph tracer). Its compact evidence remains under
`docs/experiments/results/E19/`. The current quick-iteration command family is
`rig transparent-audio`; its raw WAVs, snapshots, and manifests belong under ignored
`artifacts/e19-dev/`.

### Scorecard against the predictions

| Prediction | Outcome |
|---|---|
| Profile string is `a2dp-source` | **CORRECT** — no test change needed |
| Enabling `a2dp_sink` makes the Pixel offer and stream media | **CORRECT** |
| Phone stays out of the output candidate list | **CORRECT** |
| Idle phone opens no microphone transport | **Supported, with caveats** — see below |
| Pixel is on Android 16 or 17 | **WRONG — Android 14** (SDK 34, `UQ1A.240105.004.A1`, patch 2024-01-05) |
| `a2dp-sink` and HFP are mutually exclusive card profiles | **WRONG — one combined profile** |
| `info.state` distinguishes playing from paused | **WRONG — the node is destroyed on pause** |
| `node.autoconnect = false` is the guard we need | **WRONG — it breaks the feature outright** |

The Android version error matters for sourcing: the AOSP *Audio Managed SCO rearchitecture* page
cited earlier describes the **Android 17** model and **does not apply to this handset**. The
conclusion it was cited for still holds, but it now rests on the measurements below rather than
on that page.

### What the phone and appliance actually do

**Connected and idle** — measured over 231 consecutive one-second samples: BlueZ reports
`Connected: yes` with every UUID present including `Audio Source`, and the Pi has **zero**
audio nodes. Android: `MODE_NORMAL`, no owner, `SCO_STATE_INACTIVE`. This confirms E01's "the HFP
card materialises only once an SCO transport exists" and extends it to media.

**With `a2dp_sink` enabled** the adapter advertises `Audio Sink 0000110b`, class moves
`0x680408 → 0x6c0408`, and Service Classes gain **Rendering**. Android then connects A2DP and
makes LarkBridge `mActiveDevice` in `A2dpService`, with `HeadsetService.mActiveDevice` also
LarkBridge and `mAudioRouteAllowed: true`.

**The media stream:**

```
node.name            = bluez_input.5C_33_7B_CB_BF_C5.2
media.class          = Stream/Output/Audio          <- a client STREAM, not a source node
api.bluez5.profile   = a2dp-source
api.bluez5.codec     = sbc
info.state           = running
```

The `.2` suffix confirms the decision never to hardcode the node index: HFP source is `.0`,
HFP sink `.1`, media `.2`, all sharing the `bluez_input.<MAC>.<N>` shape.

**The card exposes ONE combined profile**, not two exclusive ones:

```
ACTIVE Profile = 65536 audio-gateway | "Audio Gateway (A2DP Source & HSP/HFP AG)"
EnumProfile:  0 off  |  65536 audio-gateway
```

There is nothing to switch between. Step 2's exclusivity claim and the reasoning in ADR-0009 were
wrong, and Step 7 is simpler for it — no profile switching is required.

**The node is destroyed on pause, not suspended.** Play/pause/play, sampled continuously:

```
 1 sample  node absent,  0 links
25 samples node present, 4 links
20 samples node absent,  0 links     <- PAUSED: node and links gone
27 samples node present, 4 links
```

So `MEDIA_ACTIVE` is simply *node present*; there is no paused node whose `info.state` could be
read. `MEDIA_READY` and `MEDIA_RESTORED_APP_PAUSED` are **graph-indistinguishable** and separable
only by supervisor-side history. The original premeasurement proposal to inject `node.state` into
`pw_snapshot()` is unnecessary and withdrawn.

### The autolink race, and why the guard has to change

Sampling as fast as `pw-dump` allows, the node and its four links appear in the **same sample**:

```
56 samples  node=0 link=0
91 samples  node=1 link=4
```

WirePlumber links the arriving stream within one sample period. The supervisor polls every 2 s,
so it cannot win that race, and the node reappears on **every unpause**, not just at connection.
Some guard is mandatory.

**But the guard ADR-0009 specified does not work.** Controlled A/B, identical ordering, playback
verified running on the phone in both arms (`dumpsys audio` `state:started`):

| Rule in force | BlueZ transport | PipeWire nodes | Links |
|---|---|---|---|
| none | `active` | 1 | 4 |
| `node.autoconnect = false` on `a2dp-source` | **`idle`** | **0** | **0** |

Because the phone's media is a `Stream/Output/Audio` **client stream**, transport acquisition is
driven *by* the session manager linking it. With autoconnect disabled nothing links it, the
transport is never acquired, and the node is never created — no node for the supervisor to own,
and no audio at all. The guard as designed would have silently broken the feature.

**`target.object` is the mechanism that works.** Proved against a decoy sink so that "configured
output" and "default sink" were different objects:

| Rule | Default sink | Media landed on |
|---|---|---|
| `target.object = <aux>` | **E19Decoy** | **aux** — the configured output |
| none | **E19Decoy** | **E19Decoy** — the default, i.e. wrong |

The second row proves the default-device dependence is real and not hypothetical; the first
proves the pin overrides it while keeping acquisition working. Step 6 must pin the target, not
disable autoconnect.

### The idle microphone — supported, and where it is still soft

Three programmatic triggers were attempted with the screen awake and unlocked. None opened a
transport; Android stayed `MODE_NORMAL` / `SCO_STATE_INACTIVE` with zero nodes throughout, and
`HeadsetService` reported `mVoiceRecognitionStarted: false`.

| Trigger | Fired? | Transport |
|---|---|---|
| `input keyevent 231` (VOICE_ASSIST) | Google app came foreground, but later than the 6 s window | none |
| `am start -a android.speech.action.RECOGNIZE_SPEECH` | `Starting: Intent {...}`, Google app foreground | none |
| `busctl … MediaTransport1 Acquire` | returned `hqq 5 1021 1024` — succeeded | none |

Two honesty notes that keep this short of proof:

- The third trigger acquired the **A2DP** transport. No HFP transport object existed to acquire,
  so it is a **null result, not a negative** — it says nothing about SCO.
- Launching the Assistant UI is not the same as the recognizer actively listening. This is
  consistent with the AOSP model and with E01, but it is weaker than a recorder app genuinely
  capturing.

**No positive control was obtained in this characterization session**: SCO was never observed
opening, so "no transport" could not yet be fully distinguished from "SCO is broken in this
arrangement". `HeadsetService` reporting LarkBridge as `mActiveDevice` with
`mAudioRouteAllowed: true` argued against the latter. The later rapid-loop Discord checkpoint
below supplies the positive control and confirms row 8; row 7 remains supported but not proven.

### Robustness defects found in passing

Both are E19-relevant because Step 7 deliberately churns the graph.

1. **Reconnecting too soon after a WirePlumber restart leaves no card at all.** `bluetoothctl`
   reports `Connected: yes`, the phone plays happily, and there is no `bluez_card` — so audio
   silently goes to the phone's own speaker. A healthy-looking state with no audio is exactly
   what the contract forbids.
2. **A WirePlumber-only restart can strand the aux sink node at volume 1.0**, and it is then
   unrecoverable: the supervisor detects `volume mismatch: desired 0.850, observed 1.000` every
   tick and cannot fix it, and `wpctl set-volume`, `pactl set-sink-volume` and `pw-cli set-param`
   all report success while the node stays at 1.0. Only a **full `pipewire` restart** rebuilds
   the node and restores the configured value. On an output feeding a car aux input, an
   unnoticed jump to 1.0 is a loud surprise, and E09 measured 1.0 as roughly +4 dB and clipping.

### Configuration change

At the operator's request, `wired_output_volume` moved **0.85 → 0.95** in
`config/bridge.toml.example` and on the deployed unit; the supervisor now reports
`desired 0.95, observed 0.95, verified True`. There is only one such knob and it governs both
call audio and media, since both leave the same sink; `output_gain_db` remains call-only at 0.0.
The code fallback in `bridge_supervisor.py` stays at 0.85 deliberately, so a unit with no
configuration keeps the measured-safe value rather than inheriting a level that spends headroom.
E09's warning is recorded next to the new value.

### Post-characterization deployed-baseline state

At the end of characterization, every change was reverted and verified: config sha256 back to
`9024a5ac8e3c463bdf7316d8f46c5251882432fc8c7a8703871e5bfdf1467b34`, `Audio Sink` absent, the four
original files in `wireplumber.conf.d`, the decoy sink unloaded, supervisor `CALL_DOWN` with
volume verified. The only intended persistent change was the 0.95 volume. Later volatile staging
does not change `/etc/larkbridge/DEPLOYED.json`, which continues to identify production baseline
`03df47e`; runtime candidate state must be read from the rapid-runner evidence instead.

### Gate status

| Row | State | Verdict |
|---|---|---|
| 3 | A2DP media active, routed to the selected output | **CONFIRMED** |
| 4 | Notification / second-app audio | not exercised |
| 7 | Ordinary app opens no microphone transport | **supported, not proven** — an ordinary app's active capture route has not yet been retained |
| 8 | App communication session opens one | **CONFIRMED** — Discord opened SCO and the verified AEC/uplink graph |

Rows 3, 4, 7 and 8 are the binding pre-deployment gate. Rows 3 and 8 are met; 4 and 7 are not.

## Rapid closed-loop media checkpoint — 2026-08-27

The fast loop now uses the GeneralPlus `1b3f:2008` input as a rig-only electrical instrument,
with the Pi AUX output looped back into it. Calibration
`20260827T225235Z-quick-aux` passed at minimum capture gain with a measured floor of approximately
`-77.94 dBFS`. The formal capture is armed only after one-second probes observe program audio at
least 40 dB above that floor; this keeps YouTube Music launch and network buffering outside the
30-second scored window.

The Pixel source is YouTube Music, controlled only through named accessibility elements. The
runner resolved the exact mini-player control
`com.google.android.apps.youtube.music:id/mini_player_play_pause_replay_button`, verified the
named `Pause video` state plus the exact-package media session, and took no screenshots. Android
Bluetooth Media audio permission was enabled by the operator. On this bench, A2DP volume had to
be 25/25: the media-session volume API was ignored under Bluetooth absolute volume, while named
playback plus volume key events produced the intended level.

### Measured AUX underrun correction

The clean A2DP source ran at 44.1 kHz and the fixed AUX sink at 48 kHz with a 512-frame graph
quantum. The AUX sink's existing ALSA period and headroom were both 480 frames. A global forced
1024-frame quantum was rejected because it caused thousands of BlueZ errors and silence. An
AUX-only runtime trial with additional headroom eliminated the AUX underrun, so commit `be38d43`
adds a narrowly matched WirePlumber rule setting `api.alsa.headroom = 960`: exactly one extra
480-frame period. It does not change period size, Bluetooth latency, or graph quantum, and both
legacy and transactional installers carry and roll back the policy.

The first committed-policy run carried 30 seconds of program audio with zero AUX errors, but the
strict scorer rejected two BlueZ source errors in its first measured second. They then stayed
flat. After a clean development-session restart, the same immutable candidate
`be38d43349-787d0cd69f2a` passed twice consecutively:

| Artifact | Elapsed | Signal above floor | Active program | Clipping | AUX / BlueZ new errors | Route loss |
|---|---:|---:|---:|---:|---:|---:|
| `20260827T234155Z-media-be38d43349-787d0cd69f2a` | 85.84 s | 41.23 dB | 30.0 s | 0.000% | 0 / 0 | 0 |
| `20260827T234344Z-media-be38d43349-787d0cd69f2a` | 52.73 s | 40.99 dB | 29.4 s | 0.000% | 0 / 0 | 0 |

A later arbitrary-song capture measured 38.86 dB above the same floor and was rejected solely by
the original 40 dB threshold even though routing, clipping, service, USB/HCI, and PipeWire-counter
checks were clean. Because catalog-mastering level and quiet passages are not controlled, the
runner now separates two gates: calibrated/reference stimuli retain the required 40 dB electrical
margin, while arbitrary YouTube Music uses a quick presence threshold of
`max(calibrated floor + 20 dB, -55 dBFS)`. The same effective threshold classifies active program
windows. This lower catalog threshold cannot satisfy reference-waveform continuity, calibration,
AEC, far-end, soak, or promotion gates; those remain separate. Raising AUX from 0.95 to 1.00 would
add only about 0.45 dB and would not have made that rejected capture reach 40 dB, so AUX remains at
the measured unclipped 0.95 setting.

Raw evidence remains ignored under `artifacts/e19-dev/iterations/`. The corresponding capture
SHA-256 values are
`fad4eea66f5239a9723e8bb3e54bec485537963d33e38d45b7a37bd8cd652f90` and
`92b2acb182b3e3a4c907eb6deefd0856c45dd7c232f173678a2922c17ff4313e`;
the `pw-top` evidence hashes are
`007e286530469f783894a02ec6e23887f8158504bc40948d661406b659788b16` and
`db69385fb7333516e0f2ce28879460643036801fa680ae7a6e68f5712330b39b`.

This arbitrary-song loop proves electrical level, clipping, deterministic routing, and PipeWire
scheduling continuity. It deliberately reports audible-dropout detection as
`NOT_MEASURED_UNREFERENCED_SOURCE`: legitimate silence in catalog music cannot be distinguished
from a dropout without a known reference waveform. A synthetic reference remains required for
promotion-grade discontinuity scoring. The Discord transition and post-AEC graph topology were
measured later below; echo suppression and true far-end validation remain open.

## Rapid Discord transport checkpoint — 2026-08-27

The first live Discord call exposed a real classifier defect. Its HFP source is a BlueZ
`headset-audio-gateway` stream whose `media.class` is also `Stream/Output/Audio`; the broad media
test therefore mistook live HFP for A2DP, tore down the graph it had just built, and repeated the
cycle. Commit `7235233` makes an explicit non-A2DP BlueZ profile override that ambiguous media
class. The first two live proofs and acoustic smoke used immutable candidate
`af84c58dcc-921ce47313f1` (revision `af84c58dcca1e9c873e343cc6bf997e2a8d0220d`), which contains
that correction. After the long pause before the final cycle, the same Git ref was hot-staged
again from a Git archive as runtime candidate `af84c58dcc-a1f2b4244b2b`. A later audit found that
the earlier worktree package had included ignored tool-cache files. The tracked source revision
and transport fix were unchanged, but the package byte sets and therefore candidate IDs were not
identical. The runner now builds worktree candidates from tracked files plus explicitly allowed
untracked files, excluding tool caches.

Three complementary live proofs passed:

1. **Supervisor-only hot proof.** With Discord's SCO transport already open, candidate supervisor
   PID `258496` built generation 1 with native AEC owner PID `258557`, reported `ACTIVE` and phone
   transport `CALL`, and held that verified state for **38.620 s** with `NRestarts=0`. Android
   reported `com.discord` owning `MODE_IN_COMMUNICATION` and `SCO_STATE_ACTIVE_INTERNAL`. The graph
   had exactly one uplink, `output.bridge.mic -> bluez_output.5C_33_7B_CB_BF_C5.1`. The selected
   FIFINE fallback fed `echo-cancel-capture`; there was no physical-microphone-to-HFP bypass. The
   hold duration came from an interactive watcher and is not retained in the hashed bundle.
2. **Full-policy A2DP-to-Discord proof.** Session
   `20260828T003518Z-af84c58dcc-921ce47313f1` started with the candidate's complete WirePlumber
   policy and a verified YouTube Music A2DP route. Transition artifact
   `20260828T003911Z-call` records `MEDIA_ACTIVE` before Discord and `ACTIVE` / `CALL` after Android
   replaced A2DP with SCO. The same supervisor PID `266343` advanced to generation 2 with native
   AEC owner PID `269910`; all six expected links were present, with no missing or unexpected
   links. It then held for **41.132 s** with `NRestarts=0`, Android still reporting Discord
   communication mode and active SCO, exactly one post-AEC uplink, and no physical-microphone
   bypass. The transition and graph are retained; the later hold duration was an interactive
   observation and is not retained in that artifact.
3. **Uninterrupted full-cycle proof.** Artifact `20260828T031200Z-full-cycle` began with media node
   `bluez_input.5C_33_7B_CB_BF_C5.2` routed only to AUX. Its `call.json` records that media node and
   `transition_from_media_s=8.179`; the retained after-state is `ACTIVE` / `CALL`, generation 2,
   supervisor PID `296574`, native AEC owner PID `297980`, and all six expected links with no
   missing or unexpected links. The hashed `pw-dump` contains exactly one active input to the HFP
   sink, from `output.bridge.mic`, and no direct physical-microphone bypass; SHA-256
   `e069af63f679fda891a3606cbce9696887f7e97d929d29383cf7a8629e180e79` is the full call
   snapshot's `pw-dump` output hash. Both retained post-call snapshots report `CALL_DOWN` /
   `MEDIA_ACTIVE`, Android `MODE_NORMAL`, SCO inactive, and the `.2` node routed only to AUX. Their
   service, system-service, and kernel-error evidence is byte-identical to the call snapshot.

The first call's teardown watcher observed the HFP edge at `1787876820.0312885`; the next status
publication was `CALL_DOWN` / `MEDIA_RESTORED_APP_PAUSED`, with the call graph removed. The full
post-teardown Android snapshot was `MODE_NORMAL` with SCO inactive. This verifies honest paused
history after one teardown; it does not claim Android resumed media. The retained artifact is
`20260828T030926Z-manual-paused-hot`; its transition JSON and evidence-manifest SHA-256 values are
`4b20c5ee4b20a8c2c7ca65ae312204f65d2fbdaf8d35ad449ce373267d957079` and
`939f8210553d82690785b71c4db9c58e35f05e4788f04321ddd87796763cfa58`.

While the second call remained stable, near-end smoke artifact
`20260828T004420Z-af84c58dcc-921ce47313f1` also passed. The user confirmed the GeneralPlus output
was feeding the fixed speaker aimed at the selected FIFINE. The raw and clean taps were 48 kHz,
mono, S16 PCM and approximately 21.5 seconds long. The known near-end stimulus measured
`-45.35 dBFS` correlated in raw (`r=0.3779`) and `-42.89 dBFS` correlated post-AEC (`r=0.5021`),
with 0.000% clipping in both captures. The scorer reported `-2.46 dB` preservation loss, meaning
the clean correlated level was 2.46 dB higher than raw. Together with the contemporaneous verified
call graph, this proves that the selected microphone's near-end signal traversed the post-AEC path
without being lost. The scorer explicitly reports
`NOT_MEASURED_USE_SEPARATE_SPEAKER_MODE_FIXTURE` for echo suppression: this near-end-only smoke
does not measure the 10 dB far-end echo-suppression gate.

One audible 1 kHz-tone attempt is explicitly **invalid evidence**: the preceding full-policy
session start had failed and completed its rollback, so the adapter was on the deployed baseline
that did not advertise `Audio Sink`. Hearing that tone from the Pixel speaker therefore says
nothing about candidate A2DP routing.

The transition JSON and its evidence manifest remain ignored under `artifacts/e19-dev/transitions/`;
their SHA-256 values are
`1cfa479d1ec5bd78249378ae6af704f9ff9310059b5145338a90a276a169eec4` and
`739ac016904a43390e4f878f1e45dd7228e51f1fdaadccf5b3c71e0206b07d2a`. The call-smoke JSON and
evidence-manifest SHA-256 values are
`da1e6aa9f3f8b97061fae71381e51c47c54e7f1806f448d3978af1d958a31448` and
`4a48d8c962d31704897f2e99a31d4fae1612bb416543947da2ce789555388364`. The uninterrupted-cycle
evidence-manifest SHA-256 is
`d63d843a00be9b940a9ec51483593f0ce01379b60657d9e31124583c0700ccdb`. These measurements confirm
the Discord positive control, the earlier targeted transitions plus one uninterrupted
media-to-call-to-media cycle, a separate paused-history teardown, post-AEC graph topology, and
near-end preservation. They do **not** constitute measured echo suppression, true far-end
validation, three transition cycles, live-Lark qualification, soak, or promotion.
