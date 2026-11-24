#pragma once

/* S31 qualification telemetry (firmware/docs/hil-rig.md section 6):
 * a JSON "boot" record, a 1 Hz JSON "stats" record, and line commands on the
 * USB Serial/JTAG console. Test hooks exist only when
 * CONFIG_LARKBRIDGE_TEST_IMAGE is set. */

#include <stddef.h>

#include "esp_err.h"

typedef enum {
    S31_TAP_I2S_OUT = 0,
    S31_TAP_LARK_IN,
    S31_TAP_COUNT,
} s31_tap_stage_t;

/* Emits the boot record and starts the stats and console tasks. */
esp_err_t s31_telemetry_start(void);

/* Test images: record a CRC/RMS digest of one block at a stage. Cheap and
 * lock-free; safe from the audio tasks. No-op in release images. */
void s31_telemetry_tap_note(s31_tap_stage_t stage, const void *pcm, size_t bytes);
