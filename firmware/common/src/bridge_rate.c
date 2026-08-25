#include "bridge_rate.h"

#include <limits.h>
#include <stddef.h>

#define Q16_ONE 65536LL
#define INTEGRAL_LIMIT (INT32_MAX / 2)

static int64_t clamp_i64(int64_t value, int64_t minimum, int64_t maximum) {
    if (value < minimum) {
        return minimum;
    }
    if (value > maximum) {
        return maximum;
    }
    return value;
}

bool bridge_rate_controller_init(bridge_rate_controller_t *controller, int32_t target_frames,
                                 int32_t proportional_gain_q16, int32_t integral_gain_q16,
                                 int32_t max_correction_ppm) {
    if (controller == NULL || target_frames <= 0 || proportional_gain_q16 < 0 ||
        integral_gain_q16 < 0 || max_correction_ppm <= 0) {
        return false;
    }
    controller->target_frames = target_frames;
    controller->proportional_gain_q16 = proportional_gain_q16;
    controller->integral_gain_q16 = integral_gain_q16;
    controller->max_correction_ppm = max_correction_ppm;
    bridge_rate_controller_reset(controller);
    return true;
}

void bridge_rate_controller_reset(bridge_rate_controller_t *controller) {
    if (controller != NULL) {
        controller->integral_error = 0;
        controller->correction_ppm = 0;
    }
}

int32_t bridge_rate_controller_update(bridge_rate_controller_t *controller, int32_t fill_frames) {
    int64_t error;
    int64_t proposed_integral;
    int64_t correction_q16;
    int64_t correction_ppm;
    if (controller == NULL || controller->target_frames <= 0 ||
        controller->max_correction_ppm <= 0) {
        return 0;
    }

    error = (int64_t)controller->target_frames - fill_frames;
    proposed_integral = clamp_i64(controller->integral_error + error, -INTEGRAL_LIMIT,
                                  INTEGRAL_LIMIT);
    correction_q16 = error * controller->proportional_gain_q16 +
                     proposed_integral * controller->integral_gain_q16;
    correction_ppm = correction_q16 / Q16_ONE;

    if (correction_ppm > controller->max_correction_ppm) {
        controller->correction_ppm = controller->max_correction_ppm;
        if (error < 0) {
            controller->integral_error = proposed_integral;
        }
    } else if (correction_ppm < -controller->max_correction_ppm) {
        controller->correction_ppm = -controller->max_correction_ppm;
        if (error > 0) {
            controller->integral_error = proposed_integral;
        }
    } else {
        controller->integral_error = proposed_integral;
        controller->correction_ppm = (int32_t)correction_ppm;
    }
    return controller->correction_ppm;
}
