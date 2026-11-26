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
#define AEC_REF_DELAY_CAP_SAMPLES 16384U /* 1024 ms at 16 kHz mono (power of 2) */
#define AEC_REF_DELAY_MAX_MS 880
#define AEC_DELAY_TARGET_MARGIN_MS 12
#define AEC_DELAY_MIN_RESIDUAL_MS 2
#define AEC_DELAY_MAX_RESIDUAL_MS 28
#define AEC_DELAY_TRACK_MS 1500U
#define AEC_LAG_BIN_SAMPLES 32U /* 2 ms at 16 kHz */
#define AEC_LAG_HISTORY_BINS 1024U /* 2048 ms window */
#define AEC_LAG_MAX_SHIFT 448 /* 0..896 ms total acoustic delay search */
#if CONFIG_LARKBRIDGE_TEST_IMAGE
#define AEC_TEST_DIAG_SAMPLES 32000U /* Exact 2.000 s at 16 kHz (125 x 256 frames) */
#define AEC_TEST_NEAR_BINS 12U
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
    int16_t *reference_delayed_16k_mono;
    int16_t *ref_delay_line;
    int16_t *output_16k_mono;
    size_t frame_samples;
    size_t mic_nominal_frames;
    size_t mic_bytes;
    size_t reference_bytes;
    uint32_t ref_delay_write_idx;
    uint32_t ref_delay_samples;
    uint32_t generation;
    uint32_t last_frame_ms;
    uint32_t last_progress_ms;
    uint32_t last_log_ms;
    uint32_t last_delay_track_ms;
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
    float *ref_env;
    float *mic_env;
    uint32_t env_head;
    uint32_t env_count;
    int last_total_lag_ms;
    int last_res_lag_ms;
    float last_lag_corr;
    int pending_delay_ms;
    uint8_t pending_delay_hits;
#if CONFIG_LARKBRIDGE_TEST_IMAGE
    uint64_t diag_ref_sq;
    uint64_t diag_mic_sq;
    uint64_t diag_echo_sq;
    uint64_t diag_near_sq;
    uint64_t diag_out_sq;
    uint32_t diag_samples;
    uint32_t diag_clip_samples;
    uint32_t near_sample_idx;
    uint32_t noise_rng;
    float noise_lp1;
    float noise_lp2;
    float diag_iq_re[AEC_TEST_NEAR_BINS];
    float diag_iq_im[AEC_TEST_NEAR_BINS];
    int16_t loop_delay[256];
#endif
} aec_worker_t;

static const char *TAG = "s31_aec";
#if CONFIG_LARKBRIDGE_TEST_IMAGE
static atomic_int s_test_delay_ms = CONFIG_LARKBRIDGE_S31_AEC_REFERENCE_DELAY_MS;
static atomic_bool s_test_auto_delay = true;
static atomic_int s_test_mode = (int)AEC_ENGINE_MODE;
static atomic_int s_test_filter_len = AEC_FILTER_LENGTH;
static atomic_int s_test_nlp_level = (int)AEC_NLP_LEVEL_NORMAL;
static atomic_int s_test_drift_ppm = 0;
static atomic_int s_test_near_rms = 0;
static atomic_int s_test_noise_rms = 0;
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
    heap_caps_free(worker->reference_delayed_16k_mono);
    heap_caps_free(worker->ref_delay_line);
    heap_caps_free(worker->output_16k_mono);
    heap_caps_free(worker->ref_env);
    heap_caps_free(worker->mic_env);
    worker->mic_48k_stereo = NULL;
    worker->mic_aligned_48k_stereo = NULL;
    worker->mic_16k_mono = NULL;
    worker->reference_16k_mono = NULL;
    worker->reference_delayed_16k_mono = NULL;
    worker->ref_delay_line = NULL;
    worker->output_16k_mono = NULL;
    worker->ref_env = NULL;
    worker->mic_env = NULL;
    worker->frame_samples = 0U;
    worker->mic_nominal_frames = 0U;
    worker->mic_bytes = 0U;
    worker->reference_bytes = 0U;
    worker->ref_delay_write_idx = 0U;
    worker->ref_delay_samples = 0U;
    worker->last_delay_track_ms = 0U;
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
    worker->env_head = 0U;
    worker->env_count = 0U;
    worker->last_total_lag_ms = 0;
    worker->last_res_lag_ms = 0;
    worker->last_lag_corr = 0.0f;
    worker->pending_delay_ms = 0;
    worker->pending_delay_hits = 0U;
    bridge_resampler_reset(&worker->mic_resampler);
#if CONFIG_LARKBRIDGE_TEST_IMAGE
    worker->diag_ref_sq = 0U;
    worker->diag_mic_sq = 0U;
    worker->diag_echo_sq = 0U;
    worker->diag_near_sq = 0U;
    worker->diag_out_sq = 0U;
    worker->diag_samples = 0U;
    worker->diag_clip_samples = 0U;
    worker->near_sample_idx = 0U;
    worker->noise_rng = 0x2468ACE1U;
    worker->noise_lp1 = 0.0f;
    worker->noise_lp2 = 0.0f;
    memset(worker->diag_iq_re, 0, sizeof(worker->diag_iq_re));
    memset(worker->diag_iq_im, 0, sizeof(worker->diag_iq_im));
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
        .nlp_level = AEC_NLP_LEVEL_NORMAL,
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
    worker->reference_delayed_16k_mono = allocate_audio_buffer(worker->reference_bytes);
    worker->ref_delay_line = allocate_audio_buffer(AEC_REF_DELAY_CAP_SAMPLES * sizeof(int16_t));
    worker->output_16k_mono = allocate_audio_buffer(worker->reference_bytes);
    worker->ref_env = allocate_audio_buffer(AEC_LAG_HISTORY_BINS * sizeof(float));
    worker->mic_env = allocate_audio_buffer(AEC_LAG_HISTORY_BINS * sizeof(float));
    if (worker->mic_48k_stereo == NULL || worker->mic_aligned_48k_stereo == NULL ||
        worker->mic_16k_mono == NULL || worker->reference_16k_mono == NULL ||
        worker->reference_delayed_16k_mono == NULL || worker->ref_delay_line == NULL ||
        worker->output_16k_mono == NULL || worker->ref_env == NULL || worker->mic_env == NULL) {
        ESP_LOGE(TAG, "AEC frame-buffer allocation failed");
        release_engine(worker);
        s31_gateway_set_fault(BRIDGE_FAULT_AEC_NOT_READY);
        return false;
    }
    memset(worker->ref_delay_line, 0, AEC_REF_DELAY_CAP_SAMPLES * sizeof(int16_t));
    int ref_delay_ms = CONFIG_LARKBRIDGE_S31_AEC_REFERENCE_DELAY_MS;
#if CONFIG_LARKBRIDGE_TEST_IMAGE
    ref_delay_ms = atomic_load_explicit(&s_test_delay_ms, memory_order_acquire);
#endif
    if (ref_delay_ms < 0) {
        ref_delay_ms = 0;
    } else if (ref_delay_ms > AEC_REF_DELAY_MAX_MS) {
        ref_delay_ms = AEC_REF_DELAY_MAX_MS;
    }
    worker->ref_delay_write_idx = 0U;
    worker->ref_delay_samples = (uint32_t)ref_delay_ms * (AEC_SAMPLE_RATE_HZ / 1000U);
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
    worker->last_delay_track_ms = worker->last_frame_ms;
#if CONFIG_LARKBRIDGE_TEST_IMAGE
    worker->noise_rng = 0x2468ACE1U;
    worker->noise_lp1 = 0.0f;
    worker->noise_lp2 = 0.0f;
#endif
    ESP_LOGI(TAG, "ESP-SR AEC active: %u samples/frame, init_delay=%d ms, publish=%d",
             (unsigned)worker->frame_samples,
             ref_delay_ms,
             AEC_PUBLISH_ENABLED);
    return true;
}

static float eval_lag_corr(const aec_worker_t *worker, int shift,
                           int j_start, int j_end, float inv_n,
                           float mean_m, float dm)
{
    const uint32_t mask = AEC_LAG_HISTORY_BINS - 1U;
    const uint32_t base = worker->env_head - AEC_LAG_HISTORY_BINS;
    float mean_r = 0.0f;
    for (int j = j_start; j < j_end; ++j) {
        uint32_t ri = (base + (uint32_t)(j - shift)) & mask;
        mean_r += worker->ref_env[ri];
    }
    mean_r *= inv_n;
    float num = 0.0f;
    float dr = 0.0f;
    for (int j = j_start; j < j_end; ++j) {
        uint32_t mi = (base + (uint32_t)j) & mask;
        uint32_t ri = (base + (uint32_t)(j - shift)) & mask;
        float rv = worker->ref_env[ri] - mean_r;
        float mv = worker->mic_env[mi] - mean_m;
        num += rv * mv;
        dr += rv * rv;
    }
    if (dr <= 1e5f) {
        return -1.0f;
    }
    return num / sqrtf(dr * dm);
}

static void track_acoustic_delay(aec_worker_t *worker, uint32_t current_ms)
{
    const size_t n = worker->frame_samples;
    const uint32_t mask = AEC_LAG_HISTORY_BINS - 1U;
    for (size_t off = 0; off + AEC_LAG_BIN_SAMPLES <= n; off += AEC_LAG_BIN_SAMPLES) {
        float r_sum = 0.0f;
        float m_sum = 0.0f;
        for (size_t k = 0; k < AEC_LAG_BIN_SAMPLES; ++k) {
            float rv = (float)worker->reference_16k_mono[off + k];
            float mv = (float)worker->mic_16k_mono[off + k];
            r_sum += rv * rv;
            m_sum += mv * mv;
        }
        uint32_t idx = worker->env_head & mask;
        worker->ref_env[idx] = sqrtf(r_sum / (float)AEC_LAG_BIN_SAMPLES);
        worker->mic_env[idx] = sqrtf(m_sum / (float)AEC_LAG_BIN_SAMPLES);
        worker->env_head++;
        if (worker->env_count < AEC_LAG_HISTORY_BINS) {
            worker->env_count++;
        }
    }

    if (worker->env_count < AEC_LAG_HISTORY_BINS ||
        (uint32_t)(current_ms - worker->last_delay_track_ms) < AEC_DELAY_TRACK_MS) {
        return;
    }
    worker->last_delay_track_ms = current_ms;

    /* Two-stage zero-mean Pearson correlation over 1.15 s of raw-ref vs mic
     * 2 ms RMS envelopes covering 0..896 ms total acoustic path delay. */
    const int j_start = AEC_LAG_MAX_SHIFT;
    const int j_end = (int)AEC_LAG_HISTORY_BINS;
    const float inv_n = 1.0f / (float)(j_end - j_start);
    const uint32_t base = worker->env_head - AEC_LAG_HISTORY_BINS;
    float mean_m = 0.0f;
    for (int j = j_start; j < j_end; ++j) {
        mean_m += worker->mic_env[(base + (uint32_t)j) & mask];
    }
    mean_m *= inv_n;
    float dm = 0.0f;
    for (int j = j_start; j < j_end; ++j) {
        float dv = worker->mic_env[(base + (uint32_t)j) & mask] - mean_m;
        dm += dv * dv;
    }
    if (dm <= 1e5f) {
        worker->last_lag_corr = 0.0f;
        return;
    }

    int coarse_best = 0;
    float best_corr = -1.0f;
    for (int shift = 0; shift <= AEC_LAG_MAX_SHIFT; shift += 4) {
        float c = eval_lag_corr(worker, shift, j_start, j_end, inv_n, mean_m, dm);
        if (c > best_corr) {
            best_corr = c;
            coarse_best = shift;
        }
    }
    int best_shift = coarse_best;
    int fine_lo = coarse_best - 3;
    int fine_hi = coarse_best + 3;
    if (fine_lo < 0) {
        fine_lo = 0;
    }
    if (fine_hi > AEC_LAG_MAX_SHIFT) {
        fine_hi = AEC_LAG_MAX_SHIFT;
    }
    for (int shift = fine_lo; shift <= fine_hi; ++shift) {
        if (shift == coarse_best) {
            continue;
        }
        float c = eval_lag_corr(worker, shift, j_start, j_end, inv_n, mean_m, dm);
        if (c > best_corr) {
            best_corr = c;
            best_shift = shift;
        }
    }

    int total_lag_ms = best_shift * 2;
    int cur_delay_ms = (int)(worker->ref_delay_samples / (AEC_SAMPLE_RATE_HZ / 1000U));
    int res_lag_ms = total_lag_ms - cur_delay_ms;
    worker->last_total_lag_ms = total_lag_ms;
    worker->last_res_lag_ms = res_lag_ms;
    worker->last_lag_corr = best_corr > 0.0f ? best_corr : 0.0f;

    bool auto_enabled = true;
#if CONFIG_LARKBRIDGE_TEST_IMAGE
    auto_enabled = atomic_load_explicit(&s_test_auto_delay, memory_order_acquire) &&
                   !atomic_load_explicit(&s_test_loopback, memory_order_acquire);
#endif
    if (!auto_enabled || best_corr < 0.72f) {
        return;
    }
    if (res_lag_ms >= AEC_DELAY_MIN_RESIDUAL_MS && res_lag_ms <= AEC_DELAY_MAX_RESIDUAL_MS) {
        worker->pending_delay_hits = 0U;
        return;
    }
    int desired_ms = total_lag_ms - AEC_DELAY_TARGET_MARGIN_MS;
    if (desired_ms < 0) {
        desired_ms = 0;
    } else if (desired_ms > AEC_REF_DELAY_MAX_MS) {
        desired_ms = AEC_REF_DELAY_MAX_MS;
    }
    if (desired_ms == cur_delay_ms) {
        worker->pending_delay_hits = 0U;
        return;
    }

    bool apply = (best_corr >= 0.80f);
    if (!apply) {
        if (worker->pending_delay_hits > 0U &&
            abs(desired_ms - worker->pending_delay_ms) <= 12) {
            apply = true;
        } else {
            worker->pending_delay_ms = desired_ms;
            worker->pending_delay_hits = 1U;
        }
    }
    if (apply) {
        worker->ref_delay_samples = (uint32_t)desired_ms * (AEC_SAMPLE_RATE_HZ / 1000U);
        worker->last_res_lag_ms = total_lag_ms - desired_ms;
        worker->pending_delay_hits = 0U;
        ESP_LOGI(TAG, "AEC auto-delay: %d -> %d ms (total=%d ms, corr=%.2f)",
                 cur_delay_ms, desired_ms, total_lag_ms, (double)best_corr);
    }
}

#if CONFIG_LARKBRIDGE_TEST_IMAGE
static const float s_near_base_hz[4] = {300.0f, 625.0f, 1175.0f, 2125.0f};
static const float s_near_weights[4] = {1.00f, 0.85f, 0.65f, 0.40f};
static const float s_near_bin_hz[AEC_TEST_NEAR_BINS] = {
    297.5f, 300.0f, 302.5f,
    622.5f, 625.0f, 627.5f,
    1172.5f, 1175.0f, 1177.5f,
    2122.5f, 2125.0f, 2127.5f,
};

static void inject_test_mic_signals(aec_worker_t *worker)
{
    const size_t n = worker->frame_samples;
    const int near_rms = atomic_load_explicit(&s_test_near_rms, memory_order_acquire);
    const int noise_rms = atomic_load_explicit(&s_test_noise_rms, memory_order_acquire);

    for (size_t i = 0; i < n; ++i) {
        int32_t raw = worker->mic_16k_mono[i];
        worker->diag_echo_sq += (uint64_t)(raw * raw);
    }
    if (near_rms <= 0 && noise_rms <= 0) {
        return;
    }

    const float two_pi_over_fs = 6.28318530718f / (float)AEC_SAMPLE_RATE_HZ;
    const float near_scale = (float)near_rms / 0.71211f;
    const float noise_scale = (float)noise_rms / 0.09379f;

    float mod_c = 1.0f, mod_s = 0.0f, mod_dc = 1.0f, mod_ds = 0.0f;
    float c[4] = {0}, s[4] = {0}, dc[4] = {0}, ds[4] = {0};
    if (near_rms > 0) {
        uint32_t idx0 = worker->near_sample_idx;
        float ph_mod = two_pi_over_fs * 2.5f * (float)idx0;
        mod_c = cosf(ph_mod);
        mod_s = sinf(ph_mod);
        mod_dc = cosf(two_pi_over_fs * 2.5f);
        mod_ds = sinf(two_pi_over_fs * 2.5f);
        for (int k = 0; k < 4; ++k) {
            float ph = two_pi_over_fs * s_near_base_hz[k] * (float)idx0;
            c[k] = cosf(ph);
            s[k] = sinf(ph);
            dc[k] = cosf(two_pi_over_fs * s_near_base_hz[k]);
            ds[k] = sinf(two_pi_over_fs * s_near_base_hz[k]);
        }
    }

    for (size_t i = 0; i < n; ++i) {
        float add = 0.0f;
        if (noise_rms > 0) {
            uint32_t x = worker->noise_rng;
            x ^= x << 13;
            x ^= x >> 17;
            x ^= x << 5;
            worker->noise_rng = x;
            float u = ((float)(x >> 8) * (1.0f / 8388608.0f)) - 1.0f;
            worker->noise_lp1 += 0.10f * (u - worker->noise_lp1);
            worker->noise_lp2 += 0.10f * (worker->noise_lp1 - worker->noise_lp2);
            add += worker->noise_lp2 * noise_scale;
        }
        if (near_rms > 0) {
            float env = 0.6f + 0.4f * mod_c;
            float chord = 0.0f;
            for (int k = 0; k < 4; ++k) {
                chord += s_near_weights[k] * s[k];
                float nc = c[k] * dc[k] - s[k] * ds[k];
                float ns = s[k] * dc[k] + c[k] * ds[k];
                c[k] = nc;
                s[k] = ns;
            }
            float nmc = mod_c * mod_dc - mod_s * mod_ds;
            float nms = mod_s * mod_dc + mod_c * mod_ds;
            mod_c = nmc;
            mod_s = nms;
            float near_f = env * chord * near_scale;
            int32_t near_i = (int32_t)lrintf(near_f);
            worker->diag_near_sq += (uint64_t)(near_i * near_i);
            add += near_f;
        }
        int32_t v = (int32_t)worker->mic_16k_mono[i] + (int32_t)lrintf(add);
        if (v > INT16_MAX) {
            v = INT16_MAX;
        } else if (v < INT16_MIN) {
            v = INT16_MIN;
        }
        worker->mic_16k_mono[i] = (int16_t)v;
    }
    if (near_rms > 0) {
        worker->near_sample_idx += (uint32_t)n;
    }
}

static void record_h10_diag(aec_worker_t *worker, uint32_t current_ms)
{
    (void)current_ms;
    const size_t n = worker->frame_samples;
    const uint32_t idx0 = worker->diag_samples;
    const bool track_near = (worker->diag_near_sq > 0U) ||
                            (atomic_load_explicit(&s_test_near_rms, memory_order_acquire) > 0);
    const float two_pi_over_fs = 6.28318530718f / (float)AEC_SAMPLE_RATE_HZ;
    float bc[AEC_TEST_NEAR_BINS];
    float bs[AEC_TEST_NEAR_BINS];
    float bdc[AEC_TEST_NEAR_BINS];
    float bds[AEC_TEST_NEAR_BINS];
    if (track_near) {
        for (size_t b = 0; b < AEC_TEST_NEAR_BINS; ++b) {
            float ph = two_pi_over_fs * s_near_bin_hz[b] * (float)idx0;
            bc[b] = cosf(ph);
            bs[b] = sinf(ph);
            bdc[b] = cosf(two_pi_over_fs * s_near_bin_hz[b]);
            bds[b] = sinf(two_pi_over_fs * s_near_bin_hz[b]);
        }
    }
    for (size_t i = 0; i < n; ++i) {
        int32_t r = worker->reference_delayed_16k_mono[i];
        int32_t m = worker->mic_16k_mono[i];
        int32_t o = worker->output_16k_mono[i];
        worker->diag_ref_sq += (uint64_t)(r * r);
        worker->diag_mic_sq += (uint64_t)(m * m);
        worker->diag_out_sq += (uint64_t)(o * o);
        if (o >= INT16_MAX || o <= INT16_MIN) {
            ++worker->diag_clip_samples;
        }
        if (track_near) {
            float of = (float)o;
            for (size_t b = 0; b < AEC_TEST_NEAR_BINS; ++b) {
                worker->diag_iq_re[b] += of * bc[b];
                worker->diag_iq_im[b] += of * bs[b];
                float nc = bc[b] * bdc[b] - bs[b] * bds[b];
                float ns = bs[b] * bdc[b] + bc[b] * bds[b];
                bc[b] = nc;
                bs[b] = ns;
            }
        }
    }
    worker->diag_samples += (uint32_t)n;

    if (worker->diag_samples < AEC_TEST_DIAG_SAMPLES) {
        return;
    }
    double ref_rms = sqrt((double)worker->diag_ref_sq / (double)worker->diag_samples);
    double mic_rms = sqrt((double)worker->diag_mic_sq / (double)worker->diag_samples);
    double out_rms = sqrt((double)worker->diag_out_sq / (double)worker->diag_samples);
    double erle_db = (mic_rms >= 20.0 && out_rms > 0.5)
                         ? 20.0 * log10(mic_rms / out_rms)
                         : (mic_rms >= 20.0 ? 60.0 : 0.0);

    if (worker->diag_near_sq > 0U) {
        double e_out_near = 0.0;
        for (size_t b = 0; b < AEC_TEST_NEAR_BINS; ++b) {
            double re = (double)worker->diag_iq_re[b];
            double im = (double)worker->diag_iq_im[b];
            e_out_near += (2.0 / (double)worker->diag_samples) * (re * re + im * im);
        }
        double e_out_total = (double)worker->diag_out_sq;
        double e_near_in = (double)worker->diag_near_sq;
        double e_echo_in = (double)worker->diag_echo_sq;
        double e_res = e_out_total - e_out_near;
        if (e_res < 1.0) {
            e_res = 1.0;
        }
        double near_rms = sqrt(e_near_in / (double)worker->diag_samples);
        double near_gain_db = 10.0 * log10(fmax(e_out_near, 1.0) / fmax(e_near_in, 1.0));
        double dt_snr_db = 10.0 * log10(fmax(e_out_near, 1.0) / e_res);
        double dt_erle_db = 10.0 * log10(fmax(e_echo_in, 1.0) / e_res);
        ESP_LOGI(TAG,
                 "H10_AEC ref_rms=%.1f mic_rms=%.1f out_rms=%.1f erle_db=%.2f "
                 "clip=%lu/%lu res_lag_ms=%d corr=%.2f int_free=%u int_lrg=%u delay_ms=%u "
                 "near_rms=%.1f near_gain_db=%.2f dt_snr_db=%.2f dt_erle_db=%.2f",
                 ref_rms, mic_rms, out_rms, erle_db,
                 (unsigned long)worker->diag_clip_samples,
                 (unsigned long)worker->diag_samples,
                 worker->last_res_lag_ms,
                 (double)worker->last_lag_corr,
                 (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT),
                 (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT),
                 (unsigned)(worker->ref_delay_samples / (AEC_SAMPLE_RATE_HZ / 1000U)),
                 near_rms, near_gain_db, dt_snr_db, dt_erle_db);
    } else {
        ESP_LOGI(TAG,
                 "H10_AEC ref_rms=%.1f mic_rms=%.1f out_rms=%.1f erle_db=%.2f "
                 "clip=%lu/%lu res_lag_ms=%d corr=%.2f int_free=%u int_lrg=%u delay_ms=%u",
                 ref_rms, mic_rms, out_rms, erle_db,
                 (unsigned long)worker->diag_clip_samples,
                 (unsigned long)worker->diag_samples,
                 worker->last_res_lag_ms,
                 (double)worker->last_lag_corr,
                 (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT),
                 (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT),
                 (unsigned)(worker->ref_delay_samples / (AEC_SAMPLE_RATE_HZ / 1000U)));
    }
    worker->diag_ref_sq = 0U;
    worker->diag_mic_sq = 0U;
    worker->diag_echo_sq = 0U;
    worker->diag_near_sq = 0U;
    worker->diag_out_sq = 0U;
    worker->diag_samples = 0U;
    worker->diag_clip_samples = 0U;
    memset(worker->diag_iq_re, 0, sizeof(worker->diag_iq_re));
    memset(worker->diag_iq_im, 0, sizeof(worker->diag_iq_im));
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
             "align=%dppm delta=%ld fill_err=%ld heap=%u min_heap=%u largest=%u delay_ms=%u",
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
             (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_8BIT),
             (unsigned)(worker->ref_delay_samples / (AEC_SAMPLE_RATE_HZ / 1000U)));
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
        inject_test_mic_signals(&worker);
#endif
        track_acoustic_delay(&worker, current_ms);
        {
            const uint32_t dmask = AEC_REF_DELAY_CAP_SAMPLES - 1U;
            const uint32_t delay = worker.ref_delay_samples;
            const size_t n = worker.frame_samples;
            for (size_t i = 0; i < n; ++i) {
                uint32_t w = (worker.ref_delay_write_idx + (uint32_t)i) & dmask;
                worker.ref_delay_line[w] = worker.reference_16k_mono[i];
                uint32_t r = (worker.ref_delay_write_idx + (uint32_t)i - delay) & dmask;
                worker.reference_delayed_16k_mono[i] = worker.ref_delay_line[r];
            }
            worker.ref_delay_write_idx += (uint32_t)n;
        }
#if CONFIG_LARKBRIDGE_TEST_IMAGE
        if (atomic_load_explicit(&s_test_loopback, memory_order_acquire) &&
            (worker.frame_samples == 256U || worker.frame_samples == 512U)) {
            /* Synthetic zero-drift echo: mic = 0.5 * delayed_ref shifted by 8 ms (128 samples). */
            const size_t d = 128U;
            const size_t n = worker.frame_samples;
            for (size_t i = 0; i < d; ++i) {
                worker.mic_16k_mono[i] = (int16_t)(worker.loop_delay[i] / 2);
            }
            for (size_t i = d; i < n; ++i) {
                worker.mic_16k_mono[i] = (int16_t)(worker.reference_delayed_16k_mono[i - d] / 2);
            }
            memcpy(worker.loop_delay, &worker.reference_delayed_16k_mono[n - d], d * sizeof(int16_t));
        }
#endif

        int64_t started_us = esp_timer_get_time();
        if (AEC_ENGINE_MODE == AEC_MODE_SR_LOW_COST) {
            aec_linear_process(worker.handle, worker.mic_16k_mono,
                               worker.reference_delayed_16k_mono, worker.output_16k_mono);
        } else {
            aec_process(worker.handle, worker.mic_16k_mono,
                        worker.reference_delayed_16k_mono, worker.output_16k_mono);
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
        if (end == NULL || *end != '\0' || ms < 0L || ms > AEC_REF_DELAY_MAX_MS) {
            return false;
        }
        atomic_store_explicit(&s_test_delay_ms, (int)ms, memory_order_release);
        s31_audio_router_reset_aec_stream();
        ESP_LOGI(TAG, "TEST: set AEC reference delay to %ld ms and reset AEC stream", ms);
        return true;
    }
    if (strncmp(kind, "aec_auto", 8U) == 0 && (kind[8] == '0' || kind[8] == '1') && kind[9] == '\0') {
        bool en = (kind[8] == '1');
        atomic_store_explicit(&s_test_auto_delay, en, memory_order_release);
        ESP_LOGI(TAG, "TEST: set AEC auto-delay=%d", (int)en);
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
    if (strncmp(kind, "aec_near", 8U) == 0 && kind[8] != '\0') {
        long rms = strtol(kind + 8, &end, 10);
        if (end == NULL || *end != '\0' || rms < 0L || rms > 2000L) {
            return false;
        }
        atomic_store_explicit(&s_test_near_rms, (int)rms, memory_order_release);
        ESP_LOGI(TAG, "TEST: set AEC near-end speech rms=%ld", rms);
        return true;
    }
    if (strncmp(kind, "aec_noise", 9U) == 0 && kind[9] != '\0') {
        long rms = strtol(kind + 9, &end, 10);
        if (end == NULL || *end != '\0' || rms < 0L || rms > 1000L) {
            return false;
        }
        atomic_store_explicit(&s_test_noise_rms, (int)rms, memory_order_release);
        ESP_LOGI(TAG, "TEST: set AEC car cabin noise rms=%ld", rms);
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
