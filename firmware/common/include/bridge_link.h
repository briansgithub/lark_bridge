#ifndef BRIDGE_LINK_H
#define BRIDGE_LINK_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define BRIDGE_HELLO_PAYLOAD_SIZE 7U
#define BRIDGE_HELLO_ROLE_OFFSET 0U
#define BRIDGE_HELLO_CAPS_OFFSET 1U
#define BRIDGE_HELLO_SESSION_OFFSET 3U
#define BRIDGE_HEARTBEAT_PAYLOAD_SIZE 2U
#define BRIDGE_HEARTBEAT_ROUTE_OFFSET 0U
#define BRIDGE_HEARTBEAT_READY_OFFSET 1U
#define BRIDGE_ROUTE_PAYLOAD_SIZE 1U
#define BRIDGE_ROUTE_VALUE_OFFSET 0U
#define BRIDGE_AVRCP_PAYLOAD_SIZE 2U
#define BRIDGE_AVRCP_KEY_CODE_OFFSET 0U
#define BRIDGE_AVRCP_KEY_STATE_OFFSET 1U
#define BRIDGE_STATUS_PAYLOAD_SIZE 41U
#define BRIDGE_STATUS_READY_OFFSET 0U
#define BRIDGE_STATUS_FIFO_FRAMES_OFFSET 1U
#define BRIDGE_STATUS_UNDERRUNS_OFFSET 3U
#define BRIDGE_STATUS_OVERRUNS_OFFSET 7U
#define BRIDGE_STATUS_CORRECTION_PPM_OFFSET 11U
#define BRIDGE_STATUS_ADAPTIVE_DELTA_OFFSET 13U
#define BRIDGE_STATUS_MISSING_FRAMES_OFFSET 17U
#define BRIDGE_STATUS_DROPPED_FRAMES_OFFSET 21U
#define BRIDGE_STATUS_I2S_ERRORS_OFFSET 25U
#define BRIDGE_STATUS_FIFO_MIN_OFFSET 29U
#define BRIDGE_STATUS_FIFO_MAX_OFFSET 31U
#define BRIDGE_STATUS_PROTOCOL_ERRORS_OFFSET 33U
#define BRIDGE_STATUS_RELIABLE_FAILURES_OFFSET 37U
#define BRIDGE_FAULT_PAYLOAD_SIZE 2U
#define BRIDGE_FAULT_CODE_OFFSET 0U
#define BRIDGE_ACK_PAYLOAD_SIZE 4U
#define BRIDGE_ACK_TYPE_OFFSET 0U
#define BRIDGE_ACK_SEQUENCE_OFFSET 1U
#define BRIDGE_ACK_STATUS_OFFSET 3U

typedef enum {
    BRIDGE_ROLE_S31_GATEWAY = 1,
    BRIDGE_ROLE_OUTPUT = 2
} bridge_role_t;

typedef enum {
    BRIDGE_CAP_USB_UAC = 1U << 0,
    BRIDGE_CAP_HFP_HF = 1U << 1,
    BRIDGE_CAP_A2DP_SINK = 1U << 2,
    BRIDGE_CAP_AVRCP_CONTROLLER = 1U << 3,
    BRIDGE_CAP_I2S_TX = 1U << 4,
    BRIDGE_CAP_A2DP_SOURCE = 1U << 5,
    BRIDGE_CAP_AVRCP_TARGET = 1U << 6,
    BRIDGE_CAP_I2S_RX = 1U << 7,
    BRIDGE_CAP_FAIL_CLOSED = 1U << 8
} bridge_capability_t;

typedef enum {
    BRIDGE_READY_BT = 1U << 0,
    BRIDGE_READY_I2S = 1U << 1,
    BRIDGE_READY_UART = 1U << 2,
    BRIDGE_READY_UAC = 1U << 3,
    BRIDGE_READY_AEC = 1U << 4,
    BRIDGE_READY_PEER = 1U << 5,
    BRIDGE_READY_AUDIO = 1U << 6
} bridge_ready_flag_t;

typedef enum {
    BRIDGE_KEY_PRESSED = 0,
    BRIDGE_KEY_RELEASED = 1,
    /* One reliable inter-board transaction; S31 emits the paired AVRCP
     * press/release locally so a link reset cannot strand a pressed key. */
    BRIDGE_KEY_TAP = 2
} bridge_key_state_t;

typedef enum {
    BRIDGE_ACK_OK = 0,
    BRIDGE_ACK_UNSUPPORTED = 1,
    BRIDGE_ACK_INVALID = 2,
    BRIDGE_ACK_NOT_READY = 3
} bridge_ack_status_t;

typedef enum {
    BRIDGE_FAULT_NONE = 0,
    BRIDGE_FAULT_HEARTBEAT_TIMEOUT = 1,
    BRIDGE_FAULT_PROTOCOL = 2,
    BRIDGE_FAULT_AUDIO_UNDERRUN = 3,
    BRIDGE_FAULT_AUDIO_OVERRUN = 4,
    BRIDGE_FAULT_UAC_LOST = 5,
    BRIDGE_FAULT_AEC_NOT_READY = 6,
    BRIDGE_FAULT_BLUETOOTH = 7
} bridge_fault_code_t;

typedef struct {
    bool initialized;
    uint16_t newest;
    uint32_t seen;
} bridge_sequence_tracker_t;

void bridge_put_u16_le(uint8_t *output, uint16_t value);
void bridge_put_u32_le(uint8_t *output, uint32_t value);
uint16_t bridge_get_u16_le(const uint8_t *input);
uint32_t bridge_get_u32_le(const uint8_t *input);
void bridge_sequence_tracker_init(bridge_sequence_tracker_t *tracker);
bool bridge_sequence_tracker_would_accept(const bridge_sequence_tracker_t *tracker,
                                          uint16_t sequence);
bool bridge_sequence_tracker_accept(bridge_sequence_tracker_t *tracker, uint16_t sequence);

#endif
