/* H2 fault-hook frames: every corruption costs the decoder exactly one error at
 * its delimiter, is never accepted, and the next valid frame decodes. */
#include "bridge_link.h"
#include "bridge_protocol.h"

#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define CHECK(condition)                                                                         \
    do {                                                                                         \
        if (!(condition)) {                                                                      \
            (void)fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #condition);        \
            return 1;                                                                            \
        }                                                                                        \
    } while (0)

typedef struct {
    unsigned packets;
    unsigned errors;
    bridge_packet_t last;
} feed_result_t;

static feed_result_t feed(bridge_stream_decoder_t *decoder, const uint8_t *bytes, size_t length) {
    feed_result_t result;
    memset(&result, 0, sizeof(result));
    for (size_t index = 0; index < length; ++index) {
        bridge_packet_t packet;
        bridge_decoder_result_t outcome = bridge_stream_decoder_feed(decoder, bytes[index], &packet);
        if (outcome == BRIDGE_DECODER_PACKET) {
            ++result.packets;
            result.last = packet;
        } else if (outcome == BRIDGE_DECODER_ERROR) {
            ++result.errors;
        }
    }
    return result;
}

static bridge_packet_t make(uint8_t type, uint16_t sequence, const uint8_t *payload, uint16_t length) {
    bridge_packet_t packet;
    memset(&packet, 0, sizeof(packet));
    packet.version = BRIDGE_PROTOCOL_VERSION;
    packet.type = type;
    packet.sequence = sequence;
    packet.payload_length = length;
    if (length != 0U) {
        memcpy(packet.payload, payload, length);
    }
    return packet;
}

static int check_kind(bridge_corruption_t kind, const bridge_packet_t *bad) {
    const uint8_t heartbeat[BRIDGE_HEARTBEAT_PAYLOAD_SIZE] = {0U, 0x07U};
    bridge_packet_t probe = make(BRIDGE_MESSAGE_HEARTBEAT, (uint16_t)(bad->sequence + 1U),
                                 heartbeat, sizeof(heartbeat));
    uint8_t corrupt[BRIDGE_PROTOCOL_MAX_CORRUPT_SIZE];
    uint8_t valid[BRIDGE_PROTOCOL_MAX_ENCODED_SIZE];
    size_t corrupt_length = 0;
    size_t valid_length = 0;
    bridge_stream_decoder_t decoder;
    feed_result_t result;

    CHECK(bridge_protocol_encode_corrupt(bad, kind, corrupt, sizeof(corrupt), &corrupt_length) == 0);
    CHECK(corrupt_length >= 3U && corrupt[corrupt_length - 1U] == 0U);
    CHECK(bridge_protocol_encode(&probe, valid, sizeof(valid), &valid_length) == 0);

    /* Mid-stream: a valid frame, the corruption, then the probe. */
    bridge_stream_decoder_init(&decoder);
    result = feed(&decoder, valid, valid_length);
    CHECK(result.packets == 1U && result.errors == 0U);
    result = feed(&decoder, corrupt, corrupt_length);
    CHECK(result.packets == 0U && result.errors == 1U);
    result = feed(&decoder, valid, valid_length);
    CHECK(result.packets == 1U && result.errors == 0U);
    CHECK(result.last.type == BRIDGE_MESSAGE_HEARTBEAT && result.last.sequence == probe.sequence);

    /* From a fresh decoder too, the way a receiver sees it after boot. */
    bridge_stream_decoder_init(&decoder);
    result = feed(&decoder, corrupt, corrupt_length);
    CHECK(result.packets == 0U && result.errors == 1U);
    return 0;
}

static int test_every_kind_for_route_and_avrcp(void) {
    const uint8_t route_hfp[BRIDGE_ROUTE_PAYLOAD_SIZE] = {2U};
    const uint8_t avrcp_tap[BRIDGE_AVRCP_PAYLOAD_SIZE] = {0x44U, 2U};
    const bridge_corruption_t kinds[] = {BRIDGE_CORRUPT_CRC, BRIDGE_CORRUPT_COBS,
                                         BRIDGE_CORRUPT_VERSION, BRIDGE_CORRUPT_OVERLONG};
    for (size_t index = 0; index < sizeof(kinds) / sizeof(kinds[0]); ++index) {
        bridge_packet_t route = make(BRIDGE_MESSAGE_ROUTE, (uint16_t)(0x1230U + index), route_hfp, 1U);
        bridge_packet_t avrcp = make(BRIDGE_MESSAGE_AVRCP_COMMAND, (uint16_t)(0xfffeU + index),
                                     avrcp_tap, 2U);
        CHECK(check_kind(kinds[index], &route) == 0);
        CHECK(check_kind(kinds[index], &avrcp) == 0);
    }
    return 0;
}

static int test_crc_sweep(void) {
    /* Any sequence: the flipped CRC bit must never escape through COBS. */
    const uint8_t route_hfp[BRIDGE_ROUTE_PAYLOAD_SIZE] = {2U};
    for (uint32_t sequence = 0U; sequence <= 0xffffU; sequence += 97U) {
        bridge_packet_t route = make(BRIDGE_MESSAGE_ROUTE, (uint16_t)sequence, route_hfp, 1U);
        CHECK(check_kind(BRIDGE_CORRUPT_CRC, &route) == 0);
        CHECK(check_kind(BRIDGE_CORRUPT_VERSION, &route) == 0);
    }
    return 0;
}

static int test_bad_arguments(void) {
    const uint8_t payload[1] = {0U};
    bridge_packet_t packet = make(BRIDGE_MESSAGE_ROUTE, 1U, payload, 1U);
    uint8_t small[8];
    uint8_t big[BRIDGE_PROTOCOL_MAX_CORRUPT_SIZE];
    size_t length = 0;
    CHECK(bridge_protocol_encode_corrupt(NULL, BRIDGE_CORRUPT_CRC, big, sizeof(big), &length) != 0);
    CHECK(bridge_protocol_encode_corrupt(&packet, (bridge_corruption_t)9, big, sizeof(big),
                                         &length) != 0);
    CHECK(bridge_protocol_encode_corrupt(&packet, BRIDGE_CORRUPT_OVERLONG, small, sizeof(small),
                                         &length) != 0);
    CHECK(bridge_protocol_encode_corrupt(&packet, BRIDGE_CORRUPT_CRC, small, 4U, &length) != 0);
    return 0;
}

int main(void) {
    int failures = test_every_kind_for_route_and_avrcp() + test_crc_sweep() + test_bad_arguments();
    if (failures == 0) {
        (void)printf("bridge_fault_frame_tests: PASS\n");
    }
    return failures == 0 ? 0 : 1;
}
