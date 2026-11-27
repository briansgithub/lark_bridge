#include "bridge_link.h"
#include "bridge_meta.h"
#include "bridge_protocol.h"

#include <stdio.h>
#include <string.h>

#define CHECK(cond)                                                                                \
    do {                                                                                           \
        if (!(cond)) {                                                                             \
            fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);                        \
            return 1;                                                                              \
        }                                                                                          \
    } while (0)

static int test_utf8(void)
{
    /* "é" = C3 A9, "€" = E2 82 AC, "😀" = F0 9F 98 80 */
    const char *ascii = "abcdef";
    CHECK(bridge_utf8_truncate(ascii, 6U, 10U) == 6U);
    CHECK(bridge_utf8_truncate(ascii, 6U, 3U) == 3U);
    const char two[] = "a\xc3\xa9";
    CHECK(bridge_utf8_truncate(two, 3U, 2U) == 1U); /* never split C3|A9 */
    CHECK(bridge_utf8_truncate(two, 3U, 3U) == 3U);
    const char three[] = "\xe2\x82\xac\xe2\x82\xac";
    CHECK(bridge_utf8_truncate(three, 6U, 5U) == 3U);
    CHECK(bridge_utf8_truncate(three, 6U, 4U) == 3U);
    CHECK(bridge_utf8_truncate(three, 6U, 3U) == 3U);
    CHECK(bridge_utf8_truncate(three, 6U, 2U) == 0U);
    const char four[] = "x\xf0\x9f\x98\x80y";
    CHECK(bridge_utf8_truncate(four, 6U, 5U) == 5U);
    CHECK(bridge_utf8_truncate(four, 6U, 4U) == 1U);
    CHECK(bridge_utf8_truncate(four, 6U, 2U) == 1U);
    CHECK(bridge_utf8_truncate(NULL, 4U, 2U) == 0U);
    return 0;
}

static int test_records(void)
{
    uint8_t buffer[BRIDGE_PROTOCOL_MAX_PAYLOAD];
    bridge_meta_record_t record;

    size_t n = bridge_meta_pack_play_status(buffer, sizeof(buffer), 7U, BRIDGE_META_STATUS_PLAYING,
                                            BRIDGE_META_FLAG_LENGTH_VALID |
                                                BRIDGE_META_FLAG_POSITION_VALID,
                                            215000U, 61234U);
    CHECK(n == BRIDGE_META_PLAY_STATUS_SIZE);
    CHECK(buffer[0] == BRIDGE_META_KIND_PLAY_STATUS && buffer[1] == 7U && buffer[2] == 1U);
    CHECK(bridge_get_u32_le(&buffer[4]) == 215000U);
    CHECK(bridge_meta_unpack(buffer, n, &record));
    CHECK(record.kind == BRIDGE_META_KIND_PLAY_STATUS && record.generation == 7U &&
          record.play_status == BRIDGE_META_STATUS_PLAYING && record.song_length_ms == 215000U &&
          record.song_position_ms == 61234U && record.flags == 3U);
    CHECK(!bridge_meta_unpack(buffer, n - 1U, &record));
    CHECK(bridge_meta_pack_play_status(buffer, 11U, 0, 0, 0, 0, 0) == 0U);

    const char *title = "LarkTest Song One";
    n = bridge_meta_pack_text(buffer, sizeof(buffer), 3U, BRIDGE_META_ATTR_TITLE, title,
                              strlen(title));
    CHECK(n == BRIDGE_META_TEXT_HEADER_SIZE + strlen(title));
    CHECK(bridge_meta_unpack(buffer, n, &record));
    CHECK(record.kind == BRIDGE_META_KIND_TEXT_ATTR && record.attr_id == 1U &&
          record.generation == 3U && strcmp(record.text, title) == 0 &&
          record.text_length == strlen(title));
    /* Wrong length, bad attr ids, embedded NUL are rejected. */
    CHECK(!bridge_meta_unpack(buffer, n + 1U, &record));
    buffer[2] = 0U;
    CHECK(!bridge_meta_unpack(buffer, n, &record));
    buffer[2] = 8U;
    CHECK(!bridge_meta_unpack(buffer, n, &record));
    buffer[2] = 1U;
    buffer[6] = '\0';
    CHECK(!bridge_meta_unpack(buffer, n, &record));
    CHECK(bridge_meta_pack_text(buffer, sizeof(buffer), 0, 0U, "x", 1U) == 0U);
    CHECK(bridge_meta_pack_text(buffer, sizeof(buffer), 0, 8U, "x", 1U) == 0U);

    /* Empty attribute is legal (cleared field). */
    n = bridge_meta_pack_text(buffer, sizeof(buffer), 1U, BRIDGE_META_ATTR_ALBUM, "", 0U);
    CHECK(n == BRIDGE_META_TEXT_HEADER_SIZE && bridge_meta_unpack(buffer, n, &record) &&
          record.text_length == 0U && record.text[0] == '\0');

    /* Long multi-byte string: truncated to <= 96 bytes on a boundary, fits the frame. */
    char long_text[200];
    size_t used = 0U;
    while (used + 3U <= sizeof(long_text) - 1U) {
        memcpy(&long_text[used], "\xe2\x82\xac", 3U);
        used += 3U;
    }
    long_text[used] = '\0';
    n = bridge_meta_pack_text(buffer, sizeof(buffer), 9U, BRIDGE_META_ATTR_ARTIST, long_text,
                              used);
    CHECK(n == BRIDGE_META_TEXT_HEADER_SIZE + 96U); /* 32 whole euro signs */
    CHECK(n <= BRIDGE_PROTOCOL_MAX_PAYLOAD);
    CHECK(bridge_meta_unpack(buffer, n, &record) && record.text_length == 96U);
    char shifted[200];
    shifted[0] = 'A';
    memcpy(&shifted[1], "\xe2\x82\xac", 3U);
    for (size_t k = 4U; k + 3U < sizeof(shifted); k += 3U) {
        memcpy(&shifted[k], "\xe2\x82\xac", 3U);
    }
    n = bridge_meta_pack_text(buffer, sizeof(buffer), 9U, BRIDGE_META_ATTR_ARTIST, shifted, 190U);
    CHECK(n == BRIDGE_META_TEXT_HEADER_SIZE + 94U); /* 'A' + 31 euro signs */
    /* Embedded NUL ends the string at the sender. */
    n = bridge_meta_pack_text(buffer, sizeof(buffer), 1U, BRIDGE_META_ATTR_TITLE, "ab\0cd", 5U);
    CHECK(n == BRIDGE_META_TEXT_HEADER_SIZE + 2U && bridge_meta_unpack(buffer, n, &record));
    /* Capacity checks. */
    CHECK(bridge_meta_pack_text(buffer, 5U, 1U, BRIDGE_META_ATTR_TITLE, "abc", 3U) == 0U);

    n = bridge_meta_pack_clear(buffer, sizeof(buffer), 4U);
    CHECK(n == 2U && bridge_meta_unpack(buffer, n, &record) &&
          record.kind == BRIDGE_META_KIND_CLEAR && record.generation == 4U);
    CHECK(!bridge_meta_unpack(buffer, 3U, &record));
    buffer[0] = 0x09U;
    CHECK(!bridge_meta_unpack(buffer, 2U, &record));
    CHECK(!bridge_meta_unpack(buffer, 1U, &record));
    return 0;
}

static int test_wire(void)
{
    /* A max-size metadata record survives the real COBS/CRC framing. */
    bridge_packet_t packet;
    bridge_packet_t decoded;
    uint8_t encoded[BRIDGE_PROTOCOL_MAX_ENCODED_SIZE];
    size_t encoded_length = 0U;
    char text[BRIDGE_META_TEXT_MAX];
    memset(text, 'z', sizeof(text));

    memset(&packet, 0, sizeof(packet));
    packet.version = BRIDGE_PROTOCOL_VERSION;
    packet.type = BRIDGE_MESSAGE_MEDIA_META;
    packet.sequence = 42U;
    packet.payload_length = (uint16_t)bridge_meta_pack_text(
        packet.payload, sizeof(packet.payload), 2U, BRIDGE_META_ATTR_TITLE, text, sizeof(text));
    CHECK(packet.payload_length == BRIDGE_META_MAX_RECORD);
    CHECK(bridge_protocol_encode(&packet, encoded, sizeof(encoded), &encoded_length) == 0);
    bridge_stream_decoder_t decoder;
    bridge_stream_decoder_init(&decoder);
    bridge_decoder_result_t result = BRIDGE_DECODER_NONE;
    for (size_t i = 0U; i < encoded_length; ++i) {
        result = bridge_stream_decoder_feed(&decoder, encoded[i], &decoded);
    }
    CHECK(result == BRIDGE_DECODER_PACKET);
    CHECK(decoded.type == BRIDGE_MESSAGE_MEDIA_META && decoded.payload_length == packet.payload_length);
    bridge_meta_record_t record;
    CHECK(bridge_meta_unpack(decoded.payload, decoded.payload_length, &record));
    CHECK(record.text_length == BRIDGE_META_TEXT_MAX);

    /* The sequence table has a slot for the new type. */
    bridge_tx_sequences_t sequences;
    memset(&sequences, 0, sizeof(sequences));
    CHECK(bridge_tx_next(&sequences, BRIDGE_MESSAGE_MEDIA_META) == 0U);
    CHECK(bridge_tx_next(&sequences, BRIDGE_MESSAGE_MEDIA_META) == 1U);
    CHECK(bridge_tx_next(&sequences, BRIDGE_MESSAGE_ACK) == 0U);
    return 0;
}

int main(void)
{
    if (test_utf8() || test_records() || test_wire()) {
        return 1;
    }
    puts("bridge meta tests passed");
    return 0;
}
