package dev.larkbridge.harness;

import android.content.Context;
import android.os.Build;
import android.os.OutcomeReceiver;
import android.telecom.CallAudioState;
import android.telecom.CallEndpoint;
import android.telecom.CallEndpointException;
import android.telecom.Connection;
import android.telecom.DisconnectCause;
import android.telecom.TelecomManager;

import org.json.JSONException;
import org.json.JSONObject;

import java.util.Collections;
import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * A self-managed call with no far end. It goes active immediately; the "remote party" is only
 * whatever audio PLAY_STIMULUS writes with USAGE_VOICE_COMMUNICATION.
 */
final class HarnessConnection extends Connection {
    private static final AtomicInteger NEXT_ID = new AtomicInteger(1);

    final int id = NEXT_ID.getAndIncrement();
    private final Context ctx;
    private volatile CallEndpoint endpoint;
    private volatile List<CallEndpoint> available = Collections.emptyList();
    volatile boolean muted;
    private boolean gone;

    HarnessConnection(Context ctx) {
        this.ctx = ctx.getApplicationContext();
        setConnectionProperties(PROPERTY_SELF_MANAGED);
        setConnectionCapabilities(CAPABILITY_HOLD | CAPABILITY_SUPPORT_HOLD | CAPABILITY_MUTE);
        setAudioModeIsVoip(true);
        setAddress(Harness.CALL_ADDRESS, TelecomManager.PRESENTATION_ALLOWED);
        setCallerDisplayName("LarkBridge harness", TelecomManager.PRESENTATION_ALLOWED);
    }

    void endLocal(String why) {
        if (gone) return;
        gone = true;
        setDisconnected(new DisconnectCause(DisconnectCause.LOCAL, why));
        destroy();
        Harness.onConnectionGone(ctx, this, "local:" + why);
    }

    @Override
    public void onStateChanged(int state) {
        Harness.log("call_state", null, "call_id", id, "state", stateToString(state));
    }

    @Override public void onDisconnect() { endLocal("telecom_disconnect"); }

    @Override public void onAbort() { endLocal("telecom_abort"); }

    @Override public void onHold() { setOnHold(); }

    @Override public void onUnhold() { setActive(); }

    @Override
    @SuppressWarnings("deprecation")
    public void onCallAudioStateChanged(CallAudioState state) {
        try {
            Harness.log("call_audio_state", null, "call_id", id, "audio_state", Harness.callAudioStateJson(state));
        } catch (JSONException ignored) {
            // values are plain strings and booleans
        }
    }

    @Override
    public void onCallEndpointChanged(CallEndpoint e) {
        endpoint = e;
        try {
            Harness.log("call_endpoint", null, "call_id", id, "endpoint", Harness.endpointJson(e));
        } catch (JSONException ignored) {
            // values are plain strings
        }
    }

    @Override
    public void onAvailableCallEndpointsChanged(List<CallEndpoint> list) {
        available = list == null ? Collections.<CallEndpoint>emptyList() : list;
        try {
            Harness.log("call_endpoints_available", null, "call_id", id, "endpoints", Harness.endpointsJson(list));
        } catch (JSONException ignored) {
            // values are plain strings
        }
    }

    @Override
    public void onMuteStateChanged(boolean isMuted) {
        muted = isMuted;
        Harness.log("call_mute", null, "call_id", id, "muted", isMuted);
    }

    @SuppressWarnings("deprecation")
    CallAudioState getCallAudioStateCompat() {
        return getCallAudioState();
    }

    JSONObject endpointJson() throws JSONException {
        return Harness.endpointJson(endpoint);
    }

    /** route is a CallAudioState.ROUTE_* value. */
    @SuppressWarnings("deprecation")
    void requestRoute(int route) {
        if (Build.VERSION.SDK_INT >= 34) {
            int type = route == CallAudioState.ROUTE_BLUETOOTH ? CallEndpoint.TYPE_BLUETOOTH
                    : route == CallAudioState.ROUTE_SPEAKER ? CallEndpoint.TYPE_SPEAKER
                    : route == CallAudioState.ROUTE_WIRED_HEADSET ? CallEndpoint.TYPE_WIRED_HEADSET
                    : CallEndpoint.TYPE_EARPIECE;
            for (final CallEndpoint e : available) {
                if (e.getEndpointType() == type) {
                    requestCallEndpointChange(e, ctx.getMainExecutor(), new OutcomeReceiver<Void, CallEndpointException>() {
                        @Override public void onResult(Void r) {
                            Harness.log("route_changed", null, "call_id", id, "endpoint", String.valueOf(e.getEndpointName()));
                        }

                        @Override public void onError(CallEndpointException err) {
                            Harness.log("route_failed", null, "call_id", id, "code", err.getCode());
                        }
                    });
                    return;
                }
            }
            Harness.log("route_failed", null, "call_id", id, "error", "endpoint_not_available");
            return;
        }
        setAudioRoute(route);
    }
}
