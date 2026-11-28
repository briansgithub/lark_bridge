#ifndef OUTPUT_BLUETOOTH_H
#define OUTPUT_BLUETOOTH_H

#include "esp_err.h"
#include "sdkconfig.h"

#include "output_sink_policy.h"

#include <stdbool.h>
#include <stdint.h>

esp_err_t output_bluetooth_start(void);

/* Car UX feature 2: pairing mode, factory wipe, saved-sink management. Each
 * queues a request to the Bluetooth worker; false means the queue was full. */
bool output_bluetooth_request_pairing(void);
bool output_bluetooth_request_factory_wipe(void);
/* Forget by MRU index (address NULL) or by address. */
bool output_bluetooth_forget_sink(int index, const uint8_t *address);

typedef struct {
    output_sink_list_t sinks;
    bool pairing;
    uint32_t pairing_remaining_ms;
    bool connected;
    bool streaming;
} output_bluetooth_sinks_t;

void output_bluetooth_get_sinks(output_bluetooth_sinks_t *status);
/* Increments once each time all remembered sinks are wiped (status LED). */
uint32_t output_bluetooth_wipe_count(void);
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
