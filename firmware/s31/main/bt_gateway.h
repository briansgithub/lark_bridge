#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"
#include "sdkconfig.h"

esp_err_t s31_bt_gateway_init(void);
bool s31_bt_avrcp_command_supported(uint8_t command);
/* origin identifies the car tap (bridge_tap_origin of output session and sequence); a repeat
 * with the same origin is a retry of the same tap. */
esp_err_t s31_bt_send_avrcp(uint8_t command, uint8_t key_state, uint32_t origin);
#if CONFIG_LARKBRIDGE_TEST_IMAGE
/* Test-image fault hooks for Astra case 5 (stranded AVRCP release). */
bool s31_bt_test_fault(const char *kind);
#endif

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
    bool reconnect_suppressed;          /* feature 1: two user disconnects */
    unsigned int intentional_disconn_count;
    uint8_t peer[6];
} s31_bt_status_t;

void s31_bt_get_status(s31_bt_status_t *status);

/* Car UX feature 3B: pops one pending BRIDGE_MESSAGE_MEDIA_META record (0 when
 * none). Called only from link_task. */
size_t s31_bt_meta_next_record(uint8_t *output, size_t capacity);
/* A new output session: resend all known metadata. */
void s31_bt_meta_resync(void);
