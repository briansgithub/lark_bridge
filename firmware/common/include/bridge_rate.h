#ifndef BRIDGE_RATE_H
#define BRIDGE_RATE_H

#include <stdbool.h>
#include <stdint.h>

/*
 * Bounded PI controller for the inter-board elastic FIFO. A positive result
 * asks the S31 source clock/resampler to run faster; a negative result asks it
 * to run slower. Gains are signed Q16.16 ppm per frame/error accumulation.
 */
typedef struct {
    int32_t target_frames;
    int32_t proportional_gain_q16;
    int32_t integral_gain_q16;
    int32_t max_correction_ppm;
    int64_t integral_error;
    int32_t correction_ppm;
} bridge_rate_controller_t;

bool bridge_rate_controller_init(bridge_rate_controller_t *controller, int32_t target_frames,
                                 int32_t proportional_gain_q16, int32_t integral_gain_q16,
                                 int32_t max_correction_ppm);
void bridge_rate_controller_reset(bridge_rate_controller_t *controller);
int32_t bridge_rate_controller_update(bridge_rate_controller_t *controller, int32_t fill_frames);

#endif
