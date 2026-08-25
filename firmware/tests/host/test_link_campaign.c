#include "bridge_link.h"
#include "bridge_protocol.h"
#include "bridge_session.h"

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
    bridge_stream_decoder_t decoder;
    bridge_sequence_tracker_t reliable_sequences;
    unsigned int route_executions;
    unsigned int avrcp_executions;
    unsigned int acknowledgements;
    unsigned int decoder_errors;
    bridge_route_t route;
} simulated_endpoint_t;

static void endpoint_init(simulated_endpoint_t *endpoint) {
    (void)memset(endpoint, 0, sizeof(*endpoint));
    bridge_stream_decoder_init(&endpoint->decoder);
    bridge_sequence_tracker_init(&endpoint->reliable_sequences);
    endpoint->route = BRIDGE_ROUTE_SILENCE;
}

static void endpoint_handle_packet(simulated_endpoint_t *endpoint, const bridge_packet_t *packet) {
    bool reliable = packet->type == BRIDGE_MESSAGE_ROUTE ||
                    packet->type == BRIDGE_MESSAGE_AVRCP_COMMAND;
    bool execute = true;
    if (reliable) {
        ++endpoint->acknowledgements;
        execute = bridge_sequence_tracker_accept(&endpoint->reliable_sequences, packet->sequence);
    }
    if (!execute) {
        return;
    }
    if (packet->type == BRIDGE_MESSAGE_ROUTE &&
        packet->payload_length == BRIDGE_ROUTE_PAYLOAD_SIZE &&
        packet->payload[BRIDGE_ROUTE_VALUE_OFFSET] <= BRIDGE_ROUTE_HFP) {
        endpoint->route = (bridge_route_t)packet->payload[BRIDGE_ROUTE_VALUE_OFFSET];
        ++endpoint->route_executions;
    } else if (packet->type == BRIDGE_MESSAGE_AVRCP_COMMAND &&
               packet->payload_length == BRIDGE_AVRCP_PAYLOAD_SIZE &&
               packet->payload[BRIDGE_AVRCP_KEY_STATE_OFFSET] <= BRIDGE_KEY_RELEASED) {
        ++endpoint->avrcp_executions;
    }
}

static void endpoint_feed(simulated_endpoint_t *endpoint, const uint8_t *wire, size_t length) {
    size_t index;
    bridge_packet_t packet;
    for (index = 0U; index < length; ++index) {
        bridge_decoder_result_t result =
            bridge_stream_decoder_feed(&endpoint->decoder, wire[index], &packet);
        if (result == BRIDGE_DECODER_PACKET) {
            endpoint_handle_packet(endpoint, &packet);
        } else if (result == BRIDGE_DECODER_ERROR) {
            ++endpoint->decoder_errors;
        }
    }
}

static int encode_command(uint8_t type, uint16_t sequence, const uint8_t *payload,
                          uint16_t payload_length, uint8_t *wire, size_t *wire_length) {
    bridge_packet_t packet;
    (void)memset(&packet, 0, sizeof(packet));
    packet.version = BRIDGE_PROTOCOL_VERSION;
    packet.type = type;
    packet.sequence = sequence;
    packet.payload_length = payload_length;
    if (payload_length > 0U) {
        (void)memcpy(packet.payload, payload, payload_length);
    }
    return bridge_protocol_encode(&packet, wire, BRIDGE_PROTOCOL_MAX_ENCODED_SIZE, wire_length);
}

int main(void) {
    simulated_endpoint_t output;
    uint8_t wire[BRIDGE_PROTOCOL_MAX_ENCODED_SIZE];
    size_t wire_length = 0U;
    uint8_t route_payload[BRIDGE_ROUTE_PAYLOAD_SIZE] = {BRIDGE_ROUTE_HFP};
    uint8_t avrcp_payload[BRIDGE_AVRCP_PAYLOAD_SIZE] = {0x4bU, BRIDGE_KEY_PRESSED};
    size_t index;

    endpoint_init(&output);
    CHECK(encode_command(BRIDGE_MESSAGE_ROUTE, 100U, route_payload, sizeof(route_payload), wire,
                         &wire_length) == 0);
    endpoint_feed(&output, wire, wire_length);
    endpoint_feed(&output, wire, wire_length);
    CHECK(output.route == BRIDGE_ROUTE_HFP);
    CHECK(output.route_executions == 1U);
    CHECK(output.acknowledgements == 2U);

    /* Corruption is contained by the delimiter; the next reliable command still executes. */
    wire[wire_length / 2U] ^= 0x40U;
    endpoint_feed(&output, wire, wire_length);
    CHECK(output.decoder_errors == 1U);
    CHECK(encode_command(BRIDGE_MESSAGE_AVRCP_COMMAND, 101U, avrcp_payload,
                         sizeof(avrcp_payload), wire, &wire_length) == 0);
    endpoint_feed(&output, wire, wire_length);
    CHECK(output.avrcp_executions == 1U);

    /* An overlong unterminated frame drops through its delimiter and then resynchronizes. */
    for (index = 0U; index < BRIDGE_PROTOCOL_MAX_ENCODED_SIZE + 10U; ++index) {
        uint8_t garbage = 0x55U;
        endpoint_feed(&output, &garbage, 1U);
    }
    {
        uint8_t delimiter = 0U;
        endpoint_feed(&output, &delimiter, 1U);
    }
    CHECK(output.decoder_errors == 2U);
    avrcp_payload[BRIDGE_AVRCP_KEY_STATE_OFFSET] = BRIDGE_KEY_RELEASED;
    CHECK(encode_command(BRIDGE_MESSAGE_AVRCP_COMMAND, 102U, avrcp_payload,
                         sizeof(avrcp_payload), wire, &wire_length) == 0);
    endpoint_feed(&output, wire, wire_length);
    CHECK(output.avrcp_executions == 2U);
    CHECK(output.acknowledgements == 4U);

    (void)puts("simulated link campaign passed");
    return 0;
}
