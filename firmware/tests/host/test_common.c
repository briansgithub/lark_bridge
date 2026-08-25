#include "bridge_pcm.h"
#include "bridge_link.h"
#include "bridge_protocol.h"
#include "bridge_ring.h"
#include "bridge_session.h"

#include <limits.h>
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

static int test_byte_ring(void) {
    uint8_t storage[5];
    uint8_t input[] = {1U, 2U, 3U, 4U, 5U};
    uint8_t output[5] = {0U};
    bridge_byte_ring_t ring;
    CHECK(!bridge_byte_ring_init(&ring, storage, 1U));
    CHECK(bridge_byte_ring_init(&ring, storage, sizeof(storage)));
    CHECK(bridge_byte_ring_capacity(&ring) == 4U);
    CHECK(bridge_byte_ring_write(&ring, input, sizeof(input)) == 4U);
    CHECK(bridge_byte_ring_count(&ring) == 4U);
    CHECK(bridge_byte_ring_read(&ring, output, 2U) == 2U);
    CHECK(output[0] == 1U && output[1] == 2U);
    CHECK(bridge_byte_ring_write(&ring, &input[4], 1U) == 1U);
    CHECK(bridge_byte_ring_read(&ring, output, sizeof(output)) == 3U);
    CHECK(output[0] == 3U && output[1] == 4U && output[2] == 5U);
    CHECK(bridge_byte_ring_count(&ring) == 0U);
    return 0;
}

static int test_frame_ring(void) {
    uint8_t storage[4U * 3U];
    uint8_t first[] = {1U, 2U, 3U, 4U};
    uint8_t second[] = {5U, 6U, 7U, 8U};
    uint8_t third[] = {9U, 10U, 11U, 12U};
    uint8_t output[4] = {0U};
    bridge_frame_ring_t ring;
    CHECK(bridge_frame_ring_init(&ring, storage, 3U, 4U));
    CHECK(bridge_frame_ring_capacity(&ring) == 2U);
    CHECK(bridge_frame_ring_push(&ring, first));
    CHECK(bridge_frame_ring_push(&ring, second));
    CHECK(!bridge_frame_ring_push(&ring, third));
    CHECK(bridge_frame_ring_pop(&ring, output));
    CHECK(memcmp(output, first, sizeof(output)) == 0);
    CHECK(bridge_frame_ring_push(&ring, third));
    CHECK(bridge_frame_ring_pop(&ring, output));
    CHECK(memcmp(output, second, sizeof(output)) == 0);
    CHECK(bridge_frame_ring_pop(&ring, output));
    CHECK(memcmp(output, third, sizeof(output)) == 0);
    CHECK(!bridge_frame_ring_pop(&ring, output));
    return 0;
}

static int test_pcm(void) {
    int16_t stereo[] = {1000, 3000, INT16_MAX, INT16_MAX, INT16_MIN, INT16_MIN, -5, -6};
    int16_t mono[4] = {0};
    int16_t duplicated[8] = {0};
    int16_t first[] = {INT16_MAX, INT16_MIN, 300, -200};
    int16_t second[] = {1, -1, 100, -100};
    int16_t mixed[4] = {0};
    bridge_pcm_stereo_to_mono_s16(stereo, mono, 4U);
    CHECK(mono[0] == 2000 && mono[1] == INT16_MAX && mono[2] == INT16_MIN && mono[3] == -5);
    bridge_pcm_mono_to_stereo_s16(mono, duplicated, 4U);
    CHECK(memcmp(duplicated, (int16_t[]){2000, 2000, INT16_MAX, INT16_MAX, INT16_MIN, INT16_MIN,
                                         -5, -5}, sizeof(duplicated)) == 0);
    bridge_pcm_mix_mono_s16(first, second, mixed, 4U);
    CHECK(mixed[0] == INT16_MAX && mixed[1] == INT16_MIN && mixed[2] == 400 && mixed[3] == -300);
    CHECK(bridge_pcm_saturate_s16(40000) == INT16_MAX);
    CHECK(bridge_pcm_saturate_s16(-40000) == INT16_MIN);
    return 0;
}

static bridge_packet_t sample_packet(void) {
    bridge_packet_t packet;
    (void)memset(&packet, 0, sizeof(packet));
    packet.version = BRIDGE_PROTOCOL_VERSION;
    packet.type = BRIDGE_MESSAGE_ROUTE;
    packet.sequence = 0x1234U;
    packet.payload_length = 5U;
    packet.payload[0] = 0U;
    packet.payload[1] = 1U;
    packet.payload[2] = 0U;
    packet.payload[3] = 2U;
    packet.payload[4] = 3U;
    return packet;
}

static int feed_all(bridge_stream_decoder_t *decoder, const uint8_t *data, size_t length,
                    bridge_packet_t *decoded) {
    size_t index;
    int packets = 0;
    for (index = 0U; index < length; ++index) {
        if (bridge_stream_decoder_feed(decoder, data[index], decoded) == BRIDGE_DECODER_PACKET) {
            ++packets;
        }
    }
    return packets;
}

static int test_protocol(void) {
    bridge_packet_t packet = sample_packet();
    bridge_packet_t decoded;
    uint8_t wire[BRIDGE_PROTOCOL_MAX_ENCODED_SIZE];
    size_t wire_length = 0U;
    bridge_stream_decoder_t decoder;
    size_t index;
    size_t payload_length;
    CHECK(bridge_crc16_ccitt((const uint8_t *)"123456789", 9U) == 0x29b1U);
    CHECK(bridge_protocol_encode(&packet, wire, sizeof(wire), &wire_length) == 0);
    CHECK(wire_length > packet.payload_length && wire[wire_length - 1U] == 0U);
    bridge_stream_decoder_init(&decoder);
    CHECK(feed_all(&decoder, wire, wire_length, &decoded) == 1);
    CHECK(decoded.version == packet.version && decoded.type == packet.type &&
          decoded.sequence == packet.sequence && decoded.payload_length == packet.payload_length);
    CHECK(memcmp(decoded.payload, packet.payload, packet.payload_length) == 0);

    /* A bad frame is rejected at its delimiter, and the following valid frame survives. */
    wire[2] ^= 0x55U;
    bridge_stream_decoder_init(&decoder);
    for (index = 0U; index < wire_length; ++index) {
        (void)bridge_stream_decoder_feed(&decoder, wire[index], &decoded);
    }
    CHECK(bridge_protocol_encode(&packet, wire, sizeof(wire), &wire_length) == 0);
    CHECK(feed_all(&decoder, wire, wire_length, &decoded) == 1);

    bridge_stream_decoder_init(&decoder);
    for (index = 0U; index < BRIDGE_PROTOCOL_MAX_ENCODED_SIZE; ++index) {
        CHECK(bridge_stream_decoder_feed(&decoder, 0x11U, &decoded) == BRIDGE_DECODER_NONE);
    }
    CHECK(bridge_stream_decoder_feed(&decoder, 0U, &decoded) == BRIDGE_DECODER_ERROR);
    CHECK(feed_all(&decoder, wire, wire_length, &decoded) == 1);
    packet.payload_length = BRIDGE_PROTOCOL_MAX_PAYLOAD + 1U;
    CHECK(bridge_protocol_encode(&packet, wire, sizeof(wire), &wire_length) != 0);
    packet = sample_packet();
    packet.type = 0xffU;
    CHECK(bridge_protocol_encode(&packet, wire, sizeof(wire), &wire_length) != 0);

    /* Cover every permitted payload size with zeros and long non-zero runs. */
    for (payload_length = 0U; payload_length <= BRIDGE_PROTOCOL_MAX_PAYLOAD; ++payload_length) {
        packet = sample_packet();
        packet.type = BRIDGE_MESSAGE_STATUS;
        packet.sequence = (uint16_t)payload_length;
        packet.payload_length = (uint16_t)payload_length;
        for (index = 0U; index < payload_length; ++index) {
            packet.payload[index] = (index % 17U == 0U) ? 0U : (uint8_t)(index + 1U);
        }
        CHECK(bridge_protocol_encode(&packet, wire, sizeof(wire), &wire_length) == 0);
        bridge_stream_decoder_init(&decoder);
        CHECK(feed_all(&decoder, wire, wire_length, &decoded) == 1);
        CHECK(decoded.type == packet.type && decoded.sequence == packet.sequence &&
              decoded.payload_length == packet.payload_length);
        CHECK(memcmp(decoded.payload, packet.payload, payload_length) == 0);
    }
    return 0;
}

static int test_link_contract(void) {
    uint8_t wire[4] = {0U};
    bridge_sequence_tracker_t tracker;
    bridge_put_u16_le(wire, 0xa1b2U);
    CHECK(bridge_get_u16_le(wire) == 0xa1b2U);
    bridge_put_u32_le(wire, 0x89abcdefU);
    CHECK(bridge_get_u32_le(wire) == 0x89abcdefU);
    bridge_sequence_tracker_init(&tracker);
    CHECK(!bridge_sequence_tracker_accept(NULL, 1U));
    CHECK(bridge_sequence_tracker_accept(&tracker, 0xffffU));
    CHECK(!bridge_sequence_tracker_accept(&tracker, 0xffffU));
    CHECK(bridge_sequence_tracker_accept(&tracker, 0U));
    CHECK(bridge_sequence_tracker_accept(&tracker, 1U));
    CHECK(!bridge_sequence_tracker_accept(&tracker, 0U));
    return 0;
}

static int test_session(void) {
    bridge_session_t session;
    bridge_session_init(&session);
    CHECK(session.route == BRIDGE_ROUTE_SILENCE && !session.hfp_uplink_enabled);
    bridge_session_apply(&session, BRIDGE_SESSION_EVENT_A2DP_UP);
    CHECK(session.route == BRIDGE_ROUTE_A2DP);
    bridge_session_apply(&session, BRIDGE_SESSION_EVENT_CALL_STARTED);
    CHECK(session.route == BRIDGE_ROUTE_SILENCE && !session.hfp_uplink_enabled);
    bridge_session_apply(&session, BRIDGE_SESSION_EVENT_HFP_UP);
    CHECK(session.route == BRIDGE_ROUTE_HFP && !session.hfp_uplink_enabled);
    bridge_session_apply(&session, BRIDGE_SESSION_EVENT_LARK_PRESENT);
    CHECK(session.route == BRIDGE_ROUTE_HFP && !session.hfp_uplink_enabled);
    bridge_session_apply(&session, BRIDGE_SESSION_EVENT_AEC_READY);
    CHECK(session.route == BRIDGE_ROUTE_HFP && session.hfp_uplink_enabled);
    bridge_session_apply(&session, BRIDGE_SESSION_EVENT_AEC_LOST);
    CHECK(session.route == BRIDGE_ROUTE_HFP && !session.hfp_uplink_enabled);
    bridge_session_apply(&session, BRIDGE_SESSION_EVENT_AEC_READY);
    CHECK(session.hfp_uplink_enabled);
    bridge_session_apply(&session, BRIDGE_SESSION_EVENT_LARK_LOST);
    CHECK(session.route == BRIDGE_ROUTE_HFP && !session.hfp_uplink_enabled);
    bridge_session_apply(&session, BRIDGE_SESSION_EVENT_HFP_DOWN);
    CHECK(session.route == BRIDGE_ROUTE_SILENCE && !session.hfp_uplink_enabled);
    bridge_session_apply(&session, BRIDGE_SESSION_EVENT_CALL_ENDED);
    CHECK(session.route == BRIDGE_ROUTE_A2DP);
    bridge_session_apply(&session, BRIDGE_SESSION_EVENT_A2DP_DOWN);
    CHECK(session.route == BRIDGE_ROUTE_SILENCE);
    return 0;
}

int main(void) {
    if (test_byte_ring() != 0 || test_frame_ring() != 0 || test_pcm() != 0 ||
        test_protocol() != 0 || test_link_contract() != 0 || test_session() != 0) {
        return 1;
    }
    (void)puts("bridge common host tests passed");
    return 0;
}
