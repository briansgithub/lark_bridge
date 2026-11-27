#pragma once

/* S31 now-playing cache (car UX feature 3B). Pure C, host tested. Bluedroid
 * AVRCP CT callbacks write it; link_task drains dirty fields as
 * BRIDGE_MESSAGE_MEDIA_META records so the callbacks never block on UART.
 * The caller serializes access (one lock around every call). */

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "bridge_meta.h"

typedef struct {
    uint8_t generation;
    bool clear_pending;
    /* bit n (1..7) = text attribute n dirty; bit 0 = play status dirty */
    uint16_t dirty;
    uint8_t text_length[BRIDGE_META_ATTR_COUNT + 1U];
    char text[BRIDGE_META_ATTR_COUNT + 1U][BRIDGE_META_TEXT_MAX + 1U];
    bool have_play_status;
    uint8_t play_status;
    uint8_t flags;
    uint32_t song_length_ms;
    uint32_t song_position_ms;
} s31_meta_cache_t;

void s31_meta_cache_init(s31_meta_cache_t *cache);
/* Phone/AVRCP gone: forget everything and queue one CLEAR. */
void s31_meta_cache_clear(s31_meta_cache_t *cache);
/* New track: next generation, text forgotten (the new values follow). */
void s31_meta_cache_track_changed(s31_meta_cache_t *cache);
/* Stores a (possibly non-terminated) attribute; truncates on a UTF-8 boundary.
 * Marks dirty only when the value changed. Returns false for a bad attr_id. */
bool s31_meta_cache_set_text(s31_meta_cache_t *cache, uint8_t attr_id, const uint8_t *text,
                             size_t length);
void s31_meta_cache_set_play_status(s31_meta_cache_t *cache, uint8_t play_status,
                                    bool length_valid, uint32_t length_ms, bool position_valid,
                                    uint32_t position_ms);
/* Position-only update (PLAY_POS_CHANGED): keeps status and length. */
void s31_meta_cache_set_position(s31_meta_cache_t *cache, uint32_t position_ms);
/* New output session (its HELLO): resend everything known. */
void s31_meta_cache_mark_all_dirty(s31_meta_cache_t *cache);
/* Pops one pending record into output (CLEAR first, then text attributes in
 * id order, then play status). Returns its size, 0 when nothing is pending. */
size_t s31_meta_cache_next_record(s31_meta_cache_t *cache, uint8_t *output, size_t capacity);
