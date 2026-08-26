#include "aec_gate.h"

#include <limits.h>
#include <stdbool.h>
#include <stdint.h>

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
#define AEC_STARTUP_TIMEOUT_MS 750U
#define AEC_TELEMETRY_MS 10000U
#define AEC_ALIGNMENT_CONTROLLER_CENTER 4096
#define AEC_ALIGNMENT_MAX_CORRECTION_PPM 500
#define AEC_ALIGNMENT_RATE_SCALE 1000000LL
#define AEC_OUTPUT_PREFILL_FRAMES 2U

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
    int64_t alignment_accumulator;
    int32_t alignment_target_samples;
    int32_t alignment_correction_ppm;
    int32_t alignment_input_delta_frames;
    bool alignment_target_valid;
    bool uplink_ready;
} aec_worker_t;

static const char *TAG = "s31_aec";

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
    worker->alignment_target_valid = false;
    worker->alignment_accumulator = 0;
    worker->alignment_correction_ppm = 0;
}

static void *allocate_audio_buffer(size_t bytes)
{
    return heap_caps_aligned_alloc(AEC_BUFFER_ALIGNMENT, bytes, MALLOC_CAP_8BIT);
}

static bool start_engine(aec_worker_t *worker)
{
    aec_config_t config = {
        .mic_num = 1,
        .ref_num = 1,
        .out_num = 1,
        .filter_length = AEC_FILTER_LENGTH,
        .sample_rate = AEC_SAMPLE_RATE_HZ,
        .caps = MALLOC_CAP_8BIT,
        .mode = AEC_MODE_FD_LOW_COST,
        .nlp_level = AEC_NLP_LEVEL_AGGR,
    };
    int frame_samples;

    s31_audio_router_reset_aec_inputs();
    worker->generation = s31_audio_router_aec_generation();
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
        worker->mic_bytes + AEC_MIC_CHANNELS * sizeof(int16_t));
    worker->mic_aligned_48k_stereo = allocate_audio_buffer(worker->mic_bytes);
    worker->mic_16k_mono = allocate_audio_buffer(worker->reference_bytes);
    worker->reference_16k_mono = allocate_audio_buffer(worker->reference_bytes);
    worker->output_16k_mono = allocate_audio_buffer(worker->reference_bytes);
    if (worker->mic_48k_stereo == NULL || worker->mic_aligned_48k_stereo == NULL ||
        worker->mic_16k_mono == NULL ||
        worker->reference_16k_mono == NULL || worker->output_16k_mono == NULL) {
        ESP_LOGE(TAG, "AEC frame-buffer allocation failed");
        release_engine(worker);
        s31_gateway_set_fault(BRIDGE_FAULT_AEC_NOT_READY);
        return false;
    }
    worker->delay_bytes_remaining =
        (size_t)CONFIG_LARKBRIDGE_S31_AEC_REFERENCE_DELAY_MS *
        (AEC_MIC_INPUT_RATE_HZ / 1000U) * AEC_MIC_CHANNELS * sizeof(int16_t);
    if (!bridge_rate_controller_init(&worker->alignment_controller,
                                     AEC_ALIGNMENT_CONTROLLER_CENTER,
                                     65536, 64,
                                     AEC_ALIGNMENT_MAX_CORRECTION_PPM)) {
        release_engine(worker);
        s31_gateway_set_fault(BRIDGE_FAULT_AEC_NOT_READY);
        return false;
    }
    worker->last_frame_ms = now_ms();
    worker->last_progress_ms = worker->last_frame_ms;
    ESP_LOGI(TAG, "ESP-SR AEC active: %u samples/frame, reference delay=%d ms, publish=%d",
             (unsigned)worker->frame_samples,
             CONFIG_LARKBRIDGE_S31_AEC_REFERENCE_DELAY_MS,
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
            continue;
        }
        if (!discard_initial_mic_delay(&worker)) {
            vTaskDelay(pdMS_TO_TICKS(AEC_POLL_MS));
            continue;
        }
        if (!worker.alignment_target_valid) {
            worker.alignment_target_samples =
                s31_audio_router_aec_fill_difference_samples();
            worker.alignment_target_valid = true;
            bridge_rate_controller_reset(&worker.alignment_controller);
        }
        int32_t fill_error = s31_audio_router_aec_fill_difference_samples() -
                             worker.alignment_target_samples;
        int64_t virtual_fill = (int64_t)AEC_ALIGNMENT_CONTROLLER_CENTER + fill_error;
        if (virtual_fill < 1) {
            virtual_fill = 1;
        } else if (virtual_fill > INT32_MAX) {
            virtual_fill = INT32_MAX;
        }
        worker.alignment_correction_ppm = bridge_rate_controller_update(
            &worker.alignment_controller, (int32_t)virtual_fill);
        int64_t proposed_accumulator = worker.alignment_accumulator -
            (int64_t)worker.alignment_correction_ppm *
            (int64_t)worker.mic_nominal_frames;
        int adjustment = 0;
        if (proposed_accumulator >= AEC_ALIGNMENT_RATE_SCALE) {
            adjustment = 1;
            proposed_accumulator -= AEC_ALIGNMENT_RATE_SCALE;
        } else if (proposed_accumulator <= -AEC_ALIGNMENT_RATE_SCALE) {
            adjustment = -1;
            proposed_accumulator += AEC_ALIGNMENT_RATE_SCALE;
        }
        size_t mic_input_frames =
            (size_t)((int)worker.mic_nominal_frames + adjustment);
        size_t mic_input_bytes = mic_input_frames * AEC_MIC_CHANNELS * sizeof(int16_t);
        if (!s31_audio_router_read_aec_inputs(
                (uint8_t *)worker.mic_48k_stereo, mic_input_bytes,
                (uint8_t *)worker.reference_16k_mono, worker.reference_bytes)) {
            uint32_t timeout = worker.engine_frames == 0U
                                   ? AEC_STARTUP_TIMEOUT_MS
                                   : AEC_STARVATION_MS;
            if ((uint32_t)(current_ms - worker.last_progress_ms) >= timeout) {
                ++worker.starvations;
                ESP_LOGW(TAG, "AEC input starved; restarting fail-closed pipeline");
                s31_gateway_set_fault(BRIDGE_FAULT_AEC_NOT_READY);
                release_engine(&worker);
            } else {
                vTaskDelay(pdMS_TO_TICKS(AEC_POLL_MS));
            }
            continue;
        }
        worker.alignment_accumulator = proposed_accumulator;
        worker.alignment_input_delta_frames += adjustment;
        worker.last_progress_ms = current_ms;
        const int16_t *aligned_mic = worker.mic_48k_stereo;
        if (adjustment != 0) {
            if (!bridge_resample_stereo_s16(worker.mic_48k_stereo,
                                            mic_input_frames,
                                            worker.mic_aligned_48k_stereo,
                                            worker.mic_nominal_frames)) {
                ++worker.resynchronizations;
                release_engine(&worker);
                continue;
            }
            aligned_mic = worker.mic_aligned_48k_stereo;
        }
        if (s31_audio_router_aec_generation() != worker.generation ||
            s31_downsample_48k_stereo_to_mono(
                aligned_mic, worker.mic_nominal_frames,
                AEC_SAMPLE_RATE_HZ, worker.mic_16k_mono,
                worker.frame_samples) != worker.frame_samples) {
            ++worker.resynchronizations;
            release_engine(&worker);
            continue;
        }

        int64_t started_us = esp_timer_get_time();
        aec_process(worker.handle, worker.mic_16k_mono,
                    worker.reference_16k_mono, worker.output_16k_mono);
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
        log_telemetry(&worker, current_ms);
    }
}

esp_err_t s31_aec_gate_init(void)
{
    s31_gateway_set_aec_ready(false);
    if (xTaskCreate(aec_task, "s31_aec", 8192U, NULL, 10U, NULL) != pdPASS) {
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
