#include "bridge_pcm.h"
#include "bridge_link.h"
#include "bridge_protocol.h"
#include "bridge_rate.h"
#include "bridge_resample.h"

#include <math.h>
#include "bridge_ring.h"
#include "bridge_route_fence.h"
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

_Static_assert(BRIDGE_HELLO_SESSION_OFFSET + sizeof(uint32_t) == BRIDGE_HELLO_PAYLOAD_SIZE,
               "HELLO session nonce must fit exactly");
_Static_assert(BRIDGE_STATUS_RELIABLE_FAILURES_OFFSET + sizeof(uint32_t) ==
                   BRIDGE_STATUS_PAYLOAD_SIZE,
               "STATUS telemetry must fit exactly");

static int test_byte_ring(void) {
    uint8_t storage[5];
    uint8_t input[] = {1U, 2U, 3U, 4U, 5U};
    uint8_t output[5] = {0U};
    bridge_byte_ring_t ring;
    CHECK(!bridge_byte_ring_init(&ring, storage, 1U));
    CHECK(bridge_byte_ring_init(&ring, storage, sizeof(storage)));
    CHECK(bridge_byte_ring_capacity(&ring) == 4U);
    CHECK(bridge_byte_ring_space(&ring) == 4U);
    CHECK(bridge_byte_ring_write(&ring, input, sizeof(input)) == 4U);
    CHECK(bridge_byte_ring_count(&ring) == 4U);
    CHECK(bridge_byte_ring_space(&ring) == 0U);
    CHECK(bridge_byte_ring_read(&ring, output, 2U) == 2U);
    CHECK(output[0] == 1U && output[1] == 2U);
    CHECK(bridge_byte_ring_space(&ring) == 2U);
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

static uint32_t deterministic_random(uint32_t *state) {
    uint32_t value = *state;
    value ^= value << 13U;
    value ^= value >> 17U;
    value ^= value << 5U;
    *state = value;
    return value;
}

static int test_protocol_random_stream(void) {
    bridge_packet_t packet;
    bridge_packet_t decoded;
    bridge_stream_decoder_t decoder;
    uint8_t wire[BRIDGE_PROTOCOL_MAX_ENCODED_SIZE];
    size_t wire_length;
    uint32_t random_state = UINT32_C(0x6c61726b);

    bridge_stream_decoder_init(&decoder);
    for (unsigned int trial = 0U; trial < 2000U; ++trial) {
        (void)memset(&packet, 0, sizeof(packet));
        packet.version = BRIDGE_PROTOCOL_VERSION;
        packet.type = (uint8_t)(BRIDGE_MESSAGE_HELLO +
                                deterministic_random(&random_state) % 7U);
        packet.sequence = (uint16_t)deterministic_random(&random_state);
        packet.payload_length =
            (uint16_t)(deterministic_random(&random_state) %
                       (BRIDGE_PROTOCOL_MAX_PAYLOAD + 1U));
        for (size_t index = 0U; index < packet.payload_length; ++index) {
            packet.payload[index] = (uint8_t)deterministic_random(&random_state);
        }
        CHECK(bridge_protocol_encode(&packet, wire, sizeof(wire), &wire_length) == 0);
        CHECK(feed_all(&decoder, wire, wire_length, &decoded) == 1);
        CHECK(decoded.version == packet.version && decoded.type == packet.type &&
              decoded.sequence == packet.sequence &&
              decoded.payload_length == packet.payload_length);
        CHECK(memcmp(decoded.payload, packet.payload, packet.payload_length) == 0);
    }
    return 0;
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
    uint8_t wire[BRIDGE_HELLO_PAYLOAD_SIZE] = {0U};
    bridge_sequence_tracker_t tracker;
    bridge_put_u16_le(wire, 0xa1b2U);
    CHECK(bridge_get_u16_le(wire) == 0xa1b2U);
    bridge_put_u32_le(&wire[BRIDGE_HELLO_SESSION_OFFSET], 0x89abcdefU);
    CHECK(bridge_get_u32_le(&wire[BRIDGE_HELLO_SESSION_OFFSET]) == 0x89abcdefU);
    bridge_sequence_tracker_init(&tracker);
    CHECK(!bridge_sequence_tracker_would_accept(NULL, 1U));
    CHECK(!bridge_sequence_tracker_accept(NULL, 1U));
    CHECK(bridge_sequence_tracker_would_accept(&tracker, 0xffffU));
    CHECK(bridge_sequence_tracker_accept(&tracker, 0xffffU));
    CHECK(!bridge_sequence_tracker_would_accept(&tracker, 0xffffU));
    CHECK(!bridge_sequence_tracker_accept(&tracker, 0xffffU));
    CHECK(bridge_sequence_tracker_accept(&tracker, 0U));
    CHECK(bridge_sequence_tracker_accept(&tracker, 1U));
    CHECK(!bridge_sequence_tracker_accept(&tracker, 0U));

    /* A command that was inspected but not committed remains retryable, even
     * if a later command completes first. */
    bridge_sequence_tracker_init(&tracker);
    CHECK(bridge_sequence_tracker_accept(&tracker, 100U));
    CHECK(bridge_sequence_tracker_would_accept(&tracker, 101U));
    CHECK(bridge_sequence_tracker_accept(&tracker, 102U));
    CHECK(bridge_sequence_tracker_would_accept(&tracker, 101U));
    CHECK(bridge_sequence_tracker_accept(&tracker, 101U));
    CHECK(!bridge_sequence_tracker_would_accept(&tracker, 101U));

    /* Entries beyond the bounded replay window fail closed. */
    CHECK(bridge_sequence_tracker_accept(&tracker, 140U));
    CHECK(!bridge_sequence_tracker_would_accept(&tracker, 100U));
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

static int test_rate_controller(void) {
    bridge_rate_controller_t controller;
    CHECK(!bridge_rate_controller_init(NULL, 100, 65536, 32768, 100));
    CHECK(!bridge_rate_controller_init(&controller, 0, 65536, 32768, 100));
    CHECK(bridge_rate_controller_init(&controller, 100, 65536, 32768, 100));
    CHECK(bridge_rate_controller_update(&controller, 100) == 0);
    CHECK(bridge_rate_controller_update(&controller, 90) == 15);
    CHECK(bridge_rate_controller_update(&controller, 90) == 20);
    CHECK(bridge_rate_controller_update(&controller, -1000) == 100);
    CHECK(controller.correction_ppm == 100);
    CHECK(bridge_rate_controller_update(&controller, 1100) == -100);
    bridge_rate_controller_reset(&controller);
    CHECK(controller.integral_error == 0 && controller.correction_ppm == 0);
    return 0;
}

static int test_rate_converges_from_prefill(void) {
    enum { PREFILL = 4096, DRIFT_PPM = 180 };
    for (int drift = -DRIFT_PPM; drift <= DRIFT_PPM; drift += 2 * DRIFT_PPM) {
        bridge_rate_controller_t controller;
        double fill = PREFILL;
        int32_t filtered = PREFILL;
        int32_t correction = 0;
        CHECK(bridge_rate_controller_init(&controller, PREFILL, 32768, 131, 500));
        CHECK(bridge_rate_controller_update(&controller, PREFILL) == 0);
        for (int update = 0; update < 6000; ++update) {
            /* 100 ms net change at 48 kHz. The producer and consumer each
             * contribute their clock error in parts per million. */
            fill += 4800.0 * (drift + correction) / 1000000.0;
            filtered += ((int32_t)fill - filtered) / 4;
            correction = bridge_rate_controller_update(&controller, filtered);
            CHECK(fill > PREFILL - 500 && fill < PREFILL + 500);
            CHECK(correction > -500 && correction < 500);
        }
        CHECK(fill > PREFILL - 8 && fill < PREFILL + 8);
        CHECK(correction + drift >= -2 && correction + drift <= 2);
    }
    return 0;
}

static int test_elastic_resampler(void) {
    const int16_t three_frames[] = {0, 100, 10, 110, 20, 120};
    const int16_t five_frames[] = {0, 100, 10, 110, 20, 120, 30, 130, 40, 140};
    int16_t expanded[10] = {0};
    int16_t contracted[6] = {0};
    int16_t copied[6] = {0};

    CHECK(!bridge_resample_stereo_s16(NULL, 3U, expanded, 5U));
    CHECK(!bridge_resample_stereo_s16(three_frames, 0U, expanded, 5U));
    CHECK(bridge_resample_stereo_s16(three_frames, 3U, expanded, 5U));
    CHECK(memcmp(expanded,
                 (int16_t[]){0, 100, 5, 105, 10, 110, 15, 115, 20, 120},
                 sizeof(expanded)) == 0);
    CHECK(bridge_resample_stereo_s16(five_frames, 5U, contracted, 3U));
    CHECK(memcmp(contracted, (int16_t[]){0, 100, 20, 120, 40, 140},
                 sizeof(contracted)) == 0);
    CHECK(bridge_resample_stereo_s16(three_frames, 3U, copied, 3U));
    CHECK(memcmp(copied, three_frames, sizeof(copied)) == 0);
    return 0;
}

static int test_continuous_resampler(void) {
    /* 48 kHz ramp to 44.1 kHz in 240-frame blocks: every output must land on
     * the ideal uniform grid, including across block boundaries. */
    enum { BLOCKS = 8, OUT = 240, SLOPE = 15 };
    static int16_t input[(BLOCKS * 262 + 1) * 2];
    int16_t output[OUT * 2];
    bridge_resampler_t resampler;
    const uint64_t step = (48000ULL << 32) / 44100U;
    size_t consumed = 0U;
    size_t produced = 0U;
    for (size_t frame = 0; frame < BLOCKS * 262U + 1U; ++frame) {
        input[frame * 2U] = (int16_t)(frame * SLOPE);
        input[frame * 2U + 1U] = (int16_t)-(int32_t)(frame * SLOPE);
    }
    bridge_resampler_reset(&resampler);
    for (int block = 0; block < BLOCKS; ++block) {
        size_t frames = bridge_resampler_input_frames(&resampler, step, OUT);
        CHECK(frames == 261U || frames == 262U);
        CHECK(!bridge_resampler_process(&resampler, step, &input[consumed * 2U],
                                        frames + 1U, output, OUT));
        CHECK(bridge_resampler_process(&resampler, step, &input[consumed * 2U], frames,
                                       output, OUT));
        for (size_t k = 0; k < OUT; ++k) {
            /* Previous starts at 0 (= input frame -1), so input[i] is at i + 1. */
            double ideal = (double)(produced + k + 1U) * 48000.0 / 44100.0 - 1.0;
            double position = output[k * 2U] / (double)SLOPE;
            CHECK(position <= ideal + 0.001 && position > ideal - 1.0 / SLOPE - 0.001);
        }
        consumed += frames;
        produced += OUT;
        CHECK(resampler.previous[0] == input[(consumed - 1U) * 2U]);
    }
    CHECK(bridge_resampler_input_frames(&resampler, 1ULL << 30, OUT) == 0U);
    CHECK(!bridge_resampler_process(NULL, step, input, 3U, output, 3U));
    return 0;
}

static int test_resampler_short_pulls(void) {
    /* A pull can land before the last consumed input, even at nominal 48 kHz
     * once negative drift compensation has introduced fractional phase. */
    const uint64_t step = 3ULL << 30;
    const int16_t input[] = {100, -100, 200, -200, 300, -300};
    const size_t expected_input[] = {1U, 1U, 1U, 0U};
    const int16_t expected_output[] = {75, 150, 225, 300};
    bridge_resampler_t resampler;
    bridge_resampler_reset(&resampler);
    size_t consumed = 0U;
    for (size_t i = 0U; i < 4U; ++i) {
        int16_t output[2];
        size_t frames = bridge_resampler_input_frames(&resampler, step, 1U);
        CHECK(frames == expected_input[i]);
        CHECK(bridge_resampler_process(&resampler, step,
                                       frames ? &input[consumed * 2U] : NULL,
                                       frames, output, 1U));
        CHECK(output[0] == expected_output[i] && output[1] == -expected_output[i]);
        consumed += frames;
    }
    CHECK(consumed == 3U && resampler.lag_q32 == 0U);
    /* Change the step with almost one frame already buffered. */
    resampler.lag_q32 = UINT32_MAX;
    int16_t output[2];
    CHECK(bridge_resampler_process(&resampler, 1ULL << 31, NULL, 0U, output, 1U));
    CHECK(output[0] == 250 && output[1] == -251);
    CHECK(bridge_resampler_input_frames(&resampler, step, SIZE_MAX) == 0U);
    CHECK(!bridge_resampler_process(&resampler, step, NULL, 0U, output, SIZE_MAX));
    return 0;
}

static int test_route_fence(void) {
    bridge_route_fence_t fence;
    bridge_route_fence_reset(&fence);
    CHECK(bridge_route_fence_set_desired(&fence, BRIDGE_ROUTE_A2DP) ==
          BRIDGE_ROUTE_FENCE_ACTION_SEND_SILENCE);
    CHECK(fence.phase == BRIDGE_ROUTE_FENCE_WAIT_SILENCE_ACK);
    CHECK(bridge_route_fence_ack(&fence, BRIDGE_ROUTE_SILENCE, true) ==
          BRIDGE_ROUTE_FENCE_ACTION_START_GUARD);
    CHECK(bridge_route_fence_guard_elapsed(&fence) ==
          BRIDGE_ROUTE_FENCE_ACTION_SEND_TARGET);
    CHECK(bridge_route_fence_ack(&fence, BRIDGE_ROUTE_A2DP, true) ==
          BRIDGE_ROUTE_FENCE_ACTION_AUTHORIZE);
    CHECK(fence.phase == BRIDGE_ROUTE_FENCE_ACTIVE);

    /* A call arriving mid-media forces a fresh SILENCE barrier. */
    CHECK(bridge_route_fence_set_desired(&fence, BRIDGE_ROUTE_HFP) ==
          BRIDGE_ROUTE_FENCE_ACTION_SEND_SILENCE);
    CHECK(bridge_route_fence_ack(&fence, BRIDGE_ROUTE_A2DP, true) ==
          BRIDGE_ROUTE_FENCE_ACTION_FAIL_CLOSED);
    CHECK(fence.phase == BRIDGE_ROUTE_FENCE_UNSYNCED);
    CHECK(bridge_route_fence_set_desired(&fence, BRIDGE_ROUTE_HFP) ==
          BRIDGE_ROUTE_FENCE_ACTION_SEND_SILENCE);
    CHECK(bridge_route_fence_ack(&fence, BRIDGE_ROUTE_SILENCE, false) ==
          BRIDGE_ROUTE_FENCE_ACTION_FAIL_CLOSED);

    /* A desired-route change at every intermediate phase restarts at SILENCE. */
    CHECK(bridge_route_fence_set_desired(&fence, BRIDGE_ROUTE_A2DP) ==
          BRIDGE_ROUTE_FENCE_ACTION_SEND_SILENCE);
    CHECK(bridge_route_fence_set_desired(&fence, BRIDGE_ROUTE_HFP) ==
          BRIDGE_ROUTE_FENCE_ACTION_SEND_SILENCE);
    CHECK(fence.phase == BRIDGE_ROUTE_FENCE_WAIT_SILENCE_ACK);
    CHECK(fence.target == BRIDGE_ROUTE_HFP);
    CHECK(bridge_route_fence_ack(&fence, BRIDGE_ROUTE_SILENCE, true) ==
          BRIDGE_ROUTE_FENCE_ACTION_START_GUARD);
    CHECK(fence.phase == BRIDGE_ROUTE_FENCE_DRAINING);
    CHECK(bridge_route_fence_set_desired(&fence, BRIDGE_ROUTE_A2DP) ==
          BRIDGE_ROUTE_FENCE_ACTION_SEND_SILENCE);
    CHECK(fence.phase == BRIDGE_ROUTE_FENCE_WAIT_SILENCE_ACK);
    CHECK(bridge_route_fence_ack(&fence, BRIDGE_ROUTE_SILENCE, true) ==
          BRIDGE_ROUTE_FENCE_ACTION_START_GUARD);
    CHECK(bridge_route_fence_guard_elapsed(&fence) ==
          BRIDGE_ROUTE_FENCE_ACTION_SEND_TARGET);
    CHECK(fence.phase == BRIDGE_ROUTE_FENCE_WAIT_TARGET_ACK);
    CHECK(bridge_route_fence_set_desired(&fence, BRIDGE_ROUTE_HFP) ==
          BRIDGE_ROUTE_FENCE_ACTION_SEND_SILENCE);
    CHECK(fence.phase == BRIDGE_ROUTE_FENCE_WAIT_SILENCE_ACK);

    /* SILENCE itself still requires an acknowledged flush barrier. */
    CHECK(bridge_route_fence_set_desired(&fence, BRIDGE_ROUTE_SILENCE) ==
          BRIDGE_ROUTE_FENCE_ACTION_SEND_SILENCE);
    CHECK(bridge_route_fence_ack(&fence, BRIDGE_ROUTE_SILENCE, true) ==
          BRIDGE_ROUTE_FENCE_ACTION_AUTHORIZE);
    return 0;
}

int main(void) {
    if (test_byte_ring() != 0 || test_frame_ring() != 0 || test_pcm() != 0 ||
        test_protocol() != 0 || test_protocol_random_stream() != 0 ||
        test_link_contract() != 0 || test_session() != 0 ||
        test_rate_controller() != 0 || test_rate_converges_from_prefill() != 0 ||
        test_elastic_resampler() != 0 ||
        test_continuous_resampler() != 0 || test_resampler_short_pulls() != 0 ||
        test_route_fence() != 0) {
        return 1;
    }
    (void)puts("bridge common host tests passed");
    return 0;
}
