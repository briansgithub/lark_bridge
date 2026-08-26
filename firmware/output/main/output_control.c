#include "output_control.h"

#include "bridge_protocol.h"
#include "driver/uart.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "output_audio.h"
#include "output_policy.h"

#include <stdatomic.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#define CONTROL_UART UART_NUM_2
#define TX_QUEUE_LENGTH 16U
#define PENDING_RELIABLE_COUNT 8U
#define RX_CHUNK_SIZE 64U
#define HELLO_INTERVAL_MS 5000U
#define HEARTBEAT_INTERVAL_MS 500U
#define STATUS_INTERVAL_MS 1000U
#define RETRY_INTERVAL_MS 250U
#define MAX_RELIABLE_ATTEMPTS 5U

typedef struct {
    uint8_t type;
    uint16_t payload_length;
    uint8_t payload[BRIDGE_PROTOCOL_MAX_PAYLOAD];
    bool reliable;
} tx_request_t;

typedef struct {
    bool used;
    uint8_t attempts;
    uint32_t retry_at_ms;
    bridge_packet_t packet;
} pending_packet_t;

static const char *TAG = "output_control";
static QueueHandle_t s_tx_queue;
static output_policy_t s_policy;
static bridge_sequence_tracker_t s_heartbeat_sequences;
static bridge_sequence_tracker_t s_route_sequences;
static pending_packet_t s_pending[PENDING_RELIABLE_COUNT];
static portMUX_TYPE s_policy_mux = portMUX_INITIALIZER_UNLOCKED;
static atomic_bool s_initialized;
static atomic_bool s_local_audio_blocked;
static atomic_uint_fast8_t s_local_ready_flags;
static uint16_t s_next_sequence = 1U;
static uint16_t s_remote_capabilities;
static uint32_t s_local_session;
static uint32_t s_remote_session;
static bool s_remote_session_valid;
static uint32_t s_protocol_errors;
static uint32_t s_reliable_failures;

static uint32_t now_ms(void) {
    return (uint32_t)(esp_timer_get_time() / 1000);
}

static bool full_audio_allowed_at(uint32_t current_ms) {
    const uint8_t local_required = (uint8_t)(BRIDGE_READY_BT | BRIDGE_READY_I2S |
                                             BRIDGE_READY_UART | BRIDGE_READY_PEER |
                                             BRIDGE_READY_AUDIO);
    uint8_t local =
        (uint8_t)atomic_load_explicit(&s_local_ready_flags, memory_order_acquire);
    bool allowed;

    portENTER_CRITICAL(&s_policy_mux);
    allowed = output_policy_audio_allowed(&s_policy, current_ms) &&
              (s_remote_capabilities & BRIDGE_CAP_I2S_TX) != 0U;
    portEXIT_CRITICAL(&s_policy_mux);
    return allowed &&
           !atomic_load_explicit(&s_local_audio_blocked, memory_order_acquire) &&
           (local & local_required) == local_required;
}

static void refresh_audio_capture(uint32_t current_ms, bool force_flush) {
    bool active = full_audio_allowed_at(current_ms);
    if (force_flush) {
        output_audio_flush();
    }
    output_audio_set_capture_active(active);
}

static bool send_wire_packet(const bridge_packet_t *packet) {
    uint8_t encoded[BRIDGE_PROTOCOL_MAX_ENCODED_SIZE];
    size_t encoded_length = 0U;
    int written;

    if (bridge_protocol_encode(packet, encoded, sizeof(encoded), &encoded_length) != 0) {
        return false;
    }
    written = uart_write_bytes(CONTROL_UART, encoded, encoded_length);
    return written == (int)encoded_length;
}

static bridge_packet_t make_packet(const tx_request_t *request) {
    bridge_packet_t packet;

    (void)memset(&packet, 0, sizeof(packet));
    packet.version = BRIDGE_PROTOCOL_VERSION;
    packet.type = request->type;
    packet.sequence = s_next_sequence++;
    packet.payload_length = request->payload_length;
    (void)memcpy(packet.payload, request->payload, request->payload_length);
    return packet;
}

static pending_packet_t *find_pending_slot(void) {
    size_t index;
    for (index = 0U; index < PENDING_RELIABLE_COUNT; ++index) {
        if (!s_pending[index].used) {
            return &s_pending[index];
        }
    }
    return NULL;
}

static bool reliable_in_flight(void) {
    size_t index;
    for (index = 0U; index < PENDING_RELIABLE_COUNT; ++index) {
        if (s_pending[index].used) {
            return true;
        }
    }
    return false;
}

static void discard_outbound_session_work(void) {
    (void)memset(s_pending, 0, sizeof(s_pending));
    if (s_tx_queue != NULL) {
        (void)xQueueReset(s_tx_queue);
    }
}

static void transmit_request(const tx_request_t *request, uint32_t current_ms) {
    bridge_packet_t packet = make_packet(request);
    pending_packet_t *pending = NULL;

    if (request->reliable) {
        pending = find_pending_slot();
        if (pending == NULL) {
            ++s_reliable_failures;
            ESP_LOGE(TAG, "reliable window full; dropping message type %u", request->type);
            return;
        }
    }
    if (!send_wire_packet(&packet)) {
        ESP_LOGW(TAG, "short/failed UART write for type %u", request->type);
    }
    if (pending != NULL) {
        pending->used = true;
        pending->attempts = 1U;
        pending->retry_at_ms = current_ms + RETRY_INTERVAL_MS;
        pending->packet = packet;
    }
}

static void queue_or_send_ack(uint8_t type, uint16_t sequence, bridge_ack_status_t status) {
    tx_request_t request = {
        .type = BRIDGE_MESSAGE_ACK,
        .payload_length = BRIDGE_ACK_PAYLOAD_SIZE,
        .reliable = false,
    };
    request.payload[BRIDGE_ACK_TYPE_OFFSET] = type;
    bridge_put_u16_le(&request.payload[BRIDGE_ACK_SEQUENCE_OFFSET], sequence);
    request.payload[BRIDGE_ACK_STATUS_OFFSET] = (uint8_t)status;
    transmit_request(&request, now_ms());
}

static void handle_ack(const bridge_packet_t *packet) {
    uint8_t acked_type;
    uint16_t acked_sequence;
    uint8_t status;
    size_t index;

    if (packet->payload_length != BRIDGE_ACK_PAYLOAD_SIZE) {
        ++s_protocol_errors;
        return;
    }
    acked_type = packet->payload[BRIDGE_ACK_TYPE_OFFSET];
    acked_sequence = bridge_get_u16_le(&packet->payload[BRIDGE_ACK_SEQUENCE_OFFSET]);
    status = packet->payload[BRIDGE_ACK_STATUS_OFFSET];
    for (index = 0U; index < PENDING_RELIABLE_COUNT; ++index) {
        pending_packet_t *pending = &s_pending[index];
        if (pending->used && pending->packet.type == acked_type &&
            pending->packet.sequence == acked_sequence) {
            if (status == BRIDGE_ACK_NOT_READY) {
                /* The receiver may have completed only the press half of an
                 * atomic TAP. Keep this exact sequence in flight so its retry
                 * can finish the release without issuing another press. */
                pending->retry_at_ms = now_ms() + RETRY_INTERVAL_MS;
                return;
            }
            pending->used = false;
            if (status != BRIDGE_ACK_OK) {
                /* INVALID and UNSUPPORTED are terminal. Never continue an
                 * ordered reliable sequence from a rejected transaction. */
                (void)xQueueReset(s_tx_queue);
                ESP_LOGW(TAG, "message type %u sequence %u rejected with status %u", acked_type,
                         acked_sequence, status);
            }
            return;
        }
    }
}

static void update_policy_after_heartbeat(const bridge_packet_t *packet, uint32_t current_ms) {
    bool before;
    bool after;

    portENTER_CRITICAL(&s_policy_mux);
    before = output_policy_audio_allowed(&s_policy, current_ms);
    if (bridge_sequence_tracker_accept(&s_heartbeat_sequences, packet->sequence)) {
        (void)output_policy_accept_heartbeat(
            &s_policy, current_ms, packet->payload[BRIDGE_HEARTBEAT_ROUTE_OFFSET],
            packet->payload[BRIDGE_HEARTBEAT_READY_OFFSET]);
    }
    after = output_policy_audio_allowed(&s_policy, current_ms);
    portEXIT_CRITICAL(&s_policy_mux);
    if (before != after) {
        refresh_audio_capture(current_ms, true);
    } else {
        refresh_audio_capture(current_ms, false);
    }
}

static void handle_hello(const bridge_packet_t *packet) {
    uint8_t role;
    uint16_t capabilities;
    uint32_t remote_session;
    output_policy_t candidate;
    bool reset_session = false;

    if (packet->payload_length != BRIDGE_HELLO_PAYLOAD_SIZE) {
        ++s_protocol_errors;
        return;
    }
    role = packet->payload[BRIDGE_HELLO_ROLE_OFFSET];
    capabilities = bridge_get_u16_le(&packet->payload[BRIDGE_HELLO_CAPS_OFFSET]);
    remote_session = bridge_get_u32_le(&packet->payload[BRIDGE_HELLO_SESSION_OFFSET]);
    output_policy_init(&candidate, CONFIG_LARKBRIDGE_OUTPUT_HEARTBEAT_TIMEOUT_MS);
    if (!output_policy_accept_hello(&candidate, role, capabilities)) {
        ++s_protocol_errors;
        return;
    }

    portENTER_CRITICAL(&s_policy_mux);
    if (!s_remote_session_valid || remote_session != s_remote_session) {
        (void)output_policy_accept_hello(&s_policy, role, capabilities);
        bridge_sequence_tracker_init(&s_heartbeat_sequences);
        bridge_sequence_tracker_init(&s_route_sequences);
        s_remote_session = remote_session;
        s_remote_session_valid = true;
        reset_session = true;
    }
    s_remote_capabilities = capabilities;
    portEXIT_CRITICAL(&s_policy_mux);
    if (reset_session) {
        /* Reliable commands belong to the old S31 boot and must never be
         * replayed into the new one. This task is the sole pending-window owner. */
        discard_outbound_session_work();
        output_audio_set_capture_active(false);
        ESP_LOGI(TAG, "accepted S31 session 0x%08lx, caps=0x%04x",
                 (unsigned long)remote_session, capabilities);
    }
}

static void handle_route(const bridge_packet_t *packet, uint32_t current_ms) {
    bridge_ack_status_t status = BRIDGE_ACK_OK;
    bool execute = false;
    bool before = false;
    bool after = false;
    bridge_route_t old_route = BRIDGE_ROUTE_SILENCE;
    bridge_route_t new_route = BRIDGE_ROUTE_SILENCE;

    if (packet->payload_length != BRIDGE_ROUTE_PAYLOAD_SIZE) {
        queue_or_send_ack(packet->type, packet->sequence, BRIDGE_ACK_INVALID);
        ++s_protocol_errors;
        return;
    }
    portENTER_CRITICAL(&s_policy_mux);
    if (!s_policy.hello_valid) {
        status = BRIDGE_ACK_NOT_READY;
    } else {
        output_policy_t candidate = s_policy;
        if (!output_policy_accept_route(
                &candidate, packet->payload[BRIDGE_ROUTE_VALUE_OFFSET])) {
            status = BRIDGE_ACK_INVALID;
        } else if (bridge_sequence_tracker_would_accept(
                       &s_route_sequences, packet->sequence)) {
            before = output_policy_audio_allowed(&s_policy, current_ms);
            old_route = s_policy.route;
            s_policy = candidate;
            (void)bridge_sequence_tracker_accept(&s_route_sequences, packet->sequence);
            execute = true;
            after = output_policy_audio_allowed(&s_policy, current_ms);
            new_route = s_policy.route;
        }
    }
    portEXIT_CRITICAL(&s_policy_mux);

    if (execute && status == BRIDGE_ACK_OK && (old_route != new_route || before != after)) {
        refresh_audio_capture(current_ms, true);
    }
    queue_or_send_ack(packet->type, packet->sequence, status);
}

static void handle_packet(const bridge_packet_t *packet, uint32_t current_ms) {
    switch (packet->type) {
    case BRIDGE_MESSAGE_HELLO:
        handle_hello(packet);
        break;
    case BRIDGE_MESSAGE_HEARTBEAT:
        if (packet->payload_length == BRIDGE_HEARTBEAT_PAYLOAD_SIZE) {
            update_policy_after_heartbeat(packet, current_ms);
        } else {
            ++s_protocol_errors;
        }
        break;
    case BRIDGE_MESSAGE_ROUTE:
        handle_route(packet, current_ms);
        break;
    case BRIDGE_MESSAGE_ACK:
        handle_ack(packet);
        break;
    case BRIDGE_MESSAGE_STATUS:
    case BRIDGE_MESSAGE_FAULT:
        /* S31 diagnostics are consumed by the coordinator/operator, not this endpoint. */
        break;
    default:
        ++s_protocol_errors;
        break;
    }
}

static void retry_pending(uint32_t current_ms) {
    size_t index;
    for (index = 0U; index < PENDING_RELIABLE_COUNT; ++index) {
        pending_packet_t *pending = &s_pending[index];
        if (!pending->used || (int32_t)(current_ms - pending->retry_at_ms) < 0) {
            continue;
        }
        if (pending->attempts >= MAX_RELIABLE_ATTEMPTS) {
            ESP_LOGE(TAG, "no ACK for type %u sequence %u", pending->packet.type,
                     pending->packet.sequence);
            pending->used = false;
            (void)xQueueReset(s_tx_queue);
            ++s_reliable_failures;
            continue;
        }
        (void)send_wire_packet(&pending->packet);
        ++pending->attempts;
        pending->retry_at_ms = current_ms + RETRY_INTERVAL_MS;
    }
}

static void send_hello(void) {
    const uint16_t capabilities = (uint16_t)(BRIDGE_CAP_A2DP_SOURCE | BRIDGE_CAP_AVRCP_TARGET |
                                             BRIDGE_CAP_I2S_RX | BRIDGE_CAP_FAIL_CLOSED);
    tx_request_t request = {
        .type = BRIDGE_MESSAGE_HELLO,
        .payload_length = BRIDGE_HELLO_PAYLOAD_SIZE,
        .reliable = false,
    };
    request.payload[BRIDGE_HELLO_ROLE_OFFSET] = BRIDGE_ROLE_OUTPUT;
    bridge_put_u16_le(&request.payload[BRIDGE_HELLO_CAPS_OFFSET], capabilities);
    bridge_put_u32_le(&request.payload[BRIDGE_HELLO_SESSION_OFFSET], s_local_session);
    transmit_request(&request, now_ms());
}

static void send_heartbeat(uint32_t current_ms) {
    tx_request_t request = {
        .type = BRIDGE_MESSAGE_HEARTBEAT,
        .payload_length = BRIDGE_HEARTBEAT_PAYLOAD_SIZE,
        .reliable = false,
    };
    portENTER_CRITICAL(&s_policy_mux);
    request.payload[BRIDGE_HEARTBEAT_ROUTE_OFFSET] = (uint8_t)s_policy.route;
    portEXIT_CRITICAL(&s_policy_mux);
    request.payload[BRIDGE_HEARTBEAT_READY_OFFSET] =
        (uint8_t)atomic_load_explicit(&s_local_ready_flags, memory_order_acquire);
    transmit_request(&request, current_ms);
}

static void send_status(uint32_t current_ms) {
    static uint32_t next_log_ms;
    output_audio_stats_t stats;
    tx_request_t request = {
        .type = BRIDGE_MESSAGE_STATUS,
        .payload_length = BRIDGE_STATUS_PAYLOAD_SIZE,
        .reliable = false,
    };
    output_audio_get_stats(&stats);
    request.payload[BRIDGE_STATUS_READY_OFFSET] =
        (uint8_t)atomic_load_explicit(&s_local_ready_flags, memory_order_acquire);
    bridge_put_u16_le(&request.payload[BRIDGE_STATUS_FIFO_FRAMES_OFFSET], stats.fifo_frames);
    bridge_put_u32_le(&request.payload[BRIDGE_STATUS_UNDERRUNS_OFFSET], stats.underruns);
    bridge_put_u32_le(&request.payload[BRIDGE_STATUS_OVERRUNS_OFFSET], stats.overruns);
    bridge_put_u16_le(&request.payload[BRIDGE_STATUS_CORRECTION_PPM_OFFSET],
                      (uint16_t)(int16_t)stats.correction_ppm);
    bridge_put_u32_le(&request.payload[BRIDGE_STATUS_ADAPTIVE_DELTA_OFFSET],
                      (uint32_t)stats.adaptive_input_delta_frames);
    bridge_put_u32_le(&request.payload[BRIDGE_STATUS_MISSING_FRAMES_OFFSET],
                      stats.missing_frames);
    bridge_put_u32_le(&request.payload[BRIDGE_STATUS_DROPPED_FRAMES_OFFSET],
                      stats.dropped_frames);
    bridge_put_u32_le(&request.payload[BRIDGE_STATUS_I2S_ERRORS_OFFSET],
                      stats.i2s_read_errors);
    bridge_put_u16_le(&request.payload[BRIDGE_STATUS_FIFO_MIN_OFFSET],
                      stats.fifo_min_frames);
    bridge_put_u16_le(&request.payload[BRIDGE_STATUS_FIFO_MAX_OFFSET],
                      stats.fifo_max_frames);
    bridge_put_u32_le(&request.payload[BRIDGE_STATUS_PROTOCOL_ERRORS_OFFSET],
                      s_protocol_errors);
    bridge_put_u32_le(&request.payload[BRIDGE_STATUS_RELIABLE_FAILURES_OFFSET],
                      s_reliable_failures);
    transmit_request(&request, current_ms);
    if ((int32_t)(current_ms - next_log_ms) >= 0) {
        ESP_LOGI(TAG,
                 "link/audio fifo=%u min=%u max=%u correction=%ldppm delta=%ld "
                 "missing=%lu dropped=%lu i2s_err=%lu proto=%lu reliable=%lu "
                 "heap=%u min_heap=%u largest=%u",
                 stats.fifo_frames, stats.fifo_min_frames, stats.fifo_max_frames,
                 (long)stats.correction_ppm,
                 (long)stats.adaptive_input_delta_frames,
                 (unsigned long)stats.missing_frames,
                 (unsigned long)stats.dropped_frames,
                 (unsigned long)stats.i2s_read_errors,
                 (unsigned long)s_protocol_errors,
                 (unsigned long)s_reliable_failures,
                 (unsigned)heap_caps_get_free_size(MALLOC_CAP_8BIT),
                 (unsigned)heap_caps_get_minimum_free_size(MALLOC_CAP_8BIT),
                 (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_8BIT));
        next_log_ms = current_ms + 5000U;
    }
}

static void control_task(void *context) {
    bridge_stream_decoder_t decoder;
    bridge_packet_t packet;
    uint8_t bytes[RX_CHUNK_SIZE];
    uint32_t next_hello = 0U;
    uint32_t next_heartbeat = 0U;
    uint32_t next_status = 0U;
    (void)context;

    bridge_stream_decoder_init(&decoder);
    for (;;) {
        int received = uart_read_bytes(CONTROL_UART, bytes, sizeof(bytes), pdMS_TO_TICKS(10U));
        uint32_t current_ms = now_ms();
        int index;
        tx_request_t request;
        bool heartbeat_timed_out;

        for (index = 0; index < received; ++index) {
            bridge_decoder_result_t result = bridge_stream_decoder_feed(&decoder, bytes[index], &packet);
            if (result == BRIDGE_DECODER_PACKET) {
                handle_packet(&packet, current_ms);
            } else if (result == BRIDGE_DECODER_ERROR) {
                ++s_protocol_errors;
            }
        }
        /* AVRCP press/release edges are ordered. Keep exactly one reliable
         * packet in flight so a damaged press is retried before its release. */
        while (xQueuePeek(s_tx_queue, &request, 0U) == pdTRUE) {
            if (request.reliable && reliable_in_flight()) {
                break;
            }
            if (xQueueReceive(s_tx_queue, &request, 0U) != pdTRUE) {
                break;
            }
            transmit_request(&request, current_ms);
        }
        retry_pending(current_ms);

        portENTER_CRITICAL(&s_policy_mux);
        heartbeat_timed_out = output_policy_tick(&s_policy, current_ms);
        portEXIT_CRITICAL(&s_policy_mux);
        if (heartbeat_timed_out) {
            discard_outbound_session_work();
            output_audio_set_capture_active(false);
            (void)output_control_report_fault(BRIDGE_FAULT_HEARTBEAT_TIMEOUT);
        }

        if ((int32_t)(current_ms - next_hello) >= 0) {
            send_hello();
            next_hello = current_ms + HELLO_INTERVAL_MS;
        }
        if ((int32_t)(current_ms - next_heartbeat) >= 0) {
            send_heartbeat(current_ms);
            next_heartbeat = current_ms + HEARTBEAT_INTERVAL_MS;
        }
        if ((int32_t)(current_ms - next_status) >= 0) {
            send_status(current_ms);
            next_status = current_ms + STATUS_INTERVAL_MS;
        }
    }
}

esp_err_t output_control_start(void) {
    uart_config_t uart_config = {
        .baud_rate = CONFIG_LARKBRIDGE_OUTPUT_UART_BAUD,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .rx_flow_ctrl_thresh = 0U,
        .source_clk = UART_SCLK_DEFAULT,
    };
    esp_err_t error;

    atomic_init(&s_initialized, false);
    atomic_init(&s_local_audio_blocked, true);
    atomic_init(&s_local_ready_flags, 0U);
    output_policy_init(&s_policy, CONFIG_LARKBRIDGE_OUTPUT_HEARTBEAT_TIMEOUT_MS);
    bridge_sequence_tracker_init(&s_heartbeat_sequences);
    bridge_sequence_tracker_init(&s_route_sequences);
    (void)memset(s_pending, 0, sizeof(s_pending));
    s_local_session = esp_random();
    s_remote_session = 0U;
    s_remote_session_valid = false;
    s_tx_queue = xQueueCreate(TX_QUEUE_LENGTH, sizeof(tx_request_t));
    if (s_tx_queue == NULL) {
        return ESP_ERR_NO_MEM;
    }
    error = uart_param_config(CONTROL_UART, &uart_config);
    if (error != ESP_OK) {
        return error;
    }
    error = uart_set_pin(CONTROL_UART, CONFIG_LARKBRIDGE_OUTPUT_UART_TX_GPIO,
                         CONFIG_LARKBRIDGE_OUTPUT_UART_RX_GPIO, UART_PIN_NO_CHANGE,
                         UART_PIN_NO_CHANGE);
    if (error != ESP_OK) {
        return error;
    }
    error = uart_driver_install(CONTROL_UART, 2048U, 1024U, 0, NULL, 0);
    if (error != ESP_OK) {
        return error;
    }
    (void)uart_flush_input(CONTROL_UART);
    if (xTaskCreate(control_task, "bridge_uart", 6144U, NULL, 16U, NULL) != pdPASS) {
        (void)uart_driver_delete(CONTROL_UART);
        return ESP_ERR_NO_MEM;
    }
    atomic_store_explicit(&s_initialized, true, memory_order_release);
    ESP_LOGI(TAG, "control UART ready at %d baud", CONFIG_LARKBRIDGE_OUTPUT_UART_BAUD);
    return ESP_OK;
}

bool output_control_is_initialized(void) {
    return atomic_load_explicit(&s_initialized, memory_order_acquire);
}

bool output_control_link_alive(void) {
    bool alive;
    uint32_t current_ms = now_ms();
    portENTER_CRITICAL(&s_policy_mux);
    alive = s_policy.hello_valid && s_policy.heartbeat_seen &&
            (uint32_t)(current_ms - s_policy.last_heartbeat_ms) < s_policy.heartbeat_timeout_ms;
    portEXIT_CRITICAL(&s_policy_mux);
    return alive;
}

bool output_control_audio_allowed(void) {
    return full_audio_allowed_at(now_ms());
}

bool output_control_send_avrcp(uint8_t key_code, bridge_key_state_t key_state) {
    tx_request_t request = {
        .type = BRIDGE_MESSAGE_AVRCP_COMMAND,
        .payload_length = BRIDGE_AVRCP_PAYLOAD_SIZE,
        .reliable = true,
    };

    if (!output_control_link_alive() ||
        (key_state != BRIDGE_KEY_PRESSED && key_state != BRIDGE_KEY_RELEASED &&
         key_state != BRIDGE_KEY_TAP)) {
        return false;
    }
    portENTER_CRITICAL(&s_policy_mux);
    bool peer_supports_avrcp =
        (s_remote_capabilities & BRIDGE_CAP_AVRCP_CONTROLLER) != 0U;
    portEXIT_CRITICAL(&s_policy_mux);
    if (!peer_supports_avrcp) {
        return false;
    }
    request.payload[BRIDGE_AVRCP_KEY_CODE_OFFSET] = key_code;
    request.payload[BRIDGE_AVRCP_KEY_STATE_OFFSET] = (uint8_t)key_state;
    return xQueueSend(s_tx_queue, &request, 0U) == pdTRUE;
}

bool output_control_report_fault(bridge_fault_code_t fault) {
    tx_request_t request = {
        .type = BRIDGE_MESSAGE_FAULT,
        .payload_length = BRIDGE_FAULT_PAYLOAD_SIZE,
        .reliable = false,
    };
    bridge_put_u16_le(&request.payload[BRIDGE_FAULT_CODE_OFFSET], (uint16_t)fault);
    return s_tx_queue != NULL && xQueueSend(s_tx_queue, &request, 0U) == pdTRUE;
}

void output_control_set_local_ready(uint8_t ready_flags) {
    uint8_t previous = (uint8_t)atomic_exchange_explicit(
        &s_local_ready_flags, ready_flags, memory_order_acq_rel);
    if (previous != ready_flags) {
        /* Local car/profile transitions are also audio boundaries. Stop I2S
         * capture and discard anything collected while the sink is absent. */
        refresh_audio_capture(now_ms(), true);
    }
}

void output_control_invalidate_local_audio(void) {
    const uint8_t clear_mask = (uint8_t)~(BRIDGE_READY_PEER | BRIDGE_READY_AUDIO);
    atomic_store_explicit(&s_local_audio_blocked, true, memory_order_release);
    (void)atomic_fetch_and_explicit(&s_local_ready_flags, clear_mask,
                                    memory_order_acq_rel);
    /* A later readiness sample cannot reopen this sticky boundary. Only a
     * validated, newer A2DP audio-start event can do that. */
    refresh_audio_capture(now_ms(), true);
}

void output_control_note_local_audio_started(void) {
    atomic_store_explicit(&s_local_audio_blocked, false, memory_order_release);
    refresh_audio_capture(now_ms(), true);
}
