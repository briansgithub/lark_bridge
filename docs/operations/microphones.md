# Microphone priority and safe failover

LarkBridge selects microphones from the ordered `[[devices.microphones]]` entries in the
installed `bridge.toml`. The first uniquely identified candidate with the required native format
wins. The shipped example prefers a live `lark-a1`, then `fifine-k053`, then `fifine-k054` when
higher-priority candidates are absent or capability-incompatible. A connected Lark receiver with
no live transmitter is capability-incompatible and therefore does not displace a usable FIFINE.

Selection never depends on ALSA card number, PipeWire enumeration order, or a lexical node-name
tie-break. `node_name` selects a preferred profile only after USB identity has matched. An
ambiguous or overlapping higher-priority candidate blocks the uplink instead of allowing a lower
candidate to hide the problem.

## Inspect the decision

```sh
python3 pi/bridged/bridgectl.py microphone status
python3 pi/bridged/bridgectl.py microphone list --json
```

The status includes the candidate ID, priority, physical identity, native format, PipeWire object
serials, USB instance generation, matched nodes, and a selection reason. `endpoints.microphone` is
the active microphone. The deprecated `endpoints.lark` field remains for one release and contains
an actual Lark node only; it is `null` when either FIFINE is active.

Candidate states are `selected`, `usable`, `absent`, `capability_mismatch`, `ambiguous`, or
`conflict`. `ambiguous` and `conflict` are safety faults. During a live call they place the graph in
`SAFE` with no microphone uplink. If every candidate is absent or incompatible, a live call enters
`WAITING_MIC`; with no call the graph remains `CALL_DOWN` while still publishing inventory.

## Configure the FIFINE fallbacks

Copy the three ordered candidate tables from `config/bridge.toml.example` into the unit's preserved
configuration. Both characterized FIFINEs use generic product text (`USB PnP Audio Device`) and
have no serial, but their hard USB identities differ: the K053 is `0c76:161f` and the K054 is
`0c76:161e`. Both capture native mono S16LE/48 kHz. The example deliberately leaves
`usb_port_path` blank so either can move between USB ports or a powered hub.

Portable matching can also accept another device with the same identity and capabilities. Set
`usb_port_path` to the topology path shown in status if a unit must be pinned. Never put an
observed ALSA card number into configuration.

Unlike the capture-only K054, the K053 also exposes a playback sink for its monitor jack. The
shipped WirePlumber policy disables only that `0c76:161f` `Audio/Sink`; K053 capture remains
available and the configured Pi AUX jack remains the local output.

Existing installations remain Lark-only until their preserved local configuration explicitly adds
the candidate array. Without the array, the supervisor synthesizes the legacy Lark candidate.
Non-empty `[devices.lark]` values still apply in legacy mode with precedence environment, local
table, built-in defaults. With an explicit array, `[devices.lark]` is ignored and legacy environment
overrides may alter only the `lark-a1` preferred profile—not its hard USB identity.

## Switching and controls

An active-device change is break-before-make: `bridge.mic` stops, the old AEC host is removed, and
a new AEC host and muted loopbacks are built against the newly selected physical instance. The
uplink is enabled only after ownership and software gain/mute readback pass. A same-name USB replug
has a different instance token and therefore receives a fresh AEC graph.

`audio.mic_gain_db` and `audio.mic_muted` control the software output of `bridge.mic`, after AEC.
They do not change the microphone's physical controls or ALSA mixer. A control or readback failure
holds the graph in `SAFE`.

## Deployment and qualification

Microphone compatibility is delivered by the transactional release/image installer, which also
refreshes the installed power-loss verifier. The boot-only `scripts/install.sh` path intentionally
does not deploy this compatibility change.

Electronic identity and native-format evidence alone does not qualify physical controls,
replacement units, acoustics, AEC, hotplug endurance, or cold-boot stability. K054 evidence remains
in `docs/experiments/E18-fifine-k054-compat.md`; K053 evidence and its deliberately abbreviated
qualification scope are tracked separately in `docs/experiments/E20-fifine-k053-compat.md`.

The K053 candidate also owns its physical `Mic Capture Volume`. The supervisor resolves the
current ALSA card from the identity-qualified PipeWire device, applies the configured gain
before starting AEC, verifies the raw mixer readback, and fails closed if any part is ambiguous.
This is separate from `audio.mic_gain_db`, which remains a post-AEC software control.
