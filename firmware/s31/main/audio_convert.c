#include "audio_convert.h"

#include <stdbool.h>
#include <stdint.h>

#define RESAMPLE_INPUT_RATE_HZ 44100U
#define RESAMPLE_OUTPUT_RATE_HZ 48000U

void s31_stereo_resampler_reset(s31_stereo_resampler_t *state)
{
    if (state != NULL) {
        state->phase = 0U;
        state->previous_left = 0;
        state->previous_right = 0;
        state->have_previous = false;
    }
}

static int16_t interpolate_sample(int16_t first, int16_t second, uint32_t phase)
{
    int64_t difference = (int64_t)second - first;
    return (int16_t)((int64_t)first +
                     (difference * phase) / (int64_t)RESAMPLE_OUTPUT_RATE_HZ);
}

size_t s31_resample_44100_to_48000_stereo(s31_stereo_resampler_t *state,
                                          const int16_t *input, size_t input_frames,
                                          int16_t *output, size_t output_frame_capacity)
{
    size_t input_index;
    size_t output_frames = 0U;
    size_t conservative_required;

    if (state == NULL || input == NULL || output == NULL || input_frames == 0U ||
        input_frames > (SIZE_MAX - (RESAMPLE_INPUT_RATE_HZ - 1U)) /
                           RESAMPLE_OUTPUT_RATE_HZ) {
        return 0U;
    }
    conservative_required =
        (input_frames * RESAMPLE_OUTPUT_RATE_HZ + RESAMPLE_INPUT_RATE_HZ - 1U) /
            RESAMPLE_INPUT_RATE_HZ +
        1U;
    if (output_frame_capacity < conservative_required) {
        return 0U;
    }

    for (input_index = 0U; input_index < input_frames; ++input_index) {
        int16_t current_left = input[input_index * 2U];
        int16_t current_right = input[input_index * 2U + 1U];
        if (!state->have_previous) {
            state->previous_left = current_left;
            state->previous_right = current_right;
            state->have_previous = true;
            continue;
        }
        while (state->phase < RESAMPLE_OUTPUT_RATE_HZ) {
            output[output_frames * 2U] = interpolate_sample(
                state->previous_left, current_left, state->phase);
            output[output_frames * 2U + 1U] = interpolate_sample(
                state->previous_right, current_right, state->phase);
            ++output_frames;
            state->phase += RESAMPLE_INPUT_RATE_HZ;
        }
        state->phase -= RESAMPLE_OUTPUT_RATE_HZ;
        state->previous_left = current_left;
        state->previous_right = current_right;
    }
    return output_frames;
}

size_t s31_expand_mono_to_48k_stereo(const int16_t *input, size_t input_samples,
                                    uint32_t input_rate_hz, int16_t *output,
                                    size_t output_frame_capacity)
{
    size_t input_index;
    size_t output_frames = 0;
    uint32_t factor;

    if (input == NULL || output == NULL ||
        (input_rate_hz != 8000U && input_rate_hz != 16000U)) {
        return 0;
    }
    factor = 48000U / input_rate_hz;
    if (input_samples > output_frame_capacity / factor) {
        input_samples = output_frame_capacity / factor;
    }
    for (input_index = 0; input_index < input_samples; ++input_index) {
        uint32_t repeat;
        for (repeat = 0; repeat < factor; ++repeat) {
            output[output_frames * 2U] = input[input_index];
            output[output_frames * 2U + 1U] = input[input_index];
            ++output_frames;
        }
    }
    return output_frames;
}

size_t s31_expand_mono_8k_to_16k(const int16_t *input, size_t input_samples,
                                 int16_t *output, size_t output_capacity)
{
    if (input == NULL || output == NULL || input_samples > output_capacity / 2U) {
        return 0U;
    }
    for (size_t index = 0U; index < input_samples; ++index) {
        output[index * 2U] = input[index];
        output[index * 2U + 1U] = input[index];
    }
    return input_samples * 2U;
}

size_t s31_downsample_48k_stereo_to_mono(const int16_t *input, size_t input_frames,
                                         uint32_t output_rate_hz, int16_t *output,
                                         size_t output_capacity)
{
    uint32_t factor;
    size_t output_samples;
    size_t output_index;

    if (input == NULL || output == NULL ||
        (output_rate_hz != 8000U && output_rate_hz != 16000U)) {
        return 0;
    }
    factor = 48000U / output_rate_hz;
    output_samples = input_frames / factor;
    if (output_samples > output_capacity) {
        output_samples = output_capacity;
    }
    for (output_index = 0; output_index < output_samples; ++output_index) {
        int64_t accumulator = 0;
        uint32_t frame;
        for (frame = 0; frame < factor; ++frame) {
            size_t index = (output_index * factor + frame) * 2U;
            accumulator += input[index];
            accumulator += input[index + 1U];
        }
        output[output_index] = (int16_t)(accumulator / (int32_t)(factor * 2U));
    }
    return output_samples;
}
