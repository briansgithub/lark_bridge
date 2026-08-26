#include "audio_router.h"

#include <limits.h>
#include <stdatomic.h>
#include <string.h>

#include "audio_convert.h"
#include "bridge_ring.h"
#include "driver/i2s_std.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "s31_config.h"

#define AUDIO_RING_BYTES (32U * 1024U)
#define MIC_RING_BYTES (48U * 1024U)
#define AEC_REFERENCE_RING_BYTES (16U * 1024U)
#define AEC_OUTPUT_RING_BYTES (8U * 1024U)
#define I2S_BLOCK_FRAMES 240U
#define I2S_BLOCK_BYTES (I2S_BLOCK_FRAMES * S31_AUDIO_CHANNELS * sizeof(int16_t))
#define HFP_INPUT_CHUNK_SAMPLES 40U
#define HFP_EXPANDED_MAX_FRAMES (HFP_INPUT_CHUNK_SAMPLES * 6U)
#define HFP_UPLINK_CHUNK_SAMPLES 40U
#define A2DP_INPUT_CHUNK_FRAMES 240U
#define A2DP_OUTPUT_MAX_FRAMES 264U

static const char *TAG = "s31_audio";
static uint8_t s_a2dp_storage[AUDIO_RING_BYTES];
static uint8_t s_hfp_storage[AUDIO_RING_BYTES];
static uint8_t s_mic_storage[MIC_RING_BYTES];
static uint8_t s_aec_reference_storage[AEC_REFERENCE_RING_BYTES];
static uint8_t s_aec_output_storage[AEC_OUTPUT_RING_BYTES];
/* Bluedroid's BTC task stack is intentionally small. These buffers are safe as
 * static storage because the A2DP Sink callback is the sole media producer. */
static int16_t s_a2dp_input_scratch[A2DP_INPUT_CHUNK_FRAMES * 2U];
static int16_t s_a2dp_output_scratch[A2DP_OUTPUT_MAX_FRAMES * 2U];
static int16_t s_hfp_input_scratch[HFP_INPUT_CHUNK_SAMPLES];
static int16_t s_hfp_output_scratch[HFP_EXPANDED_MAX_FRAMES * 2U];
static bridge_byte_ring_t s_a2dp_ring;
static bridge_byte_ring_t s_hfp_ring;
static bridge_byte_ring_t s_mic_ring;
static bridge_byte_ring_t s_aec_reference_ring;
static bridge_byte_ring_t s_aec_output_ring;
static s31_stereo_resampler_t s_a2dp_resampler;
static i2s_chan_handle_t s_i2s_tx;
static atomic_int s_route;
static atomic_bool s_uplink_ready;
static atomic_bool s_aec_output_reset_requested;
static atomic_bool s_a2dp_reset_requested;
static atomic_bool s_a2dp_flush_requested;
static atomic_uint_fast32_t s_aec_generation;
static atomic_uint_fast32_t s_hfp_rate_hz;
static atomic_uint_fast32_t s_a2dp_rate_hz;
static atomic_uint_fast32_t s_underruns;
static atomic_uint_fast32_t s_overruns;

static size_t write_aligned_ring(bridge_byte_ring_t *ring, const uint8_t *pcm,
                                 size_t length, size_t frame_bytes);

static void request_aec_reset(void)
{
    atomic_fetch_add_explicit(&s_aec_generation, 1U, memory_order_acq_rel);
    atomic_store_explicit(&s_aec_output_reset_requested, true, memory_order_release);
}

static void discard_ring(bridge_byte_ring_t *ring)
{
    uint8_t discard[256];
    while (bridge_byte_ring_read(ring, discard, sizeof(discard)) != 0U) {
    }
}

static void i2s_writer_task(void *argument)
{
    uint8_t block[I2S_BLOCK_BYTES];
    int16_t reference[I2S_BLOCK_FRAMES / 3U];
    bridge_route_t previous_route = BRIDGE_ROUTE_SILENCE;
    (void)argument;

    for (;;) {
        bridge_route_t route = (bridge_route_t)atomic_load_explicit(&s_route, memory_order_acquire);
        bridge_byte_ring_t *source = NULL;
        size_t count = 0;
        size_t bytes_written = 0;

        if (atomic_exchange_explicit(&s_a2dp_flush_requested, false,
                                     memory_order_acq_rel)) {
            discard_ring(&s_a2dp_ring);
        }

        if (route != previous_route) {
            discard_ring(&s_a2dp_ring);
            discard_ring(&s_hfp_ring);
            previous_route = route;
        }
        if (route == BRIDGE_ROUTE_A2DP) {
            source = &s_a2dp_ring;
        } else if (route == BRIDGE_ROUTE_HFP) {
            source = &s_hfp_ring;
        }
        if (source != NULL) {
            count = bridge_byte_ring_read(source, block, sizeof(block));
        }
        if (count < sizeof(block)) {
            memset(block + count, 0, sizeof(block) - count);
            if (source != NULL) {
                atomic_fetch_add_explicit(&s_underruns, 1U, memory_order_relaxed);
            }
        }
        esp_err_t result = i2s_channel_write(s_i2s_tx, block, sizeof(block), &bytes_written, 1000U);
        if (result != ESP_OK || bytes_written != sizeof(block)) {
            ESP_LOGE(TAG, "I2S write failed: %s (%u/%u bytes)", esp_err_to_name(result),
                     (unsigned)bytes_written, (unsigned)sizeof(block));
            if (route == BRIDGE_ROUTE_HFP) {
                request_aec_reset();
            }
            vTaskDelay(pdMS_TO_TICKS(20));
        } else if (route == BRIDGE_ROUTE_HFP) {
            /* Tap what was actually emitted, including any underrun zeros.
             * Queue-time HFP PCM is not an acoustic reference because the I2S
             * ring and its zero-fill policy can change the rendered stream. */
            size_t samples = s31_downsample_48k_stereo_to_mono(
                (const int16_t *)block, I2S_BLOCK_FRAMES, 16000U,
                reference, sizeof(reference) / sizeof(reference[0]));
            size_t reference_bytes = samples * sizeof(reference[0]);
            if (samples != sizeof(reference) / sizeof(reference[0]) ||
                write_aligned_ring(&s_aec_reference_ring,
                                   (const uint8_t *)reference,
                                   reference_bytes, sizeof(reference[0])) !=
                    reference_bytes) {
                request_aec_reset();
            }
        }
    }
}

esp_err_t s31_audio_router_init(void)
{
    if (!bridge_byte_ring_init(&s_a2dp_ring, s_a2dp_storage, sizeof(s_a2dp_storage)) ||
        !bridge_byte_ring_init(&s_hfp_ring, s_hfp_storage, sizeof(s_hfp_storage)) ||
        !bridge_byte_ring_init(&s_mic_ring, s_mic_storage, sizeof(s_mic_storage)) ||
        !bridge_byte_ring_init(&s_aec_reference_ring, s_aec_reference_storage,
                               sizeof(s_aec_reference_storage)) ||
        !bridge_byte_ring_init(&s_aec_output_ring, s_aec_output_storage,
                               sizeof(s_aec_output_storage))) {
        return ESP_ERR_INVALID_STATE;
    }
    atomic_init(&s_route, BRIDGE_ROUTE_SILENCE);
    atomic_init(&s_uplink_ready, false);
    s31_stereo_resampler_reset(&s_a2dp_resampler);
    atomic_init(&s_aec_output_reset_requested, true);
    atomic_init(&s_a2dp_reset_requested, true);
    atomic_init(&s_a2dp_flush_requested, false);
    atomic_init(&s_aec_generation, 1U);
    atomic_init(&s_hfp_rate_hz, 16000U);
    atomic_init(&s_a2dp_rate_hz, 48000U);
    atomic_init(&s_underruns, 0U);
    atomic_init(&s_overruns, 0U);

    i2s_chan_config_t channel_config = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_AUTO, I2S_ROLE_MASTER);
    esp_err_t result = i2s_new_channel(&channel_config, &s_i2s_tx, NULL);
    if (result != ESP_OK) {
        return result;
    }
    const i2s_std_config_t standard_config = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(S31_AUDIO_SAMPLE_RATE_HZ),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT,
                                                        I2S_SLOT_MODE_STEREO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = S31_I2S_BCLK_GPIO,
            .ws = S31_I2S_WS_GPIO,
            .dout = S31_I2S_DATA_GPIO,
            .din = I2S_GPIO_UNUSED,
            .invert_flags = {
                .mclk_inv = false,
                .bclk_inv = false,
                .ws_inv = false,
            },
        },
    };
    result = i2s_channel_init_std_mode(s_i2s_tx, &standard_config);
    if (result == ESP_OK) {
        result = i2s_channel_enable(s_i2s_tx);
    }
    if (result != ESP_OK) {
        i2s_del_channel(s_i2s_tx);
        s_i2s_tx = NULL;
        return result;
    }
    if (xTaskCreate(i2s_writer_task, "s31_i2s_tx", 4096, NULL, 9, NULL) != pdPASS) {
        i2s_channel_disable(s_i2s_tx);
        i2s_del_channel(s_i2s_tx);
        s_i2s_tx = NULL;
        return ESP_ERR_NO_MEM;
    }
    ESP_LOGI(TAG, "I2S TX ready: 48 kHz/S16/stereo, BCLK=%d WS=%d DATA=%d",
             S31_I2S_BCLK_GPIO, S31_I2S_WS_GPIO, S31_I2S_DATA_GPIO);
    return ESP_OK;
}

void s31_audio_router_set_route(bridge_route_t route)
{
    int previous = atomic_exchange_explicit(&s_route, (int)route, memory_order_acq_rel);
    if (previous != (int)route) {
        request_aec_reset();
    }
}

void s31_audio_router_set_uplink_ready(bool ready)
{
    if (!ready) {
        atomic_store_explicit(&s_aec_output_reset_requested, true, memory_order_release);
    }
    atomic_store_explicit(&s_uplink_ready, ready, memory_order_release);
}

void s31_audio_router_set_hfp_rate(uint32_t sample_rate_hz)
{
    if (sample_rate_hz == 8000U || sample_rate_hz == 16000U) {
        uint32_t previous = (uint32_t)atomic_exchange_explicit(
            &s_hfp_rate_hz, sample_rate_hz, memory_order_acq_rel);
        if (previous != sample_rate_hz) {
            request_aec_reset();
        }
    }
}

void s31_audio_router_set_a2dp_rate(uint32_t sample_rate_hz)
{
    if (sample_rate_hz == 44100U || sample_rate_hz == 48000U) {
        atomic_store_explicit(&s_a2dp_rate_hz, sample_rate_hz, memory_order_release);
        atomic_store_explicit(&s_a2dp_reset_requested, true, memory_order_release);
        atomic_store_explicit(&s_a2dp_flush_requested, true, memory_order_release);
    }
}

static size_t write_aligned_ring(bridge_byte_ring_t *ring, const uint8_t *pcm, size_t length,
                                 size_t frame_bytes)
{
    if (ring == NULL || pcm == NULL || length == 0U || frame_bytes == 0U ||
        (length % frame_bytes) != 0U || bridge_byte_ring_space(ring) < length) {
        atomic_fetch_add_explicit(&s_overruns, 1U, memory_order_relaxed);
        return 0U;
    }
    /* With one producer per ring and a consumer that only creates space, this
     * admission check makes the byte-ring write whole-frame atomic. */
    return bridge_byte_ring_write(ring, pcm, length);
}

size_t s31_audio_router_write_a2dp(const uint8_t *pcm, size_t length)
{
    if (pcm == NULL || length == 0U || (length % 4U) != 0U ||
        atomic_load_explicit(&s_route, memory_order_acquire) != BRIDGE_ROUTE_A2DP) {
        return 0U;
    }
    uint32_t rate = atomic_load_explicit(&s_a2dp_rate_hz, memory_order_acquire);
    if (atomic_exchange_explicit(&s_a2dp_reset_requested, false, memory_order_acq_rel)) {
        s31_stereo_resampler_reset(&s_a2dp_resampler);
    }
    if (rate == 48000U) {
        return write_aligned_ring(&s_a2dp_ring, pcm, length, 4U);
    }
    if (rate != 44100U) {
        return 0U;
    }

    size_t consumed_frames = 0U;
    size_t input_frames = length / 4U;
    while (consumed_frames < input_frames) {
        size_t chunk_frames = input_frames - consumed_frames;
        if (chunk_frames > A2DP_INPUT_CHUNK_FRAMES) {
            chunk_frames = A2DP_INPUT_CHUNK_FRAMES;
        }
        memcpy(s_a2dp_input_scratch, pcm + consumed_frames * 4U, chunk_frames * 4U);
        size_t output_frames = s31_resample_44100_to_48000_stereo(
            &s_a2dp_resampler, s_a2dp_input_scratch, chunk_frames,
            s_a2dp_output_scratch, A2DP_OUTPUT_MAX_FRAMES);
        if (output_frames != 0U) {
            (void)write_aligned_ring(&s_a2dp_ring,
                                     (const uint8_t *)s_a2dp_output_scratch,
                                     output_frames * 4U, 4U);
        }
        consumed_frames += chunk_frames;
    }
    return length;
}

size_t s31_audio_router_write_hfp_downlink(const uint8_t *pcm, size_t length)
{
    size_t consumed = 0;
    size_t total_written = 0;
    uint32_t rate = atomic_load_explicit(&s_hfp_rate_hz, memory_order_acquire);

    if (pcm == NULL || (length % sizeof(int16_t)) != 0U ||
        atomic_load_explicit(&s_route, memory_order_acquire) != BRIDGE_ROUTE_HFP) {
        return 0U;
    }
    while (consumed < length) {
        size_t input_bytes = length - consumed;
        if (input_bytes > sizeof(s_hfp_input_scratch)) {
            input_bytes = sizeof(s_hfp_input_scratch);
        }
        memcpy(s_hfp_input_scratch, pcm + consumed, input_bytes);
        size_t frames = s31_expand_mono_to_48k_stereo(s_hfp_input_scratch,
                                                       input_bytes / sizeof(int16_t),
                                                       rate, s_hfp_output_scratch,
                                                       HFP_EXPANDED_MAX_FRAMES);
        size_t output_bytes = frames * 2U * sizeof(int16_t);
        size_t speaker_written = write_aligned_ring(
            &s_hfp_ring, (const uint8_t *)s_hfp_output_scratch, output_bytes, 4U);
        if (speaker_written != output_bytes) {
            request_aec_reset();
            break;
        }
        total_written += speaker_written;
        consumed += input_bytes;
    }
    return total_written;
}

size_t s31_audio_router_write_lark(const uint8_t *pcm, size_t length)
{
    if (pcm == NULL || length == 0U || (length % 4U) != 0U) {
        return 0U;
    }
    if (atomic_load_explicit(&s_route, memory_order_acquire) != BRIDGE_ROUTE_HFP) {
        return 0U;
    }
    size_t written = write_aligned_ring(&s_mic_ring, pcm, length, 4U);
    if (written != length) {
        request_aec_reset();
    }
    return written;
}

size_t s31_audio_router_read_lark_for_aec(uint8_t *pcm, size_t length)
{
    if (pcm == NULL || length == 0U || (length % 4U) != 0U ||
        bridge_byte_ring_count(&s_mic_ring) < length) {
        return 0U;
    }
    return bridge_byte_ring_read(&s_mic_ring, pcm, length);
}

bool s31_audio_router_read_aec_inputs(uint8_t *mic_48k_stereo, size_t mic_length,
                                      uint8_t *reference_16k_mono,
                                      size_t reference_length)
{
    if (mic_48k_stereo == NULL || reference_16k_mono == NULL || mic_length == 0U ||
        reference_length == 0U || (mic_length % 4U) != 0U ||
        (reference_length % sizeof(int16_t)) != 0U ||
        bridge_byte_ring_count(&s_mic_ring) < mic_length ||
        bridge_byte_ring_count(&s_aec_reference_ring) < reference_length) {
        return false;
    }
    return bridge_byte_ring_read(&s_mic_ring, mic_48k_stereo, mic_length) == mic_length &&
           bridge_byte_ring_read(&s_aec_reference_ring, reference_16k_mono,
                                 reference_length) == reference_length;
}

int32_t s31_audio_router_aec_fill_difference_samples(void)
{
    /* Normalize both FIFO depths to 16-kHz mono sample time. The value is
     * observed, not used as an absolute delay: the AEC worker captures its
     * desired offset after the configured startup alignment. */
    size_t mic_16k_samples = bridge_byte_ring_count(&s_mic_ring) / (4U * 3U);
    size_t reference_samples =
        bridge_byte_ring_count(&s_aec_reference_ring) / sizeof(int16_t);
    int64_t difference = (int64_t)mic_16k_samples - (int64_t)reference_samples;
    if (difference > INT32_MAX) {
        return INT32_MAX;
    }
    if (difference < INT32_MIN) {
        return INT32_MIN;
    }
    return (int32_t)difference;
}

size_t s31_audio_router_aec_output_bytes(void)
{
    return bridge_byte_ring_count(&s_aec_output_ring);
}

void s31_audio_router_reset_aec_inputs(void)
{
    /* The AEC worker is the sole consumer of both input rings. */
    discard_ring(&s_mic_ring);
    discard_ring(&s_aec_reference_ring);
    atomic_store_explicit(&s_aec_output_reset_requested, true, memory_order_release);
}

void s31_audio_router_reset_aec_stream(void)
{
    request_aec_reset();
}

uint32_t s31_audio_router_aec_generation(void)
{
    return (uint32_t)atomic_load_explicit(&s_aec_generation, memory_order_acquire);
}

size_t s31_audio_router_write_aec_output(const uint8_t *pcm, size_t length)
{
    return write_aligned_ring(&s_aec_output_ring, pcm, length, sizeof(int16_t));
}

uint32_t s31_audio_router_fill_hfp_uplink(uint8_t *pcm, uint32_t length)
{
    int16_t processed[HFP_UPLINK_CHUNK_SAMPLES * 2U];
    uint32_t rate = atomic_load_explicit(&s_hfp_rate_hz, memory_order_acquire);
    uint32_t requested_samples = length / sizeof(int16_t);
    uint32_t produced_total = 0;

    if (pcm == NULL || requested_samples == 0U || (length % sizeof(int16_t)) != 0U) {
        return 0U;
    }
    if (atomic_exchange_explicit(&s_aec_output_reset_requested, false,
                                 memory_order_acq_rel)) {
        discard_ring(&s_aec_output_ring);
    }

    /* HFP can read only the distinct processed-AEC ring. Raw LARK capture is
     * never an uplink source, even if readiness is set incorrectly. */
    if (!atomic_load_explicit(&s_uplink_ready, memory_order_acquire)) {
        memset(pcm, 0, length);
        return length;
    }
    while (produced_total < requested_samples) {
        uint32_t chunk_samples = requested_samples - produced_total;
        if (chunk_samples > HFP_UPLINK_CHUNK_SAMPLES) {
            chunk_samples = HFP_UPLINK_CHUNK_SAMPLES;
        }
        uint32_t factor = rate == 8000U ? 2U : 1U;
        size_t input_samples = (size_t)chunk_samples * factor;
        size_t input_bytes = input_samples * sizeof(int16_t);
        size_t received = bridge_byte_ring_read(&s_aec_output_ring,
                                                (uint8_t *)processed, input_bytes);
        if (received != input_bytes) {
            memset(pcm + produced_total * sizeof(int16_t), 0,
                   (requested_samples - produced_total) * sizeof(int16_t));
            atomic_fetch_add_explicit(&s_underruns, 1U, memory_order_relaxed);
            atomic_store_explicit(&s_uplink_ready, false, memory_order_release);
            request_aec_reset();
            return length;
        }
        int16_t *destination = (int16_t *)(pcm + produced_total * sizeof(int16_t));
        for (uint32_t index = 0U; index < chunk_samples; ++index) {
            if (factor == 1U) {
                destination[index] = processed[index];
            } else {
                destination[index] = (int16_t)(((int32_t)processed[index * 2U] +
                                                processed[index * 2U + 1U]) /
                                               2);
            }
        }
        produced_total += chunk_samples;
    }
    return length;
}

void s31_audio_router_get_stats(s31_audio_stats_t *stats)
{
    bridge_route_t route;
    size_t bytes = 0;
    if (stats == NULL) {
        return;
    }
    route = (bridge_route_t)atomic_load_explicit(&s_route, memory_order_acquire);
    if (route == BRIDGE_ROUTE_A2DP) {
        bytes = bridge_byte_ring_count(&s_a2dp_ring);
    } else if (route == BRIDGE_ROUTE_HFP) {
        bytes = bridge_byte_ring_count(&s_hfp_ring);
    }
    size_t frames = bytes / 4U;
    stats->fifo_frames = frames > UINT16_MAX ? UINT16_MAX : (uint16_t)frames;
    stats->underruns = (uint32_t)atomic_load_explicit(&s_underruns, memory_order_relaxed);
    stats->overruns = (uint32_t)atomic_load_explicit(&s_overruns, memory_order_relaxed);
}
