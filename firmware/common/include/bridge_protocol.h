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

/* Deliberately invalid frames for the H2 fault hooks (test images only).
 * Each kind decodes to exactly one BRIDGE_DECODER_ERROR at its trailing
 * delimiter, and the decoder accepts the next valid frame. */
typedef enum {
    BRIDGE_CORRUPT_CRC = 1,      /* valid COBS frame whose CRC has one bit flipped */
    BRIDGE_CORRUPT_COBS = 2,     /* first COBS code points past the end of the frame */
    BRIDGE_CORRUPT_VERSION = 3,  /* correct CRC over a version-2 header */
    BRIDGE_CORRUPT_OVERLONG = 4  /* a non-zero run longer than any frame, then a delimiter */
} bridge_corruption_t;

#define BRIDGE_PROTOCOL_OVERLONG_RUN (BRIDGE_PROTOCOL_MAX_ENCODED_SIZE + 22U)
#define BRIDGE_PROTOCOL_MAX_CORRUPT_SIZE (BRIDGE_PROTOCOL_OVERLONG_RUN + 1U)

uint16_t bridge_crc16_ccitt(const uint8_t *data, size_t length);
int bridge_protocol_encode(
    const bridge_packet_t *packet, uint8_t *output, size_t output_capacity, size_t *output_length);
/* Encodes packet as the given corruption, including the trailing delimiter.
 * Returns 0, or -1 for a bad argument or a too-small buffer. */
int bridge_protocol_encode_corrupt(const bridge_packet_t *packet, bridge_corruption_t kind,
                                   uint8_t *output, size_t output_capacity,
                                   size_t *output_length);
void bridge_stream_decoder_init(bridge_stream_decoder_t *decoder);
bridge_decoder_result_t bridge_stream_decoder_feed(
    bridge_stream_decoder_t *decoder, uint8_t byte, bridge_packet_t *packet);

#endif
