#ifndef BRIDGE_RESAMPLE_H
#define BRIDGE_RESAMPLE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* Linearly maps a complete signed-16 stereo block to a new frame count while
 * preserving both endpoints. Intended for tiny (normally +/- one-frame)
 * elastic-buffer corrections, not sample-rate family conversion. */
bool bridge_resample_stereo_s16(const int16_t *input, size_t input_frames,
                                int16_t *output, size_t output_frames);

#endif
