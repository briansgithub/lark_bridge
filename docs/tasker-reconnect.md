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
5. If the exact BT500 is disconnected while its Android Bluetooth details screen is open, treat the
   event as a manual disconnect: set `%LB_AUTO` to `0` and stop any pending fallback.
6. Re-arm the fallback by setting `%LB_AUTO` to `1` after a real connection to the exact BT500.

The profile intentionally has no `BT Near` state, loop, or repeated connect action. Do not shorten
the 25-second wait below the Pi's bounded connection window; doing so recreates Android/Pi ownership
collisions. An unset `%LB_AUTO` is intentionally treated as enabled so a fresh import can reconnect
without manual initialization.

This is an Android-side hold only. It prevents Tasker from initiating an unwanted reconnect, but it
does not disable the Pi watchdog; the Pi can still reconnect the phone while it is powered and in
range. Preventing that would require a coordinated Pi policy change, which is outside this profile.
The manual-disconnect distinction also requires Tasker's accessibility service so `%WIN` identifies
the open `LarkBridge BT500` details screen; the field Pixel has that service enabled.

## Import or recovery

In Tasker, import `LarkBridge_Auto_Reconnect.prf.xml`, enable all three LarkBridge profiles, and apply
Tasker's pending changes. If profiles with the same names already exist, disable or remove the old
copies before enabling the imported bundle so only one fallback and one policy handler can run.

The included profiles are `Auto-Reconnect To LarkBridge`,
`LarkBridge Manual Hold - Bluetooth Off`, and `LarkBridge Connection Policy`. To manually re-enable
the Tasker fallback before the next successful connection, set the global Tasker variable
`%LB_AUTO` to `1`.

The field Pixel also keeps dated full Tasker backups under `/sdcard/Tasker/configs/user/`. Those are
device recovery backups and may contain unrelated personal automations, so they are deliberately not
stored in this repository.
