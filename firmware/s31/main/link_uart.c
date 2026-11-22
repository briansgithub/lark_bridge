#include "link_uart.h"

#include <stdbool.h>
#include <stdint.h>
#include <string.h>

#include "audio_router.h"
#include "bridge_link.h"
#include "bridge_protocol.h"
#include "bridge_route_fence.h"
#include "bt_gateway.h"
#include "driver/uart.h"
#include "esp_log.h"
#include "esp_random.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "gateway_state.h"
#include "s31_config.h"

#define LINK_RX_BUFFER_BYTES 2048U
#define LINK_TX_BUFFER_BYTES 2048U
#define ROUTE_RETRY_PERIOD_MS 250U
#define ROUTE_MAX_ATTEMPTS 5U

typedef struct {
    bool active;
    uint16_t sequence;
    bridge_route_t route;
    uint32_t route_generation;
    uint8_t attempts;
    TickType_t last_send;
} pending_route_t;

static const char *TAG = "s31_link";
static uint16_t s_tx_sequence;
static bridge_stream_decoder_t s_decoder;
static bridge_sequence_tracker_t s_avrcp_sequences;
static pending_route_t s_pending_route;
static bridge_route_fence_t s_route_fence;
static uint32_t s_fence_route_generation;
static TickType_t s_route_guard_until;
static TickType_t s_last_peer_heartbeat;
static bool s_peer_seen;
static bool s_output_hello_valid;
static uint32_t s_local_session;
static uint32_t s_output_session;
static bool s_output_session_valid;
static uint16_t s_last_output_fifo_frames;
static int16_t s_last_output_correction_ppm;
static int32_t s_last_output_adaptive_delta;
static uint32_t s_last_output_missing_frames;
static uint32_t s_last_output_dropped_frames;
static uint32_t s_last_output_i2s_errors;
static uint16_t s_last_output_fifo_min_frames;
static uint16_t s_last_output_fifo_max_frames;
static uint32_t s_last_output_protocol_errors;
static uint32_t s_last_output_reliable_failures;
static uint32_t s_protocol_errors;
static uint32_t s_reliable_failures;

static void announce_hello(void);
static void send_heartbeat(void);

static esp_err_t send_packet(uint8_t type, const uint8_t *payload, uint16_t payload_length,
                             uint16_t sequence)
{
    bridge_packet_t packet = {
        .version = BRIDGE_PROTOCOL_VERSION,
        .type = type,
        .sequence = sequence,
        .payload_length = payload_length,
    };
    uint8_t encoded[BRIDGE_PROTOCOL_MAX_ENCODED_SIZE];
    size_t encoded_length = 0;
    if (payload_length != 0U) {
        memcpy(packet.payload, payload, payload_length);
    }
    if (bridge_protocol_encode(&packet, encoded, sizeof(encoded), &encoded_length) != 0) {
        return ESP_ERR_INVALID_SIZE;
    }
    int written = uart_write_bytes(S31_LINK_UART_PORT, encoded, encoded_length);
    return written == (int)encoded_length ? ESP_OK : ESP_FAIL;
}

static esp_err_t send_next(uint8_t type, const uint8_t *payload, uint16_t payload_length,
                           uint16_t *sequence)
{
    uint16_t next = s_tx_sequence++;
    if (sequence != NULL) {
        *sequence = next;
    }
    return send_packet(type, payload, payload_length, next);
}

static void send_ack(const bridge_packet_t *received, bridge_ack_status_t status)
{
    uint8_t payload[BRIDGE_ACK_PAYLOAD_SIZE];
    payload[BRIDGE_ACK_TYPE_OFFSET] = received->type;
    bridge_put_u16_le(&payload[BRIDGE_ACK_SEQUENCE_OFFSET], received->sequence);
    payload[BRIDGE_ACK_STATUS_OFFSET] = (uint8_t)status;
    esp_err_t result = send_next(BRIDGE_MESSAGE_ACK, payload, sizeof(payload), NULL);
    if (result != ESP_OK) {
        ESP_LOGW(TAG, "ACK send failed: %s", esp_err_to_name(result));
    }
}

static bool output_ready(uint8_t flags)
{
    const uint8_t required = BRIDGE_READY_BT | BRIDGE_READY_I2S | BRIDGE_READY_UART |
                             BRIDGE_READY_AUDIO;
    return (flags & required) == required;
}

static void note_peer_status(uint8_t ready_flags)
{
    if (!s_output_hello_valid) {
        s31_gateway_set_output_link(false);
        return;
    }
    s_peer_seen = true;
    s_last_peer_heartbeat = xTaskGetTickCount();
    s31_gateway_set_output_link(output_ready(ready_flags));
}

static void consume_output_fifo_status(const bridge_packet_t *packet)
{
    s_last_output_fifo_frames = bridge_get_u16_le(
        &packet->payload[BRIDGE_STATUS_FIFO_FRAMES_OFFSET]);
    s_last_output_correction_ppm = (int16_t)bridge_get_u16_le(
        &packet->payload[BRIDGE_STATUS_CORRECTION_PPM_OFFSET]);
    s_last_output_adaptive_delta = (int32_t)bridge_get_u32_le(
        &packet->payload[BRIDGE_STATUS_ADAPTIVE_DELTA_OFFSET]);
    s_last_output_missing_frames = bridge_get_u32_le(
        &packet->payload[BRIDGE_STATUS_MISSING_FRAMES_OFFSET]);
    s_last_output_dropped_frames = bridge_get_u32_le(
        &packet->payload[BRIDGE_STATUS_DROPPED_FRAMES_OFFSET]);
    s_last_output_i2s_errors = bridge_get_u32_le(
        &packet->payload[BRIDGE_STATUS_I2S_ERRORS_OFFSET]);
    s_last_output_fifo_min_frames = bridge_get_u16_le(
        &packet->payload[BRIDGE_STATUS_FIFO_MIN_OFFSET]);
    s_last_output_fifo_max_frames = bridge_get_u16_le(
        &packet->payload[BRIDGE_STATUS_FIFO_MAX_OFFSET]);
    s_last_output_protocol_errors = bridge_get_u32_le(
        &packet->payload[BRIDGE_STATUS_PROTOCOL_ERRORS_OFFSET]);
    s_last_output_reliable_failures = bridge_get_u32_le(
        &packet->payload[BRIDGE_STATUS_RELIABLE_FAILURES_OFFSET]);
}

static bridge_ack_status_t dispatch_avrcp(const bridge_packet_t *packet)
{
    if (packet->payload_length != BRIDGE_AVRCP_PAYLOAD_SIZE) {
        return BRIDGE_ACK_INVALID;
    }
    uint8_t command = packet->payload[BRIDGE_AVRCP_KEY_CODE_OFFSET];
    uint8_t key_state = packet->payload[BRIDGE_AVRCP_KEY_STATE_OFFSET];
    if (key_state > BRIDGE_KEY_TAP) {
        return BRIDGE_ACK_INVALID;
    }
    if (!s31_bt_avrcp_command_supported(command)) {
        return BRIDGE_ACK_UNSUPPORTED;
    }
    if (!bridge_sequence_tracker_would_accept(&s_avrcp_sequences, packet->sequence)) {
        return BRIDGE_ACK_OK;
    }
    esp_err_t result = s31_bt_send_avrcp(command, key_state);
    if (result == ESP_OK) {
        (void)bridge_sequence_tracker_accept(&s_avrcp_sequences, packet->sequence);
        return BRIDGE_ACK_OK;
    }
    if (result == ESP_ERR_INVALID_STATE) return BRIDGE_ACK_NOT_READY;
    if (result == ESP_ERR_INVALID_ARG) return BRIDGE_ACK_UNSUPPORTED;
    return BRIDGE_ACK_NOT_READY;
}

static void handle_ack(const bridge_packet_t *packet)
{
    if (packet->payload_length != BRIDGE_ACK_PAYLOAD_SIZE || !s_pending_route.active) {
        return;
    }
    uint8_t acknowledged_type = packet->payload[BRIDGE_ACK_TYPE_OFFSET];
    uint16_t acknowledged_sequence = bridge_get_u16_le(
        &packet->payload[BRIDGE_ACK_SEQUENCE_OFFSET]);
    bridge_ack_status_t status = (bridge_ack_status_t)packet->payload[BRIDGE_ACK_STATUS_OFFSET];
    if (acknowledged_type != BRIDGE_MESSAGE_ROUTE ||
        acknowledged_sequence != s_pending_route.sequence) {
        return;
    }
    s_pending_route.active = false;
    s31_gateway_snapshot_t snapshot;
    s31_gateway_get_snapshot(&snapshot);
    if (s_pending_route.route_generation != snapshot.route_generation) {
        /* The desired route changed after this command was sent. Even if it
         * changed back to the same enum value, this ACK belongs to an obsolete
         * fence and must never revive the old authorization. */
        bridge_route_fence_reset(&s_route_fence);
        s31_gateway_force_silence();
        return;
    }
    bridge_route_fence_action_t action = bridge_route_fence_ack(
        &s_route_fence, s_pending_route.route, status == BRIDGE_ACK_OK);
    if (status != BRIDGE_ACK_OK) {
        ++s_reliable_failures;
        s31_gateway_force_silence();
        ESP_LOGE(TAG, "output board rejected route %d, ACK status=%d",
                 s_pending_route.route, status);
        s31_gateway_set_output_link(false);
        s31_gateway_set_fault(BRIDGE_FAULT_PROTOCOL);
    } else if (action == BRIDGE_ROUTE_FENCE_ACTION_START_GUARD) {
        s_route_guard_until = xTaskGetTickCount() +
                              pdMS_TO_TICKS(CONFIG_LARKBRIDGE_S31_ROUTE_DRAIN_GUARD_MS);
    } else if (action == BRIDGE_ROUTE_FENCE_ACTION_AUTHORIZE) {
        if (s31_gateway_authorize_route(s_route_fence.target,
                                        s_pending_route.route_generation)) {
            send_heartbeat();
        } else {
            bridge_route_fence_reset(&s_route_fence);
            s31_gateway_force_silence();
        }
    } else if (action == BRIDGE_ROUTE_FENCE_ACTION_FAIL_CLOSED) {
        ++s_reliable_failures;
        s31_gateway_force_silence();
    }
}

static void handle_packet(const bridge_packet_t *packet)
{
    switch (packet->type) {
    case BRIDGE_MESSAGE_HELLO: {
        const uint16_t required = BRIDGE_CAP_A2DP_SOURCE | BRIDGE_CAP_AVRCP_TARGET |
                                  BRIDGE_CAP_I2S_RX | BRIDGE_CAP_FAIL_CLOSED;
        if (packet->payload_length != BRIDGE_HELLO_PAYLOAD_SIZE ||
            packet->payload[BRIDGE_HELLO_ROLE_OFFSET] != BRIDGE_ROLE_OUTPUT) {
            s_output_hello_valid = false;
            s_peer_seen = false;
            s31_gateway_set_output_link(false);
            s31_gateway_set_fault(BRIDGE_FAULT_PROTOCOL);
            ++s_protocol_errors;
            break;
        }
        uint16_t capabilities = bridge_get_u16_le(&packet->payload[BRIDGE_HELLO_CAPS_OFFSET]);
        uint32_t output_session = bridge_get_u32_le(
            &packet->payload[BRIDGE_HELLO_SESSION_OFFSET]);
        s_output_hello_valid = (capabilities & required) == required;
        if (!s_output_hello_valid) {
            ESP_LOGE(TAG, "output HELLO lacks required capabilities: 0x%04x", capabilities);
            ++s_protocol_errors;
            s31_gateway_set_output_link(false);
            s31_gateway_set_fault(BRIDGE_FAULT_PROTOCOL);
        } else {
            if (!s_output_session_valid || output_session != s_output_session) {
                /* A peer reboot starts a new replay domain. Fail closed and
                 * force the current route to be acknowledged by that boot. */
                s_output_session = output_session;
                s_output_session_valid = true;
                s_peer_seen = false;
                s_pending_route.active = false;
                bridge_route_fence_reset(&s_route_fence);
                bridge_sequence_tracker_init(&s_avrcp_sequences);
                s31_gateway_force_silence();
                s31_gateway_set_output_link(false);
                announce_hello();
                ESP_LOGI(TAG, "accepted output session 0x%08lx, caps=0x%04x",
                         (unsigned long)output_session, capabilities);
            }
        }
        break;
    }
    case BRIDGE_MESSAGE_HEARTBEAT:
        if (packet->payload_length == BRIDGE_HEARTBEAT_PAYLOAD_SIZE) {
            note_peer_status(packet->payload[BRIDGE_HEARTBEAT_READY_OFFSET]);
        } else {
            s31_gateway_set_fault(BRIDGE_FAULT_PROTOCOL);
            ++s_protocol_errors;
        }
        break;
    case BRIDGE_MESSAGE_STATUS:
        if (packet->payload_length == BRIDGE_STATUS_PAYLOAD_SIZE) {
            note_peer_status(packet->payload[BRIDGE_STATUS_READY_OFFSET]);
            consume_output_fifo_status(packet);
        } else {
            s31_gateway_set_fault(BRIDGE_FAULT_PROTOCOL);
            ++s_protocol_errors;
        }
        break;
    case BRIDGE_MESSAGE_AVRCP_COMMAND: {
        bridge_ack_status_t status = dispatch_avrcp(packet);
        send_ack(packet, status);
        break;
    }
    case BRIDGE_MESSAGE_FAULT:
        if (packet->payload_length == BRIDGE_FAULT_PAYLOAD_SIZE) {
            bridge_fault_code_t fault = (bridge_fault_code_t)bridge_get_u16_le(
                &packet->payload[BRIDGE_FAULT_CODE_OFFSET]);
            ESP_LOGE(TAG, "output board reported fault=%d", fault);
            s31_gateway_set_fault(fault);
            s31_gateway_set_output_link(false);
        } else {
            s31_gateway_set_fault(BRIDGE_FAULT_PROTOCOL);
            ++s_protocol_errors;
        }
        break;
    case BRIDGE_MESSAGE_ACK:
        handle_ack(packet);
        break;
    default:
        ESP_LOGW(TAG, "unsupported link message type=%u", packet->type);
        ++s_protocol_errors;
        break;
    }
}

static void announce_hello(void)
{
    uint8_t payload[BRIDGE_HELLO_PAYLOAD_SIZE];
    uint16_t capabilities = BRIDGE_CAP_USB_UAC | BRIDGE_CAP_HFP_HF |
                            BRIDGE_CAP_A2DP_SINK | BRIDGE_CAP_AVRCP_CONTROLLER |
                            BRIDGE_CAP_I2S_TX | BRIDGE_CAP_FAIL_CLOSED;
    payload[BRIDGE_HELLO_ROLE_OFFSET] = BRIDGE_ROLE_S31_GATEWAY;
    bridge_put_u16_le(&payload[BRIDGE_HELLO_CAPS_OFFSET], capabilities);
    bridge_put_u32_le(&payload[BRIDGE_HELLO_SESSION_OFFSET], s_local_session);
    send_next(BRIDGE_MESSAGE_HELLO, payload, sizeof(payload), NULL);
}

static void send_heartbeat(void)
{
    s31_gateway_snapshot_t snapshot;
    uint8_t payload[BRIDGE_HEARTBEAT_PAYLOAD_SIZE];
    s31_gateway_get_snapshot(&snapshot);
    payload[BRIDGE_HEARTBEAT_ROUTE_OFFSET] = (uint8_t)snapshot.effective_route;
    payload[BRIDGE_HEARTBEAT_READY_OFFSET] = s31_gateway_ready_flags();
    send_next(BRIDGE_MESSAGE_HEARTBEAT, payload, sizeof(payload), NULL);
}

static void send_status(void)
{
    s31_audio_stats_t stats;
    uint8_t payload[BRIDGE_STATUS_PAYLOAD_SIZE];
    s31_audio_router_get_stats(&stats);
    payload[BRIDGE_STATUS_READY_OFFSET] = s31_gateway_ready_flags();
    bridge_put_u16_le(&payload[BRIDGE_STATUS_FIFO_FRAMES_OFFSET], stats.fifo_frames);
    bridge_put_u32_le(&payload[BRIDGE_STATUS_UNDERRUNS_OFFSET], stats.underruns);
    bridge_put_u32_le(&payload[BRIDGE_STATUS_OVERRUNS_OFFSET], stats.overruns);
    bridge_put_u16_le(&payload[BRIDGE_STATUS_CORRECTION_PPM_OFFSET], 0U);
    bridge_put_u32_le(&payload[BRIDGE_STATUS_ADAPTIVE_DELTA_OFFSET], 0U);
    bridge_put_u32_le(&payload[BRIDGE_STATUS_MISSING_FRAMES_OFFSET], 0U);
    bridge_put_u32_le(&payload[BRIDGE_STATUS_DROPPED_FRAMES_OFFSET], 0U);
    bridge_put_u32_le(&payload[BRIDGE_STATUS_I2S_ERRORS_OFFSET], 0U);
    bridge_put_u16_le(&payload[BRIDGE_STATUS_FIFO_MIN_OFFSET], 0U);
    bridge_put_u16_le(&payload[BRIDGE_STATUS_FIFO_MAX_OFFSET], 0U);
    bridge_put_u32_le(&payload[BRIDGE_STATUS_PROTOCOL_ERRORS_OFFSET],
                      s_protocol_errors);
    bridge_put_u32_le(&payload[BRIDGE_STATUS_RELIABLE_FAILURES_OFFSET],
                      s_reliable_failures);
    send_next(BRIDGE_MESSAGE_STATUS, payload, sizeof(payload), NULL);
}

static void start_route_announcement(bridge_route_t route)
{
    uint8_t payload[BRIDGE_ROUTE_PAYLOAD_SIZE] = {(uint8_t)route};
    s_pending_route.active = true;
    s_pending_route.route = route;
    s_pending_route.route_generation = s_fence_route_generation;
    s_pending_route.attempts = 1U;
    s_pending_route.last_send = xTaskGetTickCount();
    send_next(BRIDGE_MESSAGE_ROUTE, payload, sizeof(payload), &s_pending_route.sequence);
}

static void maintain_route(void)
{
    s31_gateway_snapshot_t snapshot;
    TickType_t now = xTaskGetTickCount();
    s31_gateway_get_snapshot(&snapshot);

    if (snapshot.route_generation != s_fence_route_generation) {
        /* Identity transitions are significant even when several occur
         * between link-task iterations and the final enum value is unchanged. */
        s_fence_route_generation = snapshot.route_generation;
        s_pending_route.active = false;
        bridge_route_fence_reset(&s_route_fence);
        s31_gateway_force_silence();
    }

    bridge_route_fence_action_t action =
        bridge_route_fence_set_desired(&s_route_fence, snapshot.desired_route);
    if (action == BRIDGE_ROUTE_FENCE_ACTION_SEND_SILENCE) {
        s_pending_route.active = false;
        s31_gateway_force_silence();
        send_heartbeat();
        start_route_announcement(BRIDGE_ROUTE_SILENCE);
        return;
    }
    if (s_route_fence.phase == BRIDGE_ROUTE_FENCE_DRAINING) {
        if ((int32_t)(now - s_route_guard_until) >= 0) {
            action = bridge_route_fence_guard_elapsed(&s_route_fence);
            if (action == BRIDGE_ROUTE_FENCE_ACTION_SEND_TARGET) {
                start_route_announcement(s_route_fence.target);
            } else {
                s31_gateway_force_silence();
            }
        }
        return;
    }
    if (s_route_fence.phase == BRIDGE_ROUTE_FENCE_ACTIVE) {
        return;
    }
    if (!s_pending_route.active ||
        (now - s_pending_route.last_send) < pdMS_TO_TICKS(ROUTE_RETRY_PERIOD_MS)) {
        return;
    }
    if (s_pending_route.attempts >= ROUTE_MAX_ATTEMPTS) {
        ESP_LOGE(TAG, "route ACK timed out");
        ++s_reliable_failures;
        s_pending_route.active = false;
        bridge_route_fence_reset(&s_route_fence);
        s31_gateway_force_silence();
        s31_gateway_set_fault(BRIDGE_FAULT_HEARTBEAT_TIMEOUT);
        s31_gateway_set_output_link(false);
        return;
    }
    uint8_t payload[BRIDGE_ROUTE_PAYLOAD_SIZE] = {(uint8_t)s_pending_route.route};
    send_packet(BRIDGE_MESSAGE_ROUTE, payload, sizeof(payload), s_pending_route.sequence);
    ++s_pending_route.attempts;
    s_pending_route.last_send = now;
}

static void link_task(void *argument)
{
    (void)argument;
    uint8_t input[256];
    TickType_t last_periodic = 0;
    TickType_t last_drift_log = 0;
    announce_hello();
    for (;;) {
        int count = uart_read_bytes(S31_LINK_UART_PORT, input, sizeof(input), pdMS_TO_TICKS(50));
        for (int index = 0; index < count; ++index) {
            bridge_packet_t packet;
            bridge_decoder_result_t result = bridge_stream_decoder_feed(
                &s_decoder, input[index], &packet);
            if (result == BRIDGE_DECODER_PACKET) {
                handle_packet(&packet);
            } else if (result == BRIDGE_DECODER_ERROR) {
                ESP_LOGW(TAG, "discarded invalid CRC/COBS link frame");
                ++s_protocol_errors;
                s31_gateway_set_fault(BRIDGE_FAULT_PROTOCOL);
            }
        }
        TickType_t now = xTaskGetTickCount();
        maintain_route();
        if ((now - last_periodic) >= pdMS_TO_TICKS(S31_LINK_HEARTBEAT_PERIOD_MS)) {
            send_heartbeat();
            send_status();
            /* Same-session HELLO is idempotent. Periodic advertisement makes
             * either board's reboot recovery independent of UART burst timing. */
            announce_hello();
            last_periodic = now;
        }
        if (s_peer_seen &&
            (now - last_drift_log) >= pdMS_TO_TICKS(5000U)) {
            ESP_LOGI(TAG,
                     "output drift fifo=%u min=%u max=%u correction=%dppm delta=%ld "
                     "missing=%lu dropped=%lu i2s_err=%lu proto=%lu reliable=%lu",
                     s_last_output_fifo_frames, s_last_output_fifo_min_frames,
                     s_last_output_fifo_max_frames, s_last_output_correction_ppm,
                     (long)s_last_output_adaptive_delta,
                     (unsigned long)s_last_output_missing_frames,
                     (unsigned long)s_last_output_dropped_frames,
                     (unsigned long)s_last_output_i2s_errors,
                     (unsigned long)s_last_output_protocol_errors,
                     (unsigned long)s_last_output_reliable_failures);
            last_drift_log = now;
        }
        if (s_peer_seen &&
            (now - s_last_peer_heartbeat) > pdMS_TO_TICKS(S31_LINK_TIMEOUT_MS)) {
            ESP_LOGE(TAG, "output heartbeat timed out; forcing silence");
            s_peer_seen = false;
            s31_gateway_set_fault(BRIDGE_FAULT_HEARTBEAT_TIMEOUT);
            s31_gateway_set_output_link(false);
            bridge_route_fence_reset(&s_route_fence);
            s31_gateway_force_silence();
        }
    }
}

esp_err_t s31_link_uart_init(void)
{
    const uart_config_t uart_config = {
        .baud_rate = S31_LINK_UART_BAUD,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    esp_err_t result = uart_param_config(S31_LINK_UART_PORT, &uart_config);
    if (result != ESP_OK) return result;
    result = uart_set_pin(S31_LINK_UART_PORT, S31_LINK_UART_TX_GPIO,
                          S31_LINK_UART_RX_GPIO, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE);
    if (result != ESP_OK) return result;
    result = uart_driver_install(S31_LINK_UART_PORT, LINK_RX_BUFFER_BYTES,
                                 LINK_TX_BUFFER_BYTES, 0, NULL, 0);
    if (result != ESP_OK) return result;

    bridge_stream_decoder_init(&s_decoder);
    bridge_sequence_tracker_init(&s_avrcp_sequences);
    s_local_session = esp_random();
    s_output_session = 0U;
    s_output_session_valid = false;
    bridge_route_fence_reset(&s_route_fence);
    s_last_output_fifo_frames = 0U;
    s_last_output_correction_ppm = 0;
    s_last_output_adaptive_delta = 0;
    s_last_output_missing_frames = 0U;
    s_last_output_dropped_frames = 0U;
    s_last_output_i2s_errors = 0U;
    s31_gateway_set_uart_ready(true);
    if (xTaskCreate(link_task, "s31_link", 6144, NULL, 8, NULL) != pdPASS) {
        s31_gateway_set_uart_ready(false);
        uart_driver_delete(S31_LINK_UART_PORT);
        return ESP_ERR_NO_MEM;
    }
    ESP_LOGI(TAG, "UART link ready at %d baud, TX=%d RX=%d", S31_LINK_UART_BAUD,
             S31_LINK_UART_TX_GPIO, S31_LINK_UART_RX_GPIO);
    return ESP_OK;
}
