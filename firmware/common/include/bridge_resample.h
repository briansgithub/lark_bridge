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

/* Phase-continuous linear resampler for signed-16 stereo streams.
 *
 * Positions are in input frames. `previous` is the last consumed input frame
 * and `lag_q32` (Q32, < 1) is how far the last produced output sat behind it.
 * Output k of the next block lies at -lag + (k + 1) * step from `previous`,
 * so the step stays uniform across block boundaries. step_q32 is the input
 * frames consumed per output frame in Q32 and must lie in [0.5, 2]. */
typedef struct {
    int16_t previous[2];
    uint32_t lag_q32;
} bridge_resampler_t;

void bridge_resampler_reset(bridge_resampler_t *resampler);

/* Input frames the next block of output_frames will consume. */
size_t bridge_resampler_input_frames(const bridge_resampler_t *resampler,
                                     uint64_t step_q32, size_t output_frames);

/* input_frames must equal bridge_resampler_input_frames() for the same step
 * and output_frames; input and output must not overlap. */
bool bridge_resampler_process(bridge_resampler_t *resampler, uint64_t step_q32,
                              const int16_t *input, size_t input_frames,
                              int16_t *output, size_t output_frames);

#endif
