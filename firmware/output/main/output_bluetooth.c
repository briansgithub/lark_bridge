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
#include "output_control.h"
#include "output_peer_store.h"

#include <stdatomic.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#define BT_EVENT_QUEUE_LENGTH 16U
#define CONNECT_TIMEOUT_MS 15000U
#define QUALIFICATION_TIMEOUT_MS 20000U
#define MEDIA_RETRY_MS 1000U
#define MEDIA_ACK_TIMEOUT_MS 1500U
#define MEDIA_ACK_MAX_TIMEOUTS 3U

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
    BT_EVENT_MEDIA_ACK
} bt_event_type_t;

typedef struct {
    bt_event_type_t type;
    esp_bd_addr_t address;
    esp_a2d_conn_hdl_t connection_handle;
    esp_a2d_mcc_t codec;
    esp_a2d_media_ctrl_t media_command;
    esp_a2d_media_ctrl_ack_t media_status;
    esp_bt_status_t bt_status;
} bt_event_t;

static const char *TAG = "output_bt";
static QueueHandle_t s_event_queue;
static atomic_bool s_stack_ready;
static atomic_bool s_peer_connected;
static atomic_bool s_audio_streaming;
static atomic_bool s_format_qualified;
static bt_connection_state_t s_state = BT_STATE_WAITING;
static portMUX_TYPE s_saved_peer_mux = portMUX_INITIALIZER_UNLOCKED;
static portMUX_TYPE s_avrcp_peer_mux = portMUX_INITIALIZER_UNLOCKED;
static esp_bd_addr_t s_saved_peer;
static esp_bd_addr_t s_active_peer;
static esp_bd_addr_t s_requested_peer;
static esp_bd_addr_t s_discovered_peer;
static esp_bd_addr_t s_avrcp_peer;
static bool s_have_saved_peer;
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

static uint32_t now_ms(void) {
    return (uint32_t)(esp_timer_get_time() / 1000);
}

static bool saved_peer_snapshot(esp_bd_addr_t address)
{
    bool present;

    portENTER_CRITICAL(&s_saved_peer_mux);
    present = s_have_saved_peer;
    if (present && address != NULL) {
        (void)memcpy(address, s_saved_peer, ESP_BD_ADDR_LEN);
    }
    portEXIT_CRITICAL(&s_saved_peer_mux);
    return present;
}

static bool saved_peer_matches(const esp_bd_addr_t address)
{
    bool matches;

    portENTER_CRITICAL(&s_saved_peer_mux);
    matches = s_have_saved_peer &&
              memcmp(s_saved_peer, address, ESP_BD_ADDR_LEN) == 0;
    portEXIT_CRITICAL(&s_saved_peer_mux);
    return matches;
}

static bool saved_peer_allows(const esp_bd_addr_t address)
{
    bool allowed;

    portENTER_CRITICAL(&s_saved_peer_mux);
    allowed = !s_have_saved_peer ||
              memcmp(s_saved_peer, address, ESP_BD_ADDR_LEN) == 0;
    portEXIT_CRITICAL(&s_saved_peer_mux);
    return allowed;
}

static void saved_peer_set(const esp_bd_addr_t address)
{
    portENTER_CRITICAL(&s_saved_peer_mux);
    (void)memcpy(s_saved_peer, address, ESP_BD_ADDR_LEN);
    s_have_saved_peer = true;
    portEXIT_CRITICAL(&s_saved_peer_mux);
}

static void saved_peer_clear(void)
{
    portENTER_CRITICAL(&s_saved_peer_mux);
    s_have_saved_peer = false;
    (void)memset(s_saved_peer, 0, sizeof(s_saved_peer));
    portEXIT_CRITICAL(&s_saved_peer_mux);
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
           atomic_load_explicit(&s_format_qualified, memory_order_acquire);
}

static void post_event(const bt_event_t *event) {
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
        }
        break;
    case ESP_AVRC_TG_CONNECTION_STATE_EVT:
        avrcp_peer_update(parameter->conn_stat.connected, parameter->conn_stat.remote_bda);
        if (parameter->conn_stat.connected &&
            !saved_peer_allows(parameter->conn_stat.remote_bda)) {
            ESP_LOGW(TAG, "rejecting AVRCP session from a non-selected output peer");
            (void)esp_bt_gap_remove_bond_device(parameter->conn_stat.remote_bda);
        }
        break;
    case ESP_AVRC_TG_REMOTE_FEATURES_EVT:
    case ESP_AVRC_TG_SET_ABSOLUTE_VOLUME_CMD_EVT:
    case ESP_AVRC_TG_REGISTER_NOTIFICATION_EVT:
    case ESP_AVRC_TG_SET_PLAYER_APP_VALUE_EVT:
        break;
    default:
        break;
    }
}

static int32_t a2dp_data_callback(uint8_t *data, int32_t length) {
    if (length < 0) {
        output_control_invalidate_local_audio();
        return 0;
    }
    if (data == NULL || length == 0) {
        return 0;
    }
    (void)memset(data, 0, (size_t)length);
    if (atomic_load_explicit(&s_format_qualified, memory_order_acquire) &&
        output_control_audio_allowed()) {
        (void)output_audio_pull(data, (size_t)length);
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
    case ESP_A2D_AUDIO_CFG_EVT:
    case ESP_A2D_PROF_STATE_EVT:
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
        if (CONFIG_LARKBRIDGE_OUTPUT_PEER_NAME[0] != '\0') {
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
        } else if (!saved_peer_allows(parameter->auth_cmpl.bda)) {
            ESP_LOGE(TAG, "removing bond attempted by a non-selected output peer");
            (void)esp_bt_gap_remove_bond_device(parameter->auth_cmpl.bda);
        }
        break;
    default:
        break;
    }
}

static bool select_48k_stereo_codec(const esp_a2d_mcc_t *capabilities, esp_a2d_mcc_t *preferred) {
    const esp_a2d_cie_sbc_t *caps;
    esp_a2d_cie_sbc_t *config;

    if (capabilities == NULL || preferred == NULL || capabilities->type != ESP_A2D_MCT_SBC) {
        return false;
    }
    caps = &capabilities->cie.sbc_info;
    if ((caps->samp_freq & ESP_A2D_SBC_CIE_SF_48K) == 0U ||
        (caps->ch_mode &
         (ESP_A2D_SBC_CIE_CH_MODE_JOINT_STEREO | ESP_A2D_SBC_CIE_CH_MODE_STEREO)) == 0U) {
        return false;
    }
    (void)memset(preferred, 0, sizeof(*preferred));
    preferred->type = ESP_A2D_MCT_SBC;
    config = &preferred->cie.sbc_info;
    config->samp_freq = ESP_A2D_SBC_CIE_SF_48K;
    config->ch_mode = (caps->ch_mode & ESP_A2D_SBC_CIE_CH_MODE_JOINT_STEREO) != 0U
                          ? ESP_A2D_SBC_CIE_CH_MODE_JOINT_STEREO
                          : ESP_A2D_SBC_CIE_CH_MODE_STEREO;
    if ((caps->block_len & ESP_A2D_SBC_CIE_BLOCK_LEN_16) != 0U) {
        config->block_len = ESP_A2D_SBC_CIE_BLOCK_LEN_16;
    } else if ((caps->block_len & ESP_A2D_SBC_CIE_BLOCK_LEN_12) != 0U) {
        config->block_len = ESP_A2D_SBC_CIE_BLOCK_LEN_12;
    } else if ((caps->block_len & ESP_A2D_SBC_CIE_BLOCK_LEN_8) != 0U) {
        config->block_len = ESP_A2D_SBC_CIE_BLOCK_LEN_8;
    } else {
        config->block_len = ESP_A2D_SBC_CIE_BLOCK_LEN_4;
    }
    config->num_subbands = (caps->num_subbands & ESP_A2D_SBC_CIE_NUM_SUBBANDS_8) != 0U
                               ? ESP_A2D_SBC_CIE_NUM_SUBBANDS_8
                               : ESP_A2D_SBC_CIE_NUM_SUBBANDS_4;
    config->alloc_mthd = (caps->alloc_mthd & ESP_A2D_SBC_CIE_ALLOC_MTHD_LOUDNESS) != 0U
                             ? ESP_A2D_SBC_CIE_ALLOC_MTHD_LOUDNESS
                             : ESP_A2D_SBC_CIE_ALLOC_MTHD_SNR;
    config->min_bitpool = caps->min_bitpool;
    config->max_bitpool = caps->max_bitpool > 53U ? 53U : caps->max_bitpool;
    return config->min_bitpool <= config->max_bitpool;
}

static void set_pairing_visibility(void) {
    esp_err_t error = esp_bt_gap_set_scan_mode(
        ESP_BT_CONNECTABLE,
        saved_peer_snapshot(NULL) ? ESP_BT_NON_DISCOVERABLE : ESP_BT_GENERAL_DISCOVERABLE);
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
        ESP_LOGI(TAG, "discovering target '%s'", CONFIG_LARKBRIDGE_OUTPUT_PEER_NAME);
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
         memcmp(s_requested_peer, event->address, ESP_BD_ADDR_LEN) != 0) ||
        (!have_saved_peer && CONFIG_LARKBRIDGE_OUTPUT_PEER_NAME[0] != '\0' &&
         s_state != BT_STATE_CONNECTING)) {
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
    (void)memcpy(s_active_peer, event->address, ESP_BD_ADDR_LEN);
    s_active_connection_handle = event->connection_handle;
    s_active_connection_valid = true;
    s_media_ack_pending = false;
    s_media_ack_timeout_count = 0U;
    s_state = BT_STATE_CONNECTED;
    s_format_ready = false;
    atomic_store_explicit(&s_format_qualified, false, memory_order_release);
    s_next_media_attempt_ms = current_ms;
    s_qualification_deadline_ms = current_ms + QUALIFICATION_TIMEOUT_MS;
    atomic_store_explicit(&s_peer_connected, true, memory_order_release);
    atomic_store_explicit(&s_audio_streaming, false, memory_order_release);
    (void)esp_bt_gap_set_scan_mode(ESP_BT_NON_CONNECTABLE, ESP_BT_NON_DISCOVERABLE);
    ESP_LOGI(TAG, "downstream A2DP peer connected");
}

static void qualify_active_peer(void)
{
    if (saved_peer_snapshot(NULL) || !s_format_ready ||
        !atomic_load_explicit(&s_audio_streaming, memory_order_acquire)) {
        return;
    }
    esp_err_t result = output_peer_store_save(s_active_peer);
    if (result != ESP_OK) {
        ESP_LOGE(TAG, "qualified output peer could not be persisted: %s",
                 esp_err_to_name(result));
        (void)output_control_report_fault(BRIDGE_FAULT_BLUETOOTH);
        return;
    }
    saved_peer_set(s_active_peer);
    ESP_LOGI(TAG, "persisted output peer after codec and stream qualification");
}

static void reject_active_peer(const char *reason)
{
    ESP_LOGE(TAG, "%s; forgetting and disconnecting output peer", reason);
    if (saved_peer_matches(s_active_peer)) {
        esp_err_t result = output_peer_store_clear();
        if (result != ESP_OK) {
            ESP_LOGW(TAG, "could not clear rejected peer: %s", esp_err_to_name(result));
        } else {
            saved_peer_clear();
        }
    }
    (void)esp_bt_gap_remove_bond_device(s_active_peer);
    avrcp_peer_update(false, NULL);
    s_format_ready = false;
    atomic_store_explicit(&s_format_qualified, false, memory_order_release);
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
    atomic_store_explicit(&s_format_qualified, false, memory_order_release);
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
    s_next_action_ms = current_ms + CONFIG_LARKBRIDGE_OUTPUT_RECONNECT_MS;
    atomic_store_explicit(&s_peer_connected, false, memory_order_release);
    atomic_store_explicit(&s_audio_streaming, false, memory_order_release);
    atomic_store_explicit(&s_format_qualified, false, memory_order_release);
    output_control_invalidate_local_audio();
    set_pairing_visibility();
    ESP_LOGW(TAG, "downstream A2DP peer disconnected");
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
        if (s_state == BT_STATE_DISCOVERING) {
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
    case BT_EVENT_SINK_CAPABILITIES: {
        esp_a2d_mcc_t preferred;
        if (!active_handle_matches(event)) {
            ESP_LOGW(TAG, "ignoring stale sink-capabilities event");
            break;
        }
        if (!select_48k_stereo_codec(&event->codec, &preferred)) {
            reject_active_peer("sink does not support required 48-kHz stereo SBC");
        } else {
            esp_err_t error =
                esp_a2d_source_set_pref_mcc(event->connection_handle, &preferred);
            if (error != ESP_OK) {
                ESP_LOGE(TAG, "cannot select 48-kHz stereo SBC: %s", esp_err_to_name(error));
                reject_active_peer("preferred-codec request failed");
            }
        }
        break;
    }
    case BT_EVENT_PREFERRED_CODEC_RESULT:
        if (!active_handle_matches(event)) {
            ESP_LOGW(TAG, "ignoring stale preferred-codec event");
            break;
        }
        s_format_ready = event->bt_status == ESP_BT_STATUS_SUCCESS;
        atomic_store_explicit(&s_format_qualified, s_format_ready, memory_order_release);
        if (!s_format_ready) {
            ESP_LOGE(TAG, "sink rejected required SBC format, status=%u", event->bt_status);
            reject_active_peer("sink rejected required SBC format");
        } else {
            s_next_media_attempt_ms = current_ms;
            qualify_active_peer();
        }
        break;
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
    default:
        break;
    }
}

static void bluetooth_worker(void *context) {
    bt_event_t event;
    (void)context;

    s_next_action_ms = now_ms();
    for (;;) {
        uint32_t current_ms;
        if (xQueueReceive(s_event_queue, &event, pdMS_TO_TICKS(100U)) == pdTRUE) {
            handle_bt_event(&event, now_ms());
        }
        current_ms = now_ms();
        if (s_state == BT_STATE_WAITING && (int32_t)(current_ms - s_next_action_ms) >= 0) {
            esp_bd_addr_t saved_peer;
            if (saved_peer_snapshot(saved_peer)) {
                start_connection(saved_peer, current_ms);
            } else if (CONFIG_LARKBRIDGE_OUTPUT_PEER_NAME[0] != '\0') {
                begin_discovery(current_ms);
            } else {
                s_next_action_ms = current_ms + CONFIG_LARKBRIDGE_OUTPUT_RECONNECT_MS;
            }
        } else if (s_state == BT_STATE_CONNECTING &&
                   (int32_t)(current_ms - s_connect_deadline_ms) >= 0) {
            ESP_LOGW(TAG, "A2DP connection attempt timed out");
            s_state = BT_STATE_WAITING;
            s_next_action_ms = current_ms + CONFIG_LARKBRIDGE_OUTPUT_RECONNECT_MS;
        } else if (s_state == BT_STATE_DISCONNECTING &&
                   (int32_t)(current_ms - s_connect_deadline_ms) >= 0) {
            ESP_LOGW(TAG, "A2DP disconnect timed out; releasing stale peer identity");
            s_active_connection_valid = false;
            s_state = BT_STATE_WAITING;
            s_next_action_ms = current_ms + CONFIG_LARKBRIDGE_OUTPUT_RECONNECT_MS;
            set_pairing_visibility();
        } else if (s_state == BT_STATE_CONNECTED && !saved_peer_snapshot(NULL) &&
                   (int32_t)(current_ms - s_qualification_deadline_ms) >= 0) {
            reject_active_peer("output peer did not qualify before timeout");
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
    atomic_init(&s_format_qualified, false);
    s_active_connection_valid = false;
    avrcp_peer_update(false, NULL);
    s_media_ack_pending = false;
    s_media_ack_timeout_count = 0U;
    s_event_queue = xQueueCreate(BT_EVENT_QUEUE_LENGTH, sizeof(bt_event_t));
    if (s_event_queue == NULL) {
        return ESP_ERR_NO_MEM;
    }
    esp_bd_addr_t saved_peer = {0};
    bool have_saved_peer = false;
    error = output_peer_store_load(saved_peer, &have_saved_peer);
    if (error != ESP_OK) {
        ESP_LOGW(TAG, "cannot load saved peer: %s", esp_err_to_name(error));
        saved_peer_clear();
    } else if (have_saved_peer) {
        saved_peer_set(saved_peer);
    } else {
        saved_peer_clear();
    }

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
             saved_peer_snapshot(NULL) ? "; saved peer loaded" : "; waiting for pairing");
    return ESP_OK;
}

bool output_bluetooth_stack_ready(void) {
    return atomic_load_explicit(&s_stack_ready, memory_order_acquire);
}

bool output_bluetooth_peer_connected(void) {
    return atomic_load_explicit(&s_peer_connected, memory_order_acquire);
}

bool output_bluetooth_audio_streaming(void) {
    return atomic_load_explicit(&s_audio_streaming, memory_order_acquire) &&
           atomic_load_explicit(&s_format_qualified, memory_order_acquire);
}
