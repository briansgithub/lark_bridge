#pragma once

#include "esp_err.h"

/* Starts the real ESP-SR worker. The worker remains fail-closed unless the
 * explicit experimental qualification option is enabled. */
esp_err_t s31_aec_gate_init(void);
