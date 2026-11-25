package dev.larkbridge.harness;

import android.content.Context;
import android.content.Intent;
import android.media.session.MediaSession;
import android.media.session.PlaybackState;
import android.view.KeyEvent;

/**
 * A media session that observes AVRCP passthrough keys (Astra case 5, H12). The Pixel's AVRCP
 * target turns the S31's passthrough commands into media key events and dispatches them to the
 * active session. Every key is logged as {"event":"media_key",...} with its action (down/up),
 * key code and repeat count, and consumed: nothing plays, pauses or changes volume. Pair it with
 * a quiet media stimulus so Android keeps this session as the media-button target.
 */
final class MediaKeySession {
    private static MediaSession session;
    private static String sessionTag;
    private static int keys;

    private MediaKeySession() {}

    static synchronized void start(Context ctx, String tag) {
        if (session == null) {
            session = new MediaSession(ctx, "LarkHarnessKeys");
            session.setCallback(new MediaSession.Callback() {
                @Override
                public boolean onMediaButtonEvent(Intent intent) {
                    KeyEvent key = intent.getParcelableExtra(Intent.EXTRA_KEY_EVENT, KeyEvent.class);
                    if (key == null) {
                        Harness.log("media_key", sessionTag, "error", "no_key_event");
                        return true;
                    }
                    keys++;
                    Harness.log("media_key", sessionTag,
                            "action", key.getAction() == KeyEvent.ACTION_DOWN ? "down"
                                    : key.getAction() == KeyEvent.ACTION_UP ? "up" : "other",
                            "keycode", KeyEvent.keyCodeToString(key.getKeyCode()),
                            "repeat", key.getRepeatCount(),
                            "down_time", key.getDownTime(),
                            "event_time", key.getEventTime(),
                            "count", keys);
                    return true; // consume: the harness never acts on keys
                }
            });
        }
        sessionTag = tag;
        session.setPlaybackState(new PlaybackState.Builder()
                .setActions(PlaybackState.ACTION_PLAY | PlaybackState.ACTION_PAUSE
                        | PlaybackState.ACTION_PLAY_PAUSE | PlaybackState.ACTION_STOP
                        | PlaybackState.ACTION_SKIP_TO_NEXT | PlaybackState.ACTION_SKIP_TO_PREVIOUS)
                .setState(PlaybackState.STATE_PLAYING, 0, 1.0f)
                .build());
        session.setActive(true);
        Harness.log("media_session", tag, "state", "active", "keys", keys);
    }

    static synchronized void stop(String tag) {
        if (session != null) {
            session.setActive(false);
            session.release();
            session = null;
        }
        Harness.log("media_session", tag, "state", "released", "keys", keys);
    }

    static synchronized boolean active() {
        return session != null;
    }
}
