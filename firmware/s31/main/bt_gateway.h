#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

esp_err_t s31_bt_gateway_init(void);
bool s31_bt_avrcp_command_supported(uint8_t command);
esp_err_t s31_bt_send_avrcp(uint8_t command, uint8_t key_state);

typedef struct {
    int hfp_service_state;
    int a2dp_connection_state;
    uint32_t a2dp_rx_packets; /* sink data callbacks since boot; wraps */
    uint32_t a2dp_rx_bytes;   /* decoded PCM bytes since boot; wraps */
    bool hfp_audio_connected;
    bool avrcp_connected;
    bool hfp_profile_ready;
    bool a2dp_profile_ready;
    bool peer_bound;
    uint8_t peer[6];
} s31_bt_status_t;

void s31_bt_get_status(s31_bt_status_t *status);
