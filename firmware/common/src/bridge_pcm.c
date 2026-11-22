#include "bridge_pcm.h"

#include <limits.h>

int16_t bridge_pcm_saturate_s16(int32_t sample) {
    if (sample > INT16_MAX) {
        return INT16_MAX;
    }
    if (sample < INT16_MIN) {
        return INT16_MIN;
    }
    return (int16_t)sample;
}

void bridge_pcm_stereo_to_mono_s16(const int16_t *stereo, int16_t *mono, size_t frames) {
    size_t index;
    if (stereo == NULL || mono == NULL) {
        return;
    }
    for (index = 0U; index < frames; ++index) {
        int32_t sum = (int32_t)stereo[index * 2U] + (int32_t)stereo[index * 2U + 1U];
        mono[index] = bridge_pcm_saturate_s16(sum / 2);
    }
}

void bridge_pcm_mono_to_stereo_s16(const int16_t *mono, int16_t *stereo, size_t frames) {
    size_t index;
    if (mono == NULL || stereo == NULL) {
        return;
    }
    for (index = 0U; index < frames; ++index) {
        stereo[index * 2U] = mono[index];
        stereo[index * 2U + 1U] = mono[index];
    }
}

void bridge_pcm_mix_mono_s16(
    const int16_t *first, const int16_t *second, int16_t *output, size_t frames) {
    size_t index;
    if (first == NULL || second == NULL || output == NULL) {
        return;
    }
    for (index = 0U; index < frames; ++index) {
        output[index] = bridge_pcm_saturate_s16((int32_t)first[index] + (int32_t)second[index]);
    }
}
