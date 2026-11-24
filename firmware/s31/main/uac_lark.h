#pragma once

#include "esp_err.h"

#include <stdbool.h>
#include <stdint.h>

typedef struct {
    bool capture_started;
    uint32_t rx_packets;
    uint32_t rx_bytes;
    uint32_t read_errors;
    uint32_t transfer_errors;
    uint32_t disconnects;
    uint32_t opens;
    uint32_t reopen_attempts;
} s31_uac_stats_t;

esp_err_t s31_uac_lark_init(void);
void s31_uac_lark_get_stats(s31_uac_stats_t *stats);
