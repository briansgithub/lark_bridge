#ifndef OUTPUT_BLUETOOTH_H
#define OUTPUT_BLUETOOTH_H

#include "esp_err.h"

#include <stdbool.h>

esp_err_t output_bluetooth_start(void);
bool output_bluetooth_stack_ready(void);
bool output_bluetooth_peer_connected(void);
bool output_bluetooth_audio_streaming(void);

#endif
