# Microphone priority and safe failover

LarkBridge selects microphones from the ordered `[[devices.microphones]]` entries in the
installed `bridge.toml`. The first uniquely identified candidate with the required native format
wins. The shipped example prefers `lark-a1` and uses `fifine-k054` only when the Lark is absent or
capability-incompatible.

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
an actual Lark node only; it is `null` when the FIFINE is active.

Candidate states are `selected`, `usable`, `absent`, `capability_mismatch`, `ambiguous`, or
`conflict`. `ambiguous` and `conflict` are safety faults. During a live call they place the graph in
`SAFE` with no microphone uplink. If every candidate is absent or incompatible, a live call enters
`WAITING_MIC`; with no call the graph remains `CALL_DOWN` while still publishing inventory.

## Configure a FIFINE K054

Copy the two ordered candidate tables from `config/bridge.toml.example` into the unit's preserved
configuration. The characterized K054 has generic USB descriptors (`0c76:161e`, product
`USB PnP Audio Device`) and no serial, so the example deliberately leaves `usb_port_path` blank.
This portable policy can also match a different capture-only mono S16LE/48 kHz device with the same
fingerprint. Set `usb_port_path` to the USB topology path shown in status if a unit must be pinned.
Never put an observed ALSA card number into configuration.

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

The FIFINE implementation is field-QA pending. Electronic identity and native-format evidence does
not qualify physical mute/gain behavior, replacement units, acoustics, AEC, hotplug endurance, or
cold-boot stability. The release-promotion gates and evidence requirements are tracked in
`docs/experiments/E18-fifine-k054-compat.md`.
