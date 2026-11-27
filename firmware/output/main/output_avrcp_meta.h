#ifndef OUTPUT_AVRCP_META_H
#define OUTPUT_AVRCP_META_H

/* AVRCP 1.3 target metadata for the car (car UX feature 3C): owns the
 * output_meta_cache under a lock, answers GetElementAttributes/GetPlayStatus
 * through the patched Bluedroid hooks (tools/patch_btc_avrc.py), and sends
 * INTERIM/CHANGED notification responses. */

#include "bridge_protocol.h"
#include "esp_avrc_api.h"
#include "esp_err.h"

#include <stdbool.h>
#include <stdint.h>

esp_err_t output_avrcp_meta_init(void);
/* Call after esp_avrc_tg_init(): advertises the notification events. */
void output_avrcp_meta_configure_tg(void);
/* One BRIDGE_MESSAGE_MEDIA_META packet from the S31. */
void output_avrcp_meta_handle_packet(const bridge_packet_t *packet);
/* S31 link timeout, new S31 session, or car AVRCP disconnect. */
void output_avrcp_meta_clear(const char *why);
/* ESP_AVRC_TG_REGISTER_NOTIFICATION_EVT: replies INTERIM right away. */
void output_avrcp_meta_on_register(uint8_t event_id, uint32_t event_parameter);
void output_avrcp_meta_on_connection(bool connected);

typedef struct {
    uint32_t records;
    uint32_t bad_records;
    uint32_t track_changes;
    uint32_t elem_attr_queries;
    uint32_t play_status_queries;
    uint32_t interim_sent;
    uint32_t changed_sent;
    uint8_t generation;
    uint8_t play_status;
} output_avrcp_meta_stats_t;

void output_avrcp_meta_get_stats(output_avrcp_meta_stats_t *stats);
/* Copies the cached title (NUL-terminated) for telemetry. */
void output_avrcp_meta_get_title(char *out, size_t capacity);

#endif
