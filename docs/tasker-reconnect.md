# Tasker reconnect fallback

The Pi watchdog owns the immediate Pixel connection. Tasker is a delayed, one-shot fallback only;
it must not race the Pi at boot or repeatedly probe the BT500.

The importable profile is
`config/tasker/LarkBridge_Auto_Reconnect.prf.xml`. It targets only `LarkBridge BT500`
(`A0:AD:9F:73:6C:24`) and contains this sequence:

1. While the exact BT500 is disconnected, wait 25 seconds.
2. Connect to `LarkBridge BT500` with an 8-second action timeout.
3. Run the connect action only if `Auto-Reconnect To LarkBridge` is still active. A normal Pi
   reconnect exits the profile before the guarded action can run.

The profile intentionally has no `BT Near` state, loop, or repeated connect action. Do not shorten
the 25-second wait below the Pi's bounded connection window; doing so recreates Android/Pi ownership
collisions.

## Import or recovery

In Tasker, import `LarkBridge_Auto_Reconnect.prf.xml`, enable the profile, and apply Tasker's pending
changes. If a profile with the same name already exists, disable or remove the old profile before
enabling the imported copy so only one fallback can run.

The field Pixel also keeps dated full Tasker backups under `/sdcard/Tasker/configs/user/`. Those are
device recovery backups and may contain unrelated personal automations, so they are deliberately not
stored in this repository.
