#ifndef OUTPUT_META_CACHE_H
#define OUTPUT_META_CACHE_H

/* 32UE now-playing cache (car UX feature 3C). Pure C, host tested. Filled from
 * S31 BRIDGE_MESSAGE_MEDIA_META records and served to the car over AVRCP 1.3
 * (GetElementAttributes, GetPlayStatus, RegisterNotification). The caller
 * serializes access and turns returned notify bits into CHANGED responses. */

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "bridge_meta.h"

#define OUTPUT_META_TRACK_DEBOUNCE_MS 150U
#define OUTPUT_META_NO_TRACK UINT64_C(0xFFFFFFFFFFFFFFFF)
#define OUTPUT_META_TRACK_SELECTED UINT64_C(0)

enum {
    OUTPUT_META_NOTIFY_STATUS = 1U << 0,
    OUTPUT_META_NOTIFY_TRACK = 1U << 1,
    OUTPUT_META_NOTIFY_POSITION = 1U << 2,
};

typedef struct {
    bool generation_valid;
    uint8_t generation;
    uint8_t text_length[BRIDGE_META_ATTR_COUNT + 1U];
    char text[BRIDGE_META_ATTR_COUNT + 1U][BRIDGE_META_TEXT_MAX + 1U];
    uint8_t play_status;
    bool length_valid;
    uint32_t length_ms;
    bool position_valid;
    uint32_t position_ms;
    uint32_t position_stamp_ms;
    uint64_t track_uid;
    uint64_t track_counter;
    bool track_change_pending;
    uint32_t track_change_due_ms;
} output_meta_cache_t;

void output_meta_cache_init(output_meta_cache_t *cache);
/* Applies one S31 record. Returns notify bits to send right away (status);
 * a new generation arms the 150 ms track-change debounce instead. */
unsigned int output_meta_cache_apply(output_meta_cache_t *cache, const bridge_meta_record_t *record,
                                     uint32_t now_ms);
/* Forget everything (CLEAR, S31 link timeout, new S31 session). Returns the
 * notify bits a car should see (no-track + stopped) if anything was known. */
unsigned int output_meta_cache_clear(output_meta_cache_t *cache);
/* Call periodically: returns OUTPUT_META_NOTIFY_TRACK once the debounce for a
 * new track has elapsed (Title/Artist/Album arrive together first). */
unsigned int output_meta_cache_poll(output_meta_cache_t *cache, uint32_t now_ms);
/* Position now, interpolated from the last report while playing, clamped to
 * the song length. 0xFFFFFFFF when unknown (AVRCP "not supported"). */
uint32_t output_meta_cache_position(const output_meta_cache_t *cache, uint32_t now_ms);
/* Length for GetPlayStatus; 0xFFFFFFFF when unknown. */
uint32_t output_meta_cache_length(const output_meta_cache_t *cache);
/* Copies attribute attr_id (1..7) into out (not terminated). Returns its length;
 * 0 when empty or unknown. */
size_t output_meta_cache_attr(const output_meta_cache_t *cache, uint8_t attr_id, uint8_t *out,
                              size_t capacity);
/* Attribute ids to answer for GetElementAttributes: num_requested == 0 means
 * all seven. Unknown ids are dropped, duplicates kept once. Returns count. */
size_t output_meta_cache_select_attrs(const uint32_t *requested, size_t num_requested,
                                      uint8_t *ids, size_t capacity);

#endif
