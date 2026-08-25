#include "bridge_protocol.h"

#include <stdbool.h>
#include <string.h>

enum {
    VERSION_OFFSET = 0,
    TYPE_OFFSET = 1,
    SEQUENCE_LOW_OFFSET = 2,
    SEQUENCE_HIGH_OFFSET = 3,
    LENGTH_LOW_OFFSET = 4,
    LENGTH_HIGH_OFFSET = 5
};

static bool message_type_valid(uint8_t type) {
    return type >= (uint8_t)BRIDGE_MESSAGE_HELLO && type <= (uint8_t)BRIDGE_MESSAGE_ACK;
}

uint16_t bridge_crc16_ccitt(const uint8_t *data, size_t length) {
    uint16_t crc = 0xffffU;
    size_t index;
    if (data == NULL) {
        return 0U;
    }
    for (index = 0U; index < length; ++index) {
        unsigned int bit;
        crc ^= (uint16_t)data[index] << 8U;
        for (bit = 0U; bit < 8U; ++bit) {
            crc = (crc & 0x8000U) != 0U ? (uint16_t)((crc << 1U) ^ 0x1021U)
                                         : (uint16_t)(crc << 1U);
        }
    }
    return crc;
}

static bool cobs_encode(
    const uint8_t *input, size_t input_length, uint8_t *output, size_t output_capacity,
    size_t *output_length) {
    size_t read_index = 0U;
    size_t write_index = 1U;
    size_t code_index = 0U;
    uint8_t code = 1U;
    if (input == NULL || output == NULL || output_length == NULL || output_capacity == 0U) {
        return false;
    }
    while (read_index < input_length) {
        if (write_index >= output_capacity) {
            return false;
        }
        if (input[read_index] == 0U) {
            output[code_index] = code;
            code = 1U;
            code_index = write_index++;
        } else {
            output[write_index++] = input[read_index];
            ++code;
            if (code == 0xffU) {
                output[code_index] = code;
                code = 1U;
                code_index = write_index++;
                if (write_index > output_capacity) {
                    return false;
                }
            }
        }
        ++read_index;
    }
    if (code_index >= output_capacity) {
        return false;
    }
    output[code_index] = code;
    *output_length = write_index;
    return true;
}

static bool cobs_decode(
    const uint8_t *input, size_t input_length, uint8_t *output, size_t output_capacity,
    size_t *output_length) {
    size_t read_index = 0U;
    size_t write_index = 0U;
    if (input == NULL || output == NULL || output_length == NULL) {
        return false;
    }
    while (read_index < input_length) {
        uint8_t code = input[read_index++];
        unsigned int copy_count;
        if (code == 0U || (size_t)(code - 1U) > input_length - read_index) {
            return false;
        }
        for (copy_count = 1U; copy_count < code; ++copy_count) {
            if (write_index >= output_capacity) {
                return false;
            }
            output[write_index++] = input[read_index++];
        }
        if (code != 0xffU && read_index < input_length) {
            if (write_index >= output_capacity) {
                return false;
            }
            output[write_index++] = 0U;
        }
    }
    *output_length = write_index;
    return true;
}

int bridge_protocol_encode(
    const bridge_packet_t *packet, uint8_t *output, size_t output_capacity, size_t *output_length) {
    uint8_t raw[BRIDGE_PROTOCOL_MAX_RAW_SIZE];
    size_t raw_length;
    size_t encoded_length;
    uint16_t crc;
    if (packet == NULL || output == NULL || output_length == NULL ||
        packet->payload_length > BRIDGE_PROTOCOL_MAX_PAYLOAD ||
        packet->version != BRIDGE_PROTOCOL_VERSION || !message_type_valid(packet->type)) {
        return -1;
    }
    raw_length = BRIDGE_PROTOCOL_HEADER_SIZE + packet->payload_length + BRIDGE_PROTOCOL_CRC_SIZE;
    if (output_capacity < raw_length + 2U) {
        return -1;
    }
    raw[VERSION_OFFSET] = packet->version;
    raw[TYPE_OFFSET] = packet->type;
    raw[SEQUENCE_LOW_OFFSET] = (uint8_t)(packet->sequence & 0xffU);
    raw[SEQUENCE_HIGH_OFFSET] = (uint8_t)(packet->sequence >> 8U);
    raw[LENGTH_LOW_OFFSET] = (uint8_t)(packet->payload_length & 0xffU);
    raw[LENGTH_HIGH_OFFSET] = (uint8_t)(packet->payload_length >> 8U);
    memcpy(&raw[BRIDGE_PROTOCOL_HEADER_SIZE], packet->payload, packet->payload_length);
    crc = bridge_crc16_ccitt(raw, raw_length - BRIDGE_PROTOCOL_CRC_SIZE);
    raw[raw_length - BRIDGE_PROTOCOL_CRC_SIZE] = (uint8_t)(crc & 0xffU);
    raw[raw_length - 1U] = (uint8_t)(crc >> 8U);
    if (!cobs_encode(raw, raw_length, output, output_capacity - 1U, &encoded_length)) {
        return -1;
    }
    output[encoded_length++] = 0U;
    *output_length = encoded_length;
    return 0;
}

void bridge_stream_decoder_init(bridge_stream_decoder_t *decoder) {
    if (decoder != NULL) {
        decoder->encoded_length = 0U;
        decoder->dropping = 0;
    }
}

static bridge_decoder_result_t decode_packet(
    const uint8_t *encoded, size_t encoded_length, bridge_packet_t *packet) {
    uint8_t raw[BRIDGE_PROTOCOL_MAX_RAW_SIZE];
    size_t raw_length;
    uint16_t payload_length;
    uint16_t received_crc;
    if (packet == NULL || !cobs_decode(encoded, encoded_length, raw, sizeof(raw), &raw_length) ||
        raw_length < BRIDGE_PROTOCOL_HEADER_SIZE + BRIDGE_PROTOCOL_CRC_SIZE) {
        return BRIDGE_DECODER_ERROR;
    }
    payload_length = (uint16_t)raw[LENGTH_LOW_OFFSET] |
                     (uint16_t)((uint16_t)raw[LENGTH_HIGH_OFFSET] << 8U);
    if (raw[VERSION_OFFSET] != BRIDGE_PROTOCOL_VERSION ||
        !message_type_valid(raw[TYPE_OFFSET]) || payload_length > BRIDGE_PROTOCOL_MAX_PAYLOAD ||
        raw_length != BRIDGE_PROTOCOL_HEADER_SIZE + payload_length + BRIDGE_PROTOCOL_CRC_SIZE) {
        return BRIDGE_DECODER_ERROR;
    }
    received_crc = (uint16_t)raw[raw_length - BRIDGE_PROTOCOL_CRC_SIZE] |
                   (uint16_t)((uint16_t)raw[raw_length - 1U] << 8U);
    if (bridge_crc16_ccitt(raw, raw_length - BRIDGE_PROTOCOL_CRC_SIZE) != received_crc) {
        return BRIDGE_DECODER_ERROR;
    }
    packet->version = raw[VERSION_OFFSET];
    packet->type = raw[TYPE_OFFSET];
    packet->sequence = (uint16_t)raw[SEQUENCE_LOW_OFFSET] |
                       (uint16_t)((uint16_t)raw[SEQUENCE_HIGH_OFFSET] << 8U);
    packet->payload_length = payload_length;
    memcpy(packet->payload, &raw[BRIDGE_PROTOCOL_HEADER_SIZE], payload_length);
    return BRIDGE_DECODER_PACKET;
}

bridge_decoder_result_t bridge_stream_decoder_feed(
    bridge_stream_decoder_t *decoder, uint8_t byte, bridge_packet_t *packet) {
    bridge_decoder_result_t result;
    if (decoder == NULL) {
        return BRIDGE_DECODER_ERROR;
    }
    if (byte != 0U) {
        if (decoder->dropping != 0) {
            return BRIDGE_DECODER_NONE;
        }
        if (decoder->encoded_length >= sizeof(decoder->encoded)) {
            decoder->encoded_length = 0U;
            decoder->dropping = 1;
            return BRIDGE_DECODER_NONE;
        }
        decoder->encoded[decoder->encoded_length++] = byte;
        return BRIDGE_DECODER_NONE;
    }
    if (decoder->dropping != 0) {
        decoder->dropping = 0;
        decoder->encoded_length = 0U;
        return BRIDGE_DECODER_ERROR;
    }
    if (decoder->encoded_length == 0U) {
        return BRIDGE_DECODER_NONE;
    }
    result = decode_packet(decoder->encoded, decoder->encoded_length, packet);
    decoder->encoded_length = 0U;
    return result;
}
