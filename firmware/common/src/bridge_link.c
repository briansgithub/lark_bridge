#include "bridge_link.h"

void bridge_put_u16_le(uint8_t *output, uint16_t value) {
    if (output != NULL) {
        output[0] = (uint8_t)(value & 0xffU);
        output[1] = (uint8_t)(value >> 8U);
    }
}

void bridge_put_u32_le(uint8_t *output, uint32_t value) {
    if (output != NULL) {
        output[0] = (uint8_t)(value & 0xffU);
        output[1] = (uint8_t)((value >> 8U) & 0xffU);
        output[2] = (uint8_t)((value >> 16U) & 0xffU);
        output[3] = (uint8_t)(value >> 24U);
    }
}

uint16_t bridge_get_u16_le(const uint8_t *input) {
    if (input == NULL) {
        return 0U;
    }
    return (uint16_t)input[0] | (uint16_t)((uint16_t)input[1] << 8U);
}

uint32_t bridge_get_u32_le(const uint8_t *input) {
    if (input == NULL) {
        return 0U;
    }
    return (uint32_t)input[0] | ((uint32_t)input[1] << 8U) | ((uint32_t)input[2] << 16U) |
           ((uint32_t)input[3] << 24U);
}

void bridge_sequence_tracker_init(bridge_sequence_tracker_t *tracker) {
    if (tracker != NULL) {
        tracker->initialized = false;
        tracker->newest = 0U;
        tracker->seen = 0U;
    }
}

bool bridge_sequence_tracker_would_accept(const bridge_sequence_tracker_t *tracker,
                                          uint16_t sequence) {
    uint16_t distance;
    if (tracker == NULL) {
        return false;
    }
    if (!tracker->initialized) {
        return true;
    }
    distance = (uint16_t)(sequence - tracker->newest);
    if (distance != 0U && distance < 0x8000U) {
        return true;
    }
    distance = (uint16_t)(tracker->newest - sequence);
    return distance < 32U && (tracker->seen & (UINT32_C(1) << distance)) == 0U;
}

bool bridge_sequence_tracker_accept(bridge_sequence_tracker_t *tracker, uint16_t sequence) {
    uint16_t distance;
    if (!bridge_sequence_tracker_would_accept(tracker, sequence)) {
        return false;
    }
    if (!tracker->initialized) {
        tracker->initialized = true;
        tracker->newest = sequence;
        tracker->seen = 1U;
        return true;
    }
    distance = (uint16_t)(sequence - tracker->newest);
    if (distance != 0U && distance < 0x8000U) {
        tracker->seen = distance >= 32U ? 1U : (tracker->seen << distance) | 1U;
        tracker->newest = sequence;
    } else {
        distance = (uint16_t)(tracker->newest - sequence);
        tracker->seen |= UINT32_C(1) << distance;
    }
    return true;
}
