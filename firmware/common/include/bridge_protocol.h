#ifndef BRIDGE_PROTOCOL_H
#define BRIDGE_PROTOCOL_H

#include <stddef.h>
#include <stdint.h>

#define BRIDGE_PROTOCOL_VERSION 1U
#define BRIDGE_PROTOCOL_MAX_PAYLOAD 128U
#define BRIDGE_PROTOCOL_HEADER_SIZE 6U
#define BRIDGE_PROTOCOL_CRC_SIZE 2U
#define BRIDGE_PROTOCOL_MAX_RAW_SIZE \
    (BRIDGE_PROTOCOL_HEADER_SIZE + BRIDGE_PROTOCOL_MAX_PAYLOAD + BRIDGE_PROTOCOL_CRC_SIZE)
#define BRIDGE_PROTOCOL_MAX_ENCODED_SIZE (BRIDGE_PROTOCOL_MAX_RAW_SIZE + 2U)

typedef enum {
    BRIDGE_MESSAGE_HELLO = 1,
    BRIDGE_MESSAGE_HEARTBEAT = 2,
    BRIDGE_MESSAGE_ROUTE = 3,
    BRIDGE_MESSAGE_AVRCP_COMMAND = 4,
    BRIDGE_MESSAGE_STATUS = 5,
    BRIDGE_MESSAGE_FAULT = 6,
    BRIDGE_MESSAGE_ACK = 7
} bridge_message_type_t;

typedef struct {
    uint8_t version;
    uint8_t type;
    uint16_t sequence;
    uint16_t payload_length;
    uint8_t payload[BRIDGE_PROTOCOL_MAX_PAYLOAD];
} bridge_packet_t;

typedef enum {
    BRIDGE_DECODER_NONE = 0,
    BRIDGE_DECODER_PACKET,
    BRIDGE_DECODER_ERROR
} bridge_decoder_result_t;

typedef struct {
    uint8_t encoded[BRIDGE_PROTOCOL_MAX_ENCODED_SIZE];
    size_t encoded_length;
    int dropping;
} bridge_stream_decoder_t;

uint16_t bridge_crc16_ccitt(const uint8_t *data, size_t length);
int bridge_protocol_encode(
    const bridge_packet_t *packet, uint8_t *output, size_t output_capacity, size_t *output_length);
void bridge_stream_decoder_init(bridge_stream_decoder_t *decoder);
bridge_decoder_result_t bridge_stream_decoder_feed(
    bridge_stream_decoder_t *decoder, uint8_t byte, bridge_packet_t *packet);

#endif
