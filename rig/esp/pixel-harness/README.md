# Pixel harness app (`dev.larkbridge.harness`)

A minimal Android app that gives the HIL rig "calls that dial no one" for Stage D of
`ESP32_WIRELESS_PORT_PLAN.md` (H9 Pixel call, H11 audio focus; see also
`firmware/docs/hil-rig.md` §7). It registers a **self-managed** `PhoneAccount`
(`CAPABILITY_SELF_MANAGED`) and a `ConnectionService`, so a harness call is a local VoIP-style
call. It never dials a number and never touches the SIM or the telephony network. The app
requests no telephony permission (no `CALL_PHONE`, no `READ_PHONE_STATE`), and the call address
is `sip:harness@larkbridge.invalid` on its own account. Self-managed calls don't appear in the
system call log.

Over Bluetooth, Telecom treats it like any VoIP call: audio mode `IN_COMMUNICATION`, and the
route goes to the connected HFP device (the S31) over SCO/eSCO. `PLAY_STIMULUS` plays a bundled
WAV as downlink audio with `USAGE_VOICE_COMMUNICATION`.

You drive it from adb with no UI taps. Every output line is one JSON object on logcat tag
`LarkHarness`.

## Layout

| Path | What |
| --- | --- |
| `AndroidManifest.xml` | package, permissions, components |
| `src/dev/larkbridge/harness/` | Java sources (no AndroidX, no Kotlin, no Gradle) |
| `tools/gen_stimulus.py` | generates `tone_1k_16k_3s.wav` at build time (1 kHz, 16 kHz mono PCM16, 3.0 s, -6 dBFS peak, 10 ms fades) |
| `tests/test_gen_stimulus.py` | unittest for the generator |
| `build.sh` | aapt2 -> javac -> d8 -> zipalign -> apksigner |

## Build

From WSL (it uses the Windows JDK and SDK through interop; no Gradle, no downloads):

```bash
cd rig/esp/pixel-harness
nice -n 15 ./build.sh          # -> build/larkbridge-harness-debug.apk
./build.sh clean
python3 -B -m unittest discover -s tests -q   # generator test
```

Defaults: `ANDROID_SDK=/mnt/c/Users/user1/Android/Sdk` (build-tools `37.0.0`, platform
`android-37.0`) and `JDK=/mnt/c/Users/user1/Tools/jdk-17`. Override any of `ANDROID_SDK`, `JDK`,
`BUILD_TOOLS`, `PLATFORM` or `PYTHON` from the environment. On a Linux host with native tools,
the script drops the `.exe`/`wslpath` handling automatically.

The first build creates a local debug keystore at `.keystore/debug.keystore`, using the standard
Android debug-key convention (password `android`). It's gitignored. Keep it: a rebuild signed with
a different key can't `install -r` over the old APK, and you'd have to uninstall first.

Gradle wasn't used: the repo has no wrapper, and AGP for compileSdk 37 would need a large
dependency download for a nine-class app. The script builds with nothing but the local SDK.

Badging of the 2026-11-23 build (`aapt2 dump badging`):

```
package: name='dev.larkbridge.harness' versionCode='1' versionName='0.1.0' platformBuildVersionName='17' platformBuildVersionCode='37' compileSdkVersion='37' compileSdkVersionCodename='17'
minSdkVersion:'31'
targetSdkVersion:'34'
uses-permission: name='android.permission.MANAGE_OWN_CALLS'
uses-permission: name='android.permission.MODIFY_AUDIO_SETTINGS'
uses-permission: name='android.permission.FOREGROUND_SERVICE'
uses-permission: name='android.permission.FOREGROUND_SERVICE_PHONE_CALL'
uses-permission: name='android.permission.RECORD_AUDIO'
uses-permission: name='android.permission.BLUETOOTH_CONNECT'
uses-permission: name='android.permission.POST_NOTIFICATIONS'
application: label='LarkBridge Harness' icon=''
launchable-activity: name='dev.larkbridge.harness.MainActivity'  label='' icon=''
```

APK contents: `AndroidManifest.xml`, `resources.arsc`, `classes.dex` (9 classes),
`assets/stimulus/tone_1k_16k_3s.wav` (stored), and a v3 APK signature.

## Permissions

| Permission | Kind | Why | How it is granted |
| --- | --- | --- | --- |
| `MANAGE_OWN_CALLS` | normal | register a self-managed account, `placeCall` on it | automatically at install (`pm grant` refuses normal permissions) |
| `MODIFY_AUDIO_SETTINGS` | normal | audio routing | at install |
| `FOREGROUND_SERVICE`, `FOREGROUND_SERVICE_PHONE_CALL` | normal | keep the process foreground during a call | at install |
| `RECORD_AUDIO` | runtime | a voice-communication session normally holds the mic; future uplink capture | `pm grant` |
| `BLUETOOTH_CONNECT` | runtime (Android 12+) | Bluetooth device names and addresses in STATUS, BT call routing | `pm grant` |
| `POST_NOTIFICATIONS` | runtime (Android 13+) | shows the foreground-service notification (the service runs without it) | `pm grant` |

## Morning steps (after 06:00 ET, once the overnight soak is finished)

Don't do any of this while the Bluetooth soak (`rig/esp/bt_soak.py`) is running. A harness call
opens SCO to the S31 and changes exactly what the soak is observing.

The commands assume `adb` is the Windows platform-tools binary
(`/mnt/c/Users/user1/AppData/Local/Android/Sdk/platform-tools/adb.exe` from WSL). Add
`-s <serial>` if more than one device is attached. Run them from `rig/esp/pixel-harness`.

```bash
ADB=/mnt/c/Users/user1/AppData/Local/Android/Sdk/platform-tools/adb.exe

# 1. install (-g grants all runtime permissions at install time)
$ADB install -r -g build/larkbridge-harness-debug.apk

# 2. grant runtime permissions explicitly (idempotent; needed if installed without -g)
$ADB shell pm grant dev.larkbridge.harness android.permission.RECORD_AUDIO
$ADB shell pm grant dev.larkbridge.harness android.permission.BLUETOOTH_CONNECT
$ADB shell pm grant dev.larkbridge.harness android.permission.POST_NOTIFICATIONS
$ADB shell dumpsys package dev.larkbridge.harness | grep -E "MANAGE_OWN_CALLS|RECORD_AUDIO|BLUETOOTH_CONNECT|POST_NOTIFICATIONS"

# 3. take the package out of the "stopped" state and register the PhoneAccount
$ADB shell am start -n dev.larkbridge.harness/.MainActivity

# 4. watch the harness output (second terminal; does not clear the log buffer)
$ADB logcat -v raw -s LarkHarness:I
```

### Smoke test

The broadcast form is `am broadcast -f 0x20 -n dev.larkbridge.harness/.ControlReceiver -a <action>`.
The `-f 0x20` is `FLAG_INCLUDE_STOPPED_PACKAGES`; it's harmless once the app has been started.

```bash
R="-f 0x20 -n dev.larkbridge.harness/.ControlReceiver"
A=dev.larkbridge.harness

$ADB shell am broadcast $R -a $A.STATUS --es tag s0
#   expect: "call":{"state":"NONE",...}, all "perms" true, "account_registered":true,
#           "stimuli":["tone_1k_16k_3s.wav"]
$ADB shell am broadcast $R -a $A.CALL_START --es tag c1
#   expect: call_requested, call_created, call_state ACTIVE, call_audio_state/call_endpoint
#           with route BLUETOOTH if the S31 HFP link is up
$ADB shell am broadcast $R -a $A.STATUS --es tag s1
#   expect: "state":"ACTIVE", "mode":"IN_COMMUNICATION",
#           "communication_device":{"type":"BLUETOOTH_SCO",...}, "sco_state":"connected"
$ADB shell am broadcast $R -a $A.PLAY_STIMULUS --es asset tone_1k_16k_3s.wav --es tag p1
#   expect: stimulus_start (routed_device BLUETOOTH_SCO), stimulus_end ~3 s later with
#           "state":"done", frames_written 48000, underruns 0
$ADB shell am broadcast $R -a $A.CALL_END --es tag e1
#   expect: call_ended "cause":"local:cmd"
$ADB shell am broadcast $R -a $A.STATUS --es tag s2
#   expect: "state":"NONE", "mode":"NORMAL"
```

Check the S31 console for the HFP call setup, SCO/eSCO open, and close, too (see risk 1 below).

## Command reference

All actions are `dev.larkbridge.harness.<ACTION>`, sent to `.ControlReceiver`. Any action takes
the optional `--es tag <id>`, which is echoed in that command's log lines so a script can match
replies.

| Action | Extras | Effect |
| --- | --- | --- |
| `CALL_START` | none | registers the account and calls `TelecomManager.placeCall` on it; the connection goes ACTIVE at once. Only one call at a time. |
| `CALL_END` | none | disconnects locally (`DisconnectCause.LOCAL`) |
| `PLAY_STIMULUS` | `--es asset <file>` (default `tone_1k_16k_3s.wav`, under `assets/stimulus/`), `--ei loops <n>` (default 1), `--es usage voice\|media` (default voice), `--ei progress_s <n>` (default 0, off) | plays the WAV with `USAGE_VOICE_COMMUNICATION` / `CONTENT_TYPE_SPEECH` (HFP/SCO), or with `--es usage media` as `USAGE_MEDIA` / `CONTENT_TYPE_MUSIC` (A2DP). With `progress_s`, it logs a `stimulus_progress` line (frames written and played, underruns, routed device) every n seconds; replaces any playing stimulus. It also plays without a call (logged as `"in_call":false`). When a call ends, a voice stimulus stops; a media stimulus keeps playing, like car media that a call interrupted (H11). |
| `STOP_STIMULUS` | none | stops playback |
| `SET_ROUTE` | `--es route bluetooth\|earpiece\|speaker\|wired` | requests a call audio route (`requestCallEndpointChange` on API 34+) |
| `STATUS` | none | logs one `"event":"status"` line |

```bash
adb shell am broadcast -f 0x20 -n dev.larkbridge.harness/.ControlReceiver -a dev.larkbridge.harness.CALL_START
adb shell am broadcast -f 0x20 -n dev.larkbridge.harness/.ControlReceiver -a dev.larkbridge.harness.CALL_END
adb shell am broadcast -f 0x20 -n dev.larkbridge.harness/.ControlReceiver -a dev.larkbridge.harness.PLAY_STIMULUS --es asset tone_1k_16k_3s.wav --ei loops 1
adb shell am broadcast -f 0x20 -n dev.larkbridge.harness/.ControlReceiver -a dev.larkbridge.harness.STOP_STIMULUS
adb shell am broadcast -f 0x20 -n dev.larkbridge.harness/.ControlReceiver -a dev.larkbridge.harness.SET_ROUTE --es route bluetooth
adb shell am broadcast -f 0x20 -n dev.larkbridge.harness/.ControlReceiver -a dev.larkbridge.harness.STATUS
```

**Foreground alternative:** the same actions and extras work through the invisible
`.ControlActivity`. It briefly brings the app to the foreground, which matters only if
`CALL_START` logs `fgs_refused` (Android can refuse a foreground-service start from a background
broadcast). The call itself works either way, because Telecom's binding keeps the process alive.

```bash
adb shell am start -n dev.larkbridge.harness/.ControlActivity -a dev.larkbridge.harness.CALL_START
```

**Access control:** both the receiver and `ControlActivity` are exported but require
`android.permission.DUMP`. The adb shell user holds it, and no third-party app can get it, so only
adb (or the system) can start harness calls.

## Output (logcat tag `LarkHarness`, one JSON object per line)

Each line has `event` and `ts_ms` (wall clock), plus `tag` when the command carried one. Events:
`cmd`, `call_requested`, `call_created`, `call_state`, `call_audio_state`, `call_endpoint`,
`call_endpoints_available`, `call_mute`, `call_ended`, `call_failed`, `route_requested`,
`route_changed`, `route_failed`, `stimulus_start`, `stimulus_end`, `stimulus_error`,
`stimulus_stop_requested`, `fgs_refused`, `fgs_failed`, `error` (with `error`:
`call_already_active`, `no_call`, `no_stimulus`, `bad_route`, `unknown_action`,
`outgoing_call_not_permitted`, `exception`), and `status`.

A `status` line looks like this:

```json
{"event":"status","tag":"s1","ts_ms":1764000000000,"sdk":36,
 "call":{"state":"ACTIVE","id":1,"duration_ms":4210,
         "audio_state":{"route":"BLUETOOTH","supported":"EARPIECE, BLUETOOTH, SPEAKER","muted":false,"bt_address":"..."},
         "endpoint":{"name":"LarkBridge","type":"BLUETOOTH"},"muted":false,
         "last_disconnect":"none","fgs":"running"},
 "audio":{"mode":"IN_COMMUNICATION","communication_device":{"type":"BLUETOOTH_SCO","product":"LarkBridge","address":"...","id":42},
          "sco_on":true,"sco_state":"connected","music_active":false,"mic_muted":false,"voice_call_volume":5},
 "stimulus":null,"perms":{"MANAGE_OWN_CALLS":true,"RECORD_AUDIO":true,"BLUETOOTH_CONNECT":true,"POST_NOTIFICATIONS":true},
 "account_registered":true,"stimuli":["tone_1k_16k_3s.wav"]}
```

(This is the expected shape, wrapped here for reading. On the device it's a single line.)

## Platform notes and open risks (check in the first morning run)

1. **HFP call indicators for self-managed calls.** Telecom routes the audio of a self-managed call
   to the active HFP device over SCO. Whether the Pixel's Bluetooth stack also sends HFP call
   status (`+CIEV` call/callsetup) for a self-managed call varies by Android version. If the S31
   gets SCO audio but no call-state indicators, H9 (audio path) still works, but H11's "call
   preempts media" check has to key off SCO/audio focus rather than HFP call state. Record what
   the S31 console shows on the first `CALL_START`.
2. **`CAPABILITY_SELF_MANAGED` is deprecated in API 37** in favor of the Jetpack Core-Telecom
   library. It still works, and it's what the plan specifies. Move to Core-Telecom only if a
   future Pixel OS drops it.
3. **targetSdk is 34**, with compileSdk 37. This keeps the Android 14 foreground-service rules
   (type `phoneCall`) and avoids opting into newer target-SDK behavior changes nobody has tested
   here.
4. **Foreground service from a background broadcast** may be refused (`fgs_refused`). The call and
   playback don't depend on it. Use `ControlActivity` if a later test needs the process at
   foreground priority.
5. Not built tonight: uplink capture (recording what the S31 sends to the phone). `RECORD_AUDIO`
   is already declared, so it can be added as a `RECORD_UPLINK` action if H9's uplink check needs
   the phone side rather than the S31 tap.

Uninstall: `adb uninstall dev.larkbridge.harness`.

## H9 uplink measurement

Keep the harness activity visible and grant `RECORD_AUDIO` before starting
the call. Send `CALL_START` with `--ez record_uplink true`. On Android 14
and later, the visible activity lets the app start its microphone foreground
service under the while-in-use permission rules. A call without the extra
uses the existing phone-call service alone.
[Android foreground service rules](https://developer.android.com/develop/background-work/services/fgs/restrictions-bg-start#wiu-restrictions)

The recorder uses the app's `VOICE_COMMUNICATION` source for the self-managed
call. It waits for the call's Bluetooth SCO communication device, then checks
the recorder's actual input route and rejects PCM from a handset mic. It
compares the input and output Bluetooth address when Android supplies both.
`VOICE_UPLINK` is a privileged telephony source.
[Android audio sources](https://developer.android.com/reference/android/media/MediaRecorder.AudioSource)

1. Start the opted-in call, then poll `STATUS` until `uplink.state` is
   `recording` and `uplink.routed_input.type` is `BLUETOOTH_SCO`. Compare
   the reported input to the S31. A route or read failure sets `state=error`.
2. Use a controlled signal at the Lark mic and compare `uplink.rms` and
   `frequency_hz` to that stimulus. AEC-locked firmware should produce
   silent uplink. Use a qualified image for an active-signal H9 result.
3. Check `frames` against `read_span_ms` at 16 kHz. Inspect
   `read_gaps_ge_100_ms` and `max_read_gap_ms` for recorder delivery gaps.
   `silent_windows` and `longest_silence_ms` describe the content in
   20 ms windows below RMS 32. These are app-side delivery measurements.
4. Send `CALL_END`, then poll `STATUS` for `uplink.state=done`, or collect
   the final `uplink_record_ended` log. A new call resets the prior result.
   The recorder discards the PCM after measurement.

For an in-place APK update from another checkout, set `DEBUG_KEYSTORE` to
the path of the debug keystore used for the installed APK before running
`build.sh`.
