#include "output_audio.h"

#include "bridge_rate.h"
#include "bridge_resample.h"
#include "driver/i2s_std.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include <stdatomic.h>
#include <stdint.h>
#include <string.h>

#define PCM_BYTES_PER_FRAME 4U
#define I2S_READ_FRAMES 480U
#define I2S_READ_BYTES (I2S_READ_FRAMES * PCM_BYTES_PER_FRAME)
#define I2S_CLOCK_ACTIVE_MS 100U
#define ADAPTIVE_BLOCK_FRAMES 240U
#define RATE_CORRECTION_SCALE 1000000LL
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
static int16_t s_adaptive_input[(ADAPTIVE_BLOCK_FRAMES + 1U) * 2U];
static int16_t s_adaptive_output[ADAPTIVE_BLOCK_FRAMES * 2U];
static bridge_rate_controller_t s_rate_controller;
static int64_t s_frame_adjustment_accumulator;
static int32_t s_held_correction_ppm;
static uint32_t s_last_rate_update_ms;

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
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(48000U),
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
    s_frame_adjustment_accumulator = 0;
    s_held_correction_ppm = 0;
    s_last_rate_update_ms = 0U;
    if (!bridge_rate_controller_init(
            &s_rate_controller, CONFIG_LARKBRIDGE_OUTPUT_AUDIO_RING_FRAMES / 2,
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
    return output_audio_is_initialized() && last != 0U && (uint32_t)(now_ms - last) < I2S_CLOCK_ACTIVE_MS;
}

void output_audio_set_capture_active(bool active)
{
    bool previous = atomic_exchange_explicit(&s_capture_active, active,
                                             memory_order_acq_rel);
    if (previous != active) {
        output_audio_flush();
    }
}

size_t output_audio_pull(void *destination, size_t length) {
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
    output_length = length - (length % PCM_BYTES_PER_FRAME);
    (void)memset(output, 0, output_length);
    if (!atomic_load_explicit(&s_capture_active, memory_order_acquire)) {
        return 0U;
    }
    if (atomic_exchange_explicit(&s_rate_reset_requested, false, memory_order_acq_rel)) {
        bridge_rate_controller_reset(&s_rate_controller);
        s_frame_adjustment_accumulator = 0;
        s_held_correction_ppm = 0;
        s_last_rate_update_ms = 0U;
    }
    portENTER_CRITICAL(&s_ring.mux);
    fill_frames = s_ring.count / PCM_BYTES_PER_FRAME;
    portEXIT_CRITICAL(&s_ring.mux);
    size_t prefill_frames = CONFIG_LARKBRIDGE_OUTPUT_AUDIO_PREFILL_FRAMES;
    size_t maximum_prefill = CONFIG_LARKBRIDGE_OUTPUT_AUDIO_RING_FRAMES / 2U;
    if (prefill_frames > maximum_prefill) {
        prefill_frames = maximum_prefill;
    }
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
        bridge_rate_controller_reset(&s_rate_controller);
        s_frame_adjustment_accumulator = 0;
        s_held_correction_ppm = 0;
    }
    current_ms = (uint32_t)(esp_timer_get_time() / 1000);
    if (s_last_rate_update_ms == 0U ||
        (uint32_t)(current_ms - s_last_rate_update_ms) >= RATE_UPDATE_INTERVAL_MS) {
        s_held_correction_ppm =
            bridge_rate_controller_update(&s_rate_controller, (int32_t)fill_frames);
        s_last_rate_update_ms = current_ms;
    }
    correction = s_held_correction_ppm;
    portENTER_CRITICAL(&s_ring.mux);
    s_ring.correction_ppm = correction;
    portEXIT_CRITICAL(&s_ring.mux);

    while (output_offset < output_length) {
        size_t output_frames = (output_length - output_offset) / PCM_BYTES_PER_FRAME;
        int adjustment = 0;
        if (output_frames > ADAPTIVE_BLOCK_FRAMES) {
            output_frames = ADAPTIVE_BLOCK_FRAMES;
        }
        s_frame_adjustment_accumulator -= (int64_t)correction * (int64_t)output_frames;
        if (s_frame_adjustment_accumulator >= RATE_CORRECTION_SCALE) {
            adjustment = 1;
            s_frame_adjustment_accumulator -= RATE_CORRECTION_SCALE;
        } else if (s_frame_adjustment_accumulator <= -RATE_CORRECTION_SCALE) {
            adjustment = -1;
            s_frame_adjustment_accumulator += RATE_CORRECTION_SCALE;
        }
        size_t input_frames = (size_t)((int)output_frames + adjustment);
        size_t input_bytes = input_frames * PCM_BYTES_PER_FRAME;
        bool apply_adjustment;

        portENTER_CRITICAL(&s_ring.mux);
        apply_adjustment = s_ring.count >= input_bytes;
        if (!apply_adjustment) {
            if (adjustment != 0) {
                s_frame_adjustment_accumulator +=
                    (int64_t)adjustment * RATE_CORRECTION_SCALE;
                adjustment = 0;
                input_frames = output_frames;
                input_bytes = input_frames * PCM_BYTES_PER_FRAME;
            }
            if (input_bytes > s_ring.count) {
                input_bytes = s_ring.count - (s_ring.count % PCM_BYTES_PER_FRAME);
                input_frames = input_bytes / PCM_BYTES_PER_FRAME;
                ++s_ring.underruns;
                s_ring.missing_frames += (uint32_t)(output_frames - input_frames);
            }
        }
        (void)ring_read_locked((uint8_t *)s_adaptive_input, input_bytes);
        if (apply_adjustment && adjustment != 0) {
            s_ring.adaptive_input_delta_frames += adjustment;
        }
        portEXIT_CRITICAL(&s_ring.mux);

        if (input_frames == output_frames) {
            (void)memcpy(output + output_offset, s_adaptive_input, input_bytes);
            produced += input_bytes;
        } else if (input_frames > 0U &&
                   bridge_resample_stereo_s16(s_adaptive_input, input_frames,
                                              s_adaptive_output,
                                              output_frames)) {
            (void)memcpy(output + output_offset, s_adaptive_output,
                         output_frames * PCM_BYTES_PER_FRAME);
            produced += output_frames * PCM_BYTES_PER_FRAME;
        }
        output_offset += output_frames * PCM_BYTES_PER_FRAME;
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
