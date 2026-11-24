package dev.larkbridge.harness;

import android.content.Context;
import android.media.AudioAttributes;
import android.media.AudioFormat;
import android.media.AudioTrack;
import android.os.SystemClock;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;

/**
 * Plays a bundled 16-bit PCM WAV as call downlink audio (USAGE_VOICE_COMMUNICATION), or as media
 * (USAGE_MEDIA, so it routes to A2DP) when {@code media} is set.
 */
final class StimulusPlayer extends Thread {
    private final Context ctx;
    private final String path;
    private final int loops;
    private final String tag;
    private final boolean inCall;
    private final boolean media;
    private final int progressSeconds;
    private volatile boolean cancelled;
    private volatile long framesWritten;
    private volatile long totalFrames;
    private volatile int rate;
    private volatile String state = "starting";

    StimulusPlayer(Context ctx, String path, int loops, String tag, boolean inCall, boolean media,
                   int progressSeconds) {
        super("LarkHarness-stimulus");
        this.ctx = ctx;
        this.path = path;
        this.loops = loops;
        this.tag = tag;
        this.inCall = inCall;
        this.media = media;
        this.progressSeconds = Math.max(0, progressSeconds);
    }

    void cancel() {
        cancelled = true;
        interrupt();
    }

    JSONObject statusJson() throws JSONException {
        JSONObject o = new JSONObject();
        o.put("asset", path);
        o.put("state", state);
        o.put("loops", loops);
        o.put("usage", media ? "media" : "voice");
        o.put("frames_written", framesWritten);
        o.put("frames_total", totalFrames);
        o.put("rate", rate);
        return o;
    }

    @Override
    public void run() {
        AudioTrack track = null;
        try {
            byte[] wav = readAsset(path);
            ByteBuffer b = ByteBuffer.wrap(wav).order(ByteOrder.LITTLE_ENDIAN);
            if (wav.length < 12 || b.getInt(0) != 0x46464952 /*RIFF*/ || b.getInt(8) != 0x45564157 /*WAVE*/) {
                throw new IOException("not a RIFF/WAVE file");
            }
            int channels = 0, bits = 0, fmt = 0, dataOff = -1, dataLen = 0;
            int pos = 12;
            while (pos + 8 <= wav.length) {
                int id = b.getInt(pos);
                int len = b.getInt(pos + 4);
                if (id == 0x20746d66 /*fmt */) {
                    fmt = b.getShort(pos + 8) & 0xffff;
                    channels = b.getShort(pos + 10) & 0xffff;
                    rate = b.getInt(pos + 12);
                    bits = b.getShort(pos + 22) & 0xffff;
                } else if (id == 0x61746164 /*data*/) {
                    dataOff = pos + 8;
                    dataLen = Math.min(len, wav.length - dataOff);
                    break;
                }
                pos += 8 + len + (len & 1);
            }
            if (fmt != 1 || bits != 16 || (channels != 1 && channels != 2) || dataOff < 0) {
                throw new IOException("need PCM16 mono/stereo; got fmt=" + fmt + " bits=" + bits + " ch=" + channels);
            }
            int frameBytes = 2 * channels;
            long framesPerLoop = dataLen / frameBytes;
            totalFrames = framesPerLoop * loops;

            AudioFormat af = new AudioFormat.Builder()
                    .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                    .setSampleRate(rate)
                    .setChannelMask(channels == 1 ? AudioFormat.CHANNEL_OUT_MONO : AudioFormat.CHANNEL_OUT_STEREO)
                    .build();
            int minBuf = AudioTrack.getMinBufferSize(rate, af.getChannelMask(), AudioFormat.ENCODING_PCM_16BIT);
            track = new AudioTrack.Builder()
                    .setAudioAttributes(new AudioAttributes.Builder()
                            .setUsage(media ? AudioAttributes.USAGE_MEDIA : AudioAttributes.USAGE_VOICE_COMMUNICATION)
                            .setContentType(media ? AudioAttributes.CONTENT_TYPE_MUSIC : AudioAttributes.CONTENT_TYPE_SPEECH)
                            .build())
                    .setAudioFormat(af)
                    .setTransferMode(AudioTrack.MODE_STREAM)
                    .setBufferSizeInBytes(Math.max(minBuf * 2, rate * frameBytes / 5))
                    .build();
            track.play();
            long t0 = SystemClock.elapsedRealtime();
            state = "playing";
            Harness.log("stimulus_start", tag, "asset", path, "rate", rate, "channels", channels,
                    "frames_total", totalFrames, "in_call", inCall, "usage", media ? "media" : "voice",
                    "routed_device", Harness.deviceJson(track.getRoutedDevice()));

            int chunk = (rate / 50) * frameBytes; // 20 ms
            long nextProgress = progressSeconds > 0 ? t0 + 1000L * progressSeconds : Long.MAX_VALUE;
            outer:
            for (int l = 0; l < loops; l++) {
                for (int off = 0; off < framesPerLoop * frameBytes;) {
                    if (cancelled) break outer;
                    int n = (int) Math.min(chunk, framesPerLoop * frameBytes - off);
                    int w = track.write(wav, dataOff + off, n);
                    if (cancelled) break outer;
                    if (w <= 0 || w % frameBytes != 0) {
                        throw new IOException("AudioTrack.write=" + w);
                    }
                    off += w;
                    framesWritten += w / frameBytes;
                    long now = SystemClock.elapsedRealtime();
                    if (now >= nextProgress) {
                        nextProgress = now + 1000L * progressSeconds;
                        Harness.log("stimulus_progress", tag, "loop", l, "frames_written", framesWritten,
                                "frames_played", track.getPlaybackHeadPosition() & 0xffffffffL,
                                "underruns", track.getUnderrunCount(), "elapsed_ms", now - t0,
                                "routed_device", Harness.deviceJson(track.getRoutedDevice()));
                    }
                }
            }
            // Wait for the tail to drain, then report.
            while (!cancelled && track.getPlaybackHeadPosition() < framesWritten) {
                long before = track.getPlaybackHeadPosition();
                SystemClock.sleep(20);
                if (track.getPlaybackHeadPosition() == before && SystemClock.elapsedRealtime() - t0 > 1000 + 1000 * totalFrames / rate * 2) {
                    break; // head stalled well past the expected end
                }
            }
            state = cancelled ? "cancelled" : "done";
            Harness.log("stimulus_end", tag, "asset", path, "state", state,
                    "frames_written", framesWritten, "frames_played", track.getPlaybackHeadPosition() & 0xffffffffL,
                    "underruns", track.getUnderrunCount(), "elapsed_ms", SystemClock.elapsedRealtime() - t0,
                    "routed_device", Harness.deviceJson(track.getRoutedDevice()));
        } catch (IOException | JSONException | RuntimeException e) {
            state = "error";
            Harness.log("stimulus_error", tag, "asset", path, "detail", e.toString());
        } finally {
            if (track != null) {
                try {
                    track.stop();
                } catch (IllegalStateException ignored) {
                    // never started
                }
                track.release();
            }
        }
    }

    private byte[] readAsset(String p) throws IOException {
        try (InputStream in = ctx.getAssets().open(p)) {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[16384];
            for (int n; (n = in.read(buf)) > 0; ) out.write(buf, 0, n);
            return out.toByteArray();
        }
    }
}
