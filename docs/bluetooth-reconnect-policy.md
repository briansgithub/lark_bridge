# Pixel reconnect and bond-repair policy

Status: implemented for the USB-BT500 call controller.

The Pi initiates the Pixel connection immediately after the configured BT500 is resolved. The
normal path gives the D-Bus `Connect` request 8 seconds, then observes BlueZ for 12 seconds. If the
request is still pending, the watchdog cancels only the configured Pixel object and retries once.
This keeps a normal car boot within the 25-second connection target without resetting Bluetooth,
PipeWire, WirePlumber, another adapter, or another bond.

## When automatic repair is allowed

Automatic bond replacement requires one narrow signature on the same healthy controller:

1. `Connect` reports `InProgress`;
2. the 12-second observation window expires;
3. an exact-device `Disconnect` quiesces that operation; and
4. the immediate retry reports `InProgress` again.

A page timeout, ordinary refusal, missing phone, phone Bluetooth being off, absent bond, or changed
controller identity does not delete a key. Those cases remain in bounded reconnect handling or
report `pairing_required` for an operator.

## Repair transaction

Before removing the Pixel object, the watchdog stops `bridge-pairing-seal.timer` and synchronously
seals the current `/var/lib/bluetooth` database as the rollback snapshot. It then removes only the
configured Pixel object on the resolved BT500 and opens a 120-second `NoInputNoOutput` pairing
window. Pairable and discoverable are enabled only for that window.

The replacement is accepted only when the configured Pixel is paired, bonded, and trusted on that
same BT500. Any newly paired, unexpected device is removed. Success closes the window, unregisters
the agent, seals the new key, restarts the periodic seal timer, and reconnects immediately.

Timeout or cancellation closes pairability and discovery but does not seal the bondless live state.
The timer remains paused, so a reboot restores the pre-repair snapshot. If Android forgot the key,
the user must tap **LarkBridge BT500** and approve the Pixel's pairing dialog; that confirmation is
an Android security requirement and is never bypassed.

## Operator commands

```bash
bridgectl phone status
bridgectl phone status --json
sudo bridgectl phone repair
```

Status includes `bond_state`, `repair_state`, the repair trigger and deadline, reconnect timing,
the watchdog action, and current instructions. Outside a repair window, readiness requires the
BT500 to report `Pairable: no` and `Discoverable: no`.
