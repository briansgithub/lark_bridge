#include "bridge_link.h"
#include "bridge_protocol.h"
#include "bridge_route_fence.h"
#include "bridge_session.h"

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define CHECK(condition)                                                                  \
    do {                                                                                  \
        if (!(condition)) {                                                               \
            (void)fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #condition); \
            return 1;                                                                     \
        }                                                                                 \
    } while (0)

typedef struct {
    bridge_stream_decoder_t decoder;
    bridge_sequence_tracker_t reliable_sequences;
    unsigned int route_executions;
    unsigned int tap_executions;
    unsigned int acknowledgements;
    unsigned int decoder_errors;
    bridge_route_t route;
} simulated_output_t;

typedef struct {
    bridge_route_fence_t fence;
    uint32_t generation;
    uint32_t pending_generation;
    bridge_route_t authorized_route;
    unsigned int obsolete_acks;
} simulated_gateway_t;

typedef struct {
    bool release_pending;
    uint8_t command;
    unsigned int presses;
    unsigned int releases;
} simulated_tap_dispatch_t;

static void output_init(simulated_output_t *output)
{
    (void)memset(output, 0, sizeof(*output));
    bridge_stream_decoder_init(&output->decoder);
    bridge_sequence_tracker_init(&output->reliable_sequences);
    output->route = BRIDGE_ROUTE_SILENCE;
}

static void output_handle_packet(simulated_output_t *output,
                                 const bridge_packet_t *packet)
{
    bool reliable = packet->type == BRIDGE_MESSAGE_ROUTE ||
                    packet->type == BRIDGE_MESSAGE_AVRCP_COMMAND;
    bool execute = true;

    if (reliable) {
        ++output->acknowledgements;
        execute = bridge_sequence_tracker_accept(&output->reliable_sequences,
                                                  packet->sequence);
    }
    if (!execute) {
        return;
    }
    if (packet->type == BRIDGE_MESSAGE_ROUTE &&
        packet->payload_length == BRIDGE_ROUTE_PAYLOAD_SIZE &&
        packet->payload[BRIDGE_ROUTE_VALUE_OFFSET] <= BRIDGE_ROUTE_HFP) {
        output->route = (bridge_route_t)packet->payload[BRIDGE_ROUTE_VALUE_OFFSET];
        ++output->route_executions;
    } else if (packet->type == BRIDGE_MESSAGE_AVRCP_COMMAND &&
               packet->payload_length == BRIDGE_AVRCP_PAYLOAD_SIZE &&
               packet->payload[BRIDGE_AVRCP_KEY_STATE_OFFSET] == BRIDGE_KEY_TAP) {
        ++output->tap_executions;
    }
}

static void output_feed(simulated_output_t *output, const uint8_t *wire,
                        size_t length)
{
    size_t index;
    bridge_packet_t packet;

    for (index = 0U; index < length; ++index) {
        bridge_decoder_result_t result =
            bridge_stream_decoder_feed(&output->decoder, wire[index], &packet);
        if (result == BRIDGE_DECODER_PACKET) {
            output_handle_packet(output, &packet);
        } else if (result == BRIDGE_DECODER_ERROR) {
            ++output->decoder_errors;
        }
    }
}

static int encode_command(uint8_t type, uint16_t sequence,
                          const uint8_t *payload, uint16_t payload_length,
                          uint8_t *wire, size_t *wire_length)
{
    bridge_packet_t packet;

    (void)memset(&packet, 0, sizeof(packet));
    packet.version = BRIDGE_PROTOCOL_VERSION;
    packet.type = type;
    packet.sequence = sequence;
    packet.payload_length = payload_length;
    if (payload_length > 0U) {
        (void)memcpy(packet.payload, payload, payload_length);
    }
    return bridge_protocol_encode(&packet, wire,
                                  BRIDGE_PROTOCOL_MAX_ENCODED_SIZE, wire_length);
}

static bridge_route_fence_action_t gateway_set_desired(
    simulated_gateway_t *gateway, bridge_route_t route)
{
    ++gateway->generation;
    gateway->pending_generation = gateway->generation;
    gateway->authorized_route = BRIDGE_ROUTE_SILENCE;
    return bridge_route_fence_set_desired(&gateway->fence, route);
}

static bridge_route_fence_action_t gateway_ack(simulated_gateway_t *gateway,
                                               bridge_route_t route,
                                               uint32_t generation)
{
    bridge_route_fence_action_t action;

    if (generation != gateway->pending_generation) {
        ++gateway->obsolete_acks;
        return BRIDGE_ROUTE_FENCE_ACTION_NONE;
    }
    action = bridge_route_fence_ack(&gateway->fence, route, true);
    if (action == BRIDGE_ROUTE_FENCE_ACTION_AUTHORIZE) {
        gateway->authorized_route = gateway->fence.target;
    }
    return action;
}

static bool tap_dispatch(simulated_tap_dispatch_t *dispatch, uint8_t command,
                         bool fail_release)
{
    if (dispatch->release_pending) {
        if (dispatch->command != command) {
            return false;
        }
    } else {
        dispatch->command = command;
        dispatch->release_pending = true;
        ++dispatch->presses;
    }
    if (fail_release) {
        return false;
    }
    ++dispatch->releases;
    dispatch->release_pending = false;
    return true;
}

static int run_route_campaign(void)
{
    simulated_output_t output;
    simulated_gateway_t gateway;
    bridge_session_t focus;
    uint8_t wire[BRIDGE_PROTOCOL_MAX_ENCODED_SIZE];
    uint8_t route_payload[BRIDGE_ROUTE_PAYLOAD_SIZE];
    size_t wire_length = 0U;
    uint32_t media_generation;

    output_init(&output);
    (void)memset(&gateway, 0, sizeof(gateway));
    bridge_route_fence_reset(&gateway.fence);
    bridge_session_init(&focus);
    bridge_session_apply(&focus, BRIDGE_SESSION_EVENT_A2DP_UP);
    CHECK(focus.route == BRIDGE_ROUTE_A2DP);

    CHECK(gateway_set_desired(&gateway, focus.route) ==
          BRIDGE_ROUTE_FENCE_ACTION_SEND_SILENCE);
    media_generation = gateway.pending_generation;
    route_payload[0] = BRIDGE_ROUTE_SILENCE;
    CHECK(encode_command(BRIDGE_MESSAGE_ROUTE, 10U, route_payload,
                         sizeof(route_payload), wire, &wire_length) == 0);
    /* The first attempt is dropped. A duplicated retry is ACKed twice but
     * executes once at the output endpoint. */
    output_feed(&output, wire, wire_length);
    output_feed(&output, wire, wire_length);
    CHECK(output.route_executions == 1U && output.acknowledgements == 2U);
    CHECK(gateway_ack(&gateway, BRIDGE_ROUTE_SILENCE, media_generation) ==
          BRIDGE_ROUTE_FENCE_ACTION_START_GUARD);
    CHECK(bridge_route_fence_guard_elapsed(&gateway.fence) ==
          BRIDGE_ROUTE_FENCE_ACTION_SEND_TARGET);
    route_payload[0] = BRIDGE_ROUTE_A2DP;
    CHECK(encode_command(BRIDGE_MESSAGE_ROUTE, 11U, route_payload,
                         sizeof(route_payload), wire, &wire_length) == 0);
    output_feed(&output, wire, wire_length);
    CHECK(gateway_ack(&gateway, BRIDGE_ROUTE_A2DP, media_generation) ==
          BRIDGE_ROUTE_FENCE_ACTION_AUTHORIZE);
    CHECK(gateway.authorized_route == BRIDGE_ROUTE_A2DP);

    /* CALL > MEDIA. If focus changes A2DP -> HFP -> A2DP before an ACK is
     * serviced, generation fencing rejects the obsolete HFP ACK. */
    bridge_session_apply(&focus, BRIDGE_SESSION_EVENT_HFP_UP);
    bridge_session_apply(&focus, BRIDGE_SESSION_EVENT_CALL_STARTED);
    CHECK(focus.route == BRIDGE_ROUTE_HFP);
    CHECK(gateway_set_desired(&gateway, focus.route) ==
          BRIDGE_ROUTE_FENCE_ACTION_SEND_SILENCE);
    {
        uint32_t hfp_generation = gateway.pending_generation;
        bridge_session_apply(&focus, BRIDGE_SESSION_EVENT_CALL_ENDED);
        CHECK(focus.route == BRIDGE_ROUTE_A2DP);
        CHECK(gateway_set_desired(&gateway, focus.route) ==
              BRIDGE_ROUTE_FENCE_ACTION_SEND_SILENCE);
        CHECK(gateway_ack(&gateway, BRIDGE_ROUTE_HFP, hfp_generation) ==
              BRIDGE_ROUTE_FENCE_ACTION_NONE);
        CHECK(gateway.obsolete_acks == 1U);
        CHECK(gateway.authorized_route == BRIDGE_ROUTE_SILENCE);
    }

    /* AEC readiness gates only call uplink; raw LARK audio never substitutes. */
    bridge_session_apply(&focus, BRIDGE_SESSION_EVENT_CALL_STARTED);
    bridge_session_apply(&focus, BRIDGE_SESSION_EVENT_LARK_PRESENT);
    CHECK(!focus.hfp_uplink_enabled);
    bridge_session_apply(&focus, BRIDGE_SESSION_EVENT_AEC_READY);
    CHECK(focus.hfp_uplink_enabled && focus.route == BRIDGE_ROUTE_HFP);
    bridge_session_apply(&focus, BRIDGE_SESSION_EVENT_LARK_LOST);
    CHECK(!focus.hfp_uplink_enabled && focus.route == BRIDGE_ROUTE_HFP);
    return 0;
}

static int run_link_and_control_campaign(void)
{
    simulated_output_t output;
    simulated_tap_dispatch_t tap = {0};
    uint8_t wire[BRIDGE_PROTOCOL_MAX_ENCODED_SIZE];
    uint8_t avrcp_payload[BRIDGE_AVRCP_PAYLOAD_SIZE] = {0x4bU, BRIDGE_KEY_TAP};
    size_t wire_length = 0U;
    size_t index;

    output_init(&output);
    CHECK(encode_command(BRIDGE_MESSAGE_AVRCP_COMMAND, 100U, avrcp_payload,
                         sizeof(avrcp_payload), wire, &wire_length) == 0);
    output_feed(&output, wire, wire_length);
    output_feed(&output, wire, wire_length);
    CHECK(output.tap_executions == 1U && output.acknowledgements == 2U);

    /* If the local release enqueue fails, retry only that release. */
    CHECK(!tap_dispatch(&tap, avrcp_payload[0], true));
    CHECK(tap.presses == 1U && tap.releases == 0U && tap.release_pending);
    CHECK(tap_dispatch(&tap, avrcp_payload[0], false));
    CHECK(tap.presses == 1U && tap.releases == 1U && !tap.release_pending);

    /* Corruption and an overlong frame are delimited; the next frame recovers. */
    wire[wire_length / 2U] ^= 0x40U;
    output_feed(&output, wire, wire_length);
    CHECK(output.decoder_errors == 1U);
    for (index = 0U; index < BRIDGE_PROTOCOL_MAX_ENCODED_SIZE + 10U; ++index) {
        const uint8_t garbage = 0x55U;
        output_feed(&output, &garbage, 1U);
    }
    {
        const uint8_t delimiter = 0U;
        output_feed(&output, &delimiter, 1U);
    }
    CHECK(output.decoder_errors == 2U);

    /* A peer reboot starts a new session and replay window; no pre-reset
     * queued command survives, while sequence reuse in the new session works. */
    output_init(&output);
    CHECK(encode_command(BRIDGE_MESSAGE_AVRCP_COMMAND, 100U, avrcp_payload,
                         sizeof(avrcp_payload), wire, &wire_length) == 0);
    output_feed(&output, wire, wire_length);
    CHECK(output.tap_executions == 1U);
    return 0;
}

int main(void)
{
    CHECK(run_route_campaign() == 0);
    CHECK(run_link_and_control_campaign() == 0);
    (void)puts("deterministic two-endpoint link campaign passed");
    return 0;
}
