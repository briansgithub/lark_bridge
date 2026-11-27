#include "output_meta_cache.h"

#include <string.h>

void output_meta_cache_init(output_meta_cache_t *cache)
{
    memset(cache, 0, sizeof(*cache));
    cache->play_status = BRIDGE_META_STATUS_STOPPED;
    cache->track_uid = OUTPUT_META_NO_TRACK;
}

static bool knows_anything(const output_meta_cache_t *cache)
{
    if (cache->generation_valid || cache->play_status != BRIDGE_META_STATUS_STOPPED ||
        cache->track_uid != OUTPUT_META_NO_TRACK) {
        return true;
    }
    for (unsigned int id = 1U; id <= BRIDGE_META_ATTR_COUNT; ++id) {
        if (cache->text_length[id] != 0U) {
            return true;
        }
    }
    return false;
}

static void start_track(output_meta_cache_t *cache, uint8_t generation, uint32_t now_ms)
{
    cache->generation_valid = true;
    cache->generation = generation;
    memset(cache->text_length, 0, sizeof(cache->text_length));
    memset(cache->text, 0, sizeof(cache->text));
    /* AVRCP 1.3: any value but all-ones means "a track is selected"; a fresh
     * value per track lets head units that compare identifiers notice. */
    ++cache->track_counter;
    if (cache->track_counter == OUTPUT_META_NO_TRACK) {
        cache->track_counter = 1U;
    }
    cache->track_uid = cache->track_counter;
    cache->track_change_pending = true;
    cache->track_change_due_ms = now_ms + OUTPUT_META_TRACK_DEBOUNCE_MS;
}

unsigned int output_meta_cache_apply(output_meta_cache_t *cache, const bridge_meta_record_t *record,
                                     uint32_t now_ms)
{
    unsigned int notify = 0U;

    if (record->kind == BRIDGE_META_KIND_CLEAR) {
        return output_meta_cache_clear(cache);
    }
    if (record->kind != BRIDGE_META_KIND_TEXT_ATTR &&
        record->kind != BRIDGE_META_KIND_PLAY_STATUS) {
        return 0U;
    }
    if (!cache->generation_valid || record->generation != cache->generation) {
        start_track(cache, record->generation, now_ms);
    }
    if (record->kind == BRIDGE_META_KIND_TEXT_ATTR) {
        if (record->attr_id >= 1U && record->attr_id <= BRIDGE_META_ATTR_COUNT) {
            memcpy(cache->text[record->attr_id], record->text, record->text_length);
            cache->text[record->attr_id][record->text_length] = '\0';
            cache->text_length[record->attr_id] = record->text_length;
            if (cache->track_change_pending) {
                /* Keep collecting the burst: the car queries once it is complete. */
                cache->track_change_due_ms = now_ms + OUTPUT_META_TRACK_DEBOUNCE_MS;
            }
        }
        return 0U;
    }
    if (record->play_status != cache->play_status) {
        notify |= OUTPUT_META_NOTIFY_STATUS;
    }
    bool position_valid = (record->flags & BRIDGE_META_FLAG_POSITION_VALID) != 0U;
    if (position_valid && (!cache->position_valid ||
                           record->song_position_ms != output_meta_cache_position(cache, now_ms))) {
        notify |= OUTPUT_META_NOTIFY_POSITION;
    }
    cache->play_status = record->play_status;
    cache->length_valid = (record->flags & BRIDGE_META_FLAG_LENGTH_VALID) != 0U;
    cache->length_ms = cache->length_valid ? record->song_length_ms : 0U;
    cache->position_valid = position_valid;
    cache->position_ms = position_valid ? record->song_position_ms : 0U;
    cache->position_stamp_ms = now_ms;
    return notify;
}

unsigned int output_meta_cache_clear(output_meta_cache_t *cache)
{
    bool had = knows_anything(cache);
    bool had_status = cache->play_status != BRIDGE_META_STATUS_STOPPED;
    uint64_t counter = cache->track_counter;
    output_meta_cache_init(cache);
    cache->track_counter = counter;
    if (!had) {
        return 0U;
    }
    return OUTPUT_META_NOTIFY_TRACK | (had_status ? OUTPUT_META_NOTIFY_STATUS : 0U);
}

unsigned int output_meta_cache_poll(output_meta_cache_t *cache, uint32_t now_ms)
{
    if (cache->track_change_pending && (int32_t)(now_ms - cache->track_change_due_ms) >= 0) {
        cache->track_change_pending = false;
        return OUTPUT_META_NOTIFY_TRACK;
    }
    return 0U;
}

uint32_t output_meta_cache_position(const output_meta_cache_t *cache, uint32_t now_ms)
{
    if (!cache->position_valid) {
        return UINT32_MAX;
    }
    uint32_t position = cache->position_ms;
    if (cache->play_status == BRIDGE_META_STATUS_PLAYING) {
        uint32_t elapsed = now_ms - cache->position_stamp_ms;
        position = (UINT32_MAX - position < elapsed) ? UINT32_MAX - 1U : position + elapsed;
    }
    if (cache->length_valid && cache->length_ms != 0U && position > cache->length_ms) {
        position = cache->length_ms;
    }
    return position;
}

uint32_t output_meta_cache_length(const output_meta_cache_t *cache)
{
    return cache->length_valid ? cache->length_ms : UINT32_MAX;
}

size_t output_meta_cache_attr(const output_meta_cache_t *cache, uint8_t attr_id, uint8_t *out,
                              size_t capacity)
{
    if (attr_id < 1U || attr_id > BRIDGE_META_ATTR_COUNT || out == NULL) {
        return 0U;
    }
    size_t length = cache->text_length[attr_id];
    if (length > capacity) {
        length = bridge_utf8_truncate(cache->text[attr_id], length, capacity);
    }
    memcpy(out, cache->text[attr_id], length);
    return length;
}

size_t output_meta_cache_select_attrs(const uint32_t *requested, size_t num_requested,
                                      uint8_t *ids, size_t capacity)
{
    size_t count = 0U;
    if (num_requested == 0U || requested == NULL) {
        for (uint8_t id = 1U; id <= BRIDGE_META_ATTR_COUNT && count < capacity; ++id) {
            ids[count++] = id;
        }
        return count;
    }
    for (size_t i = 0U; i < num_requested && count < capacity; ++i) {
        uint32_t id = requested[i];
        if (id < 1U || id > BRIDGE_META_ATTR_COUNT) {
            continue;
        }
        bool seen = false;
        for (size_t j = 0U; j < count; ++j) {
            seen = seen || ids[j] == (uint8_t)id;
        }
        if (!seen) {
            ids[count++] = (uint8_t)id;
        }
    }
    return count;
}
