#ifndef OUTPUT_AUDIO_H
#define OUTPUT_AUDIO_H

#include "esp_err.h"

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef struct {
    uint32_t underruns;
    uint32_t overruns;
    uint32_t missing_frames;
    uint32_t dropped_frames;
    uint32_t i2s_read_errors;
    uint32_t i2s_gap_max_ms;
    int32_t adaptive_input_delta_frames;
    int32_t correction_ppm;
    uint16_t fifo_frames;
    uint16_t fifo_min_frames;
    uint16_t fifo_max_frames;
} output_audio_stats_t;

esp_err_t output_audio_start(void);
bool output_audio_is_initialized(void);
bool output_audio_clock_active(void);
void output_audio_set_capture_active(bool active);
size_t output_audio_pull(void *destination, size_t length, uint32_t rate, bool mono);
void output_audio_flush(void);
void output_audio_get_stats(output_audio_stats_t *stats);
/* Test images only: replace the A2DP-out PCM with a tone (hz == 0 stops it).
 * Returns false in release images. */
bool output_audio_set_test_tone(uint32_t hz, int32_t dbfs);

void output_audio_test_fifo_window(void);

#endif
