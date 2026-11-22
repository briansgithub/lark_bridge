#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* Pure conversion seams; these functions do not depend on ESP-IDF. */
typedef struct {
    uint32_t phase;
    int16_t previous_left;
    int16_t previous_right;
    bool have_previous;
} s31_stereo_resampler_t;

void s31_stereo_resampler_reset(s31_stereo_resampler_t *state);
size_t s31_resample_44100_to_48000_stereo(s31_stereo_resampler_t *state,
                                          const int16_t *input, size_t input_frames,
                                          int16_t *output, size_t output_frame_capacity);

size_t s31_expand_mono_to_48k_stereo(const int16_t *input, size_t input_samples,
                                    uint32_t input_rate_hz, int16_t *output,
                                    size_t output_frame_capacity);

size_t s31_expand_mono_8k_to_16k(const int16_t *input, size_t input_samples,
                                 int16_t *output, size_t output_capacity);

size_t s31_downsample_48k_stereo_to_mono(const int16_t *input, size_t input_frames,
                                         uint32_t output_rate_hz, int16_t *output,
                                         size_t output_capacity);
