#ifndef OUTPUT_BLUETOOTH_H
#define OUTPUT_BLUETOOTH_H

#include "esp_err.h"
#include "sdkconfig.h"

#include <stdbool.h>

esp_err_t output_bluetooth_start(void);
bool output_bluetooth_stack_ready(void);
bool output_bluetooth_peer_connected(void);
bool output_bluetooth_audio_streaming(void);
#if CONFIG_LARKBRIDGE_TEST_IMAGE
/* Test-image fault hooks for Astra case 4 (lost Bluetooth events). */
bool output_bluetooth_test_fault(const char *kind);
/* Test-image peer forget: clears NVS peer, removes BT bonds, disconnects, and enters pairing mode. */
bool output_bluetooth_test_forget(void);
#endif

#endif
