#include "output_audio.h"

#include "bridge_rate.h"
#include "bridge_resample.h"
#include "driver/i2s_std.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "output_telemetry.h"
#include "sdkconfig.h"

#include <math.h>
#include <stdatomic.h>
#include <stdint.h>
#include <string.h>

#define PCM_BYTES_PER_FRAME 4U
#define I2S_READ_FRAMES 480U
#define I2S_READ_BYTES (I2S_READ_FRAMES * PCM_BYTES_PER_FRAME)
#define I2S_CLOCK_ACTIVE_MS 100U
#define ADAPTIVE_BLOCK_FRAMES 240U
#define I2S_RATE_HZ 48000U
#define MIN_SINK_RATE_HZ 44100U
/* Worst case: lowest sink rate with the FIFO-draining correction at its limit. */
#define ADAPTIVE_INPUT_FRAMES \
    ((ADAPTIVE_BLOCK_FRAMES * I2S_RATE_HZ + MIN_SINK_RATE_HZ - 1U) / MIN_SINK_RATE_HZ + 2U)
#define RATE_CORRECTION_SCALE 1000000LL
/* Fill samples are taken per A2DP pull, which arrives in bursts; average them
 * per update and low-pass by 1/RATE_FILL_SMOOTHING so burst phase does not
 * reach the proportional term. */
#define RATE_FILL_SMOOTHING 4
#define RATE_MAX_CORRECTION_PPM 500
#define RATE_UPDATE_INTERVAL_MS 100U

typedef struct {
    uint8_t storage[CONFIG_LARKBRIDGE_OUTPUT_AUDIO_RING_FRAMES * PCM_BYTES_PER_FRAME];
    size_t read_index;
    size_t write_index;
    size_t count;
    uint32_t underruns;
    uint32_t overruns;
    uint32_t missing_frames;
    uint32_t dropped_frames;
    uint32_t i2s_read_errors;
    int32_t adaptive_input_delta_frames;
    int32_t correction_ppm;
    uint32_t fifo_min_frames;
    uint32_t fifo_max_frames;
    portMUX_TYPE mux;
} audio_ring_t;

static const char *TAG = "output_audio";
static audio_ring_t s_ring = {.mux = portMUX_INITIALIZER_UNLOCKED};
static i2s_chan_handle_t s_rx_channel;
static atomic_bool s_initialized;
static atomic_bool s_capture_active;
static atomic_bool s_primed;
static atomic_bool s_rate_reset_requested;
static atomic_uint_fast32_t s_last_read_ms;
static uint8_t s_i2s_buffer[I2S_READ_BYTES];
static int16_t s_adaptive_input[ADAPTIVE_INPUT_FRAMES * 2U];
static int16_t s_adaptive_output[ADAPTIVE_BLOCK_FRAMES * 2U];
static bridge_rate_controller_t s_rate_controller;
static bridge_resampler_t s_resampler;
static uint32_t s_nominal_input_remainder;
static int64_t s_fill_sum;
static uint32_t s_fill_samples;
static int32_t s_fill_filtered;
static int32_t s_held_correction_ppm;
static uint32_t s_last_rate_update_ms;

static size_t prefill_target_frames(void) {
    size_t prefill_frames = CONFIG_LARKBRIDGE_OUTPUT_AUDIO_PREFILL_FRAMES;
    size_t maximum_prefill = CONFIG_LARKBRIDGE_OUTPUT_AUDIO_RING_FRAMES / 2U;
    return prefill_frames > maximum_prefill ? maximum_prefill : prefill_frames;
}

static void reset_rate_state(void) {
    bridge_rate_controller_reset(&s_rate_controller);
    bridge_resampler_reset(&s_resampler);
    s_nominal_input_remainder = 0U;
    s_held_correction_ppm = 0;
    s_last_rate_update_ms = 0U;
    s_fill_sum = 0;
    s_fill_samples = 0U;
    s_fill_filtered = (int32_t)prefill_target_frames();
}

static size_t ring_capacity(void) {
    return sizeof(s_ring.storage);
}

static void ring_copy_in(const uint8_t *source, size_t length) {
    size_t first = ring_capacity() - s_ring.write_index;
    if (first > length) {
        first = length;
    }
    (void)memcpy(&s_ring.storage[s_ring.write_index], source, first);
    (void)memcpy(s_ring.storage, source + first, length - first);
    s_ring.write_index = (s_ring.write_index + length) % ring_capacity();
    s_ring.count += length;
}

static void ring_write_overwrite_oldest(const uint8_t *source, size_t length) {
    size_t free_bytes;
    size_t dropped = 0U;

    if (length > ring_capacity()) {
        source += length - ring_capacity();
        length = ring_capacity();
    }
    length -= length % PCM_BYTES_PER_FRAME;
    if (length == 0U) {
        return;
    }

    portENTER_CRITICAL(&s_ring.mux);
    free_bytes = ring_capacity() - s_ring.count;
    if (length > free_bytes) {
        dropped = length - free_bytes;
        dropped += (PCM_BYTES_PER_FRAME - (dropped % PCM_BYTES_PER_FRAME)) % PCM_BYTES_PER_FRAME;
        if (dropped > s_ring.count) {
            dropped = s_ring.count;
        }
        s_ring.read_index = (s_ring.read_index + dropped) % ring_capacity();
        s_ring.count -= dropped;
        ++s_ring.overruns;
        s_ring.dropped_frames += (uint32_t)(dropped / PCM_BYTES_PER_FRAME);
    }
    ring_copy_in(source, length);
    uint32_t fill_frames = (uint32_t)(s_ring.count / PCM_BYTES_PER_FRAME);
    if (fill_frames > s_ring.fifo_max_frames) {
        s_ring.fifo_max_frames = fill_frames;
    }
    portEXIT_CRITICAL(&s_ring.mux);
}

static size_t ring_read_locked(uint8_t *destination, size_t length)
{
    size_t first = ring_capacity() - s_ring.read_index;
    if (first > length) {
        first = length;
    }
    (void)memcpy(destination, &s_ring.storage[s_ring.read_index], first);
    (void)memcpy(destination + first, s_ring.storage, length - first);
    s_ring.read_index = (s_ring.read_index + length) % ring_capacity();
    s_ring.count -= length;
    if (s_ring.fifo_min_frames != UINT32_MAX) {
        uint32_t fill_frames = (uint32_t)(s_ring.count / PCM_BYTES_PER_FRAME);
        if (fill_frames < s_ring.fifo_min_frames) {
            s_ring.fifo_min_frames = fill_frames;
        }
    }
    return length;
}

static void i2s_reader_task(void *context) {
    (void)context;

    for (;;) {
        size_t bytes_read = 0U;
        esp_err_t error =
            i2s_channel_read(s_rx_channel, s_i2s_buffer, sizeof(s_i2s_buffer), &bytes_read, 250U);
        if (error == ESP_OK && bytes_read > 0U) {
            bytes_read -= bytes_read % PCM_BYTES_PER_FRAME;
#if CONFIG_LARKBRIDGE_TEST_IMAGE
            output_telemetry_tap_note(OUTPUT_TAP_I2S_IN, s_i2s_buffer, bytes_read, I2S_RATE_HZ);
#endif
            if (atomic_load_explicit(&s_capture_active, memory_order_acquire)) {
                ring_write_overwrite_oldest(s_i2s_buffer, bytes_read);
            }
            atomic_store_explicit(
                &s_last_read_ms, (uint32_t)(esp_timer_get_time() / 1000), memory_order_release);
        } else if (error != ESP_ERR_TIMEOUT) {
            portENTER_CRITICAL(&s_ring.mux);
            ++s_ring.i2s_read_errors;
            portEXIT_CRITICAL(&s_ring.mux);
            ESP_LOGE(TAG, "I2S receive failed: %s", esp_err_to_name(error));
            vTaskDelay(pdMS_TO_TICKS(100U));
        }
    }
}

esp_err_t output_audio_start(void) {
    i2s_chan_config_t channel_config = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_SLAVE);
    i2s_std_config_t standard_config = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(I2S_RATE_HZ),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(
            I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO),
        .gpio_cfg =
            {
                .mclk = I2S_GPIO_UNUSED,
                .bclk = CONFIG_LARKBRIDGE_OUTPUT_I2S_BCLK_GPIO,
                .ws = CONFIG_LARKBRIDGE_OUTPUT_I2S_WS_GPIO,
                .dout = I2S_GPIO_UNUSED,
                .din = CONFIG_LARKBRIDGE_OUTPUT_I2S_DATA_GPIO,
                .invert_flags =
                    {
                        .mclk_inv = false,
                        .bclk_inv = false,
                        .ws_inv = false,
                    },
            },
    };
    esp_err_t error;

    (void)memset(s_ring.storage, 0, sizeof(s_ring.storage));
    s_ring.read_index = 0U;
    s_ring.write_index = 0U;
    s_ring.count = 0U;
    s_ring.underruns = 0U;
    s_ring.overruns = 0U;
    s_ring.missing_frames = 0U;
    s_ring.dropped_frames = 0U;
    s_ring.i2s_read_errors = 0U;
    s_ring.adaptive_input_delta_frames = 0;
    s_ring.correction_ppm = 0;
    s_ring.fifo_min_frames = UINT32_MAX;
    s_ring.fifo_max_frames = 0U;
    atomic_init(&s_initialized, false);
    atomic_init(&s_capture_active, false);
    atomic_init(&s_primed, false);
    atomic_init(&s_rate_reset_requested, false);
    atomic_init(&s_last_read_ms, 0U);
    s_held_correction_ppm = 0;
    s_last_rate_update_ms = 0U;
    /* Regulate around the level playback starts at, so a fresh route begins
     * with zero error instead of saturating the correction while it fills. */
    if (!bridge_rate_controller_init(
            &s_rate_controller, (int32_t)prefill_target_frames(),
            32768, 131, RATE_MAX_CORRECTION_PPM)) {
        return ESP_ERR_INVALID_STATE;
    }

    channel_config.dma_desc_num = 8U;
    channel_config.dma_frame_num = 240U;
    error = i2s_new_channel(&channel_config, NULL, &s_rx_channel);
    if (error != ESP_OK) {
        return error;
    }
    error = i2s_channel_init_std_mode(s_rx_channel, &standard_config);
    if (error != ESP_OK) {
        (void)i2s_del_channel(s_rx_channel);
        s_rx_channel = NULL;
        return error;
    }
    error = i2s_channel_enable(s_rx_channel);
    if (error != ESP_OK) {
        (void)i2s_del_channel(s_rx_channel);
        s_rx_channel = NULL;
        return error;
    }
    if (xTaskCreate(i2s_reader_task, "i2s_rx", 4096U, NULL, 18U, NULL) != pdPASS) {
        (void)i2s_channel_disable(s_rx_channel);
        (void)i2s_del_channel(s_rx_channel);
        s_rx_channel = NULL;
        return ESP_ERR_NO_MEM;
    }

    atomic_store_explicit(&s_initialized, true, memory_order_release);
    ESP_LOGI(TAG, "I2S slave RX ready: 48 kHz, S16 stereo, ring=%u frames",
             (unsigned int)CONFIG_LARKBRIDGE_OUTPUT_AUDIO_RING_FRAMES);
    return ESP_OK;
}

bool output_audio_is_initialized(void) {
    return atomic_load_explicit(&s_initialized, memory_order_acquire);
}

bool output_audio_clock_active(uint32_t now_ms) {
    uint32_t last = (uint32_t)atomic_load_explicit(&s_last_read_ms, memory_order_acquire);
    /* The reader task may stamp a read after the caller sampled now_ms; that
     * negative age is a live clock, not a 49-day-old one. */
    return output_audio_is_initialized() && last != 0U &&
           (int32_t)(now_ms - last) < (int32_t)I2S_CLOCK_ACTIVE_MS;
}

void output_audio_set_capture_active(bool active)
{
    bool previous = atomic_exchange_explicit(&s_capture_active, active,
                                             memory_order_acq_rel);
    if (previous != active) {
        output_audio_flush();
    }
}

#if CONFIG_LARKBRIDGE_TEST_IMAGE
static atomic_uint_fast32_t s_tone_hz;
static atomic_int s_tone_dbfs;
static float s_tone_phase;

static size_t apply_test_tone(uint8_t *output, size_t length, uint32_t rate, bool mono) {
    uint32_t hz = (uint32_t)atomic_load_explicit(&s_tone_hz, memory_order_acquire);
    int16_t *samples = (int16_t *)output;
    size_t channels = mono ? 1U : 2U;
    size_t frames = length / (channels * sizeof(int16_t));
    if (hz == 0U) {
        return 0U;
    }
    float amplitude =
        32767.0f * powf(10.0f, (float)atomic_load(&s_tone_dbfs) / 20.0f);
    float step = 2.0f * (float)M_PI * (float)hz / (float)rate;
    for (size_t frame = 0; frame < frames; ++frame) {
        int16_t sample = (int16_t)lrintf(amplitude * sinf(s_tone_phase));
        samples[frame * channels] = sample;
        if (!mono) {
            samples[frame * 2U + 1U] = (int16_t)(sample == INT16_MIN ? INT16_MAX : -sample);
        }
        s_tone_phase += step;
        if (s_tone_phase >= 2.0f * (float)M_PI) {
            s_tone_phase -= 2.0f * (float)M_PI;
        }
    }
    return frames * channels * sizeof(int16_t);
}
#endif

bool output_audio_set_test_tone(uint32_t hz, int32_t dbfs) {
#if CONFIG_LARKBRIDGE_TEST_IMAGE
    atomic_store_explicit(&s_tone_dbfs, (int)dbfs, memory_order_release);
    atomic_store_explicit(&s_tone_hz, hz, memory_order_release);
    return true;
#else
    (void)hz;
    (void)dbfs;
    return false;
#endif
}

static size_t pull_from_ring(void *destination, size_t length, uint32_t rate, bool mono);

size_t output_audio_pull(void *destination, size_t length, uint32_t rate, bool mono) {
    size_t produced = pull_from_ring(destination, length, rate, mono);
#if CONFIG_LARKBRIDGE_TEST_IMAGE
    if (destination != NULL && length >= sizeof(int16_t)) {
        size_t toned = apply_test_tone(destination, length, rate, mono);
        if (toned != 0U) {
            produced = toned;
        }
        if (produced != 0U && !mono) {
            output_telemetry_tap_note(OUTPUT_TAP_A2DP_OUT, destination, produced, rate);
        }
    }
#endif
    return produced;
}

static size_t pull_from_ring(void *destination, size_t length, uint32_t rate, bool mono) {
    uint8_t *output = destination;
    size_t output_length;
    size_t output_offset = 0U;
    size_t produced = 0U;
    int32_t correction;
    size_t fill_frames;
    uint32_t current_ms;

    if (output == NULL || length == 0U) {
        return 0U;
    }
    size_t output_frame_bytes = mono ? sizeof(int16_t) : PCM_BYTES_PER_FRAME;
    output_length = length - (length % output_frame_bytes);
    (void)memset(output, 0, output_length);
    if (!atomic_load_explicit(&s_capture_active, memory_order_acquire)) {
        return 0U;
    }
    if (atomic_exchange_explicit(&s_rate_reset_requested, false, memory_order_acq_rel)) {
        reset_rate_state();
    }
    portENTER_CRITICAL(&s_ring.mux);
    fill_frames = s_ring.count / PCM_BYTES_PER_FRAME;
    portEXIT_CRITICAL(&s_ring.mux);
    size_t prefill_frames = prefill_target_frames();
    if (!atomic_load_explicit(&s_primed, memory_order_acquire)) {
        if (fill_frames < prefill_frames) {
            return 0U;
        }
        atomic_store_explicit(&s_primed, true, memory_order_release);
        portENTER_CRITICAL(&s_ring.mux);
        s_ring.fifo_min_frames = (uint32_t)(s_ring.count / PCM_BYTES_PER_FRAME);
        if (s_ring.fifo_min_frames > s_ring.fifo_max_frames) {
            s_ring.fifo_max_frames = s_ring.fifo_min_frames;
        }
        portEXIT_CRITICAL(&s_ring.mux);
        reset_rate_state();
    }
    s_fill_sum += (int64_t)fill_frames;
    ++s_fill_samples;
    current_ms = (uint32_t)(esp_timer_get_time() / 1000);
    if (s_last_rate_update_ms == 0U ||
        (uint32_t)(current_ms - s_last_rate_update_ms) >= RATE_UPDATE_INTERVAL_MS) {
        int32_t mean = (int32_t)(s_fill_sum / (int64_t)s_fill_samples);
        s_fill_filtered += (mean - s_fill_filtered) / RATE_FILL_SMOOTHING;
        s_fill_sum = 0;
        s_fill_samples = 0U;
        s_held_correction_ppm =
            bridge_rate_controller_update(&s_rate_controller, s_fill_filtered);
        s_last_rate_update_ms = current_ms;
    }
    correction = s_held_correction_ppm;
    portENTER_CRITICAL(&s_ring.mux);
    s_ring.correction_ppm = correction;
    portEXIT_CRITICAL(&s_ring.mux);

    /* Input frames per output frame in Q32. Positive correction consumes
     * fewer input frames so the FIFO fills. */
    uint64_t nominal_step = ((uint64_t)I2S_RATE_HZ << 32) / rate;
    uint64_t step = (uint64_t)((int64_t)nominal_step -
                               (int64_t)(nominal_step / RATE_CORRECTION_SCALE) * correction);

    while (output_offset < output_length) {
        size_t output_frames = (output_length - output_offset) / output_frame_bytes;
        if (output_frames > ADAPTIVE_BLOCK_FRAMES) {
            output_frames = ADAPTIVE_BLOCK_FRAMES;
        }
        uint32_t scaled_frames = (uint32_t)output_frames * I2S_RATE_HZ + s_nominal_input_remainder;
        size_t nominal_input_frames = scaled_frames / rate;
        s_nominal_input_remainder = scaled_frames % rate;
        size_t input_frames = bridge_resampler_input_frames(&s_resampler, step, output_frames);
        if (input_frames == 0U || input_frames > ADAPTIVE_INPUT_FRAMES) {
            break;
        }
        size_t input_bytes = input_frames * PCM_BYTES_PER_FRAME;
        size_t available_bytes;

        portENTER_CRITICAL(&s_ring.mux);
        available_bytes = s_ring.count - (s_ring.count % PCM_BYTES_PER_FRAME);
        if (available_bytes < input_bytes) {
            ++s_ring.underruns;
            s_ring.missing_frames +=
                (uint32_t)((input_bytes - available_bytes) / PCM_BYTES_PER_FRAME);
        } else {
            available_bytes = input_bytes;
        }
        (void)ring_read_locked((uint8_t *)s_adaptive_input, available_bytes);
        s_ring.adaptive_input_delta_frames +=
            (int32_t)input_frames - (int32_t)nominal_input_frames;
        portEXIT_CRITICAL(&s_ring.mux);
        /* On underrun, hold the last real frame rather than jumping to zero. */
        size_t have_frames = available_bytes / PCM_BYTES_PER_FRAME;
        for (size_t frame = have_frames; frame < input_frames; ++frame) {
            s_adaptive_input[frame * 2U] =
                have_frames == 0U ? s_resampler.previous[0] : s_adaptive_input[(have_frames - 1U) * 2U];
            s_adaptive_input[frame * 2U + 1U] =
                have_frames == 0U ? s_resampler.previous[1]
                                  : s_adaptive_input[(have_frames - 1U) * 2U + 1U];
        }

        const int16_t *samples =
            bridge_resampler_process(&s_resampler, step, s_adaptive_input, input_frames,
                                     s_adaptive_output, output_frames)
                ? s_adaptive_output
                : NULL;
        size_t output_bytes = output_frames * output_frame_bytes;
        if (samples != NULL) {
            if (mono) {
                for (size_t frame = 0; frame < output_frames; ++frame) {
                    int16_t sample = (int16_t)(((int32_t)samples[frame * 2U] +
                                                samples[frame * 2U + 1U]) / 2);
                    (void)memcpy(output + output_offset + frame * sizeof(sample),
                                 &sample, sizeof(sample));
                }
            } else {
                (void)memcpy(output + output_offset, samples, output_bytes);
            }
            produced += output_bytes;
        }
        output_offset += output_bytes;
    }
    return produced;
}

void output_audio_flush(void) {
    portENTER_CRITICAL(&s_ring.mux);
    s_ring.read_index = s_ring.write_index;
    s_ring.count = 0U;
    s_ring.correction_ppm = 0;
    s_ring.fifo_min_frames = UINT32_MAX;
    s_ring.fifo_max_frames = 0U;
    portEXIT_CRITICAL(&s_ring.mux);
    atomic_store_explicit(&s_primed, false, memory_order_release);
    atomic_store_explicit(&s_rate_reset_requested, true, memory_order_release);
}

#if CONFIG_LARKBRIDGE_TEST_IMAGE
void output_audio_test_fifo_window(void)
{
    portENTER_CRITICAL(&s_ring.mux);
    s_ring.fifo_min_frames = s_ring.count / PCM_BYTES_PER_FRAME;
    s_ring.fifo_max_frames = s_ring.fifo_min_frames;
    portEXIT_CRITICAL(&s_ring.mux);
}
#endif

void output_audio_get_stats(output_audio_stats_t *stats) {
    size_t fifo_frames;

    if (stats == NULL) {
        return;
    }
    portENTER_CRITICAL(&s_ring.mux);
    fifo_frames = s_ring.count / PCM_BYTES_PER_FRAME;
    stats->underruns = s_ring.underruns;
    stats->overruns = s_ring.overruns;
    stats->missing_frames = s_ring.missing_frames;
    stats->dropped_frames = s_ring.dropped_frames;
    stats->i2s_read_errors = s_ring.i2s_read_errors;
    stats->adaptive_input_delta_frames = s_ring.adaptive_input_delta_frames;
    stats->correction_ppm = s_ring.correction_ppm;
    stats->fifo_frames = fifo_frames > UINT16_MAX ? UINT16_MAX : (uint16_t)fifo_frames;
    stats->fifo_min_frames = s_ring.fifo_min_frames == UINT32_MAX
                                 ? 0U
                                 : (s_ring.fifo_min_frames > UINT16_MAX
                                        ? UINT16_MAX
                                        : (uint16_t)s_ring.fifo_min_frames);
    stats->fifo_max_frames = s_ring.fifo_max_frames > UINT16_MAX
                                 ? UINT16_MAX
                                 : (uint16_t)s_ring.fifo_max_frames;
    portEXIT_CRITICAL(&s_ring.mux);
}
