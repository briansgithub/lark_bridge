#include "media_meta_cache.h"

#include <stdio.h>
#include <string.h>

#define CHECK(cond)                                                                                \
    do {                                                                                           \
        if (!(cond)) {                                                                             \
            fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);                        \
            return 1;                                                                              \
        }                                                                                          \
    } while (0)

static size_t drain(s31_meta_cache_t *c, bridge_meta_record_t *records, size_t max)
{
    uint8_t buffer[128];
    size_t count = 0U;
    size_t n;
    while ((n = s31_meta_cache_next_record(c, buffer, sizeof(buffer))) != 0U && count < max) {
        if (!bridge_meta_unpack(buffer, n, &records[count])) {
            return 999U;
        }
        ++count;
    }
    return count;
}

int main(void)
{
    s31_meta_cache_t c;
    bridge_meta_record_t r[16];
    uint8_t buffer[128];

    s31_meta_cache_init(&c);
    CHECK(s31_meta_cache_next_record(&c, buffer, sizeof(buffer)) == 0U);

    /* Track 1 arrives. */
    s31_meta_cache_track_changed(&c);
    CHECK(c.generation == 1U);
    CHECK(s31_meta_cache_set_text(&c, BRIDGE_META_ATTR_TITLE, (const uint8_t *)"LarkTest Song One", 17U));
    CHECK(s31_meta_cache_set_text(&c, BRIDGE_META_ATTR_ARTIST, (const uint8_t *)"Test Artist A", 13U));
    CHECK(s31_meta_cache_set_text(&c, BRIDGE_META_ATTR_ALBUM, (const uint8_t *)"Bench Album", 11U));
    CHECK(!s31_meta_cache_set_text(&c, 0U, (const uint8_t *)"x", 1U));
    CHECK(!s31_meta_cache_set_text(&c, 8U, (const uint8_t *)"x", 1U));
    s31_meta_cache_set_play_status(&c, BRIDGE_META_STATUS_PLAYING, true, 180000U, true, 1000U);
    CHECK(drain(&c, r, 16U) == 4U);
    CHECK(r[0].attr_id == 1U && strcmp(r[0].text, "LarkTest Song One") == 0 && r[0].generation == 1U);
    CHECK(r[1].attr_id == 2U && r[2].attr_id == 3U);
    CHECK(r[3].kind == BRIDGE_META_KIND_PLAY_STATUS && r[3].song_length_ms == 180000U &&
          r[3].flags == (BRIDGE_META_FLAG_LENGTH_VALID | BRIDGE_META_FLAG_POSITION_VALID));

    /* Unchanged values do not resend. */
    CHECK(s31_meta_cache_set_text(&c, BRIDGE_META_ATTR_TITLE, (const uint8_t *)"LarkTest Song One", 17U));
    s31_meta_cache_set_play_status(&c, BRIDGE_META_STATUS_PLAYING, true, 180000U, true, 1000U);
    CHECK(drain(&c, r, 16U) == 0U);

    /* Pause + position updates keep the length. */
    s31_meta_cache_set_play_status(&c, BRIDGE_META_STATUS_PAUSED, true, 180000U, true, 5000U);
    s31_meta_cache_set_position(&c, 6000U);
    CHECK(drain(&c, r, 16U) == 1U); /* coalesced into one record */
    CHECK(r[0].play_status == BRIDGE_META_STATUS_PAUSED && r[0].song_position_ms == 6000U &&
          r[0].song_length_ms == 180000U);

    /* New output session: everything known is resent. */
    s31_meta_cache_mark_all_dirty(&c);
    CHECK(drain(&c, r, 16U) == 4U);

    /* Track change: generation bumps, old text is not resent under the new gen. */
    s31_meta_cache_track_changed(&c);
    CHECK(c.generation == 2U && drain(&c, r, 16U) == 0U);
    CHECK(s31_meta_cache_set_text(&c, BRIDGE_META_ATTR_TITLE, (const uint8_t *)"LarkTest Song Two", 17U));
    CHECK(drain(&c, r, 16U) == 1U && r[0].generation == 2U);
    s31_meta_cache_mark_all_dirty(&c);
    CHECK(drain(&c, r, 16U) == 2U); /* title + play status only */

    /* Non-terminated text with an embedded NUL and an over-long UTF-8 value. */
    const uint8_t nul_text[] = {'a', 'b', 0, 'c'};
    CHECK(s31_meta_cache_set_text(&c, BRIDGE_META_ATTR_GENRE, nul_text, sizeof(nul_text)));
    CHECK(c.text_length[BRIDGE_META_ATTR_GENRE] == 2U);
    uint8_t long_text[150];
    for (size_t i = 0U; i + 1U < sizeof(long_text); i += 2U) {
        long_text[i] = 0xc3U;
        long_text[i + 1U] = 0xa9U;
    }
    CHECK(s31_meta_cache_set_text(&c, BRIDGE_META_ATTR_ALBUM, long_text, sizeof(long_text)));
    CHECK(c.text_length[BRIDGE_META_ATTR_ALBUM] == 96U);
    long_text[0] = 'x'; /* now 'x' + 0xa9 + pairs: odd alignment */
    CHECK(s31_meta_cache_set_text(&c, BRIDGE_META_ATTR_ALBUM, long_text, sizeof(long_text)));
    CHECK(c.text_length[BRIDGE_META_ATTR_ALBUM] == 96U || c.text_length[BRIDGE_META_ATTR_ALBUM] == 95U);
    (void)drain(&c, r, 16U);

    /* Phone disconnect: one CLEAR, nothing else, even with prior dirt. */
    s31_meta_cache_set_text(&c, BRIDGE_META_ATTR_ARTIST, (const uint8_t *)"Test Artist B", 13U);
    s31_meta_cache_clear(&c);
    CHECK(drain(&c, r, 16U) == 1U && r[0].kind == BRIDGE_META_KIND_CLEAR);
    s31_meta_cache_mark_all_dirty(&c);
    CHECK(drain(&c, r, 16U) == 0U);

    /* Capacity too small keeps the record pending. */
    s31_meta_cache_set_text(&c, BRIDGE_META_ATTR_TITLE, (const uint8_t *)"abc", 3U);
    CHECK(s31_meta_cache_next_record(&c, buffer, 4U) == 0U);
    CHECK(s31_meta_cache_next_record(&c, buffer, sizeof(buffer)) == 7U);

    /* Position before any status. */
    s31_meta_cache_init(&c);
    s31_meta_cache_set_position(&c, 1234U);
    CHECK(drain(&c, r, 16U) == 1U && r[0].song_position_ms == 1234U &&
          r[0].flags == BRIDGE_META_FLAG_POSITION_VALID);
    puts("s31 media meta cache tests passed");
    return 0;
}
