#ifndef BRIDGE_RATE_H
#define BRIDGE_RATE_H

#include <stdbool.h>
#include <stdint.h>

/*
 * Bounded PI controller for the output board's elastic FIFO. A positive result
 * means the FIFO is low, so the local consumer periodically uses one fewer
 * input frame; a negative result means it is high, so it periodically consumes
 * one extra input frame. Gains are signed Q16.16 ppm per frame/error
 * accumulation.
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
