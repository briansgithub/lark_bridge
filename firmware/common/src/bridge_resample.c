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
