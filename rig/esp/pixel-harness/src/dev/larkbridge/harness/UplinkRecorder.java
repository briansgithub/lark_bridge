package dev.larkbridge.harness;

import android.content.Context;
import android.media.AudioDeviceInfo;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.os.SystemClock;

import org.json.JSONException;
import org.json.JSONObject;

/** Measures the call's Bluetooth microphone stream. PCM is discarded after analysis. */
final class UplinkRecorder extends Thread {
    private static final int RATE = 16000;
    private static final int CHUNK = RATE / 50;
    private static final int SILENCE_RMS = 32;
    private static final int CROSSING_THRESHOLD = 64;
    private static final int GAP_MS = 100;
    private static final int ROUTE_TIMEOUT_MS = 8000;

    private final AudioManager manager;
    private final int callId;
    private final String tag;
    private volatile boolean cancelled;
    private volatile JSONObject status;
    private long frames, windows, silentWindows, squareSum, crossings, crossingFrames;
    private long signalFrames, lastCrossing, silentFrames, longestSilence, firstRead, lastRead;
    private long gaps, maxGap;
    private boolean crossingArmed;
    private AudioDeviceInfo routed;

    UplinkRecorder(Context context, int callId, String tag) {
        super("harness-uplink");
        manager = context.getSystemService(AudioManager.class);
        this.callId = callId;
        this.tag = tag;
        publish("waiting_for_sco", null);
    }

    void cancel() { cancelled = true; }
    JSONObject statusJson() { return status; }

    @Override
    public void run() {
        AudioRecord record = null;
        String state = "done";
        String error = null;
        try {
            long deadline = SystemClock.elapsedRealtime() + ROUTE_TIMEOUT_MS;
            AudioDeviceInfo communication;
            do {
                if (cancelled) return;
                communication = manager.getCommunicationDevice();
                if (isSco(communication)) break;
                SystemClock.sleep(20);
            } while (SystemClock.elapsedRealtime() < deadline);
            if (!isSco(communication)) throw new IllegalStateException("no Bluetooth SCO communication device");

            int minimum = AudioRecord.getMinBufferSize(
                    RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
            if (minimum <= 0) throw new IllegalStateException("unsupported 16 kHz input: " + minimum);
            record = new AudioRecord.Builder()
                    .setAudioSource(MediaRecorder.AudioSource.VOICE_COMMUNICATION)
                    .setAudioFormat(new AudioFormat.Builder().setSampleRate(RATE)
                            .setChannelMask(AudioFormat.CHANNEL_IN_MONO)
                            .setEncoding(AudioFormat.ENCODING_PCM_16BIT).build())
                    .setBufferSizeInBytes(Math.max(minimum, CHUNK * 8)).build();
            record.startRecording();

            short[] samples = new short[CHUNK];
            int used = 0;
            deadline = SystemClock.elapsedRealtime() + ROUTE_TIMEOUT_MS;
            long lastPublish = 0;
            while (!cancelled) {
                int count = record.read(samples, used, CHUNK - used, AudioRecord.READ_NON_BLOCKING);
                if (count < 0) throw new IllegalStateException("AudioRecord read: " + count);
                long now = SystemClock.elapsedRealtime();
                AudioDeviceInfo input = record.getRoutedDevice();
                if (!matchesSco(communication, input) || (routed != null && input.getId() != routed.getId())) {
                    if (routed != null || now >= deadline) {
                        throw new IllegalStateException("Bluetooth SCO recorder route unavailable or changed");
                    }
                    used = 0;
                    SystemClock.sleep(10);
                    continue;
                }
                if (count == 0) {
                    if (lastRead == 0 && now >= deadline) {
                        throw new IllegalStateException("Bluetooth SCO delivered no PCM");
                    }
                    if (lastRead != 0 && now - lastRead > 2000) {
                        throw new IllegalStateException("Bluetooth SCO PCM stalled");
                    }
                    SystemClock.sleep(10);
                    continue;
                }
                routed = input;
                if (firstRead == 0) firstRead = now;
                if (lastRead != 0) {
                    long gap = now - lastRead;
                    maxGap = Math.max(maxGap, gap);
                    if (gap >= GAP_MS) {
                        gaps++;
                        crossingArmed = false;
                        lastCrossing = 0;
                        silentFrames = 0;
                        System.arraycopy(samples, used, samples, 0, count);
                        used = 0;
                    }
                }
                lastRead = now;
                used += count;
                if (used == CHUNK) {
                    measure(samples);
                    used = 0;
                }
                if (now - lastPublish >= 250) {
                    publish("recording", null);
                    lastPublish = now;
                }
            }
        } catch (RuntimeException e) {
            state = "error";
            error = e.toString();
        } finally {
            if (record != null) record.release();
            publish(state, error);
            Harness.log("uplink_record_ended", tag, "uplink", status);
        }
    }

    private static boolean isSco(AudioDeviceInfo device) {
        return device != null && device.getType() == AudioDeviceInfo.TYPE_BLUETOOTH_SCO;
    }

    private static boolean matchesSco(AudioDeviceInfo communication, AudioDeviceInfo input) {
        if (!isSco(input)) return false;
        String outputAddress = communication.getAddress();
        String inputAddress = input.getAddress();
        return outputAddress.isEmpty() || inputAddress.isEmpty() || outputAddress.equals(inputAddress);
    }

    private void measure(short[] samples) {
        long energy = 0;
        for (short sample : samples) energy += (long) sample * sample;
        squareSum += energy;
        frames += CHUNK;
        windows++;
        if (energy < (long) SILENCE_RMS * SILENCE_RMS * CHUNK) {
            silentWindows++;
            silentFrames += CHUNK;
            longestSilence = Math.max(longestSilence, silentFrames);
            crossingArmed = false;
            lastCrossing = 0;
            return;
        }
        silentFrames = 0;
        for (short sample : samples) {
            signalFrames++;
            if (sample <= -CROSSING_THRESHOLD) crossingArmed = true;
            if (sample < CROSSING_THRESHOLD || !crossingArmed) continue;
            crossingArmed = false;
            if (lastCrossing != 0) {
                crossings++;
                crossingFrames += signalFrames - lastCrossing;
            }
            lastCrossing = signalFrames;
        }
    }

    private void publish(String state, String error) {
        JSONObject snapshot = new JSONObject();
        try {
            snapshot.put("state", state);
            snapshot.put("call_id", callId);
            if (error != null) snapshot.put("error", error);
            snapshot.put("routed_input", routed == null ? JSONObject.NULL : Harness.deviceJson(routed));
            snapshot.put("sample_rate", RATE);
            snapshot.put("frames", frames);
            snapshot.put("read_span_ms", lastRead - firstRead);
            snapshot.put("rms", frames == 0 ? 0 : Math.sqrt((double) squareSum / frames));
            snapshot.put("frequency_hz", crossingFrames == 0 ? JSONObject.NULL
                    : (double) RATE * crossings / crossingFrames);
            snapshot.put("windows", windows);
            snapshot.put("silent_windows", silentWindows);
            snapshot.put("longest_silence_ms", longestSilence * 1000 / RATE);
            snapshot.put("read_gaps_ge_100_ms", gaps);
            snapshot.put("max_read_gap_ms", maxGap);
        } catch (JSONException e) {
            throw new IllegalStateException(e);
        }
        status = snapshot;
    }
}
