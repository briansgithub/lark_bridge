#pragma once

/* Output-board qualification telemetry (firmware/docs/hil-rig.md section 6):
 * a JSON "boot" record, a 1 Hz JSON "stats" record, and line commands on the
 * UART0 console. Test hooks exist only when CONFIG_LARKBRIDGE_TEST_IMAGE is set. */

#include <stddef.h>

#include "esp_err.h"

typedef enum {
    OUTPUT_TAP_I2S_IN = 0,
    OUTPUT_TAP_A2DP_OUT,
    OUTPUT_TAP_COUNT,
} output_tap_stage_t;

esp_err_t output_telemetry_start(void);
void output_telemetry_tap_note(output_tap_stage_t stage, const void *pcm, size_t bytes);
