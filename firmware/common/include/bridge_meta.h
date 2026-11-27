#ifndef BRIDGE_META_H
#define BRIDGE_META_H

/* BRIDGE_MESSAGE_MEDIA_META payloads (car UX feature 3): best-effort AVRCP 1.3
 * now-playing metadata from the S31 (AVRCP CT toward the phone) to the output
 * board (AVRCP TG toward the car). One sub-record per packet, never ACKed, so
 * the audio path never waits on metadata. All sub-records fit well inside
 * BRIDGE_PROTOCOL_MAX_PAYLOAD (128), which stays unchanged.
 *
 *   PLAY_STATUS (12 B): kind, gen, play_status, flags, length_ms u32 LE, position_ms u32 LE
 *   TEXT_ATTR (4+n B):  kind, gen, attr_id, n, utf8[n]   (n <= 96, cut on a codepoint boundary)
 *   CLEAR (2 B):        kind, gen
 *
 * gen is the S31 track generation (increments on each track change, wraps). */

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define BRIDGE_META_KIND_PLAY_STATUS 0x01U
#define BRIDGE_META_KIND_TEXT_ATTR 0x02U
#define BRIDGE_META_KIND_CLEAR 0x03U

#define BRIDGE_META_PLAY_STATUS_SIZE 12U
#define BRIDGE_META_TEXT_HEADER_SIZE 4U
#define BRIDGE_META_TEXT_MAX 96U
#define BRIDGE_META_CLEAR_SIZE 2U
#define BRIDGE_META_MAX_RECORD (BRIDGE_META_TEXT_HEADER_SIZE + BRIDGE_META_TEXT_MAX)

/* AVRCP 1.3 media attribute IDs (same numbering as the spec / ESP-IDF). */
#define BRIDGE_META_ATTR_TITLE 1U
#define BRIDGE_META_ATTR_ARTIST 2U
#define BRIDGE_META_ATTR_ALBUM 3U
#define BRIDGE_META_ATTR_TRACK_NUM 4U
#define BRIDGE_META_ATTR_NUM_TRACKS 5U
#define BRIDGE_META_ATTR_GENRE 6U
#define BRIDGE_META_ATTR_PLAYING_TIME 7U
#define BRIDGE_META_ATTR_COUNT 7U

/* AVRCP play status values. */
#define BRIDGE_META_STATUS_STOPPED 0x00U
#define BRIDGE_META_STATUS_PLAYING 0x01U
#define BRIDGE_META_STATUS_PAUSED 0x02U
#define BRIDGE_META_STATUS_FWD_SEEK 0x03U
#define BRIDGE_META_STATUS_REV_SEEK 0x04U
#define BRIDGE_META_STATUS_ERROR 0xffU

#define BRIDGE_META_FLAG_LENGTH_VALID 0x01U
#define BRIDGE_META_FLAG_POSITION_VALID 0x02U

typedef struct {
    uint8_t kind;
    uint8_t generation;
    /* PLAY_STATUS */
    uint8_t play_status;
    uint8_t flags;
    uint32_t song_length_ms;
    uint32_t song_position_ms;
    /* TEXT_ATTR */
    uint8_t attr_id;
    uint8_t text_length;
    char text[BRIDGE_META_TEXT_MAX + 1U]; /* NUL-terminated copy */
} bridge_meta_record_t;

/* Longest prefix of text[0..length) that is at most max bytes and does not end
 * inside a multi-byte UTF-8 sequence. */
size_t bridge_utf8_truncate(const char *text, size_t length, size_t max);

/* Each pack returns the record size, or 0 if the arguments or capacity are bad. */
size_t bridge_meta_pack_play_status(uint8_t *output, size_t capacity, uint8_t generation,
                                    uint8_t play_status, uint8_t flags, uint32_t length_ms,
                                    uint32_t position_ms);
size_t bridge_meta_pack_text(uint8_t *output, size_t capacity, uint8_t generation,
                             uint8_t attr_id, const char *text, size_t length);
size_t bridge_meta_pack_clear(uint8_t *output, size_t capacity, uint8_t generation);

/* Strict decode: exact sizes, known kind, attr_id 1..7, no embedded NUL. */
bool bridge_meta_unpack(const uint8_t *input, size_t length, bridge_meta_record_t *record);

#endif
