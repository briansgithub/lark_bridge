#include "aec_gate.h"

#include <limits.h>
#include <math.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include "audio_convert.h"
#include "audio_router.h"
#include "bridge_rate.h"
#include "bridge_resample.h"
#include "esp_aec.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "gateway_state.h"

#define AEC_SAMPLE_RATE_HZ 16000U
#define AEC_MIC_INPUT_RATE_HZ 48000U
#define AEC_MIC_CHANNELS 2U
#define AEC_FILTER_LENGTH 4
#define AEC_BUFFER_ALIGNMENT 16U
#define AEC_MAX_FRAME_SAMPLES 2048U
#define AEC_POLL_MS 4U
#define AEC_RETRY_MS 1000U
#define AEC_STARVATION_MS 125U
#define AEC_STARTUP_TIMEOUT_MS 1200U
#define AEC_TELEMETRY_MS 10000U
#define AEC_ALIGNMENT_CONTROLLER_CENTER 4096
#define AEC_ALIGNMENT_MAX_CORRECTION_PPM 1500
#define AEC_ALIGNMENT_MAX_STEP_FRAMES 16
#define AEC_ALIGNMENT_RATE_SCALE 1000000LL
#define AEC_ALIGNMENT_UPDATE_MS 100U
#define AEC_ALIGNMENT_FILL_SMOOTHING 4
#define AEC_OUTPUT_PREFILL_FRAMES 5U
#if CONFIG_LARKBRIDGE_TEST_IMAGE
#define AEC_TEST_DIAG_MS 2000U
#define AEC_LAG_BIN_SAMPLES 32U /* 2 ms at 16 kHz */
#define AEC_LAG_HISTORY_BINS 1024U /* 2048 ms window (>3 burst cycles) */
#define AEC_LAG_MIN_SHIFT (-64) /* -128 ms */
#define AEC_LAG_MAX_SHIFT 128 /* +256 ms */
#endif

#ifdef CONFIG_LARKBRIDGE_S31_AEC_LINEAR_ONLY
#define AEC_ENGINE_MODE AEC_MODE_SR_LOW_COST
#else
/* ESP-SR 2.5.1 AEC_MODE_FD_* (512-sample frames) fails to converge on esp32s31
 * even on zero-drift synthetic loopback; AEC_MODE_VOIP_LOW_COST (256-sample
 * 16 ms frames) sustains >20 dB ERLE with >80 KB free internal SRAM. */
#define AEC_ENGINE_MODE AEC_MODE_VOIP_LOW_COST
#endif

#ifdef CONFIG_LARKBRIDGE_S31_EXPERIMENTAL_AEC_UPLINK
#define AEC_PUBLISH_ENABLED true
#else
#define AEC_PUBLISH_ENABLED false
#endif

typedef struct {
    aec_handle_t *handle;
    int16_t *mic_48k_stereo;
    int16_t *mic_aligned_48k_stereo;
    int16_t *mic_16k_mono;
    int16_t *reference_16k_mono;
    int16_t *output_16k_mono;
    size_t frame_samples;
    size_t mic_nominal_frames;
    size_t mic_bytes;
    size_t reference_bytes;
    size_t delay_bytes_remaining;
    uint32_t generation;
    uint32_t last_frame_ms;
    uint32_t last_progress_ms;
    uint32_t last_log_ms;
    uint32_t processed_frames;
    uint32_t resynchronizations;
    uint32_t starvations;
    uint32_t output_drops;
    uint32_t max_process_us;
    uint32_t engine_frames;
    bridge_rate_controller_t alignment_controller;
    bridge_resampler_t mic_resampler;
    int64_t alignment_target_q16;
    int32_t alignment_target_samples;
    int32_t alignment_correction_ppm;
    int32_t alignment_input_delta_frames;
    int64_t alignment_fill_sum;
    uint32_t alignment_fill_samples;
    int32_t alignment_fill_filtered;
    uint32_t alignment_last_update_ms;
    bool alignment_target_valid;
    bool uplink_ready;
#if CONFIG_LARKBRIDGE_TEST_IMAGE
    uint32_t last_diag_ms;
    uint64_t diag_ref_sq;
    uint64_t diag_mic_sq;
    uint64_t diag_out_sq;
    uint32_t diag_samples;
    uint32_t diag_clip_samples;
    float *ref_env;
    float *mic_env;
    uint32_t env_head;
    uint32_t env_count;
    int16_t loop_delay[256];
#endif
} aec_worker_t;

static const char *TAG = "s31_aec";
#if CONFIG_LARKBRIDGE_TEST_IMAGE
static atomic_int s_test_delay_ms = CONFIG_LARKBRIDGE_S31_AEC_REFERENCE_DELAY_MS;
static atomic_int s_test_mode = (int)AEC_ENGINE_MODE;
static atomic_int s_test_filter_len = AEC_FILTER_LENGTH;
static atomic_int s_test_nlp_level = (int)AEC_NLP_LEVEL_AGGR;
static atomic_int s_test_drift_ppm = 0;
static atomic_bool s_test_loopback = false;
#endif

static uint32_t now_ms(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000);
}

static bool session_is_eligible(void)
{
    s31_gateway_snapshot_t snapshot;
    s31_gateway_get_snapshot(&snapshot);
    return snapshot.session.lark_present && snapshot.session.hfp_up &&
           snapshot.session.call_active &&
           snapshot.effective_route == BRIDGE_ROUTE_HFP;
}

static void set_uplink_ready(aec_worker_t *worker, bool ready)
{
    if (worker->uplink_ready != ready) {
        worker->uplink_ready = ready;
        s31_gateway_set_aec_ready(ready);
    }
}

static void release_engine(aec_worker_t *worker)
{
    set_uplink_ready(worker, false);
    aec_destroy(worker->handle);
    worker->handle = NULL;
    heap_caps_free(worker->mic_48k_stereo);
    heap_caps_free(worker->mic_aligned_48k_stereo);
    heap_caps_free(worker->mic_16k_mono);
    heap_caps_free(worker->reference_16k_mono);
    heap_caps_free(worker->output_16k_mono);
    worker->mic_48k_stereo = NULL;
    worker->mic_aligned_48k_stereo = NULL;
    worker->mic_16k_mono = NULL;
    worker->reference_16k_mono = NULL;
    worker->output_16k_mono = NULL;
    worker->frame_samples = 0U;
    worker->mic_nominal_frames = 0U;
    worker->mic_bytes = 0U;
    worker->reference_bytes = 0U;
    worker->delay_bytes_remaining = 0U;
    worker->engine_frames = 0U;
    worker->max_process_us = 0U;
    worker->alignment_target_valid = false;
    worker->alignment_target_q16 = 0;
    worker->alignment_correction_ppm = 0;
    worker->alignment_input_delta_frames = 0;
    worker->alignment_fill_sum = 0;
    worker->alignment_fill_samples = 0U;
    worker->alignment_fill_filtered = 0;
    worker->alignment_last_update_ms = 0U;
    bridge_resampler_reset(&worker->mic_resampler);
#if CONFIG_LARKBRIDGE_TEST_IMAGE
    heap_caps_free(worker->ref_env);
    heap_caps_free(worker->mic_env);
    worker->ref_env = NULL;
    worker->mic_env = NULL;
    worker->diag_ref_sq = 0U;
    worker->diag_mic_sq = 0U;
    worker->diag_out_sq = 0U;
    worker->diag_samples = 0U;
    worker->diag_clip_samples = 0U;
    worker->env_head = 0U;
    worker->env_count = 0U;
    memset(worker->loop_delay, 0, sizeof(worker->loop_delay));
#endif
}

static uint32_t aec_heap_caps(void)
{
    if (heap_caps_get_total_size(MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT) > 0U) {
        return MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT;
    }
    return MALLOC_CAP_8BIT;
}

static void *allocate_audio_buffer(size_t bytes)
{
    return heap_caps_aligned_alloc(AEC_BUFFER_ALIGNMENT, bytes, aec_heap_caps());
}

static bool start_engine(aec_worker_t *worker)
{
    aec_config_t config = {
        .mic_num = 1,
        .ref_num = 1,
        .out_num = 1,
        .filter_length = AEC_FILTER_LENGTH,
        .sample_rate = AEC_SAMPLE_RATE_HZ,
        .caps = aec_heap_caps(),
        .mode = AEC_ENGINE_MODE,
        .nlp_level = AEC_NLP_LEVEL_AGGR,
    };
#if CONFIG_LARKBRIDGE_TEST_IMAGE
    config.mode = (aec_mode_t)atomic_load_explicit(&s_test_mode, memory_order_acquire);
    config.filter_length = atomic_load_explicit(&s_test_filter_len, memory_order_acquire);
    config.nlp_level = (aec_nlp_level_t)atomic_load_explicit(&s_test_nlp_level, memory_order_acquire);
#endif
    int frame_samples;

    s31_audio_router_reset_aec_inputs();
    worker->generation = s31_audio_router_aec_generation();
    ESP_LOGI(TAG, "D5: AEC create mode=%d core=%d stack_min=%u internal_free=%u "
             "internal_largest=%u psram_free=%u psram_largest=%u",
             (int)config.mode, (int)xPortGetCoreID(),
             (unsigned)uxTaskGetStackHighWaterMark(NULL),
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT),
             (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT),
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT),
             (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    worker->handle = aec_create_from_config(&config);
    if (worker->handle == NULL) {
        ESP_LOGE(TAG, "ESP-SR AEC allocation failed");
        s31_gateway_set_fault(BRIDGE_FAULT_AEC_NOT_READY);
        return false;
    }
    frame_samples = aec_get_chunksize(worker->handle);
    if (frame_samples <= 0 || (size_t)frame_samples > AEC_MAX_FRAME_SAMPLES) {
        ESP_LOGE(TAG, "ESP-SR returned invalid AEC frame size %d", frame_samples);
        release_engine(worker);
        s31_gateway_set_fault(BRIDGE_FAULT_AEC_NOT_READY);
        return false;
    }
    worker->frame_samples = (size_t)frame_samples;
    worker->mic_nominal_frames = worker->frame_samples *
                                 (AEC_MIC_INPUT_RATE_HZ / AEC_SAMPLE_RATE_HZ);
    worker->mic_bytes = worker->mic_nominal_frames *
                        AEC_MIC_CHANNELS * sizeof(int16_t);
    worker->reference_bytes = worker->frame_samples * sizeof(int16_t);
    worker->mic_48k_stereo = allocate_audio_buffer(
        worker->mic_bytes + AEC_ALIGNMENT_MAX_STEP_FRAMES * AEC_MIC_CHANNELS * sizeof(int16_t));
    worker->mic_aligned_48k_stereo = allocate_audio_buffer(worker->mic_bytes);
    worker->mic_16k_mono = allocate_audio_buffer(worker->reference_bytes);
    worker->reference_16k_mono = allocate_audio_buffer(worker->reference_bytes);
    worker->output_16k_mono = allocate_audio_buffer(worker->reference_bytes);
#if CONFIG_LARKBRIDGE_TEST_IMAGE
    worker->ref_env = allocate_audio_buffer(AEC_LAG_HISTORY_BINS * sizeof(float));
    worker->mic_env = allocate_audio_buffer(AEC_LAG_HISTORY_BINS * sizeof(float));
#endif
    if (worker->mic_48k_stereo == NULL || worker->mic_aligned_48k_stereo == NULL ||
        worker->mic_16k_mono == NULL ||
        worker->reference_16k_mono == NULL || worker->output_16k_mono == NULL
#if CONFIG_LARKBRIDGE_TEST_IMAGE
        || worker->ref_env == NULL || worker->mic_env == NULL
#endif
    ) {
        ESP_LOGE(TAG, "AEC frame-buffer allocation failed");
        release_engine(worker);
        s31_gateway_set_fault(BRIDGE_FAULT_AEC_NOT_READY);
        return false;
    }
    int ref_delay_ms = CONFIG_LARKBRIDGE_S31_AEC_REFERENCE_DELAY_MS;
#if CONFIG_LARKBRIDGE_TEST_IMAGE
    ref_delay_ms = atomic_load_explicit(&s_test_delay_ms, memory_order_acquire);
#endif
    worker->delay_bytes_remaining =
        (size_t)ref_delay_ms *
        (AEC_MIC_INPUT_RATE_HZ / 1000U) * AEC_MIC_CHANNELS * sizeof(int16_t);
    if (!bridge_rate_controller_init(&worker->alignment_controller,
                                     AEC_ALIGNMENT_CONTROLLER_CENTER,
                                     32768, 64,
                                     AEC_ALIGNMENT_MAX_CORRECTION_PPM)) {
        release_engine(worker);
        s31_gateway_set_fault(BRIDGE_FAULT_AEC_NOT_READY);
        return false;
    }
    worker->last_frame_ms = now_ms();
    worker->last_progress_ms = worker->last_frame_ms;
    ESP_LOGI(TAG, "ESP-SR AEC active: %u samples/frame, reference delay=%d ms, publish=%d",
             (unsigned)worker->frame_samples,
             ref_delay_ms,
             AEC_PUBLISH_ENABLED);
    return true;
}

static bool discard_initial_mic_delay(aec_worker_t *worker)
{
    if (worker->delay_bytes_remaining == 0U) {
        return true;
    }
    size_t chunk = worker->delay_bytes_remaining;
    if (chunk > worker->mic_bytes) {
        chunk = worker->mic_bytes;
    }
    size_t discarded = s31_audio_router_read_lark_for_aec(
        (uint8_t *)worker->mic_48k_stereo, chunk);
    if (discarded == chunk) {
        worker->delay_bytes_remaining -= chunk;
        worker->last_progress_ms = now_ms();
    }
    return worker->delay_bytes_remaining == 0U;
}

#if CONFIG_LARKBRIDGE_TEST_IMAGE
static void record_h10_diag(aec_worker_t *worker, uint32_t current_ms)
{
    size_t n = worker->frame_samples;
    for (size_t i = 0; i < n; ++i) {
        int32_t r = worker->reference_16k_mono[i];
        int32_t m = worker->mic_16k_mono[i];
        int32_t o = worker->output_16k_mono[i];
        worker->diag_ref_sq += (uint64_t)(r * r);
        worker->diag_mic_sq += (uint64_t)(m * m);
        worker->diag_out_sq += (uint64_t)(o * o);
        if (o >= INT16_MAX || o <= INT16_MIN) {
            ++worker->diag_clip_samples;
        }
    }
    worker->diag_samples += (uint32_t)n;

    for (size_t off = 0; off + AEC_LAG_BIN_SAMPLES <= n; off += AEC_LAG_BIN_SAMPLES) {
        float r_sum = 0.0f;
        float m_sum = 0.0f;
        for (size_t k = 0; k < AEC_LAG_BIN_SAMPLES; ++k) {
            float rv = (float)worker->reference_16k_mono[off + k];
            float mv = (float)worker->mic_16k_mono[off + k];
            r_sum += rv * rv;
            m_sum += mv * mv;
        }
        uint32_t idx = worker->env_head & (AEC_LAG_HISTORY_BINS - 1U);
        worker->ref_env[idx] = sqrtf(r_sum / (float)AEC_LAG_BIN_SAMPLES);
        worker->mic_env[idx] = sqrtf(m_sum / (float)AEC_LAG_BIN_SAMPLES);
        worker->env_head++;
        if (worker->env_count < AEC_LAG_HISTORY_BINS) {
            worker->env_count++;
        }
    }

    if ((uint32_t)(current_ms - worker->last_diag_ms) < AEC_TEST_DIAG_MS ||
        worker->diag_samples == 0U) {
        return;
    }
    worker->last_diag_ms = current_ms;
    double ref_rms = sqrt((double)worker->diag_ref_sq / (double)worker->diag_samples);
    double mic_rms = sqrt((double)worker->diag_mic_sq / (double)worker->diag_samples);
    double out_rms = sqrt((double)worker->diag_out_sq / (double)worker->diag_samples);
    double erle_db = (mic_rms >= 20.0 && out_rms > 0.5)
                         ? 20.0 * log10(mic_rms / out_rms)
                         : (mic_rms >= 20.0 ? 60.0 : 0.0);

    int best_shift = 0;
    double best_corr = -1.0;
    if (worker->env_count >= AEC_LAG_HISTORY_BINS) {
        /* Zero-mean Pearson correlation over 1.66 s (>2.5 burst cycles) eliminates
         * flat-top burst ambiguity. Positive shift d: mic[t] matches ref[t - d]. */
        const int j_start = AEC_LAG_MAX_SHIFT;
        const int j_end = (int)AEC_LAG_HISTORY_BINS + AEC_LAG_MIN_SHIFT;
        const float inv_n = 1.0f / (float)(j_end - j_start);
        float mean_m = 0.0f;
        for (int j = j_start; j < j_end; ++j) {
            uint32_t mi = (worker->env_head - AEC_LAG_HISTORY_BINS + (uint32_t)j) &
                          (AEC_LAG_HISTORY_BINS - 1U);
            mean_m += worker->mic_env[mi];
        }
        mean_m *= inv_n;
        float dm = 0.0f;
        for (int j = j_start; j < j_end; ++j) {
            uint32_t mi = (worker->env_head - AEC_LAG_HISTORY_BINS + (uint32_t)j) &
                          (AEC_LAG_HISTORY_BINS - 1U);
            float dv = worker->mic_env[mi] - mean_m;
            dm += dv * dv;
        }
        if (dm > 1e5f) {
            for (int shift = AEC_LAG_MIN_SHIFT; shift <= AEC_LAG_MAX_SHIFT; ++shift) {
                float mean_r = 0.0f;
                for (int j = j_start; j < j_end; ++j) {
                    uint32_t ri = (worker->env_head - AEC_LAG_HISTORY_BINS + (uint32_t)(j - shift)) &
                                  (AEC_LAG_HISTORY_BINS - 1U);
                    mean_r += worker->ref_env[ri];
                }
                mean_r *= inv_n;
                float num = 0.0f, dr = 0.0f;
                for (int j = j_start; j < j_end; ++j) {
                    uint32_t mi = (worker->env_head - AEC_LAG_HISTORY_BINS + (uint32_t)j) &
                                  (AEC_LAG_HISTORY_BINS - 1U);
                    uint32_t ri = (worker->env_head - AEC_LAG_HISTORY_BINS + (uint32_t)(j - shift)) &
                                  (AEC_LAG_HISTORY_BINS - 1U);
                    float rv = worker->ref_env[ri] - mean_r;
                    float mv = worker->mic_env[mi] - mean_m;
                    num += rv * mv;
                    dr += rv * rv;
                }
                if (dr > 1e5f) {
                    double c = (double)num / sqrt((double)dr * (double)dm);
                    if (c > best_corr) {
                        best_corr = c;
                        best_shift = shift;
                    }
                }
            }
        }
    }
    ESP_LOGI(TAG,
             "H10_AEC ref_rms=%.1f mic_rms=%.1f out_rms=%.1f erle_db=%.2f "
             "clip=%lu/%lu res_lag_ms=%d corr=%.2f int_free=%u int_lrg=%u",
             ref_rms, mic_rms, out_rms, erle_db,
             (unsigned long)worker->diag_clip_samples,
             (unsigned long)worker->diag_samples,
             best_shift * 2, best_corr > 0.0 ? best_corr : 0.0,
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT),
             (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
    worker->diag_ref_sq = 0U;
    worker->diag_mic_sq = 0U;
    worker->diag_out_sq = 0U;
    worker->diag_samples = 0U;
    worker->diag_clip_samples = 0U;
}
#endif

static void log_telemetry(aec_worker_t *worker, uint32_t current_ms)
{
    if ((uint32_t)(current_ms - worker->last_log_ms) < AEC_TELEMETRY_MS) {
        return;
    }
    worker->last_log_ms = current_ms;
    ESP_LOGI(TAG,
             "AEC frames=%lu resync=%lu starve=%lu output_drop=%lu max_us=%lu "
             "align=%dppm delta=%ld fill_err=%ld heap=%u min_heap=%u largest=%u",
             (unsigned long)worker->processed_frames,
             (unsigned long)worker->resynchronizations,
             (unsigned long)worker->starvations,
             (unsigned long)worker->output_drops,
             (unsigned long)worker->max_process_us,
             worker->alignment_correction_ppm,
             (long)worker->alignment_input_delta_frames,
             (long)(s31_audio_router_aec_fill_difference_samples() -
                    worker->alignment_target_samples),
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_8BIT),
             (unsigned)heap_caps_get_minimum_free_size(MALLOC_CAP_8BIT),
             (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_8BIT));
}

static void aec_task(void *argument)
{
    aec_worker_t worker = {0};
    uint32_t retry_at_ms = 0U;
    (void)argument;

    for (;;) {
        uint32_t current_ms = now_ms();
        if (!session_is_eligible()) {
            if (worker.handle != NULL) {
                release_engine(&worker);
                s31_audio_router_reset_aec_inputs();
            }
            vTaskDelay(pdMS_TO_TICKS(20U));
            continue;
        }
        if (worker.handle == NULL) {
            if ((int32_t)(current_ms - retry_at_ms) < 0) {
                vTaskDelay(pdMS_TO_TICKS(20U));
                continue;
            }
            if (!start_engine(&worker)) {
                retry_at_ms = current_ms + AEC_RETRY_MS;
                vTaskDelay(pdMS_TO_TICKS(20U));
                continue;
            }
        }
        uint32_t generation = s31_audio_router_aec_generation();
        if (generation != worker.generation) {
            ++worker.resynchronizations;
            release_engine(&worker);
            vTaskDelay(1);
            continue;
        }
        if (!discard_initial_mic_delay(&worker)) {
            vTaskDelay(pdMS_TO_TICKS(AEC_POLL_MS));
            continue;
        }
        if (!worker.alignment_target_valid) {
            worker.alignment_target_samples =
                s31_audio_router_aec_fill_difference_samples();
            worker.alignment_target_q16 = (int64_t)worker.alignment_target_samples << 16;
            worker.alignment_target_valid = true;
            worker.alignment_fill_sum = 0;
            worker.alignment_fill_samples = 0U;
            worker.alignment_fill_filtered = 0;
            worker.alignment_last_update_ms = current_ms;
            bridge_rate_controller_reset(&worker.alignment_controller);
            bridge_resampler_reset(&worker.mic_resampler);
        }
        int32_t fill_error = s31_audio_router_aec_fill_difference_samples() -
                             worker.alignment_target_samples;
        worker.alignment_fill_sum += (int64_t)fill_error;
        ++worker.alignment_fill_samples;
        if ((uint32_t)(current_ms - worker.alignment_last_update_ms) >= AEC_ALIGNMENT_UPDATE_MS &&
            worker.alignment_fill_samples > 0U) {
            int32_t mean = (int32_t)(worker.alignment_fill_sum /
                                     (int64_t)worker.alignment_fill_samples);
            worker.alignment_fill_filtered +=
                (mean - worker.alignment_fill_filtered) / AEC_ALIGNMENT_FILL_SMOOTHING;
            worker.alignment_fill_sum = 0;
            worker.alignment_fill_samples = 0U;
            worker.alignment_last_update_ms = current_ms;
            int64_t virtual_fill =
                (int64_t)AEC_ALIGNMENT_CONTROLLER_CENTER + worker.alignment_fill_filtered;
            if (virtual_fill < 1) {
                virtual_fill = 1;
            } else if (virtual_fill > INT32_MAX) {
                virtual_fill = INT32_MAX;
            }
            worker.alignment_correction_ppm = bridge_rate_controller_update(
                &worker.alignment_controller, (int32_t)virtual_fill);
        }
        int64_t step_delta_q32 =
            ((int64_t)worker.alignment_correction_ppm << 32) / AEC_ALIGNMENT_RATE_SCALE;
        uint64_t step_q32 = (uint64_t)((int64_t)(1ULL << 32) - step_delta_q32);
        size_t mic_input_frames = bridge_resampler_input_frames(
            &worker.mic_resampler, step_q32, worker.mic_nominal_frames);
        int adjustment = (int)mic_input_frames - (int)worker.mic_nominal_frames;
        if (mic_input_frames == 0U ||
            mic_input_frames > worker.mic_nominal_frames + AEC_ALIGNMENT_MAX_STEP_FRAMES) {
            ++worker.resynchronizations;
            release_engine(&worker);
            continue;
        }
        size_t mic_input_bytes = mic_input_frames * AEC_MIC_CHANNELS * sizeof(int16_t);
        if (!s31_audio_router_read_aec_inputs(
                (uint8_t *)worker.mic_48k_stereo, mic_input_bytes,
                (uint8_t *)worker.reference_16k_mono, worker.reference_bytes)) {
            uint32_t timeout = worker.engine_frames == 0U
                                   ? AEC_STARTUP_TIMEOUT_MS
                                   : AEC_STARVATION_MS;
            /* last_progress_ms can be stamped after current_ms was sampled (engine start,
             * initial delay discard, end of a frame): use a fresh, signed elapsed time so a
             * stamp "in the future" reads as zero rather than wrapping to ~49 days. */
            int32_t idle_ms = (int32_t)(now_ms() - worker.last_progress_ms);
            if (idle_ms < 0) {
                idle_ms = 0;
            }
            if ((uint32_t)idle_ms >= timeout) {
                ++worker.starvations;
                size_t mic_have = 0U, reference_have = 0U;
                s31_audio_router_aec_ring_bytes(&mic_have, &reference_have);
                ESP_LOGW(TAG, "AEC input starved; restarting fail-closed pipeline "
                         "(mic %u/%u ref %u/%u bytes, frames=%lu, idle=%lu ms, max_proc=%lu us)",
                         (unsigned)mic_have, (unsigned)mic_input_bytes,
                         (unsigned)reference_have, (unsigned)worker.reference_bytes,
                         (unsigned long)worker.engine_frames,
                         (unsigned long)idle_ms,
                         (unsigned long)worker.max_process_us);
                s31_gateway_set_fault(BRIDGE_FAULT_AEC_NOT_READY);
                release_engine(&worker);
            } else {
                vTaskDelay(pdMS_TO_TICKS(AEC_POLL_MS));
            }
            continue;
        }
#if CONFIG_LARKBRIDGE_TEST_IMAGE
        int drift_ppm = atomic_load_explicit(&s_test_drift_ppm, memory_order_acquire);
        if (drift_ppm != 0) {
            /* Positive drift_ppm means acoustic lag grows (+mic lagging ref): advance mic faster,
             * which lowers target (mic - ref) by drift_ppm * frame_samples / 1e6 samples/frame. */
            worker.alignment_target_q16 -=
                ((int64_t)drift_ppm * (int64_t)worker.frame_samples << 16) /
                AEC_ALIGNMENT_RATE_SCALE;
            worker.alignment_target_samples = (int32_t)(worker.alignment_target_q16 >> 16);
        }
#endif
        worker.alignment_input_delta_frames += adjustment;
        worker.last_progress_ms = current_ms;
        if (!bridge_resampler_process(&worker.mic_resampler, step_q32,
                                      worker.mic_48k_stereo, mic_input_frames,
                                      worker.mic_aligned_48k_stereo,
                                      worker.mic_nominal_frames)) {
            ++worker.resynchronizations;
            release_engine(&worker);
            continue;
        }
        const int16_t *aligned_mic = worker.mic_aligned_48k_stereo;
        if (s31_audio_router_aec_generation() != worker.generation ||
            s31_downsample_48k_stereo_to_mono(
                aligned_mic, worker.mic_nominal_frames,
                AEC_SAMPLE_RATE_HZ, worker.mic_16k_mono,
                worker.frame_samples) != worker.frame_samples) {
            ++worker.resynchronizations;
            release_engine(&worker);
            continue;
        }
#if CONFIG_LARKBRIDGE_TEST_IMAGE
        if (atomic_load_explicit(&s_test_loopback, memory_order_acquire) &&
            worker.frame_samples == 512U) {
            /* Synthetic zero-drift echo: mic = 0.5 * ref delayed by 16 ms (256 samples). */
            for (size_t i = 0; i < 256U; ++i) {
                worker.mic_16k_mono[i] = (int16_t)(worker.loop_delay[i] / 2);
            }
            for (size_t i = 256U; i < 512U; ++i) {
                worker.mic_16k_mono[i] = (int16_t)(worker.reference_16k_mono[i - 256U] / 2);
            }
            memcpy(worker.loop_delay, &worker.reference_16k_mono[256], 256U * sizeof(int16_t));
        }
#endif

        int64_t started_us = esp_timer_get_time();
        if (AEC_ENGINE_MODE == AEC_MODE_SR_LOW_COST) {
            aec_linear_process(worker.handle, worker.mic_16k_mono,
                               worker.reference_16k_mono, worker.output_16k_mono);
        } else {
            aec_process(worker.handle, worker.mic_16k_mono,
                        worker.reference_16k_mono, worker.output_16k_mono);
        }
        uint32_t elapsed_us = (uint32_t)(esp_timer_get_time() - started_us);
        if (elapsed_us > worker.max_process_us) {
            worker.max_process_us = elapsed_us;
        }
        if (s31_audio_router_aec_generation() != worker.generation) {
            ++worker.resynchronizations;
            release_engine(&worker);
            continue;
        }
        if (AEC_PUBLISH_ENABLED) {
            size_t written = s31_audio_router_write_aec_output(
                (const uint8_t *)worker.output_16k_mono, worker.reference_bytes);
            if (written != worker.reference_bytes) {
                ++worker.output_drops;
                ESP_LOGW(TAG, "AEC output ring overflow; restarting fail-closed pipeline");
                s31_gateway_set_fault(BRIDGE_FAULT_AEC_NOT_READY);
                release_engine(&worker);
                continue;
            }
            if (s31_audio_router_aec_output_bytes() >=
                AEC_OUTPUT_PREFILL_FRAMES * worker.reference_bytes) {
                set_uplink_ready(&worker, true);
            }
        }
        ++worker.processed_frames;
        ++worker.engine_frames;
        worker.last_frame_ms = current_ms;
        /* Input deadlines count from when the worker is idle again, not from before a
         * (possibly slow, PSRAM-resident) process call. */
        worker.last_progress_ms = now_ms();
#if CONFIG_LARKBRIDGE_TEST_IMAGE
        record_h10_diag(&worker, current_ms);
#endif
        log_telemetry(&worker, current_ms);
        /* Yield 1 raw tick (guaranteed >= 1 tick even at 100 Hz where
         * pdMS_TO_TICKS(1) == 0) every 16 frames (~256 ms at 16 ms/frame) so
         * IDLE feeds the task watchdog without adding real-time pressure. */
        if ((worker.processed_frames & 15U) == 0U) {
            vTaskDelay(1);
        }
    }
}

esp_err_t s31_aec_gate_init(void)
{
    s31_gateway_set_aec_ready(false);
#ifndef CONFIG_LARKBRIDGE_S31_AEC_ENGINE
    /* ESP-SR's engine faults on creation on this target (see Kconfig). */
    ESP_LOGW(TAG, "ESP-SR AEC engine disabled; call uplink stays silent");
    s31_gateway_set_fault(BRIDGE_FAULT_AEC_NOT_READY);
    return ESP_OK;
#endif
    /* ESP-SR 2.5.1's FD nonlinear initializer hardcodes SPIRAM caps.
     * With no such heap it can dereference a failed allocation. */
    if (AEC_ENGINE_MODE == AEC_MODE_FD_LOW_COST &&
        heap_caps_get_total_size(MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT) == 0U) {
        ESP_LOGE(TAG, "D5: FD AEC requires a registered 8-bit PSRAM heap");
        s31_gateway_set_fault(BRIDGE_FAULT_AEC_NOT_READY);
        return ESP_OK;
    }
    if (xTaskCreatePinnedToCore(aec_task, "s31_aec", 8192U, NULL, 5U, NULL, 1) != pdPASS) {
        ESP_LOGE(TAG, "could not create AEC worker");
        return ESP_ERR_NO_MEM;
    }
    if (AEC_PUBLISH_ENABLED) {
        ESP_LOGW(TAG, "experimental AEC uplink ENABLED; hardware qualification is mandatory");
    } else {
        ESP_LOGW(TAG, "ESP-SR AEC is linked and observable, but uplink remains qualification-locked");
    }
    return ESP_OK;
}

#if CONFIG_LARKBRIDGE_TEST_IMAGE
bool s31_aec_test_fault(const char *kind)
{
    char *end = NULL;
    if (strncmp(kind, "aec_d", 5U) == 0 && kind[5] != '\0') {
        long ms = strtol(kind + 5, &end, 10);
        if (end == NULL || *end != '\0' || ms < 0L || ms > 950L) {
            return false;
        }
        atomic_store_explicit(&s_test_delay_ms, (int)ms, memory_order_release);
        s31_audio_router_reset_aec_stream();
        ESP_LOGI(TAG, "TEST: set AEC reference delay to %ld ms and reset AEC stream", ms);
        return true;
    }
    if (strncmp(kind, "aec_m", 5U) == 0 && kind[5] != '\0') {
        long mode = strtol(kind + 5, &end, 10);
        if (end == NULL || *end != '\0' || mode < 0L || mode > 6L || mode == 2L) {
            return false;
        }
        atomic_store_explicit(&s_test_mode, (int)mode, memory_order_release);
        s31_audio_router_reset_aec_stream();
        ESP_LOGI(TAG, "TEST: set AEC mode=%ld and reset AEC stream", mode);
        return true;
    }
    if (strncmp(kind, "aec_f", 5U) == 0 && kind[5] != '\0') {
        long flen = strtol(kind + 5, &end, 10);
        if (end == NULL || *end != '\0' || flen < 2L || flen > 16L) {
            return false;
        }
        atomic_store_explicit(&s_test_filter_len, (int)flen, memory_order_release);
        s31_audio_router_reset_aec_stream();
        ESP_LOGI(TAG, "TEST: set AEC filter_length=%ld and reset AEC stream", flen);
        return true;
    }
    if (strncmp(kind, "aec_n", 5U) == 0 && kind[5] != '\0') {
        long nlp = strtol(kind + 5, &end, 10);
        if (end == NULL || *end != '\0' || nlp < 0L || nlp > 2L) {
            return false;
        }
        atomic_store_explicit(&s_test_nlp_level, (int)nlp, memory_order_release);
        s31_audio_router_reset_aec_stream();
        ESP_LOGI(TAG, "TEST: set AEC nlp_level=%ld and reset AEC stream", nlp);
        return true;
    }
    if (strncmp(kind, "aec_p", 5U) == 0 && kind[5] != '\0') {
        long ppm = strtol(kind + 5, &end, 10);
        if (end == NULL || *end != '\0' || ppm < -2000L || ppm > 2000L) {
            return false;
        }
        atomic_store_explicit(&s_test_drift_ppm, (int)ppm, memory_order_release);
        ESP_LOGI(TAG, "TEST: set AEC drift_ppm=%ld", ppm);
        return true;
    }
    if (strncmp(kind, "aec_loop", 8U) == 0 && (kind[8] == '0' || kind[8] == '1') && kind[9] == '\0') {
        bool en = (kind[8] == '1');
        atomic_store_explicit(&s_test_loopback, en, memory_order_release);
        s31_audio_router_reset_aec_stream();
        ESP_LOGI(TAG, "TEST: set AEC synthetic loopback=%d", (int)en);
        return true;
    }
    if (strcmp(kind, "aec_lark_lost") == 0) {
        s31_gateway_apply_session_event(BRIDGE_SESSION_EVENT_LARK_LOST);
        ESP_LOGW(TAG, "TEST: injected LARK_LOST session event");
        return true;
    }
    if (strcmp(kind, "aec_lark_present") == 0) {
        s31_gateway_apply_session_event(BRIDGE_SESSION_EVENT_LARK_PRESENT);
        ESP_LOGI(TAG, "TEST: injected LARK_PRESENT session event");
        return true;
    }
    return false;
}
#endif
