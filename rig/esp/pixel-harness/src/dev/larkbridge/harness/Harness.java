package dev.larkbridge.harness;

import android.Manifest;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.PackageManager;
import android.media.AudioDeviceInfo;
import android.media.AudioManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.SystemClock;
import android.telecom.CallAudioState;
import android.telecom.CallEndpoint;
import android.telecom.Connection;
import android.telecom.PhoneAccount;
import android.telecom.PhoneAccountHandle;
import android.telecom.TelecomManager;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.util.List;

/**
 * Command dispatch and shared state. Every method runs on the main thread (broadcast receiver,
 * activity, and Telecom Connection callbacks all arrive there), except StimulusPlayer, which only
 * touches its own fields.
 *
 * Every log line is one JSON object on tag {@link #TAG}, so the rig can parse
 * {@code adb logcat -s LarkHarness:I -v raw} line by line.
 */
final class Harness {
    static final String TAG = "LarkHarness";
    static final String PREFIX = "dev.larkbridge.harness.";
    static final String CALL_START = PREFIX + "CALL_START";
    static final String CALL_END = PREFIX + "CALL_END";
    static final String PLAY_STIMULUS = PREFIX + "PLAY_STIMULUS";
    static final String STOP_STIMULUS = PREFIX + "STOP_STIMULUS";
    static final String SET_ROUTE = PREFIX + "SET_ROUTE";
    static final String STATUS = PREFIX + "STATUS";
    static final String MEDIA_SESSION_START = PREFIX + "MEDIA_SESSION_START";
    static final String MEDIA_SESSION_STOP = PREFIX + "MEDIA_SESSION_STOP";

    static final String EXTRA_ASSET = "asset";   // PLAY_STIMULUS: file under assets/stimulus/
    static final String EXTRA_LOOPS = "loops";   // PLAY_STIMULUS: repeat count, default 1
    static final String EXTRA_ROUTE = "route";   // SET_ROUTE: bluetooth|earpiece|speaker|wired
    static final String EXTRA_USAGE = "usage";   // PLAY_STIMULUS: voice (default) | media (A2DP)
    static final String EXTRA_PROGRESS = "progress_s"; // PLAY_STIMULUS: progress log period, 0 = off
    static final String EXTRA_RECORD_UPLINK = "record_uplink"; // CALL_START opt-in
    static final String EXTRA_TAG = "tag";       // any command: echoed in its log lines

    static final String DEFAULT_ASSET = "tone_1k_16k_3s.wav";
    /** A SIP-scheme address that is never resolvable; nothing is ever dialed with it anyway. */
    static final Uri CALL_ADDRESS = Uri.fromParts(PhoneAccount.SCHEME_SIP, "harness@larkbridge.invalid", null);
    private static final String ACCOUNT_ID = "larkbridge-harness";

    private static HarnessConnection connection;
    private static String pendingTag;
    private static String lastDisconnect = "none";
    private static long callStartedElapsed;
    private static StimulusPlayer player;
    private static String fgsState = "stopped";
    private static boolean recordUplink;
    private static UplinkRecorder uplink;
    private static Context recordingContext;
    private static String recordingTag;
    private static String recordingError;

    private Harness() {}

    // ---------------------------------------------------------------- dispatch

    static void handle(Context ctx, Intent intent, String via) {
        Context app = ctx.getApplicationContext();
        String action = intent == null ? null : intent.getAction();
        String tag = intent == null ? null : intent.getStringExtra(EXTRA_TAG);
        String cmd = action != null && action.startsWith(PREFIX) ? action.substring(PREFIX.length()) : String.valueOf(action);
        log("cmd", tag, "cmd", cmd, "via", via);
        try {
            if (CALL_START.equals(action)) {
                callStart(app, tag, intent.getBooleanExtra(EXTRA_RECORD_UPLINK, false));
            } else if (CALL_END.equals(action)) {
                callEnd(tag);
            } else if (PLAY_STIMULUS.equals(action)) {
                String asset = intent.getStringExtra(EXTRA_ASSET);
                playStimulus(app, tag, asset == null ? DEFAULT_ASSET : asset, intent.getIntExtra(EXTRA_LOOPS, 1),
                        "media".equals(intent.getStringExtra(EXTRA_USAGE)), intent.getIntExtra(EXTRA_PROGRESS, 0));
            } else if (STOP_STIMULUS.equals(action)) {
                stopStimulus(tag);
            } else if (SET_ROUTE.equals(action)) {
                setRoute(tag, intent.getStringExtra(EXTRA_ROUTE));
            } else if (STATUS.equals(action)) {
                status(app, tag);
            } else if (MEDIA_SESSION_START.equals(action)) {
                MediaKeySession.start(app, tag);
            } else if (MEDIA_SESSION_STOP.equals(action)) {
                MediaKeySession.stop(tag);
            } else {
                log("error", tag, "error", "unknown_action", "action", action);
            }
        } catch (RuntimeException e) {
            log("error", tag, "error", "exception", "cmd", cmd, "detail", e.toString());
        }
    }

    // ---------------------------------------------------------------- telecom

    static PhoneAccountHandle accountHandle(Context ctx) {
        return new PhoneAccountHandle(new ComponentName(ctx, HarnessConnectionService.class), ACCOUNT_ID);
    }

    /**
     * Idempotent; a self-managed account is enabled as soon as it is registered.
     * CAPABILITY_SELF_MANAGED is deprecated in API 37 in favor of Jetpack Core-Telecom, but it
     * still works and keeps this app dependency-free. See README "Platform notes".
     */
    @SuppressWarnings("deprecation")
    static void registerAccount(Context ctx) {
        PhoneAccount account = PhoneAccount.builder(accountHandle(ctx), "LarkBridge Harness")
                .setCapabilities(PhoneAccount.CAPABILITY_SELF_MANAGED)
                .addSupportedUriScheme(PhoneAccount.SCHEME_SIP)
                .setShortDescription("Local test calls; dials no one")
                .build();
        ctx.getSystemService(TelecomManager.class).registerPhoneAccount(account);
    }

    private static void callStart(Context ctx, String tag, boolean record) {
        if (connection != null || fgsState.equals("requested")) {
            log("error", tag, "error", "call_already_active");
            return;
        }
        TelecomManager tm = ctx.getSystemService(TelecomManager.class);
        registerAccount(ctx);
        PhoneAccountHandle handle = accountHandle(ctx);
        if (!tm.isOutgoingCallPermitted(handle)) {
            log("error", tag, "error", "outgoing_call_not_permitted");
            return;
        }
        Bundle extras = new Bundle();
        extras.putParcelable(TelecomManager.EXTRA_PHONE_ACCOUNT_HANDLE, handle);
        extras.putBoolean(TelecomManager.EXTRA_START_CALL_WITH_SPEAKERPHONE, false);
        pendingTag = tag;
        recordUplink = record;
        uplink = null;
        recordingContext = record ? ctx : null;
        recordingTag = record ? tag : null;
        recordingError = null;
        startForeground(ctx, tag);
        try {
            tm.placeCall(CALL_ADDRESS, extras);
        } catch (RuntimeException e) {
            onConnectionFailed(ctx, e.toString());
            throw e;
        }
        log("call_requested", tag);
    }

    private static void callEnd(String tag) {
        if (connection == null) {
            log("error", tag, "error", "no_call");
            return;
        }
        connection.endLocal("cmd");
    }

    private static void setRoute(String tag, String route) {
        if (connection == null) {
            log("error", tag, "error", "no_call");
            return;
        }
        int r;
        if ("bluetooth".equals(route)) r = CallAudioState.ROUTE_BLUETOOTH;
        else if ("earpiece".equals(route)) r = CallAudioState.ROUTE_EARPIECE;
        else if ("speaker".equals(route)) r = CallAudioState.ROUTE_SPEAKER;
        else if ("wired".equals(route)) r = CallAudioState.ROUTE_WIRED_HEADSET;
        else {
            log("error", tag, "error", "bad_route", "route", route);
            return;
        }
        connection.requestRoute(r);
        log("route_requested", tag, "route", route);
    }

    // Called from HarnessConnectionService / HarnessConnection.

    static void onConnectionCreated(HarnessConnection c) {
        connection = c;
        callStartedElapsed = SystemClock.elapsedRealtime();
        log("call_created", pendingTag, "call_id", c.id);
        pendingTag = null;
        maybeRecordUplink();
    }

    static void onConnectionFailed(Context ctx, String why) {
        log("call_failed", pendingTag, "reason", why);
        pendingTag = null;
        stopUplink();
        stopForeground(ctx);
    }

    static void onConnectionGone(Context ctx, HarnessConnection c, String cause) {
        if (connection != c) return;
        long dur = SystemClock.elapsedRealtime() - callStartedElapsed;
        connection = null;
        lastDisconnect = cause;
        // A media stimulus stands in for car media that a call interrupts (H11):
        // it must outlive the call. Voice stimuli belong to the call.
        if (player != null && !player.isMedia()) stopStimulus(null);
        stopUplink();
        stopForeground(ctx);
        log("call_ended", null, "call_id", c.id, "cause", cause, "duration_ms", dur);
    }

    // ---------------------------------------------------------------- foreground service

    private static void startForeground(Context ctx, String tag) {
        try {
            ctx.startForegroundService(new Intent(ctx, CallForegroundService.class)
                    .putExtra(EXTRA_RECORD_UPLINK, recordUplink));
            fgsState = "requested";
        } catch (RuntimeException e) {
            // Background FGS start can be refused (Android 12+). The call still works: Telecom's
            // binding to HarnessConnectionService keeps the process alive for the call.
            fgsState = "refused";
            if (recordUplink) recordingError = e.toString();
            log("fgs_refused", tag, "detail", e.toString());
        }
    }

    private static void stopForeground(Context ctx) {
        ctx.stopService(new Intent(ctx, CallForegroundService.class));
        fgsState = "stopped";
    }

    static void onForegroundStarted(boolean ok, String detail) {
        fgsState = ok ? "running" : "failed";
        if (!ok) {
            if (recordUplink) recordingError = detail;
            log("fgs_failed", null, "detail", detail);
        }
        maybeRecordUplink();
    }

    private static void maybeRecordUplink() {
        if (!recordUplink || uplink != null || connection == null || !"running".equals(fgsState)) return;
        uplink = new UplinkRecorder(recordingContext, connection.id, recordingTag);
        uplink.start();
    }

    private static void stopUplink() {
        if (uplink != null) uplink.cancel();
        if (recordUplink && uplink == null && recordingError == null) {
            recordingError = "call ended before uplink recording started";
        }
        recordingContext = null;
        recordingTag = null;
    }

    // ---------------------------------------------------------------- stimulus

    private static void playStimulus(Context ctx, String tag, String asset, int loops, boolean media,
                                     int progressSeconds) {
        stopStimulus(null);
        String path = asset.contains("/") ? asset : "stimulus/" + asset;
        player = new StimulusPlayer(ctx, path, Math.max(1, loops), tag, connection != null, media,
                progressSeconds);
        player.start();
    }

    private static void stopStimulus(String tag) {
        if (player != null) {
            player.cancel();
            player = null;
            if (tag != null) log("stimulus_stop_requested", tag);
        } else if (tag != null) {
            log("error", tag, "error", "no_stimulus");
        }
    }

    // ---------------------------------------------------------------- status

    @SuppressWarnings("deprecation") // isBluetoothScoOn: still the plainest SCO signal
    private static void status(Context ctx, String tag) {
        JSONObject o = new JSONObject();
        try {
            o.put("event", "status");
            if (tag != null) o.put("tag", tag);
            o.put("ts_ms", System.currentTimeMillis());
            o.put("sdk", Build.VERSION.SDK_INT);

            JSONObject call = new JSONObject();
            if (connection == null) {
                call.put("state", "NONE");
            } else {
                call.put("state", Connection.stateToString(connection.getState()));
                call.put("id", connection.id);
                call.put("duration_ms", SystemClock.elapsedRealtime() - callStartedElapsed);
                call.put("audio_state", callAudioStateJson(connection.getCallAudioStateCompat()));
                call.put("endpoint", connection.endpointJson());
                call.put("muted", connection.muted);
            }
            call.put("last_disconnect", lastDisconnect);
            call.put("fgs", fgsState);
            o.put("call", call);

            AudioManager am = ctx.getSystemService(AudioManager.class);
            JSONObject audio = new JSONObject();
            audio.put("mode", modeName(am.getMode()));
            audio.put("communication_device", deviceJson(am.getCommunicationDevice()));
            audio.put("sco_on", am.isBluetoothScoOn());
            audio.put("sco_state", scoStateName(ctx));
            audio.put("music_active", am.isMusicActive());
            audio.put("mic_muted", am.isMicrophoneMute());
            audio.put("voice_call_volume", am.getStreamVolume(AudioManager.STREAM_VOICE_CALL));
            o.put("audio", audio);

            StimulusPlayer p = player;
            o.put("stimulus", p == null ? JSONObject.NULL : p.statusJson());
            if (uplink != null) {
                o.put("uplink", uplink.statusJson());
            } else if (recordUplink) {
                JSONObject capture = new JSONObject();
                capture.put("state", recordingError == null ? "waiting_for_foreground_service" : "error");
                if (recordingError != null) capture.put("error", recordingError);
                o.put("uplink", capture);
            } else {
                o.put("uplink", JSONObject.NULL);
            }

            JSONObject perms = new JSONObject();
            for (String perm : new String[] {Manifest.permission.MANAGE_OWN_CALLS, Manifest.permission.RECORD_AUDIO,
                    Manifest.permission.BLUETOOTH_CONNECT, Manifest.permission.POST_NOTIFICATIONS}) {
                perms.put(perm.substring(perm.lastIndexOf('.') + 1),
                        ctx.checkSelfPermission(perm) == PackageManager.PERMISSION_GRANTED);
            }
            o.put("perms", perms);

            TelecomManager tm = ctx.getSystemService(TelecomManager.class);
            try {
                o.put("account_registered", tm.getPhoneAccount(accountHandle(ctx)) != null);
            } catch (SecurityException e) {
                // getPhoneAccount needs READ_PHONE_NUMBERS on recent Android; the harness doesn't ask for it.
                o.put("account_registered", "unknown: " + e.getMessage());
            }

            String[] assets = ctx.getAssets().list("stimulus");
            o.put("stimuli", new JSONArray(assets == null ? new String[0] : assets));
        } catch (JSONException | java.io.IOException | RuntimeException e) {
            try {
                o.put("status_error", e.toString());
            } catch (JSONException ignored) {
                // cannot happen for a String value
            }
        }
        Log.i(TAG, o.toString());
    }

    static JSONObject callAudioStateJson(CallAudioState s) throws JSONException {
        if (s == null) return null;
        JSONObject o = new JSONObject();
        o.put("route", CallAudioState.audioRouteToString(s.getRoute()));
        o.put("supported", CallAudioState.audioRouteToString(s.getSupportedRouteMask()));
        o.put("muted", s.isMuted());
        if (s.getActiveBluetoothDevice() != null) {
            o.put("bt_address", s.getActiveBluetoothDevice().getAddress());
        }
        return o;
    }

    static JSONObject endpointJson(CallEndpoint e) throws JSONException {
        if (e == null) return null;
        JSONObject o = new JSONObject();
        o.put("name", String.valueOf(e.getEndpointName()));
        o.put("type", endpointTypeName(e.getEndpointType()));
        return o;
    }

    static JSONArray endpointsJson(List<CallEndpoint> list) throws JSONException {
        JSONArray a = new JSONArray();
        if (list != null) for (CallEndpoint e : list) a.put(endpointJson(e));
        return a;
    }

    static JSONObject deviceJson(AudioDeviceInfo d) throws JSONException {
        if (d == null) return null;
        JSONObject o = new JSONObject();
        o.put("type", deviceTypeName(d.getType()));
        o.put("product", String.valueOf(d.getProductName()));
        o.put("address", d.getAddress());
        o.put("id", d.getId());
        return o;
    }

    @SuppressWarnings("deprecation") // the SCO sticky broadcast still reports HFP SCO state
    private static String scoStateName(Context ctx) {
        IntentFilter f = new IntentFilter(AudioManager.ACTION_SCO_AUDIO_STATE_UPDATED);
        Intent sticky = Build.VERSION.SDK_INT >= 33
                ? ctx.registerReceiver(null, f, Context.RECEIVER_NOT_EXPORTED)
                : ctx.registerReceiver(null, f);
        if (sticky == null) return "unknown";
        switch (sticky.getIntExtra(AudioManager.EXTRA_SCO_AUDIO_STATE, -99)) {
            case AudioManager.SCO_AUDIO_STATE_CONNECTED: return "connected";
            case AudioManager.SCO_AUDIO_STATE_CONNECTING: return "connecting";
            case AudioManager.SCO_AUDIO_STATE_DISCONNECTED: return "disconnected";
            case AudioManager.SCO_AUDIO_STATE_ERROR: return "error";
            default: return "unknown";
        }
    }

    static String modeName(int m) {
        switch (m) {
            case AudioManager.MODE_NORMAL: return "NORMAL";
            case AudioManager.MODE_RINGTONE: return "RINGTONE";
            case AudioManager.MODE_IN_CALL: return "IN_CALL";
            case AudioManager.MODE_IN_COMMUNICATION: return "IN_COMMUNICATION";
            case AudioManager.MODE_CALL_SCREENING: return "CALL_SCREENING";
            default: return "MODE_" + m;
        }
    }

    static String endpointTypeName(int t) {
        switch (t) {
            case CallEndpoint.TYPE_EARPIECE: return "EARPIECE";
            case CallEndpoint.TYPE_BLUETOOTH: return "BLUETOOTH";
            case CallEndpoint.TYPE_WIRED_HEADSET: return "WIRED_HEADSET";
            case CallEndpoint.TYPE_SPEAKER: return "SPEAKER";
            case CallEndpoint.TYPE_STREAMING: return "STREAMING";
            default: return "TYPE_" + t;
        }
    }

    static String deviceTypeName(int t) {
        switch (t) {
            case AudioDeviceInfo.TYPE_BUILTIN_EARPIECE: return "BUILTIN_EARPIECE";
            case AudioDeviceInfo.TYPE_BUILTIN_SPEAKER: return "BUILTIN_SPEAKER";
            case AudioDeviceInfo.TYPE_BLUETOOTH_SCO: return "BLUETOOTH_SCO";
            case AudioDeviceInfo.TYPE_BLUETOOTH_A2DP: return "BLUETOOTH_A2DP";
            case AudioDeviceInfo.TYPE_BLE_HEADSET: return "BLE_HEADSET";
            case AudioDeviceInfo.TYPE_WIRED_HEADSET: return "WIRED_HEADSET";
            case AudioDeviceInfo.TYPE_WIRED_HEADPHONES: return "WIRED_HEADPHONES";
            case AudioDeviceInfo.TYPE_USB_HEADSET: return "USB_HEADSET";
            case AudioDeviceInfo.TYPE_HEARING_AID: return "HEARING_AID";
            default: return "TYPE_" + t;
        }
    }

    // ---------------------------------------------------------------- logging

    /** Logs {"event":event,"tag":tag,k1:v1,...} as one line. */
    static void log(String event, String tag, Object... kv) {
        JSONObject o = new JSONObject();
        try {
            o.put("event", event);
            if (tag != null) o.put("tag", tag);
            o.put("ts_ms", System.currentTimeMillis());
            for (int i = 0; i + 1 < kv.length; i += 2) {
                o.put(String.valueOf(kv[i]), kv[i + 1] == null ? JSONObject.NULL : kv[i + 1]);
            }
        } catch (JSONException e) {
            Log.w(TAG, "{\"event\":\"log_error\"}");
            return;
        }
        Log.i(TAG, o.toString());
    }
}
