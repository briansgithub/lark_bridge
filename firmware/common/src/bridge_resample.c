#include "bridge_resample.h"

#include <stdint.h>
#include <string.h>

bool bridge_resample_stereo_s16(const int16_t *input, size_t input_frames,
                                int16_t *output, size_t output_frames)
{
    if (input == NULL || output == NULL || input_frames == 0U || output_frames == 0U) {
        return false;
    }
    if (input_frames == output_frames) {
        memmove(output, input, input_frames * 2U * sizeof(int16_t));
        return true;
    }
    if (input_frames == 1U || output_frames == 1U) {
        for (size_t frame = 0U; frame < output_frames; ++frame) {
            output[frame * 2U] = input[0];
            output[frame * 2U + 1U] = input[1];
        }
        return true;
    }

    const uint64_t denominator = output_frames - 1U;
    for (size_t frame = 0U; frame < output_frames; ++frame) {
        const uint64_t position = (uint64_t)frame * (input_frames - 1U);
        const size_t first = (size_t)(position / denominator);
        const size_t second = first + 1U < input_frames ? first + 1U : first;
        const int64_t fraction = (int64_t)(position % denominator);
        for (size_t channel = 0U; channel < 2U; ++channel) {
            const int32_t a = input[first * 2U + channel];
            const int32_t b = input[second * 2U + channel];
            const int64_t interpolated =
                (int64_t)a * ((int64_t)denominator - fraction) +
                (int64_t)b * fraction;
            output[frame * 2U + channel] =
                (int16_t)(interpolated / (int64_t)denominator);
        }
    }
    return true;
}

#define Q32_ONE (1ULL << 32)

static bool step_valid(uint64_t step_q32)
{
    return step_q32 >= Q32_ONE / 2U && step_q32 <= 2U * Q32_ONE;
}

void bridge_resampler_reset(bridge_resampler_t *resampler)
{
    if (resampler != NULL) {
        resampler->previous[0] = 0;
        resampler->previous[1] = 0;
        resampler->lag_q32 = 0U;
    }
}

size_t bridge_resampler_input_frames(const bridge_resampler_t *resampler,
                                     uint64_t step_q32, size_t output_frames)
{
    if (resampler == NULL || !step_valid(step_q32) || output_frames == 0U) {
        return 0U;
    }
    uint64_t end = (uint64_t)output_frames * step_q32 - resampler->lag_q32;
    return (size_t)((end + Q32_ONE - 1U) >> 32);
}

bool bridge_resampler_process(bridge_resampler_t *resampler, uint64_t step_q32,
                              const int16_t *input, size_t input_frames,
                              int16_t *output, size_t output_frames)
{
    if (resampler == NULL || input == NULL || output == NULL || output_frames == 0U ||
        input_frames == 0U ||
        input_frames != bridge_resampler_input_frames(resampler, step_q32, output_frames)) {
        return false;
    }
    /* Position relative to `previous` (index 0); input[i] sits at i + 1. */
    uint64_t position = step_q32 - resampler->lag_q32;
    for (size_t frame = 0U; frame < output_frames; ++frame, position += step_q32) {
        size_t upper = (size_t)((position + Q32_ONE - 1U) >> 32);
        uint64_t fraction;
        if (upper == 0U) {
            upper = 1U;
            fraction = 0U;
        } else {
            fraction = position - ((uint64_t)(upper - 1U) << 32);
        }
        for (size_t channel = 0U; channel < 2U; ++channel) {
            const int64_t a = upper == 1U ? resampler->previous[channel]
                                          : input[(upper - 2U) * 2U + channel];
            const int64_t b = input[(upper - 1U) * 2U + channel];
            const int64_t value =
                a * (int64_t)(Q32_ONE - fraction) + b * (int64_t)fraction;
            output[frame * 2U + channel] = (int16_t)(value >> 32);
        }
    }
    position -= step_q32;
    resampler->lag_q32 = (uint32_t)(((uint64_t)input_frames << 32) - position);
    resampler->previous[0] = input[(input_frames - 1U) * 2U];
    resampler->previous[1] = input[(input_frames - 1U) * 2U + 1U];
    return true;
}
