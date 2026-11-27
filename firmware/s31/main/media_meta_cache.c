#include "media_meta_cache.h"

#include <string.h>

#define PLAY_STATUS_BIT 0x0001U

void s31_meta_cache_init(s31_meta_cache_t *cache)
{
    memset(cache, 0, sizeof(*cache));
}

static void forget_values(s31_meta_cache_t *cache)
{
    memset(cache->text_length, 0, sizeof(cache->text_length));
    memset(cache->text, 0, sizeof(cache->text));
    cache->have_play_status = false;
    cache->play_status = BRIDGE_META_STATUS_STOPPED;
    cache->flags = 0U;
    cache->song_length_ms = 0U;
    cache->song_position_ms = 0U;
}

void s31_meta_cache_clear(s31_meta_cache_t *cache)
{
    forget_values(cache);
    cache->dirty = 0U;
    cache->clear_pending = true;
}

void s31_meta_cache_track_changed(s31_meta_cache_t *cache)
{
    ++cache->generation;
    memset(cache->text_length, 0, sizeof(cache->text_length));
    memset(cache->text, 0, sizeof(cache->text));
    cache->dirty &= PLAY_STATUS_BIT;
}

bool s31_meta_cache_set_text(s31_meta_cache_t *cache, uint8_t attr_id, const uint8_t *text,
                             size_t length)
{
    if (attr_id < 1U || attr_id > BRIDGE_META_ATTR_COUNT || (text == NULL && length != 0U)) {
        return false;
    }
    if (text != NULL) {
        const void *nul = memchr(text, '\0', length);
        if (nul != NULL) {
            length = (size_t)((const uint8_t *)nul - text);
        }
    }
    size_t used = bridge_utf8_truncate((const char *)text, length, BRIDGE_META_TEXT_MAX);
    if (used == cache->text_length[attr_id] &&
        (used == 0U || memcmp(cache->text[attr_id], text, used) == 0)) {
        return true;
    }
    if (used != 0U) {
        memcpy(cache->text[attr_id], text, used);
    }
    cache->text[attr_id][used] = '\0';
    cache->text_length[attr_id] = (uint8_t)used;
    cache->dirty |= (uint16_t)(1U << attr_id);
    return true;
}

void s31_meta_cache_set_play_status(s31_meta_cache_t *cache, uint8_t play_status,
                                    bool length_valid, uint32_t length_ms, bool position_valid,
                                    uint32_t position_ms)
{
    uint8_t flags = (uint8_t)((length_valid ? BRIDGE_META_FLAG_LENGTH_VALID : 0U) |
                              (position_valid ? BRIDGE_META_FLAG_POSITION_VALID : 0U));
    if (!length_valid) {
        length_ms = 0U;
    }
    if (!position_valid) {
        position_ms = 0U;
    }
    if (cache->have_play_status && cache->play_status == play_status && cache->flags == flags &&
        cache->song_length_ms == length_ms && cache->song_position_ms == position_ms) {
        return;
    }
    cache->have_play_status = true;
    cache->play_status = play_status;
    cache->flags = flags;
    cache->song_length_ms = length_ms;
    cache->song_position_ms = position_ms;
    cache->dirty |= PLAY_STATUS_BIT;
}

void s31_meta_cache_set_position(s31_meta_cache_t *cache, uint32_t position_ms)
{
    if (!cache->have_play_status) {
        s31_meta_cache_set_play_status(cache, BRIDGE_META_STATUS_PLAYING, false, 0U, true,
                                       position_ms);
        return;
    }
    bool length_valid = (cache->flags & BRIDGE_META_FLAG_LENGTH_VALID) != 0U;
    s31_meta_cache_set_play_status(cache, cache->play_status, length_valid, cache->song_length_ms,
                                   true, position_ms);
}

void s31_meta_cache_mark_all_dirty(s31_meta_cache_t *cache)
{
    for (unsigned int id = 1U; id <= BRIDGE_META_ATTR_COUNT; ++id) {
        if (cache->text_length[id] != 0U) {
            cache->dirty |= (uint16_t)(1U << id);
        }
    }
    if (cache->have_play_status) {
        cache->dirty |= PLAY_STATUS_BIT;
    }
}

size_t s31_meta_cache_next_record(s31_meta_cache_t *cache, uint8_t *output, size_t capacity)
{
    if (cache->clear_pending) {
        size_t n = bridge_meta_pack_clear(output, capacity, cache->generation);
        if (n != 0U) {
            cache->clear_pending = false;
        }
        return n;
    }
    for (unsigned int id = 1U; id <= BRIDGE_META_ATTR_COUNT; ++id) {
        if ((cache->dirty & (1U << id)) != 0U) {
            size_t n = bridge_meta_pack_text(output, capacity, cache->generation, (uint8_t)id,
                                             cache->text[id], cache->text_length[id]);
            if (n != 0U) {
                cache->dirty &= (uint16_t)~(1U << id);
            }
            return n;
        }
    }
    if ((cache->dirty & PLAY_STATUS_BIT) != 0U) {
        size_t n = bridge_meta_pack_play_status(output, capacity, cache->generation,
                                                cache->play_status, cache->flags,
                                                cache->song_length_ms, cache->song_position_ms);
        if (n != 0U) {
            cache->dirty &= (uint16_t)~PLAY_STATUS_BIT;
        }
        return n;
    }
    return 0U;
}
