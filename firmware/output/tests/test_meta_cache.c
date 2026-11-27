#include "output_meta_cache.h"

#include <stdio.h>
#include <string.h>

#define CHECK(cond)                                                                                \
    do {                                                                                           \
        if (!(cond)) {                                                                             \
            fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);                        \
            return 1;                                                                              \
        }                                                                                          \
    } while (0)

static bridge_meta_record_t text(uint8_t gen, uint8_t id, const char *value)
{
    bridge_meta_record_t r;
    memset(&r, 0, sizeof(r));
    r.kind = BRIDGE_META_KIND_TEXT_ATTR;
    r.generation = gen;
    r.attr_id = id;
    r.text_length = (uint8_t)strlen(value);
    memcpy(r.text, value, r.text_length);
    return r;
}

static bridge_meta_record_t status(uint8_t gen, uint8_t play, uint32_t len, uint32_t pos)
{
    bridge_meta_record_t r;
    memset(&r, 0, sizeof(r));
    r.kind = BRIDGE_META_KIND_PLAY_STATUS;
    r.generation = gen;
    r.play_status = play;
    r.flags = BRIDGE_META_FLAG_LENGTH_VALID | BRIDGE_META_FLAG_POSITION_VALID;
    r.song_length_ms = len;
    r.song_position_ms = pos;
    return r;
}

int main(void)
{
    output_meta_cache_t c;
    uint8_t out[128];
    bridge_meta_record_t r;

    output_meta_cache_init(&c);
    CHECK(c.track_uid == OUTPUT_META_NO_TRACK);
    CHECK(output_meta_cache_position(&c, 0U) == UINT32_MAX);
    CHECK(output_meta_cache_length(&c) == UINT32_MAX);
    CHECK(output_meta_cache_clear(&c) == 0U); /* nothing known: no notification */

    /* Track 1 burst: debounce holds the TRACK_CHANGED until 150 ms after the last field. */
    r = text(1U, BRIDGE_META_ATTR_TITLE, "LarkTest Song One");
    CHECK(output_meta_cache_apply(&c, &r, 1000U) == 0U);
    uint64_t uid1 = c.track_uid;
    CHECK(uid1 != OUTPUT_META_NO_TRACK);
    CHECK(output_meta_cache_poll(&c, 1100U) == 0U);
    r = text(1U, BRIDGE_META_ATTR_ARTIST, "Test Artist A");
    CHECK(output_meta_cache_apply(&c, &r, 1120U) == 0U);
    r = text(1U, BRIDGE_META_ATTR_ALBUM, "Bench Album");
    CHECK(output_meta_cache_apply(&c, &r, 1140U) == 0U);
    CHECK(output_meta_cache_poll(&c, 1260U) == 0U);
    CHECK(output_meta_cache_poll(&c, 1290U) == OUTPUT_META_NOTIFY_TRACK);
    CHECK(output_meta_cache_poll(&c, 1400U) == 0U); /* once */
    CHECK(output_meta_cache_attr(&c, BRIDGE_META_ATTR_TITLE, out, sizeof(out)) == 17U &&
          memcmp(out, "LarkTest Song One", 17U) == 0);
    CHECK(output_meta_cache_attr(&c, BRIDGE_META_ATTR_GENRE, out, sizeof(out)) == 0U);
    CHECK(output_meta_cache_attr(&c, 0U, out, sizeof(out)) == 0U);
    CHECK(output_meta_cache_attr(&c, BRIDGE_META_ATTR_TITLE, out, 4U) == 4U);

    /* Play status: immediate notify on change, interpolated position, clamped. */
    r = status(1U, BRIDGE_META_STATUS_PLAYING, 180000U, 10000U);
    unsigned int n = output_meta_cache_apply(&c, &r, 2000U);
    CHECK((n & OUTPUT_META_NOTIFY_STATUS) != 0U && (n & OUTPUT_META_NOTIFY_POSITION) != 0U);
    CHECK(c.track_uid == uid1); /* same generation: same track */
    CHECK(output_meta_cache_position(&c, 2000U) == 10000U);
    CHECK(output_meta_cache_position(&c, 4500U) == 12500U);
    CHECK(output_meta_cache_position(&c, 2000U + 500000U) == 180000U);
    CHECK(output_meta_cache_length(&c) == 180000U);
    /* Same status re-reported on schedule: no STATUS notify. */
    r = status(1U, BRIDGE_META_STATUS_PLAYING, 180000U, 12500U);
    CHECK((output_meta_cache_apply(&c, &r, 4500U) & OUTPUT_META_NOTIFY_STATUS) == 0U);
    /* Pause freezes the position. */
    r = status(1U, BRIDGE_META_STATUS_PAUSED, 180000U, 13000U);
    CHECK((output_meta_cache_apply(&c, &r, 5000U) & OUTPUT_META_NOTIFY_STATUS) != 0U);
    CHECK(output_meta_cache_position(&c, 9000U) == 13000U);

    /* Track 2: new generation resets old text so the car never sees a half update. */
    r = text(2U, BRIDGE_META_ATTR_TITLE, "LarkTest Song Two");
    CHECK(output_meta_cache_apply(&c, &r, 10000U) == 0U);
    CHECK(c.track_uid != uid1 && c.track_uid != OUTPUT_META_NO_TRACK);
    CHECK(output_meta_cache_attr(&c, BRIDGE_META_ATTR_ARTIST, out, sizeof(out)) == 0U);
    r = text(2U, BRIDGE_META_ATTR_ARTIST, "Test Artist B");
    (void)output_meta_cache_apply(&c, &r, 10050U);
    CHECK(output_meta_cache_poll(&c, 10199U) == 0U);
    CHECK(output_meta_cache_poll(&c, 10200U) == OUTPUT_META_NOTIFY_TRACK);
    CHECK(output_meta_cache_attr(&c, BRIDGE_META_ATTR_ARTIST, out, sizeof(out)) == 13U);
    CHECK(output_meta_cache_attr(&c, BRIDGE_META_ATTR_ALBUM, out, sizeof(out)) == 0U);

    /* Clear (phone gone): TRACK (no track) + STATUS (stopped) notifications. */
    n = output_meta_cache_clear(&c);
    CHECK(n == (OUTPUT_META_NOTIFY_TRACK | OUTPUT_META_NOTIFY_STATUS));
    CHECK(c.track_uid == OUTPUT_META_NO_TRACK && c.play_status == BRIDGE_META_STATUS_STOPPED);
    CHECK(output_meta_cache_attr(&c, BRIDGE_META_ATTR_TITLE, out, sizeof(out)) == 0U);
    CHECK(output_meta_cache_clear(&c) == 0U);
    /* CLEAR record goes through apply as well. */
    r = text(5U, BRIDGE_META_ATTR_TITLE, "x");
    (void)output_meta_cache_apply(&c, &r, 20000U);
    memset(&r, 0, sizeof(r));
    r.kind = BRIDGE_META_KIND_CLEAR;
    CHECK((output_meta_cache_apply(&c, &r, 20010U) & OUTPUT_META_NOTIFY_TRACK) != 0U);
    CHECK(!c.track_change_pending);
    /* A uid after clear differs from every earlier one. */
    r = text(5U, BRIDGE_META_ATTR_TITLE, "again");
    (void)output_meta_cache_apply(&c, &r, 30000U);
    CHECK(c.track_uid != uid1 && c.track_uid != OUTPUT_META_NO_TRACK);

    /* Attribute selection for GetElementAttributes. */
    uint8_t ids[8];
    CHECK(output_meta_cache_select_attrs(NULL, 0U, ids, 8U) == 7U && ids[0] == 1U && ids[6] == 7U);
    const uint32_t req[] = {2U, 1U, 9U, 2U, 0U, 3U};
    CHECK(output_meta_cache_select_attrs(req, 6U, ids, 8U) == 3U && ids[0] == 2U && ids[1] == 1U &&
          ids[2] == 3U);
    CHECK(output_meta_cache_select_attrs(req, 6U, ids, 2U) == 2U);

    /* Position without length, wrap-safe interpolation. */
    output_meta_cache_init(&c);
    r = status(9U, BRIDGE_META_STATUS_PLAYING, 0U, 1000U);
    r.flags = BRIDGE_META_FLAG_POSITION_VALID;
    (void)output_meta_cache_apply(&c, &r, UINT32_MAX - 100U);
    CHECK(output_meta_cache_length(&c) == UINT32_MAX);
    CHECK(output_meta_cache_position(&c, 899U) == 2000U);
    puts("output meta cache tests passed");
    return 0;
}
