#pragma once

#include <stdbool.h>

#include "esp_err.h"
#include "sdkconfig.h"

/* Starts the real ESP-SR worker. The worker remains fail-closed unless the
 * explicit experimental qualification option is enabled. */
esp_err_t s31_aec_gate_init(void);

#if CONFIG_LARKBRIDGE_TEST_IMAGE
/* Test-image fault hook: "aec_d<NNN>" sets initial reference delay (0..450 ms)
 * and resets the AEC stream for live H10 acoustic-delay calibration. */
bool s31_aec_test_fault(const char *kind);
#endif
