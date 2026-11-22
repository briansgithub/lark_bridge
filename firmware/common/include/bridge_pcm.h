#ifndef BRIDGE_PCM_H
#define BRIDGE_PCM_H

#include <stddef.h>
#include <stdint.h>

int16_t bridge_pcm_saturate_s16(int32_t sample);
void bridge_pcm_stereo_to_mono_s16(const int16_t *stereo, int16_t *mono, size_t frames);
void bridge_pcm_mono_to_stereo_s16(const int16_t *mono, int16_t *stereo, size_t frames);
void bridge_pcm_mix_mono_s16(
    const int16_t *first, const int16_t *second, int16_t *output, size_t frames);

#endif
