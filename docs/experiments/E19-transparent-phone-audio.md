# E19 — Can the appliance carry Pixel media and microphone audio transparently, not only during calls?

- **Status:** In progress — Steps 1–2 of 13 complete; feasibility not yet established
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

Step 11 is the only checkpoint authorized to mutate the Pi, and it may not deploy until the current
appliance is preserved:

- Capture a **consistent full-card raw image** — quiesce audio, Bluetooth, watchdog and persistence
  writers, `sync`, remount `LARKDATA` read-only, stream the whole card, and restore read-write and
  the services through a guaranteed cleanup path.
- Hash the complete image (SHA-256) and record size, source device, partition layout, boot ID,
  deployed manifest, configuration hash, stopped services, and restoration status.
- Store the image **outside Git** (~15 GiB, contains device secrets) and update
  [`docs/image-retention-2026-08-26.md`](../image-retention-2026-08-26.md).
- **Delete no older image.** In particular the `c63c823` image may not be retired until the new
  image has been captured, hashed, restored, and booted.

## Checkpoints

| # | Checkpoint | Status |
|---|---|---|
| 1 | Establish the isolated workspace | **COMPLETE** |
| 2 | Static Bluetooth and audio architecture audit | **COMPLETE** |
| 3 | Live Pixel and Pi capability characterization | Not started |
| 4 | Feasibility verdict and approved contract | Not started — expected `DECISION_REQUIRED` |
| 5 | Encode the routing contract as tests | Not started |
| 6 | Implement deterministic idle media routing | Not started |
| 7 | Implement safe A2DP/HFP transitions | Not started |
| 8 | Integrate the approved idle microphone behavior | Not started |
| 9 | Status, CLI, policy, installer, and documentation | Not started |
| 10 | Independent local regression and safety review | Not started |
| 11 | Preserve the baseline image and deploy | Not started |
| 12 | Abbreviated live acceptance test | Not started |
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
