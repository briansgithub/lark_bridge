#include "output_sbc.h"
#include "output_bluetooth.h"

#include "esp_a2dp_api.h"
#include "esp_avrc_api.h"
#include "esp_bt.h"
#include "esp_bt_device.h"
#include "esp_bt_main.h"
#include "esp_gap_bt_api.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "output_audio.h"
#include "output_avrcp_meta.h"
#include "output_control.h"
#include "output_peer_store.h"
#include "output_sink_policy.h"

#include <stdatomic.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#define BT_EVENT_QUEUE_LENGTH 16U
#define CONNECT_TIMEOUT_MS 15000U
#define QUALIFICATION_TIMEOUT_MS 20000U
#define MEDIA_RETRY_MS 1000U
/* While streaming, the A2DP stack pulls PCM every few ms. A silence this long means the
 * link is gone even if its DISCONNECTED event was lost (Astra finding 4). */
#define A2DP_PULL_STALL_MS 3000U
#define MEDIA_ACK_TIMEOUT_MS 1500U
#define MEDIA_ACK_MAX_TIMEOUTS 3U
/* Car UX feature 2: dual pairing mode (discoverable + CoD-filtered inquiry). */
#define PAIRING_WINDOW_MS 45000U
#define PAIRING_RETRY_MS 1000U
#define NAME_CACHE_SIZE 8U

typedef enum {
    BT_STATE_WAITING = 0,
    BT_STATE_DISCOVERING,
    BT_STATE_CONNECTING,
    BT_STATE_DISCONNECTING,
    BT_STATE_CONNECTED
} bt_connection_state_t;

typedef enum {
    BT_EVENT_DISCOVERY_MATCH,
    BT_EVENT_DISCOVERY_STOPPED,
    BT_EVENT_CONNECTED,
    BT_EVENT_DISCONNECTED,
    BT_EVENT_AUDIO_STARTED,
    BT_EVENT_AUDIO_SUSPENDED,
    BT_EVENT_SINK_CAPABILITIES,
    BT_EVENT_PREFERRED_CODEC_RESULT,
    BT_EVENT_MEDIA_ACK,
    BT_EVENT_CANDIDATE,
    BT_EVENT_AUTH_FAILED,
    BT_EVENT_PAIR_REQUEST,
    BT_EVENT_FACTORY_WIPE,
    BT_EVENT_FORGET_SINK,
#if CONFIG_LARKBRIDGE_TEST_IMAGE
    BT_EVENT_TEST_FORGET,
#endif
} bt_event_type_t;

typedef struct {
    bt_event_type_t type;
    esp_bd_addr_t address;
    esp_a2d_conn_hdl_t connection_handle;
    esp_a2d_mcc_t codec;
    esp_a2d_media_ctrl_t media_command;
    esp_a2d_media_ctrl_ack_t media_status;
    esp_bt_status_t bt_status;
    int8_t rssi;
    uint32_t cod;
    int32_t index;
    bool address_valid;
} bt_event_t;

static const char *TAG = "output_bt";
static QueueHandle_t s_event_queue;
static atomic_bool s_stack_ready;
static atomic_bool s_peer_connected;
static atomic_bool s_audio_streaming;
/* A nonzero PCM format publishes rate and channel count together to the data callback. */
static atomic_uint s_pcm_format;
static esp_a2d_mcc_t s_sink_caps;
static esp_a2d_mcc_t s_requested_codec;
static bool s_codec_request_pending;
static bool s_codec_fallback;
static bt_connection_state_t s_state = BT_STATE_WAITING;
static portMUX_TYPE s_saved_peer_mux = portMUX_INITIALIZER_UNLOCKED;
static portMUX_TYPE s_avrcp_peer_mux = portMUX_INITIALIZER_UNLOCKED;
/* MRU list of saved sinks (slot 0 = most recent). Written only by the worker,
 * read from Bluedroid callbacks and telemetry under s_saved_peer_mux. */
static output_sink_list_t s_sinks;
static atomic_bool s_pairing_open;
static atomic_uint s_pairing_deadline_ms;
static output_sink_candidates_t s_candidates;
static unsigned int s_rr_cursor;
/* Legacy PIN fallback (0000 -> 1234) for one candidate, under s_saved_peer_mux. */
static bool s_pin_fallback_active;
static esp_bd_addr_t s_pin_fallback_bda;
static struct {
    esp_bd_addr_t address;
    char name[OUTPUT_SINK_NAME_MAX];
    bool used;
} s_names[NAME_CACHE_SIZE];
static unsigned int s_names_next;
static esp_bd_addr_t s_active_peer;
static esp_bd_addr_t s_requested_peer;
static esp_bd_addr_t s_discovered_peer;
static esp_bd_addr_t s_avrcp_peer;
static bool s_have_discovered_peer;
static bool s_avrcp_peer_connected;
static bool s_format_ready;
static bool s_active_connection_valid;
static esp_a2d_conn_hdl_t s_active_connection_handle;
static bool s_media_ack_pending;
static esp_a2d_media_ctrl_t s_expected_media_ack;
static uint8_t s_media_ack_timeout_count;
static uint32_t s_next_action_ms;
static uint32_t s_connect_deadline_ms;
static uint32_t s_next_media_attempt_ms;
static uint32_t s_media_ack_deadline_ms;
static uint32_t s_qualification_deadline_ms;

static void apply_class_of_device(const char *why);

static uint32_t now_ms(void) {
    return (uint32_t)(esp_timer_get_time() / 1000);
}

static bool pairing_open(void)
{
    return atomic_load_explicit(&s_pairing_open, memory_order_acquire);
}

/* Slot 0 (most recently used) sink. */
static bool saved_peer_snapshot(esp_bd_addr_t address)
{
    bool present;

    portENTER_CRITICAL(&s_saved_peer_mux);
    present = s_sinks.count > 0U;
    if (present && address != NULL) {
        (void)memcpy(address, s_sinks.sinks[0].addr, ESP_BD_ADDR_LEN);
    }
    portEXIT_CRITICAL(&s_saved_peer_mux);
    return present;
}

static void sinks_snapshot(output_sink_list_t *list)
{
    portENTER_CRITICAL(&s_saved_peer_mux);
    *list = s_sinks;
    portEXIT_CRITICAL(&s_saved_peer_mux);
}

static void sinks_commit(const output_sink_list_t *list)
{
    portENTER_CRITICAL(&s_saved_peer_mux);
    s_sinks = *list;
    portEXIT_CRITICAL(&s_saved_peer_mux);
}

static bool saved_peer_matches(const esp_bd_addr_t address)
{
    bool matches;

    portENTER_CRITICAL(&s_saved_peer_mux);
    matches = output_sink_list_find(&s_sinks, address) >= 0;
    portEXIT_CRITICAL(&s_saved_peer_mux);
    return matches;
}

/* Pairing gate: any peer while pairing is open or nothing is saved yet,
 * otherwise only saved sinks. */
static bool saved_peer_allows(const esp_bd_addr_t address)
{
    bool allowed;

    if (pairing_open()) {
        return true;
    }
    portENTER_CRITICAL(&s_saved_peer_mux);
    allowed = s_sinks.count == 0U || output_sink_list_find(&s_sinks, address) >= 0;
    portEXIT_CRITICAL(&s_saved_peer_mux);
    return allowed;
}

static void name_cache_put(const esp_bd_addr_t address, const char *name)
{
    if (name == NULL || name[0] == '\0') {
        return;
    }
    portENTER_CRITICAL(&s_saved_peer_mux);
    unsigned int slot = s_names_next;
    bool found = false;
    for (unsigned int i = 0U; i < NAME_CACHE_SIZE; ++i) {
        if (s_names[i].used && memcmp(s_names[i].address, address, ESP_BD_ADDR_LEN) == 0) {
            slot = i;
            found = true;
            break;
        }
    }
    if (!found) {
        s_names_next = (s_names_next + 1U) % NAME_CACHE_SIZE;
    }
    (void)memcpy(s_names[slot].address, address, ESP_BD_ADDR_LEN);
    size_t length = strnlen(name, OUTPUT_SINK_NAME_MAX - 1U);
    (void)memcpy(s_names[slot].name, name, length);
    s_names[slot].name[length] = '\0';
    s_names[slot].used = true;
    portEXIT_CRITICAL(&s_saved_peer_mux);
}

static void name_cache_get(const esp_bd_addr_t address, char name[OUTPUT_SINK_NAME_MAX])
{
    name[0] = '\0';
    portENTER_CRITICAL(&s_saved_peer_mux);
    for (unsigned int i = 0U; i < NAME_CACHE_SIZE; ++i) {
        if (s_names[i].used && memcmp(s_names[i].address, address, ESP_BD_ADDR_LEN) == 0) {
            (void)memcpy(name, s_names[i].name, OUTPUT_SINK_NAME_MAX);
            break;
        }
    }
    portEXIT_CRITICAL(&s_saved_peer_mux);
}

static bool pin_fallback_for(const esp_bd_addr_t address)
{
    bool active;

    portENTER_CRITICAL(&s_saved_peer_mux);
    active = s_pin_fallback_active && memcmp(s_pin_fallback_bda, address, ESP_BD_ADDR_LEN) == 0;
    portEXIT_CRITICAL(&s_saved_peer_mux);
    return active;
}

static void pin_fallback_set(const uint8_t *address, bool active)
{
    portENTER_CRITICAL(&s_saved_peer_mux);
    s_pin_fallback_active = active;
    if (address != NULL) {
        (void)memcpy(s_pin_fallback_bda, address, ESP_BD_ADDR_LEN);
    }
    portEXIT_CRITICAL(&s_saved_peer_mux);
}

#define ADDR_FMT "%02x:%02x:%02x:%02x:%02x:%02x"
#define ADDR_ARGS(a) (a)[0], (a)[1], (a)[2], (a)[3], (a)[4], (a)[5]

static void log_sinks(const char *why)
{
    output_sink_list_t list;
    sinks_snapshot(&list);
    ESP_LOGI(TAG, "MRU sinks (%s): %u saved", why, (unsigned int)list.count);
    for (unsigned int i = 0U; i < list.count; ++i) {
        ESP_LOGI(TAG, "MRU slot %u: " ADDR_FMT " '%s'", i, ADDR_ARGS(list.sinks[i].addr),
                 list.sinks[i].name);
    }
}

static void avrcp_peer_update(bool connected, const esp_bd_addr_t address)
{
    portENTER_CRITICAL(&s_avrcp_peer_mux);
    s_avrcp_peer_connected = connected;
    if (connected && address != NULL) {
        (void)memcpy(s_avrcp_peer, address, ESP_BD_ADDR_LEN);
    } else {
        (void)memset(s_avrcp_peer, 0, sizeof(s_avrcp_peer));
    }
    portEXIT_CRITICAL(&s_avrcp_peer_mux);
}

static bool avrcp_peer_snapshot(esp_bd_addr_t address)
{
    bool connected;

    portENTER_CRITICAL(&s_avrcp_peer_mux);
    connected = s_avrcp_peer_connected;
    if (connected && address != NULL) {
        (void)memcpy(address, s_avrcp_peer, ESP_BD_ADDR_LEN);
    }
    portEXIT_CRITICAL(&s_avrcp_peer_mux);
    return connected;
}

static bool avrcp_forwarding_allowed(void)
{
    esp_bd_addr_t peer;

    return avrcp_peer_snapshot(peer) && saved_peer_matches(peer) &&
           atomic_load_explicit(&s_peer_connected, memory_order_acquire) &&
           (atomic_load_explicit(&s_pcm_format, memory_order_acquire) != 0U);
}

#if CONFIG_LARKBRIDGE_TEST_IMAGE
/* Astra case 4: drop the next DISCONNECTED / DISCOVERY_STOPPED as a queue overflow would. */
static atomic_bool s_test_drop_disconnected;
static atomic_bool s_test_drop_discovery_stopped;

static bool test_drop_event(const bt_event_t *event) {
    atomic_bool *flag = event->type == BT_EVENT_DISCONNECTED ? &s_test_drop_disconnected
                        : event->type == BT_EVENT_DISCOVERY_STOPPED ? &s_test_drop_discovery_stopped
                                                                    : NULL;
    if (flag == NULL || !atomic_exchange_explicit(flag, false, memory_order_acq_rel)) {
        return false;
    }
    ESP_LOGE(TAG, "TEST: dropped Bluetooth event %d (simulated queue overflow)", (int)event->type);
    (void)output_control_report_fault(BRIDGE_FAULT_BLUETOOTH);
    return true;
}

bool output_bluetooth_test_fault(const char *kind) {
    if (strcmp(kind, "bt_drop_disc") == 0) {
        atomic_store_explicit(&s_test_drop_disconnected, true, memory_order_release);
    } else if (strcmp(kind, "bt_drop_dstop") == 0) {
        atomic_store_explicit(&s_test_drop_discovery_stopped, true, memory_order_release);
    } else if (strcmp(kind, "bt_disconnect") == 0) {
        /* Stands in for sink power loss: the link drops without a worker request. */
        esp_bd_addr_t peer;
        if (!avrcp_peer_snapshot(peer) && !saved_peer_snapshot(peer)) {
            return false;
        }
        esp_err_t result = esp_a2d_source_disconnect(peer);
        ESP_LOGW(TAG, "TEST: A2DP source disconnect requested: %s", esp_err_to_name(result));
        return result == ESP_OK;
    } else {
        return false;
    }
    ESP_LOGW(TAG, "TEST: next %s event will be dropped", kind + 8);
    return true;
}

bool output_bluetooth_test_forget(void) {
    if (s_event_queue == NULL) {
        return false;
    }
    bt_event_t event = {.type = BT_EVENT_TEST_FORGET};
    return xQueueSend(s_event_queue, &event, 0U) == pdTRUE;
}
#endif

static void post_event(const bt_event_t *event) {
#if CONFIG_LARKBRIDGE_TEST_IMAGE
    if (test_drop_event(event)) {
        return;
    }
#endif
    if (s_event_queue == NULL || xQueueSend(s_event_queue, event, 0U) != pdTRUE) {
        ESP_LOGE(TAG, "Bluetooth event queue overflow");
        (void)output_control_report_fault(BRIDGE_FAULT_BLUETOOTH);
    }
}

static bool supported_avrcp_key(uint8_t key_code) {
    switch (key_code) {
    case ESP_AVRC_PT_CMD_PLAY:
    case ESP_AVRC_PT_CMD_STOP:
    case ESP_AVRC_PT_CMD_PAUSE:
    case ESP_AVRC_PT_CMD_FORWARD:
    case ESP_AVRC_PT_CMD_BACKWARD:
    case ESP_AVRC_PT_CMD_VOL_UP:
    case ESP_AVRC_PT_CMD_VOL_DOWN:
        return true;
    default:
        return false;
    }
}

static void configure_avrcp_filter(void) {
    static const esp_avrc_pt_cmd_t desired[] = {
        ESP_AVRC_PT_CMD_PLAY,     ESP_AVRC_PT_CMD_STOP,     ESP_AVRC_PT_CMD_PAUSE,
        ESP_AVRC_PT_CMD_FORWARD,  ESP_AVRC_PT_CMD_BACKWARD, ESP_AVRC_PT_CMD_VOL_UP,
        ESP_AVRC_PT_CMD_VOL_DOWN,
    };
    esp_avrc_psth_bit_mask_t allowed = {0};
    esp_avrc_psth_bit_mask_t supported = {0};
    size_t index;

    if (esp_avrc_tg_get_psth_cmd_filter(ESP_AVRC_PSTH_FILTER_ALLOWED_CMD, &allowed) != ESP_OK) {
        ESP_LOGE(TAG, "cannot read AVRCP passthrough filter");
        return;
    }
    for (index = 0U; index < sizeof(desired) / sizeof(desired[0]); ++index) {
        if (esp_avrc_psth_bit_mask_operation(
                ESP_AVRC_BIT_MASK_OP_TEST, &allowed, desired[index])) {
            (void)esp_avrc_psth_bit_mask_operation(
                ESP_AVRC_BIT_MASK_OP_SET, &supported, desired[index]);
        }
    }
    if (esp_avrc_tg_set_psth_cmd_filter(ESP_AVRC_PSTH_FILTER_SUPPORTED_CMD, &supported) !=
        ESP_OK) {
        ESP_LOGE(TAG, "cannot set AVRCP passthrough filter");
    }
}

static void avrcp_callback(esp_avrc_tg_cb_event_t event, esp_avrc_tg_cb_param_t *parameter) {
    if (parameter == NULL) {
        return;
    }
    switch (event) {
    case ESP_AVRC_TG_PASSTHROUGH_CMD_EVT:
        if (supported_avrcp_key(parameter->psth_cmd.key_code) &&
            avrcp_forwarding_allowed()) {
            /* Forward one atomic tap on the car's press edge. The S31 emits
             * the paired Pixel press/release locally, eliminating a stuck-key
             * failure if UART or either board resets between edge packets. */
            if (parameter->psth_cmd.key_state == ESP_AVRC_PT_CMD_STATE_PRESSED) {
                (void)output_control_send_avrcp(parameter->psth_cmd.key_code,
                                                BRIDGE_KEY_TAP);
            }
        } else if (supported_avrcp_key(parameter->psth_cmd.key_code)) {
            ESP_LOGW(TAG, "ignoring AVRCP command from an unqualified output peer");
        }
        break;
    case ESP_AVRC_TG_PROF_STATE_EVT:
        if (parameter->avrc_tg_init_stat.state == ESP_AVRC_INIT_SUCCESS) {
            configure_avrcp_filter();
            output_avrcp_meta_configure_tg();
        }
        break;
    case ESP_AVRC_TG_CONNECTION_STATE_EVT:
        avrcp_peer_update(parameter->conn_stat.connected, parameter->conn_stat.remote_bda);
        output_avrcp_meta_on_connection(parameter->conn_stat.connected);
        if (parameter->conn_stat.connected &&
            !saved_peer_allows(parameter->conn_stat.remote_bda)) {
            ESP_LOGW(TAG, "rejecting AVRCP session from a non-selected output peer");
            (void)esp_bt_gap_remove_bond_device(parameter->conn_stat.remote_bda);
        }
        break;
    case ESP_AVRC_TG_REGISTER_NOTIFICATION_EVT:
        output_avrcp_meta_on_register(parameter->reg_ntf.event_id,
                                      parameter->reg_ntf.event_parameter);
        break;
    case ESP_AVRC_TG_REMOTE_FEATURES_EVT:
    case ESP_AVRC_TG_SET_ABSOLUTE_VOLUME_CMD_EVT:
    case ESP_AVRC_TG_SET_PLAYER_APP_VALUE_EVT:
        break;
    default:
        break;
    }
}

static atomic_uint_fast32_t s_last_pull_ms;

static int32_t a2dp_data_callback(uint8_t *data, int32_t length) {
    if (length > 0) {
        atomic_store_explicit(&s_last_pull_ms, now_ms(), memory_order_relaxed);
    }
    if (length < 0) {
        output_control_invalidate_local_audio();
        return 0;
    }
    if (data == NULL || length == 0) {
        return 0;
    }
    (void)memset(data, 0, (size_t)length);
    unsigned int format = atomic_load_explicit(&s_pcm_format, memory_order_acquire);
    if (format != 0U && output_control_audio_allowed()) {
        (void)output_audio_pull(data, (size_t)length, format >> 1U,
                                (format & 1U) != 0U);
    }
    return length;
}

static void a2dp_callback(esp_a2d_cb_event_t event, esp_a2d_cb_param_t *parameter) {
    bt_event_t queued = {0};

    if (parameter == NULL) {
        return;
    }
    switch (event) {
    case ESP_A2D_CONNECTION_STATE_EVT:
        if (parameter->conn_stat.state == ESP_A2D_CONNECTION_STATE_CONNECTED) {
            queued.type = BT_EVENT_CONNECTED;
        } else if (parameter->conn_stat.state == ESP_A2D_CONNECTION_STATE_DISCONNECTED) {
            queued.type = BT_EVENT_DISCONNECTED;
        } else {
            return;
        }
        (void)memcpy(queued.address, parameter->conn_stat.remote_bda, ESP_BD_ADDR_LEN);
        queued.connection_handle = parameter->conn_stat.conn_hdl;
        post_event(&queued);
        break;
    case ESP_A2D_AUDIO_STATE_EVT:
        queued.type = parameter->audio_stat.state == ESP_A2D_AUDIO_STATE_STARTED
                          ? BT_EVENT_AUDIO_STARTED
                          : BT_EVENT_AUDIO_SUSPENDED;
        (void)memcpy(queued.address, parameter->audio_stat.remote_bda, ESP_BD_ADDR_LEN);
        queued.connection_handle = parameter->audio_stat.conn_hdl;
        post_event(&queued);
        break;
    case ESP_A2D_REPORT_SNK_CODEC_CAPS_EVT:
        queued.type = BT_EVENT_SINK_CAPABILITIES;
        queued.connection_handle = parameter->a2d_report_snk_codec_caps_stat.conn_hdl;
        queued.codec = parameter->a2d_report_snk_codec_caps_stat.mcc;
        post_event(&queued);
        break;
    case ESP_A2D_SRC_SET_PREF_MCC_EVT:
        queued.type = BT_EVENT_PREFERRED_CODEC_RESULT;
        queued.connection_handle = parameter->a2d_set_pref_mcc_stat.conn_hdl;
        queued.bt_status = parameter->a2d_set_pref_mcc_stat.set_status;
        post_event(&queued);
        break;
    case ESP_A2D_MEDIA_CTRL_ACK_EVT:
        queued.type = BT_EVENT_MEDIA_ACK;
        queued.media_command = parameter->media_ctrl_stat.cmd;
        queued.media_status = parameter->media_ctrl_stat.status;
        post_event(&queued);
        break;
    case ESP_A2D_PROF_STATE_EVT:
        if (parameter->a2d_prof_stat.init_state == ESP_A2D_INIT_SUCCESS) {
            apply_class_of_device("on A2DP init");
        }
        break;
    case ESP_A2D_AUDIO_CFG_EVT:
    case ESP_A2D_SEP_REG_STATE_EVT:
    case ESP_A2D_SNK_PSC_CFG_EVT:
    case ESP_A2D_SNK_SET_DELAY_VALUE_EVT:
    case ESP_A2D_SNK_GET_DELAY_VALUE_EVT:
    case ESP_A2D_REPORT_SNK_DELAY_VALUE_EVT:
        break;
    default:
        break;
    }
}

static bool name_from_discovery(
    const esp_bt_gap_cb_param_t *parameter, char *name, size_t name_capacity) {
    int index;
    uint8_t *eir = NULL;

    if (parameter == NULL || name == NULL || name_capacity == 0U) {
        return false;
    }
    name[0] = '\0';
    for (index = 0; index < parameter->disc_res.num_prop; ++index) {
        esp_bt_gap_dev_prop_t *property = &parameter->disc_res.prop[index];
        if (property->type == ESP_BT_GAP_DEV_PROP_BDNAME && property->val != NULL) {
            size_t length = (size_t)property->len;
            if (length >= name_capacity) {
                length = name_capacity - 1U;
            }
            (void)memcpy(name, property->val, length);
            name[length] = '\0';
            return true;
        }
        if (property->type == ESP_BT_GAP_DEV_PROP_EIR) {
            eir = property->val;
        }
    }
    if (eir != NULL) {
        uint8_t length = 0U;
        uint8_t *resolved =
            esp_bt_gap_resolve_eir_data(eir, ESP_BT_EIR_TYPE_CMPL_LOCAL_NAME, &length);
        if (resolved == NULL) {
            resolved = esp_bt_gap_resolve_eir_data(
                eir, ESP_BT_EIR_TYPE_SHORT_LOCAL_NAME, &length);
        }
        if (resolved != NULL) {
            size_t copy_length = length;
            if (copy_length >= name_capacity) {
                copy_length = name_capacity - 1U;
            }
            (void)memcpy(name, resolved, copy_length);
            name[copy_length] = '\0';
            return true;
        }
    }
    return false;
}

static void gap_callback(esp_bt_gap_cb_event_t event, esp_bt_gap_cb_param_t *parameter) {
    bt_event_t queued = {0};

    if (parameter == NULL) {
        return;
    }
    switch (event) {
    case ESP_BT_GAP_DISC_RES_EVT:
        if (pairing_open()) {
            char name[ESP_BT_GAP_MAX_BDNAME_LEN + 1U];
            uint32_t cod = 0U;
            int8_t rssi = -127;
            bool have_name = name_from_discovery(parameter, name, sizeof(name));
            for (int i = 0; i < parameter->disc_res.num_prop; ++i) {
                esp_bt_gap_dev_prop_t *property = &parameter->disc_res.prop[i];
                if (property->type == ESP_BT_GAP_DEV_PROP_COD && property->val != NULL &&
                    property->len >= (int)sizeof(uint32_t)) {
                    (void)memcpy(&cod, property->val, sizeof(cod));
                } else if (property->type == ESP_BT_GAP_DEV_PROP_RSSI && property->val != NULL &&
                           property->len >= 1) {
                    rssi = *(int8_t *)property->val;
                }
            }
            if (have_name) {
                name_cache_put(parameter->disc_res.bda, name);
            }
            if (output_sink_cod_is_audio(cod) && !(have_name && output_sink_name_is_self(name))) {
                queued.type = BT_EVENT_CANDIDATE;
                (void)memcpy(queued.address, parameter->disc_res.bda, ESP_BD_ADDR_LEN);
                queued.cod = cod;
                queued.rssi = rssi;
                post_event(&queued);
            }
        } else if (CONFIG_LARKBRIDGE_OUTPUT_PEER_NAME[0] != '\0') {
            char name[ESP_BT_GAP_MAX_BDNAME_LEN + 1U];
            if (name_from_discovery(parameter, name, sizeof(name)) &&
                strcmp(name, CONFIG_LARKBRIDGE_OUTPUT_PEER_NAME) == 0) {
                queued.type = BT_EVENT_DISCOVERY_MATCH;
                (void)memcpy(queued.address, parameter->disc_res.bda, ESP_BD_ADDR_LEN);
                post_event(&queued);
            }
        }
        break;
    case ESP_BT_GAP_DISC_STATE_CHANGED_EVT:
        if (parameter->disc_st_chg.state == ESP_BT_GAP_DISCOVERY_STOPPED) {
            queued.type = BT_EVENT_DISCOVERY_STOPPED;
            post_event(&queued);
        }
        break;
    case ESP_BT_GAP_PIN_REQ_EVT: {
        esp_bt_pin_code_t pin = {0};
        size_t length = strlen(CONFIG_LARKBRIDGE_OUTPUT_LEGACY_PIN);
        if (length == 0U || length > ESP_BT_PIN_CODE_LEN) {
            length = 4U;
            (void)memcpy(pin, "0000", length);
        } else {
            (void)memcpy(pin, CONFIG_LARKBRIDGE_OUTPUT_LEGACY_PIN, length);
        }
        if (pin_fallback_for(parameter->pin_req.bda)) {
            const char *fallback =
                strcmp(CONFIG_LARKBRIDGE_OUTPUT_LEGACY_PIN, "1234") == 0 ? "0000" : "1234";
            length = 4U;
            (void)memcpy(pin, fallback, length);
            ESP_LOGW(TAG, "legacy PIN fallback %s for " ADDR_FMT, fallback,
                     ADDR_ARGS(parameter->pin_req.bda));
        }
        bool allowed = saved_peer_allows(parameter->pin_req.bda);
        (void)esp_bt_gap_pin_reply(parameter->pin_req.bda, allowed,
                                   allowed ? (uint8_t)length : 0U, pin);
        break;
    }
    case ESP_BT_GAP_CFM_REQ_EVT:
        (void)esp_bt_gap_ssp_confirm_reply(
            parameter->cfm_req.bda,
            saved_peer_allows(parameter->cfm_req.bda));
        break;
    case ESP_BT_GAP_AUTH_CMPL_EVT:
        if (parameter->auth_cmpl.stat != ESP_BT_STATUS_SUCCESS) {
            ESP_LOGE(TAG, "pairing failed, status=%u", parameter->auth_cmpl.stat);
            (void)output_control_report_fault(BRIDGE_FAULT_BLUETOOTH);
            queued.type = BT_EVENT_AUTH_FAILED;
            (void)memcpy(queued.address, parameter->auth_cmpl.bda, ESP_BD_ADDR_LEN);
            post_event(&queued);
        } else if (!saved_peer_allows(parameter->auth_cmpl.bda)) {
            ESP_LOGE(TAG, "removing bond attempted by a non-selected output peer");
            (void)esp_bt_gap_remove_bond_device(parameter->auth_cmpl.bda);
        } else {
            name_cache_put(parameter->auth_cmpl.bda, (const char *)parameter->auth_cmpl.device_name);
            ESP_LOGI(TAG, "paired with " ADDR_FMT " '%s'", ADDR_ARGS(parameter->auth_cmpl.bda),
                     (const char *)parameter->auth_cmpl.device_name);
        }
        break;
    default:
        break;
    }
}

static bool select_sbc_codec(const esp_a2d_mcc_t *capabilities, bool fallback,
                             esp_a2d_mcc_t *preferred)
{
    if (capabilities->type != ESP_A2D_MCT_SBC) {
        return false;
    }
    const esp_a2d_cie_sbc_t *caps = &capabilities->cie.sbc_info;
    const output_sbc_t offered = {
        .samp_freq = caps->samp_freq, .ch_mode = caps->ch_mode,
        .block_len = caps->block_len, .num_subbands = caps->num_subbands,
        .alloc_mthd = caps->alloc_mthd,
        .min_bitpool = caps->min_bitpool, .max_bitpool = caps->max_bitpool,
    };
    output_sbc_t selected;
    if (!output_sbc_select(&offered, fallback, &selected)) {
        return false;
    }
    (void)memset(preferred, 0, sizeof(*preferred));
    preferred->type = ESP_A2D_MCT_SBC;
    preferred->cie.sbc_info = (esp_a2d_cie_sbc_t){
        .samp_freq = selected.samp_freq, .ch_mode = selected.ch_mode,
        .block_len = selected.block_len, .num_subbands = selected.num_subbands,
        .alloc_mthd = selected.alloc_mthd,
        .min_bitpool = selected.min_bitpool, .max_bitpool = selected.max_bitpool,
    };
    return true;
}

static bool request_sbc_codec(bool fallback)
{
    esp_a2d_mcc_t codec;
    if (!select_sbc_codec(&s_sink_caps, fallback, &codec)) {
        return false;
    }
    if (fallback && memcmp(&codec, &s_requested_codec, sizeof(codec)) == 0) {
        return false;
    }
    s_requested_codec = codec;
    s_codec_fallback = fallback;
    const esp_a2d_cie_sbc_t *sbc = &codec.cie.sbc_info;
    ESP_LOGI(TAG, "requesting SBC rate=%u mode=0x%x bitpool=%u-%u fallback=%u",
             sbc->samp_freq == ESP_A2D_SBC_CIE_SF_44K ? 44100U : 48000U,
             (unsigned int)sbc->ch_mode, (unsigned int)sbc->min_bitpool,
             (unsigned int)sbc->max_bitpool, (unsigned int)fallback);
    esp_err_t result = esp_a2d_source_set_pref_mcc(s_active_connection_handle, &codec);
    s_codec_request_pending = result == ESP_OK;
    if (!s_codec_request_pending) {
        ESP_LOGW(TAG, "SBC request failed: %s", esp_err_to_name(result));
    }
    return s_codec_request_pending;
}

/* Head units such as the Honda CR-V list only phones when they search, so
 * present as a smartphone offering audio + rendering services. Bluedroid's AV
 * registration (bta_av_main.c) rewrites the major class to Audio after
 * esp_a2d_source_init() returns, so this is re-applied once the profile is up
 * and every time pairing mode opens. */
static void apply_class_of_device(const char *why) {
    esp_bt_cod_t class_of_device = {
        .minor = 0x03, /* smartphone */
        .major = ESP_BT_COD_MAJOR_DEV_PHONE,
        .service = ESP_BT_COD_SRVC_AUDIO | ESP_BT_COD_SRVC_RENDERING,
    };
    esp_err_t error = esp_bt_gap_set_cod(class_of_device, ESP_BT_SET_COD_ALL);
    if (error != ESP_OK) {
        ESP_LOGW(TAG, "cannot set Class of Device (%s): %s", why, esp_err_to_name(error));
        return;
    }
    esp_bt_cod_t applied = {0};
    if (esp_bt_gap_get_cod(&applied) == ESP_OK) {
        uint32_t raw = ((uint32_t)applied.service << 13U) | ((uint32_t)applied.major << 8U) |
                       ((uint32_t)applied.minor << 2U);
        ESP_LOGI(TAG, "Class of Device 0x%06lx (major=0x%02x minor=0x%02x service=0x%03x) %s",
                 (unsigned long)raw, (unsigned int)applied.major, (unsigned int)applied.minor,
                 (unsigned int)applied.service, why);
    }
}

static void set_pairing_visibility(void) {
    esp_err_t error = esp_bt_gap_set_scan_mode(
        ESP_BT_CONNECTABLE,
        (pairing_open() || !saved_peer_snapshot(NULL)) ? ESP_BT_GENERAL_DISCOVERABLE
                                                       : ESP_BT_NON_DISCOVERABLE);
    if (error != ESP_OK) {
        ESP_LOGW(TAG, "cannot enter pairing scan mode: %s", esp_err_to_name(error));
    }
}

static void start_connection(const esp_bd_addr_t address, uint32_t current_ms) {
    esp_err_t error = esp_a2d_source_connect((uint8_t *)address);
    if (error == ESP_OK) {
        (void)memcpy(s_requested_peer, address, ESP_BD_ADDR_LEN);
        s_state = BT_STATE_CONNECTING;
        s_connect_deadline_ms = current_ms + CONNECT_TIMEOUT_MS;
        ESP_LOGI(TAG, "A2DP connection requested");
    } else {
        s_state = BT_STATE_WAITING;
        s_next_action_ms = current_ms + CONFIG_LARKBRIDGE_OUTPUT_RECONNECT_MS;
        ESP_LOGW(TAG, "A2DP connect request failed: %s", esp_err_to_name(error));
    }
}

static void begin_discovery(uint32_t current_ms) {
    uint32_t units =
        ((uint32_t)CONFIG_LARKBRIDGE_OUTPUT_DISCOVERY_SECONDS * 100U + 127U) / 128U;
    if (units < 1U) {
        units = 1U;
    }
    if (units > 0x30U) {
        units = 0x30U;
    }
    s_have_discovered_peer = false;
    if (esp_bt_gap_start_discovery(ESP_BT_INQ_MODE_GENERAL_INQUIRY, (uint8_t)units, 0U) ==
        ESP_OK) {
        s_state = BT_STATE_DISCOVERING;
        if (pairing_open()) {
            output_sink_candidates_init(&s_candidates);
            ESP_LOGI(TAG, "pairing inquiry: scanning for audio sinks (CoD major A/V)");
        } else {
            ESP_LOGI(TAG, "discovering target '%s'", CONFIG_LARKBRIDGE_OUTPUT_PEER_NAME);
        }
    } else {
        s_next_action_ms = current_ms + CONFIG_LARKBRIDGE_OUTPUT_RECONNECT_MS;
    }
}

static void handle_connected(const bt_event_t *event, uint32_t current_ms) {
    bool have_saved_peer = saved_peer_snapshot(NULL);

    if (s_state == BT_STATE_DISCONNECTING) {
        ESP_LOGW(TAG, "rejecting connection while the prior peer is disconnecting");
        (void)esp_a2d_source_disconnect((uint8_t *)event->address);
        return;
    }
    if ((s_state == BT_STATE_CONNECTING &&
         memcmp(s_requested_peer, event->address, ESP_BD_ADDR_LEN) != 0 &&
         !saved_peer_allows(event->address)) ||
        (!have_saved_peer && CONFIG_LARKBRIDGE_OUTPUT_PEER_NAME[0] != '\0' &&
         s_state != BT_STATE_CONNECTING && !pairing_open())) {
        esp_bd_addr_t rejected_address;
        (void)memcpy(rejected_address, event->address, ESP_BD_ADDR_LEN);
        ESP_LOGW(TAG, "rejecting unsolicited peer during an active connection attempt");
        (void)esp_bt_gap_remove_bond_device(rejected_address);
        (void)esp_a2d_source_disconnect((uint8_t *)event->address);
        return;
    }
    if (s_active_connection_valid &&
        (event->connection_handle != s_active_connection_handle ||
         memcmp(s_active_peer, event->address, ESP_BD_ADDR_LEN) != 0)) {
        ESP_LOGW(TAG, "rejecting a second concurrent output connection");
        (void)esp_a2d_source_disconnect((uint8_t *)event->address);
        return;
    }
    if (!saved_peer_allows(event->address)) {
        ESP_LOGW(TAG, "rejecting connection from a peer other than the saved sink");
        (void)esp_a2d_source_disconnect((uint8_t *)event->address);
        return;
    }
    if (s_state == BT_STATE_DISCOVERING) {
        (void)esp_bt_gap_cancel_discovery();
    }
    (void)memcpy(s_active_peer, event->address, ESP_BD_ADDR_LEN);
    s_active_connection_handle = event->connection_handle;
    s_active_connection_valid = true;
    s_media_ack_pending = false;
    s_media_ack_timeout_count = 0U;
    s_state = BT_STATE_CONNECTED;
    s_format_ready = false;
    s_codec_request_pending = false;
    s_codec_fallback = false;
    atomic_store_explicit(&s_pcm_format, 0U, memory_order_release);
    s_next_media_attempt_ms = current_ms;
    s_qualification_deadline_ms = current_ms + QUALIFICATION_TIMEOUT_MS;
    atomic_store_explicit(&s_peer_connected, true, memory_order_release);
    atomic_store_explicit(&s_audio_streaming, false, memory_order_release);
    (void)esp_bt_gap_set_scan_mode(ESP_BT_NON_CONNECTABLE, ESP_BT_NON_DISCOVERABLE);
    ESP_LOGI(TAG, "downstream A2DP peer connected: " ADDR_FMT "%s", ADDR_ARGS(event->address),
             pairing_open() ? " (pairing mode)" : "");
}

static void exit_pairing(const char *why)
{
    if (!atomic_exchange_explicit(&s_pairing_open, false, memory_order_acq_rel)) {
        return;
    }
    pin_fallback_set(NULL, false);
    output_sink_candidates_init(&s_candidates);
    if (s_state == BT_STATE_DISCOVERING) {
        (void)esp_bt_gap_cancel_discovery();
    }
    if (s_state != BT_STATE_CONNECTED) {
        set_pairing_visibility();
    }
    ESP_LOGI(TAG, "pairing mode off (%s)", why);
}

static void qualify_active_peer(void)
{
    if (!s_format_ready || !atomic_load_explicit(&s_audio_streaming, memory_order_acquire)) {
        return;
    }
    char name[OUTPUT_SINK_NAME_MAX];
    output_sink_list_t next;
    name_cache_get(s_active_peer, name);
    sinks_snapshot(&next);
    bool was_saved = output_sink_list_find(&next, s_active_peer) >= 0;
    if (output_sink_list_promote(&next, s_active_peer, name)) {
        esp_err_t result = output_peer_store_save_list(&next);
        if (result != ESP_OK) {
            ESP_LOGE(TAG, "qualified output peer could not be persisted: %s",
                     esp_err_to_name(result));
            (void)output_control_report_fault(BRIDGE_FAULT_BLUETOOTH);
            return;
        }
        sinks_commit(&next);
        ESP_LOGI(TAG, "%s sink " ADDR_FMT " '%s' to MRU slot 0 after codec and stream qualification",
                 was_saved ? "promoted" : "saved new", ADDR_ARGS(s_active_peer), next.sinks[0].name);
        log_sinks("updated");
    }
    s_rr_cursor = 0U;
    exit_pairing("sink connected and qualified");
}

static void reject_active_peer(const char *reason)
{
    ESP_LOGE(TAG, "%s; forgetting and disconnecting output peer", reason);
    if (saved_peer_matches(s_active_peer)) {
        output_sink_list_t next;
        sinks_snapshot(&next);
        (void)output_sink_list_remove(&next, s_active_peer);
        esp_err_t result = output_peer_store_save_list(&next);
        if (result != ESP_OK) {
            ESP_LOGW(TAG, "could not clear rejected peer: %s", esp_err_to_name(result));
        } else {
            sinks_commit(&next);
        }
    }
    (void)esp_bt_gap_remove_bond_device(s_active_peer);
    avrcp_peer_update(false, NULL);
    s_format_ready = false;
    atomic_store_explicit(&s_pcm_format, 0U, memory_order_release);
    atomic_store_explicit(&s_audio_streaming, false, memory_order_release);
    atomic_store_explicit(&s_peer_connected, false, memory_order_release);
    output_control_invalidate_local_audio();
    (void)output_control_report_fault(BRIDGE_FAULT_BLUETOOTH);
    (void)esp_a2d_source_disconnect(s_active_peer);
    s_media_ack_pending = false;
    s_state = BT_STATE_DISCONNECTING;
    s_connect_deadline_ms = now_ms() + CONNECT_TIMEOUT_MS;
}

static void recycle_active_connection(const char *reason)
{
    ESP_LOGW(TAG, "%s; disconnecting output peer for a bounded retry", reason);
    avrcp_peer_update(false, NULL);
    s_format_ready = false;
    atomic_store_explicit(&s_pcm_format, 0U, memory_order_release);
    atomic_store_explicit(&s_audio_streaming, false, memory_order_release);
    atomic_store_explicit(&s_peer_connected, false, memory_order_release);
    output_control_invalidate_local_audio();
    (void)output_control_report_fault(BRIDGE_FAULT_BLUETOOTH);
    (void)esp_a2d_source_disconnect(s_active_peer);
    s_media_ack_pending = false;
    s_state = BT_STATE_DISCONNECTING;
    s_connect_deadline_ms = now_ms() + CONNECT_TIMEOUT_MS;
}

static bool active_event_matches(const bt_event_t *event)
{
    return s_state == BT_STATE_CONNECTED && s_active_connection_valid &&
           event->connection_handle == s_active_connection_handle &&
           memcmp(event->address, s_active_peer, ESP_BD_ADDR_LEN) == 0;
}

static bool active_handle_matches(const bt_event_t *event)
{
    return s_state == BT_STATE_CONNECTED && s_active_connection_valid &&
           event->connection_handle == s_active_connection_handle;
}

static bool request_media_control(esp_a2d_media_ctrl_t command)
{
    if (s_media_ack_pending || esp_a2d_media_ctrl(command) != ESP_OK) {
        return false;
    }
    s_media_ack_pending = true;
    s_expected_media_ack = command;
    s_media_ack_deadline_ms = now_ms() + MEDIA_ACK_TIMEOUT_MS;
    return true;
}

static void handle_disconnected(const bt_event_t *event, uint32_t current_ms) {
    if (s_active_connection_valid &&
        (event->connection_handle != s_active_connection_handle ||
         memcmp(event->address, s_active_peer, ESP_BD_ADDR_LEN) != 0)) {
        ESP_LOGW(TAG, "ignoring stale output disconnect event");
        return;
    }
    if (!s_active_connection_valid) {
        if (s_state != BT_STATE_CONNECTING ||
            memcmp(event->address, s_requested_peer, ESP_BD_ADDR_LEN) != 0) {
            ESP_LOGW(TAG, "ignoring disconnect from an unrequested peer");
            return;
        }
    }
    s_state = BT_STATE_WAITING;
    s_active_connection_valid = false;
    avrcp_peer_update(false, NULL);
    s_media_ack_pending = false;
    s_media_ack_timeout_count = 0U;
    s_format_ready = false;
    s_have_discovered_peer = false;
    s_next_action_ms = current_ms + (pairing_open() ? PAIRING_RETRY_MS
                                                    : CONFIG_LARKBRIDGE_OUTPUT_RECONNECT_MS);
    atomic_store_explicit(&s_peer_connected, false, memory_order_release);
    atomic_store_explicit(&s_audio_streaming, false, memory_order_release);
    atomic_store_explicit(&s_pcm_format, 0U, memory_order_release);
    output_control_invalidate_local_audio();
    set_pairing_visibility();
    ESP_LOGW(TAG, "downstream A2DP peer disconnected: " ADDR_FMT, ADDR_ARGS(event->address));
}

static void drop_active_link(uint32_t current_ms)
{
    avrcp_peer_update(false, NULL);
    s_format_ready = false;
    s_media_ack_pending = false;
    s_media_ack_timeout_count = 0U;
    s_have_discovered_peer = false;
    atomic_store_explicit(&s_pcm_format, 0U, memory_order_release);
    atomic_store_explicit(&s_audio_streaming, false, memory_order_release);
    atomic_store_explicit(&s_peer_connected, false, memory_order_release);
    output_control_invalidate_local_audio();
    if (s_state == BT_STATE_CONNECTED && s_active_connection_valid) {
        (void)esp_a2d_source_disconnect(s_active_peer);
        s_state = BT_STATE_DISCONNECTING;
        s_connect_deadline_ms = current_ms + CONNECT_TIMEOUT_MS;
    } else if (s_state == BT_STATE_CONNECTING) {
        (void)esp_a2d_source_disconnect(s_requested_peer);
        s_state = BT_STATE_WAITING;
        s_next_action_ms = current_ms + PAIRING_RETRY_MS;
    } else if (s_state == BT_STATE_DISCOVERING) {
        (void)esp_bt_gap_cancel_discovery();
        s_state = BT_STATE_WAITING;
        s_next_action_ms = current_ms + PAIRING_RETRY_MS;
    }
}

static void enter_pairing(uint32_t current_ms, const char *why)
{
    output_sink_candidates_init(&s_candidates);
    pin_fallback_set(NULL, false);
    atomic_store_explicit(&s_pairing_deadline_ms, current_ms + PAIRING_WINDOW_MS,
                          memory_order_release);
    atomic_store_explicit(&s_pairing_open, true, memory_order_release);
    drop_active_link(current_ms);
    if (s_state == BT_STATE_WAITING) {
        s_next_action_ms = current_ms;
    }
    apply_class_of_device("on pairing");
    set_pairing_visibility();
    ESP_LOGI(TAG, "pairing mode on (%s) for %u s: discoverable as '%s' and scanning for sinks",
             why, PAIRING_WINDOW_MS / 1000U, CONFIG_LARKBRIDGE_OUTPUT_DEVICE_NAME);
}

static void wipe_all_sinks(uint32_t current_ms)
{
    output_sink_list_t empty;
    output_sink_list_init(&empty);
    esp_err_t result = output_peer_store_clear();
    if (result != ESP_OK) {
        ESP_LOGW(TAG, "could not erase saved sinks: %s", esp_err_to_name(result));
    }
    sinks_commit(&empty);
    s_rr_cursor = 0U;
    int bond_count = esp_bt_gap_get_bond_device_num();
    if (bond_count > 0) {
        if (bond_count > 8) {
            bond_count = 8;
        }
        esp_bd_addr_t bonds[8];
        if (esp_bt_gap_get_bond_device_list(&bond_count, bonds) == ESP_OK) {
            for (int i = 0; i < bond_count; ++i) {
                (void)esp_bt_gap_remove_bond_device(bonds[i]);
            }
        }
    }
    drop_active_link(current_ms);
    if (s_state == BT_STATE_WAITING) {
        s_next_action_ms = current_ms + CONFIG_LARKBRIDGE_OUTPUT_RECONNECT_MS;
    }
    set_pairing_visibility();
    ESP_LOGW(TAG, "factory wipe: all saved sinks and Bluetooth bonds removed");
}

static void forget_sink(const bt_event_t *event, uint32_t current_ms)
{
    output_sink_list_t next;
    esp_bd_addr_t address;
    sinks_snapshot(&next);
    int index = event->address_valid ? output_sink_list_find(&next, event->address) : event->index;
    if (index < 0 || index >= (int)next.count) {
        ESP_LOGW(TAG, "forget_sink: no such saved sink");
        return;
    }
    (void)memcpy(address, next.sinks[index].addr, ESP_BD_ADDR_LEN);
    (void)output_sink_list_remove_index(&next, (unsigned int)index);
    esp_err_t result = output_peer_store_save_list(&next);
    if (result != ESP_OK) {
        ESP_LOGW(TAG, "forget_sink: persist failed: %s", esp_err_to_name(result));
        return;
    }
    sinks_commit(&next);
    s_rr_cursor = 0U;
    (void)esp_bt_gap_remove_bond_device(address);
    if (s_active_connection_valid && memcmp(s_active_peer, address, ESP_BD_ADDR_LEN) == 0) {
        drop_active_link(current_ms);
    }
    set_pairing_visibility();
    ESP_LOGI(TAG, "forgot sink " ADDR_FMT, ADDR_ARGS(address));
    log_sinks("after forget");
}

static void handle_bt_event(const bt_event_t *event, uint32_t current_ms) {
    switch (event->type) {
    case BT_EVENT_DISCOVERY_MATCH:
        if (s_state == BT_STATE_DISCOVERING && !s_have_discovered_peer) {
            (void)memcpy(s_discovered_peer, event->address, ESP_BD_ADDR_LEN);
            s_have_discovered_peer = true;
            (void)esp_bt_gap_cancel_discovery();
        }
        break;
    case BT_EVENT_DISCOVERY_STOPPED:
        if (s_state == BT_STATE_DISCOVERING && pairing_open()) {
            ESP_LOGI(TAG, "pairing inquiry done: %u audio candidate(s)",
                     (unsigned int)s_candidates.count);
            for (unsigned int i = 0U; i < s_candidates.count; ++i) {
                ESP_LOGI(TAG, "candidate %u: " ADDR_FMT " rssi=%d cod=0x%06lx", i,
                         ADDR_ARGS(s_candidates.items[i].addr), (int)s_candidates.items[i].rssi,
                         (unsigned long)s_candidates.items[i].cod);
            }
            s_state = BT_STATE_WAITING;
            s_next_action_ms = current_ms;
        } else if (s_state == BT_STATE_DISCOVERING) {
            if (s_have_discovered_peer) {
                start_connection(s_discovered_peer, current_ms);
            } else {
                s_state = BT_STATE_WAITING;
                s_next_action_ms = current_ms + CONFIG_LARKBRIDGE_OUTPUT_RECONNECT_MS;
            }
        }
        break;
    case BT_EVENT_CONNECTED:
        handle_connected(event, current_ms);
        break;
    case BT_EVENT_DISCONNECTED:
        handle_disconnected(event, current_ms);
        break;
    case BT_EVENT_AUDIO_STARTED:
        if (!active_event_matches(event)) {
            ESP_LOGW(TAG, "ignoring stale audio-start event");
            break;
        }
        atomic_store_explicit(&s_last_pull_ms, now_ms(), memory_order_relaxed);
        atomic_store_explicit(&s_audio_streaming, true, memory_order_release);
        s_media_ack_pending = false;
        s_media_ack_timeout_count = 0U;
        qualify_active_peer();
        if (saved_peer_matches(s_active_peer)) {
            output_control_note_local_audio_started();
        } else {
            reject_active_peer("qualified output peer identity was not persisted");
        }
        break;
    case BT_EVENT_AUDIO_SUSPENDED:
        if (!active_event_matches(event)) {
            ESP_LOGW(TAG, "ignoring stale audio-suspend event");
            break;
        }
        atomic_store_explicit(&s_audio_streaming, false, memory_order_release);
        s_media_ack_pending = false;
        output_control_invalidate_local_audio();
        s_next_media_attempt_ms = current_ms + MEDIA_RETRY_MS;
        break;
    case BT_EVENT_SINK_CAPABILITIES:
        if (!active_handle_matches(event) || s_codec_request_pending || s_format_ready) {
            break;
        }
        s_sink_caps = event->codec;
        if (!request_sbc_codec(false) && !request_sbc_codec(true)) {
            recycle_active_connection("no usable SBC configuration request");
        }
        break;
    case BT_EVENT_PREFERRED_CODEC_RESULT: {
        if (!active_handle_matches(event) || !s_codec_request_pending) {
            break;
        }
        s_codec_request_pending = false;
        if (event->bt_status != ESP_BT_STATUS_SUCCESS) {
            ESP_LOGW(TAG, "SBC configuration rejected, status=%u",
                     (unsigned int)event->bt_status);
            if (s_codec_fallback || !request_sbc_codec(true)) {
                recycle_active_connection("SBC negotiation did not complete");
            }
            break;
        }
        const esp_a2d_cie_sbc_t *sbc = &s_requested_codec.cie.sbc_info;
        unsigned int rate = sbc->samp_freq == ESP_A2D_SBC_CIE_SF_44K ? 44100U : 48000U;
        unsigned int mono = sbc->ch_mode == ESP_A2D_SBC_CIE_CH_MODE_MONO ? 1U : 0U;
        s_format_ready = true;
        output_audio_flush();
        atomic_store_explicit(&s_pcm_format, (rate << 1U) | mono, memory_order_release);
        ESP_LOGI(TAG, "SBC format accepted: %u Hz, %u channel(s)", rate, mono ? 1U : 2U);
        s_next_media_attempt_ms = current_ms;
        qualify_active_peer();
        break;
    }
    case BT_EVENT_MEDIA_ACK:
        if (s_state != BT_STATE_CONNECTED || !s_media_ack_pending ||
            event->media_command != s_expected_media_ack) {
            ESP_LOGW(TAG, "ignoring stale/unexpected media ACK");
            break;
        }
        s_media_ack_pending = false;
        if (event->media_status != ESP_A2D_MEDIA_CTRL_ACK_SUCCESS) {
            if (s_media_ack_timeout_count < UINT8_MAX) {
                ++s_media_ack_timeout_count;
            }
            if (s_media_ack_timeout_count >= MEDIA_ACK_MAX_TIMEOUTS) {
                recycle_active_connection("repeated A2DP media-control failures");
            } else {
                s_next_media_attempt_ms = current_ms + MEDIA_RETRY_MS;
            }
        } else if (event->media_command == ESP_A2D_MEDIA_CTRL_CHECK_SRC_RDY) {
            if (!request_media_control(ESP_A2D_MEDIA_CTRL_START)) {
                s_next_media_attempt_ms = current_ms + MEDIA_RETRY_MS;
            }
        } else if (event->media_command == ESP_A2D_MEDIA_CTRL_START) {
            s_media_ack_timeout_count = 0U;
        } else if (event->media_command == ESP_A2D_MEDIA_CTRL_SUSPEND) {
            atomic_store_explicit(&s_audio_streaming, false, memory_order_release);
            s_next_media_attempt_ms = current_ms + MEDIA_RETRY_MS;
        }
        break;
    case BT_EVENT_CANDIDATE:
        if (s_state == BT_STATE_DISCOVERING && pairing_open()) {
            (void)output_sink_candidates_offer(&s_candidates, event->address, event->rssi,
                                               event->cod);
        }
        break;
    case BT_EVENT_AUTH_FAILED:
        if (pairing_open() && !pin_fallback_for(event->address)) {
            pin_fallback_set(event->address, true);
            for (unsigned int i = 0U; i < s_candidates.count; ++i) {
                if (memcmp(s_candidates.items[i].addr, event->address, ESP_BD_ADDR_LEN) == 0) {
                    s_candidates.items[i].attempted = false;
                }
            }
            ESP_LOGW(TAG, "auth failed for " ADDR_FMT "; retrying once with fallback PIN",
                     ADDR_ARGS(event->address));
        } else if (pin_fallback_for(event->address)) {
            pin_fallback_set(NULL, false);
        }
        break;
    case BT_EVENT_PAIR_REQUEST:
        enter_pairing(current_ms, "requested");
        break;
    case BT_EVENT_FACTORY_WIPE:
        wipe_all_sinks(current_ms);
        enter_pairing(current_ms, "factory wipe");
        break;
    case BT_EVENT_FORGET_SINK:
        forget_sink(event, current_ms);
        break;
#if CONFIG_LARKBRIDGE_TEST_IMAGE
    case BT_EVENT_TEST_FORGET:
        ESP_LOGW(TAG, "TEST: forgetting saved output peers and clearing BT bonds");
        wipe_all_sinks(current_ms);
        break;
#endif
    default:
        break;
    }
}

static void bluetooth_worker(void *context) {
    bt_event_t event;
    (void)context;

    s_next_action_ms = now_ms();
    uint32_t cod_reapply_ms = s_next_action_ms + 1500U;
    bool cod_reapplied = false;
    for (;;) {
        uint32_t current_ms;
        if (xQueueReceive(s_event_queue, &event, pdMS_TO_TICKS(100U)) == pdTRUE) {
            handle_bt_event(&event, now_ms());
        }
        current_ms = now_ms();
        if (!cod_reapplied && (int32_t)(current_ms - cod_reapply_ms) >= 0) {
            cod_reapplied = true;
            apply_class_of_device("after A2DP registration");
        }
        if (pairing_open() &&
            (int32_t)(current_ms - atomic_load_explicit(&s_pairing_deadline_ms,
                                                        memory_order_acquire)) >= 0) {
            bool was_discovering = s_state == BT_STATE_DISCOVERING;
            exit_pairing("window expired");
            if (was_discovering) {
                s_state = BT_STATE_WAITING;
                s_next_action_ms = current_ms + PAIRING_RETRY_MS;
            }
        }
        if (s_state == BT_STATE_WAITING && (int32_t)(current_ms - s_next_action_ms) >= 0) {
            output_sink_list_t sinks;
            sinks_snapshot(&sinks);
            if (pairing_open()) {
                int next = output_sink_candidates_next(&s_candidates);
                if (next >= 0) {
                    ESP_LOGI(TAG, "pairing: trying candidate " ADDR_FMT " rssi=%d",
                             ADDR_ARGS(s_candidates.items[next].addr),
                             (int)s_candidates.items[next].rssi);
                    start_connection(s_candidates.items[next].addr, current_ms);
                } else {
                    begin_discovery(current_ms);
                }
            } else if (sinks.count > 0U) {
                unsigned int index = s_rr_cursor % sinks.count;
                s_rr_cursor = output_sink_next_index(index, sinks.count);
                ESP_LOGI(TAG, "reconnect: MRU slot %u " ADDR_FMT " '%s'", index,
                         ADDR_ARGS(sinks.sinks[index].addr), sinks.sinks[index].name);
                start_connection(sinks.sinks[index].addr, current_ms);
            } else if (CONFIG_LARKBRIDGE_OUTPUT_PEER_NAME[0] != '\0') {
                begin_discovery(current_ms);
            } else {
                s_next_action_ms = current_ms + CONFIG_LARKBRIDGE_OUTPUT_RECONNECT_MS;
            }
        } else if (s_state == BT_STATE_CONNECTING &&
                   (int32_t)(current_ms - s_connect_deadline_ms) >= 0) {
            ESP_LOGW(TAG, "A2DP connection attempt timed out");
            s_state = BT_STATE_WAITING;
            s_next_action_ms = current_ms + (pairing_open() ? PAIRING_RETRY_MS
                                                            : CONFIG_LARKBRIDGE_OUTPUT_RECONNECT_MS);
        } else if (s_state == BT_STATE_DISCONNECTING &&
                   (int32_t)(current_ms - s_connect_deadline_ms) >= 0) {
            ESP_LOGW(TAG, "A2DP disconnect timed out; releasing stale peer identity");
            s_active_connection_valid = false;
            s_state = BT_STATE_WAITING;
            s_next_action_ms = current_ms + CONFIG_LARKBRIDGE_OUTPUT_RECONNECT_MS;
            set_pairing_visibility();
        } else if (s_state == BT_STATE_CONNECTED &&
                   atomic_load_explicit(&s_audio_streaming, memory_order_acquire) &&
                   /* Signed: the data callback may stamp a time after current_ms. */
                   (int32_t)(current_ms - (uint32_t)atomic_load_explicit(
                                              &s_last_pull_ms, memory_order_relaxed)) >=
                       (int32_t)A2DP_PULL_STALL_MS) {
            recycle_active_connection("A2DP source stopped pulling audio; link presumed lost");
        } else if (s_state == BT_STATE_CONNECTED &&
                   (!s_format_ready || !saved_peer_matches(s_active_peer)) &&
                   (int32_t)(current_ms - s_qualification_deadline_ms) >= 0) {
            recycle_active_connection("output peer did not qualify before timeout");
        } else if (s_state == BT_STATE_CONNECTED && s_media_ack_pending &&
                   (int32_t)(current_ms - s_media_ack_deadline_ms) >= 0) {
            s_media_ack_pending = false;
            if (s_media_ack_timeout_count < UINT8_MAX) {
                ++s_media_ack_timeout_count;
            }
            if (s_media_ack_timeout_count >= MEDIA_ACK_MAX_TIMEOUTS) {
                recycle_active_connection("repeated A2DP media-control ACK timeouts");
            } else {
                ESP_LOGW(TAG, "A2DP media-control ACK timed out; retrying");
                s_next_media_attempt_ms = current_ms;
            }
        } else if (s_state == BT_STATE_CONNECTED && s_format_ready &&
                   !atomic_load_explicit(&s_audio_streaming, memory_order_acquire) &&
                   (int32_t)(current_ms - s_next_media_attempt_ms) >= 0) {
            if (!request_media_control(ESP_A2D_MEDIA_CTRL_CHECK_SRC_RDY)) {
                s_next_media_attempt_ms = current_ms + MEDIA_RETRY_MS;
            } else {
                s_next_media_attempt_ms = current_ms + MEDIA_RETRY_MS;
            }
        }
    }
}

esp_err_t output_bluetooth_start(void) {
    esp_bt_controller_config_t controller_config = BT_CONTROLLER_INIT_CONFIG_DEFAULT();
    esp_bluedroid_config_t bluedroid_config = BT_BLUEDROID_INIT_CONFIG_DEFAULT();
    esp_bt_io_cap_t io_capability = ESP_BT_IO_CAP_NONE;
    esp_bt_pin_code_t pin = {0};
    size_t pin_length = strlen(CONFIG_LARKBRIDGE_OUTPUT_LEGACY_PIN);
    esp_err_t error;

    atomic_init(&s_stack_ready, false);
    atomic_init(&s_peer_connected, false);
    atomic_init(&s_audio_streaming, false);
    atomic_init(&s_pcm_format, 0U);
    s_active_connection_valid = false;
    avrcp_peer_update(false, NULL);
    s_media_ack_pending = false;
    s_media_ack_timeout_count = 0U;
    atomic_init(&s_pairing_open, false);
    atomic_init(&s_pairing_deadline_ms, 0U);
    output_sink_candidates_init(&s_candidates);
    s_event_queue = xQueueCreate(BT_EVENT_QUEUE_LENGTH, sizeof(bt_event_t));
    if (s_event_queue == NULL) {
        return ESP_ERR_NO_MEM;
    }
    output_sink_list_t loaded;
    bool migrated = false;
    error = output_peer_store_load_list(&loaded, &migrated);
    if (error != ESP_OK) {
        ESP_LOGW(TAG, "cannot load saved sinks: %s", esp_err_to_name(error));
        output_sink_list_init(&loaded);
    }
    sinks_commit(&loaded);
    if (migrated) {
        ESP_LOGI(TAG, "migrated legacy peer_bda " ADDR_FMT " into peer_list MRU slot 0",
                 ADDR_ARGS(loaded.sinks[0].addr));
    }
    log_sinks(migrated ? "after legacy migration" : "loaded");

    error = esp_bt_controller_mem_release(ESP_BT_MODE_BLE);
    if (error != ESP_OK) {
        return error;
    }
    error = esp_bt_controller_init(&controller_config);
    if (error != ESP_OK) {
        return error;
    }
    error = esp_bt_controller_enable(ESP_BT_MODE_CLASSIC_BT);
    if (error != ESP_OK) {
        return error;
    }
    error = esp_bluedroid_init_with_cfg(&bluedroid_config);
    if (error != ESP_OK) {
        return error;
    }
    error = esp_bluedroid_enable();
    if (error != ESP_OK) {
        return error;
    }

    error = esp_bt_gap_register_callback(gap_callback);
    if (error != ESP_OK) {
        return error;
    }
    error = esp_bt_gap_set_device_name(CONFIG_LARKBRIDGE_OUTPUT_DEVICE_NAME);
    if (error != ESP_OK) {
        return error;
    }
    (void)esp_bt_gap_set_security_param(
        ESP_BT_SP_IOCAP_MODE, &io_capability, sizeof(io_capability));
    if (pin_length == 0U || pin_length > ESP_BT_PIN_CODE_LEN) {
        pin_length = 4U;
        (void)memcpy(pin, "0000", pin_length);
    } else {
        (void)memcpy(pin, CONFIG_LARKBRIDGE_OUTPUT_LEGACY_PIN, pin_length);
    }
    (void)esp_bt_gap_set_pin(ESP_BT_PIN_TYPE_FIXED, (uint8_t)pin_length, pin);

    error = output_avrcp_meta_init();
    if (error != ESP_OK) {
        ESP_LOGW(TAG, "AVRCP metadata init failed: %s", esp_err_to_name(error));
    }
    error = esp_avrc_tg_register_callback(avrcp_callback);
    if (error != ESP_OK) {
        return error;
    }
    error = esp_avrc_tg_init();
    if (error != ESP_OK) {
        return error;
    }
    error = esp_a2d_register_callback(a2dp_callback);
    if (error != ESP_OK) {
        return error;
    }
    error = esp_a2d_source_register_data_callback(a2dp_data_callback);
    if (error != ESP_OK) {
        return error;
    }
    error = esp_a2d_source_init();
    if (error != ESP_OK) {
        return error;
    }
    set_pairing_visibility();
    if (xTaskCreate(bluetooth_worker, "output_bt", 6144U, NULL, 12U, NULL) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }

    atomic_store_explicit(&s_stack_ready, true, memory_order_release);
    ESP_LOGI(TAG, "Bluetooth Classic ready as '%s'%s", CONFIG_LARKBRIDGE_OUTPUT_DEVICE_NAME,
             saved_peer_snapshot(NULL) ? "; saved sinks loaded" : "; no saved sinks");
    if (!saved_peer_snapshot(NULL) && CONFIG_LARKBRIDGE_OUTPUT_PEER_NAME[0] == '\0') {
        (void)output_bluetooth_request_pairing();
    }
    return ESP_OK;
}

static bool queue_control_event(const bt_event_t *event)
{
    return s_event_queue != NULL && xQueueSend(s_event_queue, event, 0U) == pdTRUE;
}

bool output_bluetooth_request_pairing(void)
{
    bt_event_t event = {.type = BT_EVENT_PAIR_REQUEST};
    return queue_control_event(&event);
}

bool output_bluetooth_request_factory_wipe(void)
{
    bt_event_t event = {.type = BT_EVENT_FACTORY_WIPE};
    return queue_control_event(&event);
}

bool output_bluetooth_forget_sink(int index, const uint8_t *address)
{
    bt_event_t event = {.type = BT_EVENT_FORGET_SINK, .index = index};
    if (address != NULL) {
        (void)memcpy(event.address, address, ESP_BD_ADDR_LEN);
        event.address_valid = true;
    }
    return queue_control_event(&event);
}

void output_bluetooth_get_sinks(output_bluetooth_sinks_t *status)
{
    if (status == NULL) {
        return;
    }
    sinks_snapshot(&status->sinks);
    status->pairing = pairing_open();
    uint32_t remaining =
        atomic_load_explicit(&s_pairing_deadline_ms, memory_order_acquire) - now_ms();
    status->pairing_remaining_ms =
        status->pairing && (int32_t)remaining > 0 ? remaining : 0U;
    status->connected = atomic_load_explicit(&s_peer_connected, memory_order_acquire);
    status->streaming = output_bluetooth_audio_streaming();
}

bool output_bluetooth_stack_ready(void) {
    return atomic_load_explicit(&s_stack_ready, memory_order_acquire);
}

bool output_bluetooth_peer_connected(void) {
    return atomic_load_explicit(&s_peer_connected, memory_order_acquire);
}

bool output_bluetooth_audio_streaming(void) {
    return atomic_load_explicit(&s_audio_streaming, memory_order_acquire) &&
           (atomic_load_explicit(&s_pcm_format, memory_order_acquire) != 0U);
}
