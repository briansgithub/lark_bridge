#pragma once

#include "esp_err.h"

#include <stdbool.h>
#include <stdint.h>

typedef struct {
    bool peer_seen;
    bool output_hello_valid;
    bool output_session_valid;
    uint32_t protocol_errors;
    uint32_t reliable_failures;
    uint16_t output_fifo_frames;
    uint16_t output_fifo_min_frames;
    uint16_t output_fifo_max_frames;
    int16_t output_correction_ppm;
    uint32_t output_missing_frames;
    uint32_t output_dropped_frames;
    uint32_t output_i2s_errors;
    uint32_t output_protocol_errors;
    uint32_t output_reliable_failures;
} s31_link_stats_t;

esp_err_t s31_link_uart_init(void);
/* Telemetry snapshot; word-sized fields are read without a lock. */
void s31_link_uart_get_stats(s31_link_stats_t *stats);#if CONFIG_LARKBRIDGE_TEST_IMAGE
#include "bridge_console.h"
bool s31_link_uart_test_fault(const char *kind);
void s31_link_uart_test_stats(bridge_json_t *json);
#endif
