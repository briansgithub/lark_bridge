# Tasker reconnect fallback

The Pi watchdog owns the immediate Pixel connection. Tasker is a delayed, one-shot fallback only;
it must not race the Pi at boot or repeatedly probe the BT500.

The importable profile bundle is
`config/tasker/LarkBridge_Auto_Reconnect.prf.xml`. It targets only `LarkBridge BT500`
(`A0:AD:9F:73:6C:24`) and contains this policy:

1. While the exact BT500 is disconnected, wait 25 seconds.
2. Connect to `LarkBridge BT500` with an 8-second action timeout.
3. Run the connect action only if `Auto-Reconnect To LarkBridge` is still active, Bluetooth is on,
   and `%LB_AUTO` is not `0`. A normal Pi reconnect exits the profile before the guarded action can
   run.
4. If Bluetooth is turned off, set `%LB_AUTO` to `0` and stop any pending fallback. Turning
   Bluetooth back on does not itself clear the hold.
5. On the first exact-device disconnect, set `%LB_DISC_UNTIL` to ten seconds in the future. A new
   exact-device connection event inside that window sets `%LB_RESTORED` to `1`; the Pi independently
   requires its restored media profile to be ready.
6. If the restored connection is disconnected again before the deadline, set `%LB_AUTO` to `0`,
   set `%LB_HOLD` to `1`, and stop any pending fallback. A failed reconnect never reaches the
   restored state and therefore cannot trigger this hold.
7. Re-arm the fallback and clear `%LB_HOLD` after a real connection to the exact BT500. This lets
   an explicit Android **Connect** tap resume immediately.

The profile intentionally has no `BT Near` state, loop, or repeated connect action. Do not shorten
the 25-second wait below the Pi's bounded connection window; doing so recreates Android/Pi ownership
collisions. An unset `%LB_AUTO` is intentionally treated as enabled so a fresh import can reconnect
without manual initialization.

The Tasker hold mirrors the Pi watchdog's `manual_hold`: both require a restored exact-device
connection between two losses inside the same ten-second window, while the Pi applies the stricter
profile-ready gate. The Pi suppresses its device/profile requests while Tasker suppresses its
delayed fallback, so neither owner reconnects against the operator's choice. The policy does not
inspect `%WIN`, does not depend on the currently visible screen title, and does not require Tasker's
accessibility service for disconnect detection.

## Import or recovery

In Tasker, import `LarkBridge_Auto_Reconnect.prf.xml`, overwrite the three same-named profiles when
upgrading, enable all three LarkBridge profiles, and apply Tasker's pending changes. Confirm that
only one fallback and one exact-device policy handler are enabled.

The included profiles are `Auto-Reconnect To LarkBridge`,
`LarkBridge Manual Hold - Bluetooth Off`, and `LarkBridge Connection Policy`. To manually re-enable
the Tasker fallback before the next successful connection, set the global Tasker variable
`%LB_AUTO` to `1`.

The field Pixel also keeps dated full Tasker backups under `/sdcard/Tasker/configs/user/`. The live
double-Disconnect deployment is captured in
`2026-09-01_larkbridge-post-double-disconnect.xml`. Those backups may contain unrelated personal
automations, so they are deliberately not stored in this repository.
