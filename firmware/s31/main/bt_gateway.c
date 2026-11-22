#include "bt_gateway.h"

#include <stdatomic.h>
#include <string.h>

#include "audio_router.h"
#include "bridge_link.h"
#include "bridge_session.h"
#include "esp_a2dp_api.h"
#include "esp_avrc_api.h"
#include "esp_bt.h"
#include "esp_bt_device.h"
#include "esp_bt_main.h"
#include "esp_gap_bt_api.h"
#include "esp_hf_client_api.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "gateway_state.h"
#include "s31_peer_store.h"
#include "s31_config.h"

static const char *TAG = "s31_bt";
static atomic_bool s_hfp_audio_connected;
static atomic_bool s_avrcp_connected;
static atomic_bool s_a2dp_format_supported;
static atomic_bool s_hfp_profile_ready;
static atomic_bool s_a2dp_profile_ready;
static atomic_bool s_tap_release_pending;
static atomic_int s_hfp_service_state;
static atomic_int s_a2dp_connection_state;
static bool s_call_present;
static bool s_call_setup;
static bool s_call_route_active;
static uint8_t s_next_transaction_label;
static uint8_t s_key_transaction_label[128];
static atomic_uint_fast8_t s_tap_command;
static atomic_uint_fast8_t s_tap_label;
static portMUX_TYPE s_upstream_mux = portMUX_INITIALIZER_UNLOCKED;
static esp_bd_addr_t s_upstream_bda;
static bool s_upstream_bda_valid;

static bool address_matches(const uint8_t first[ESP_BD_ADDR_LEN],
                            const uint8_t second[ESP_BD_ADDR_LEN])
{
    return memcmp(first, second, ESP_BD_ADDR_LEN) == 0;
}

static uint32_t now_ms(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000);
}

static bool upstream_snapshot(esp_bd_addr_t address)
{
    bool valid;

    portENTER_CRITICAL(&s_upstream_mux);
    valid = s_upstream_bda_valid;
    if (valid && address != NULL) {
        memcpy(address, s_upstream_bda, ESP_BD_ADDR_LEN);
    }
    portEXIT_CRITICAL(&s_upstream_mux);
    return valid;
}

static void upstream_identity_set(const esp_bd_addr_t address)
{
    portENTER_CRITICAL(&s_upstream_mux);
    memcpy(s_upstream_bda, address, ESP_BD_ADDR_LEN);
    s_upstream_bda_valid = true;
    portEXIT_CRITICAL(&s_upstream_mux);
}

static void upstream_identity_clear(void)
{
    portENTER_CRITICAL(&s_upstream_mux);
    memset(s_upstream_bda, 0, sizeof(s_upstream_bda));
    s_upstream_bda_valid = false;
    portEXIT_CRITICAL(&s_upstream_mux);
}

static bool accept_or_bind_upstream(const uint8_t address[ESP_BD_ADDR_LEN])
{
    bool accepted;
    bool newly_bound = false;

    portENTER_CRITICAL(&s_upstream_mux);
    if (s_upstream_bda_valid) {
        accepted = address_matches(s_upstream_bda, address);
    } else {
        memcpy(s_upstream_bda, address, ESP_BD_ADDR_LEN);
        s_upstream_bda_valid = true;
        accepted = true;
        newly_bound = true;
    }
    portEXIT_CRITICAL(&s_upstream_mux);
    if (!newly_bound) {
        return accepted;
    }
    esp_err_t result = s31_peer_store_save(address);
    if (result != ESP_OK) {
        ESP_LOGE(TAG, "could not persist Pixel identity: %s", esp_err_to_name(result));
        s31_gateway_set_fault(BRIDGE_FAULT_BLUETOOTH);
        upstream_identity_clear();
        return false;
    }
    result = esp_bt_gap_set_scan_mode(ESP_BT_CONNECTABLE, ESP_BT_NON_DISCOVERABLE);
    if (result != ESP_OK) {
        ESP_LOGW(TAG, "could not close pairing visibility: %s", esp_err_to_name(result));
    }
    ESP_LOGI(TAG, "bound upstream Pixel identity to %02x:%02x:%02x:%02x:%02x:%02x",
             address[0], address[1], address[2], address[3], address[4], address[5]);
    return true;
}

static bool upstream_allowed(const uint8_t address[ESP_BD_ADDR_LEN])
{
    bool allowed;

    portENTER_CRITICAL(&s_upstream_mux);
    allowed = s_upstream_bda_valid && address_matches(s_upstream_bda, address);
    portEXIT_CRITICAL(&s_upstream_mux);
    return allowed;
}

static bool upstream_allows_pairing(const uint8_t address[ESP_BD_ADDR_LEN])
{
    bool allowed;

    portENTER_CRITICAL(&s_upstream_mux);
    allowed = !s_upstream_bda_valid || address_matches(s_upstream_bda, address);
    portEXIT_CRITICAL(&s_upstream_mux);
    return allowed;
}

bool s31_bt_avrcp_command_supported(uint8_t command)
{
    switch (command) {
    case ESP_AVRC_PT_CMD_VOL_UP:
    case ESP_AVRC_PT_CMD_VOL_DOWN:
    case ESP_AVRC_PT_CMD_MUTE:
    case ESP_AVRC_PT_CMD_PLAY:
    case ESP_AVRC_PT_CMD_STOP:
    case ESP_AVRC_PT_CMD_PAUSE:
    case ESP_AVRC_PT_CMD_REWIND:
    case ESP_AVRC_PT_CMD_FAST_FORWARD:
    case ESP_AVRC_PT_CMD_FORWARD:
    case ESP_AVRC_PT_CMD_BACKWARD:
        return true;
    default:
        return false;
    }
}

static void update_call_route(void)
{
    bool active = s_call_present || s_call_setup;
    if (active != s_call_route_active) {
        s_call_route_active = active;
        s31_gateway_apply_session_event(active ? BRIDGE_SESSION_EVENT_CALL_STARTED
                                               : BRIDGE_SESSION_EVENT_CALL_ENDED);
    }
}

static void hfp_incoming_data_callback(const uint8_t *data, uint32_t length)
{
    s31_audio_router_write_hfp_downlink(data, length);
    /* Incoming SCO packets are the exact link cadence and continue even when
     * the LARK is absent. Always request a matching uplink packet; the router
     * supplies processed AEC audio or deterministic digital silence. */
    if (atomic_load_explicit(&s_hfp_audio_connected, memory_order_acquire)) {
        esp_hf_client_outgoing_data_ready();
    }
}

static uint32_t hfp_outgoing_data_callback(uint8_t *data, uint32_t length)
{
    return s31_audio_router_fill_hfp_uplink(data, length);
}

static void hfp_callback(esp_hf_client_cb_event_t event, esp_hf_client_cb_param_t *parameter)
{
    switch (event) {
    case ESP_HF_CLIENT_CONNECTION_STATE_EVT:
        atomic_store_explicit(&s_hfp_service_state, parameter->conn_stat.state,
                              memory_order_release);
        ESP_LOGI(TAG, "HFP service state=%d", parameter->conn_stat.state);
        if ((parameter->conn_stat.state == ESP_HF_CLIENT_CONNECTION_STATE_CONNECTED ||
             parameter->conn_stat.state == ESP_HF_CLIENT_CONNECTION_STATE_SLC_CONNECTED) &&
            !accept_or_bind_upstream(parameter->conn_stat.remote_bda)) {
            ESP_LOGE(TAG, "rejecting HFP from a non-Pixel peer");
            (void)esp_hf_client_disconnect(parameter->conn_stat.remote_bda);
            break;
        }
        if (parameter->conn_stat.state == ESP_HF_CLIENT_CONNECTION_STATE_DISCONNECTED) {
            atomic_store(&s_hfp_audio_connected, false);
            s31_gateway_apply_session_event(BRIDGE_SESSION_EVENT_HFP_DOWN);
            s_call_present = false;
            s_call_setup = false;
            update_call_route();
        }
        break;
    case ESP_HF_CLIENT_AUDIO_STATE_EVT:
        if (parameter->audio_stat.state == ESP_HF_CLIENT_AUDIO_STATE_CONNECTED ||
            parameter->audio_stat.state == ESP_HF_CLIENT_AUDIO_STATE_CONNECTED_MSBC) {
            if (!upstream_allowed(parameter->audio_stat.remote_bda)) {
                ESP_LOGE(TAG, "rejecting HFP audio from a non-Pixel peer");
                (void)esp_hf_client_disconnect_audio(parameter->audio_stat.remote_bda);
                break;
            }
            uint32_t rate = parameter->audio_stat.state == ESP_HF_CLIENT_AUDIO_STATE_CONNECTED_MSBC
                                ? 16000U
                                : 8000U;
            s31_audio_router_set_hfp_rate(rate);
            esp_err_t result = esp_hf_client_register_data_callback(
                hfp_incoming_data_callback, hfp_outgoing_data_callback);
            if (result != ESP_OK) {
                ESP_LOGE(TAG, "HFP PCM callback registration failed: %s", esp_err_to_name(result));
                s31_gateway_set_fault(BRIDGE_FAULT_BLUETOOTH);
                break;
            }
            atomic_store(&s_hfp_audio_connected, true);
            s31_gateway_apply_session_event(BRIDGE_SESSION_EVENT_HFP_UP);
            esp_hf_client_outgoing_data_ready();
            ESP_LOGI(TAG, "HFP audio connected at %lu Hz", (unsigned long)rate);
        } else if (parameter->audio_stat.state == ESP_HF_CLIENT_AUDIO_STATE_DISCONNECTED) {
            atomic_store(&s_hfp_audio_connected, false);
            s31_gateway_apply_session_event(BRIDGE_SESSION_EVENT_HFP_DOWN);
            ESP_LOGI(TAG, "HFP audio disconnected");
        }
        break;
    case ESP_HF_CLIENT_CIND_CALL_EVT:
        s_call_present = parameter->call.status == ESP_HF_CALL_STATUS_CALL_IN_PROGRESS;
        update_call_route();
        break;
    case ESP_HF_CLIENT_CIND_CALL_SETUP_EVT:
        s_call_setup = parameter->call_setup.status != ESP_HF_CALL_SETUP_STATUS_IDLE;
        update_call_route();
        break;
    case ESP_HF_CLIENT_PROF_STATE_EVT:
        atomic_store_explicit(&s_hfp_profile_ready,
                              parameter->prof_stat.state == ESP_HF_INIT_SUCCESS,
                              memory_order_release);
        ESP_LOGI(TAG, "HFP profile state=%d", parameter->prof_stat.state);
        break;
    default:
        break;
    }
}

static void a2dp_data_callback(const uint8_t *data, uint32_t length)
{
    if (atomic_load_explicit(&s_a2dp_format_supported, memory_order_acquire)) {
        s31_audio_router_write_a2dp(data, length);
    }
}

static void a2dp_callback(esp_a2d_cb_event_t event, esp_a2d_cb_param_t *parameter)
{
    switch (event) {
    case ESP_A2D_CONNECTION_STATE_EVT:
        atomic_store_explicit(&s_a2dp_connection_state, parameter->conn_stat.state,
                              memory_order_release);
        ESP_LOGI(TAG, "A2DP connection state=%d", parameter->conn_stat.state);
        if (parameter->conn_stat.state == ESP_A2D_CONNECTION_STATE_CONNECTED &&
            !accept_or_bind_upstream(parameter->conn_stat.remote_bda)) {
            ESP_LOGE(TAG, "rejecting A2DP from a non-Pixel peer");
            (void)esp_a2d_sink_disconnect(parameter->conn_stat.remote_bda);
            atomic_store(&s_a2dp_format_supported, false);
            s31_gateway_apply_session_event(BRIDGE_SESSION_EVENT_A2DP_DOWN);
            break;
        }
        if (parameter->conn_stat.state == ESP_A2D_CONNECTION_STATE_DISCONNECTED) {
            atomic_store(&s_a2dp_format_supported, false);
            s31_gateway_apply_session_event(BRIDGE_SESSION_EVENT_A2DP_DOWN);
        }
        break;
    case ESP_A2D_AUDIO_CFG_EVT: {
        const esp_a2d_mcc_t *codec = &parameter->audio_cfg.mcc;
        bool peer_allowed = upstream_allowed(parameter->audio_cfg.remote_bda);
        uint32_t rate = codec->cie.sbc_info.samp_freq == ESP_A2D_SBC_CIE_SF_44K
                            ? 44100U
                            : 48000U;
        bool supported = peer_allowed && codec->type == ESP_A2D_MCT_SBC &&
                         (codec->cie.sbc_info.samp_freq == ESP_A2D_SBC_CIE_SF_44K ||
                          codec->cie.sbc_info.samp_freq == ESP_A2D_SBC_CIE_SF_48K) &&
                         (codec->cie.sbc_info.ch_mode == ESP_A2D_SBC_CIE_CH_MODE_STEREO ||
                          codec->cie.sbc_info.ch_mode == ESP_A2D_SBC_CIE_CH_MODE_JOINT_STEREO);
        atomic_store(&s_a2dp_format_supported, supported);
        if (!supported) {
            ESP_LOGE(TAG, "A2DP peer/format unsupported; requiring Pixel SBC stereo at 44.1/48 kHz");
            s31_gateway_apply_session_event(BRIDGE_SESSION_EVENT_A2DP_DOWN);
            s31_gateway_set_fault(BRIDGE_FAULT_BLUETOOTH);
            (void)esp_a2d_sink_disconnect(parameter->audio_cfg.remote_bda);
        } else {
            s31_audio_router_set_a2dp_rate(rate);
            ESP_LOGI(TAG, "A2DP media configured at %lu Hz stereo SBC",
                     (unsigned long)rate);
        }
        break;
    }
    case ESP_A2D_AUDIO_STATE_EVT:
        if (parameter->audio_stat.state == ESP_A2D_AUDIO_STATE_STARTED &&
            atomic_load_explicit(&s_a2dp_format_supported, memory_order_acquire) &&
            upstream_allowed(parameter->audio_stat.remote_bda)) {
            s31_gateway_apply_session_event(BRIDGE_SESSION_EVENT_A2DP_UP);
        } else {
            s31_gateway_apply_session_event(BRIDGE_SESSION_EVENT_A2DP_DOWN);
        }
        break;
    case ESP_A2D_PROF_STATE_EVT:
        atomic_store_explicit(&s_a2dp_profile_ready,
                              parameter->a2d_prof_stat.init_state == ESP_A2D_INIT_SUCCESS,
                              memory_order_release);
        ESP_LOGI(TAG, "A2DP profile state=%d", parameter->a2d_prof_stat.init_state);
        break;
    default:
        break;
    }
}

static void avrcp_callback(esp_avrc_ct_cb_event_t event, esp_avrc_ct_cb_param_t *parameter)
{
    if (event == ESP_AVRC_CT_CONNECTION_STATE_EVT) {
        bool accepted = parameter->conn_stat.connected
                            ? accept_or_bind_upstream(parameter->conn_stat.remote_bda)
                            : upstream_allowed(parameter->conn_stat.remote_bda);
        bool connected = parameter->conn_stat.connected && accepted;
        atomic_store(&s_avrcp_connected, connected);
        if (!connected) {
            atomic_store_explicit(&s_tap_release_pending, false, memory_order_release);
        }
        s31_gateway_set_avrcp_connected(connected);
        ESP_LOGI(TAG, "AVRCP controller connected=%d accepted=%d",
                 parameter->conn_stat.connected, accepted);
    }
}

static void gap_callback(esp_bt_gap_cb_event_t event, esp_bt_gap_cb_param_t *parameter)
{
    switch (event) {
    case ESP_BT_GAP_AUTH_CMPL_EVT:
        if (parameter->auth_cmpl.stat == ESP_BT_STATUS_SUCCESS) {
            if (accept_or_bind_upstream(parameter->auth_cmpl.bda)) {
                ESP_LOGI(TAG, "Bluetooth authentication succeeded for %s",
                         parameter->auth_cmpl.device_name);
            } else {
                ESP_LOGE(TAG, "removing bond attempted by a non-Pixel peer");
                (void)esp_bt_gap_remove_bond_device(parameter->auth_cmpl.bda);
            }
        } else {
            ESP_LOGE(TAG, "Bluetooth authentication failed, status=%d",
                     parameter->auth_cmpl.stat);
        }
        break;
    case ESP_BT_GAP_CFM_REQ_EVT:
        esp_bt_gap_ssp_confirm_reply(
            parameter->cfm_req.bda,
            upstream_allows_pairing(parameter->cfm_req.bda));
        break;
    case ESP_BT_GAP_PIN_REQ_EVT: {
        esp_bt_pin_code_t pin = {'0', '0', '0', '0'};
        bool accept = upstream_allows_pairing(parameter->pin_req.bda);
        esp_bt_gap_pin_reply(parameter->pin_req.bda, accept, accept ? 4 : 0, pin);
        break;
    }
    default:
        break;
    }
}

static esp_err_t initialize_profiles(void)
{
    esp_err_t result = esp_bt_gap_set_device_name(S31_BT_DEVICE_NAME);
    if (result != ESP_OK) return result;
    result = esp_bt_gap_register_callback(gap_callback);
    if (result != ESP_OK) return result;

    esp_bt_io_cap_t io_capability = ESP_BT_IO_CAP_NONE;
    result = esp_bt_gap_set_security_param(ESP_BT_SP_IOCAP_MODE, &io_capability,
                                           sizeof(io_capability));
    if (result != ESP_OK) return result;
    esp_bt_pin_code_t pin = {'0', '0', '0', '0'};
    result = esp_bt_gap_set_pin(ESP_BT_PIN_TYPE_FIXED, 4, pin);
    if (result != ESP_OK) return result;

    esp_bt_cod_t class_of_device = {
        .minor = 0x01, /* Audio/Video wearable headset */
        .major = ESP_BT_COD_MAJOR_DEV_AV,
    };
    result = esp_bt_gap_set_cod(class_of_device, ESP_BT_SET_COD_MAJOR_MINOR);
    if (result != ESP_OK) return result;

    result = esp_avrc_ct_register_callback(avrcp_callback);
    if (result != ESP_OK) return result;
    result = esp_avrc_ct_init();
    if (result != ESP_OK) return result;

    result = esp_a2d_register_callback(a2dp_callback);
    if (result != ESP_OK) return result;
    result = esp_a2d_sink_init();
    if (result != ESP_OK) return result;
    result = esp_a2d_sink_register_data_callback(a2dp_data_callback);
    if (result != ESP_OK) return result;

    result = esp_hf_client_register_callback(hfp_callback);
    if (result != ESP_OK) return result;
    result = esp_hf_client_init();
    if (result != ESP_OK) return result;

    return esp_bt_gap_set_scan_mode(
        ESP_BT_CONNECTABLE,
        upstream_snapshot(NULL) ? ESP_BT_NON_DISCOVERABLE : ESP_BT_GENERAL_DISCOVERABLE);
}

static void reconnect_worker(void *context)
{
    uint32_t next_attempt_ms = now_ms() + S31_BT_RECONNECT_INITIAL_MS;
    uint32_t backoff_ms = S31_BT_RECONNECT_INITIAL_MS;
    (void)context;

    for (;;) {
        esp_bd_addr_t peer;
        bool profiles_ready =
            atomic_load_explicit(&s_hfp_profile_ready, memory_order_acquire) &&
            atomic_load_explicit(&s_a2dp_profile_ready, memory_order_acquire);
        int hfp_state = atomic_load_explicit(&s_hfp_service_state, memory_order_acquire);
        int a2dp_state = atomic_load_explicit(&s_a2dp_connection_state, memory_order_acquire);
        bool hfp_up = hfp_state == ESP_HF_CLIENT_CONNECTION_STATE_SLC_CONNECTED;
        bool a2dp_up = a2dp_state == ESP_A2D_CONNECTION_STATE_CONNECTED;
        uint32_t current_ms = now_ms();

        if (hfp_up && a2dp_up) {
            backoff_ms = S31_BT_RECONNECT_INITIAL_MS;
            next_attempt_ms = current_ms + backoff_ms;
        } else if (profiles_ready && upstream_snapshot(peer) &&
                   (int32_t)(current_ms - next_attempt_ms) >= 0) {
            bool requested = false;
            if (hfp_state == ESP_HF_CLIENT_CONNECTION_STATE_DISCONNECTED) {
                esp_err_t result = esp_hf_client_connect(peer);
                requested = true;
                ESP_LOGI(TAG, "HFP reconnect request: %s", esp_err_to_name(result));
            }
            if (a2dp_state == ESP_A2D_CONNECTION_STATE_DISCONNECTED) {
                esp_err_t result = esp_a2d_sink_connect(peer);
                requested = true;
                ESP_LOGI(TAG, "A2DP reconnect request: %s", esp_err_to_name(result));
            }
            if (requested) {
                next_attempt_ms = current_ms + backoff_ms;
                if (backoff_ms < S31_BT_RECONNECT_MAX_MS / 2U) {
                    backoff_ms *= 2U;
                } else {
                    backoff_ms = S31_BT_RECONNECT_MAX_MS;
                }
            } else {
                next_attempt_ms = current_ms + S31_BT_RECONNECT_INITIAL_MS;
            }
        }
        vTaskDelay(pdMS_TO_TICKS(250U));
    }
}

esp_err_t s31_bt_gateway_init(void)
{
    atomic_init(&s_hfp_audio_connected, false);
    atomic_init(&s_avrcp_connected, false);
    atomic_init(&s_a2dp_format_supported, false);
    atomic_init(&s_hfp_profile_ready, false);
    atomic_init(&s_a2dp_profile_ready, false);
    atomic_init(&s_tap_release_pending, false);
    atomic_init(&s_tap_command, 0U);
    atomic_init(&s_tap_label, 0U);
    atomic_init(&s_hfp_service_state, ESP_HF_CLIENT_CONNECTION_STATE_DISCONNECTED);
    atomic_init(&s_a2dp_connection_state, ESP_A2D_CONNECTION_STATE_DISCONNECTED);
    memset(s_key_transaction_label, 0, sizeof(s_key_transaction_label));
    s_call_present = false;
    s_call_setup = false;
    s_call_route_active = false;
    esp_bd_addr_t saved_peer = {0};
    bool have_saved_peer = false;
    esp_err_t result = s31_peer_store_load(saved_peer, &have_saved_peer);
    if (result != ESP_OK) {
        ESP_LOGE(TAG, "could not load Pixel identity: %s", esp_err_to_name(result));
        upstream_identity_clear();
    } else if (have_saved_peer) {
        upstream_identity_set(saved_peer);
    } else {
        upstream_identity_clear();
    }

    result = esp_bt_controller_mem_release(ESP_BT_MODE_BLE);
    if (result != ESP_OK) return result;
    esp_bt_controller_config_t controller_config = BT_CONTROLLER_INIT_CONFIG_DEFAULT();
    result = esp_bt_controller_init(&controller_config);
    if (result != ESP_OK) return result;
    result = esp_bt_controller_enable(ESP_BT_MODE_CLASSIC_BT);
    if (result != ESP_OK) return result;

    esp_bluedroid_config_t bluedroid_config = BT_BLUEDROID_INIT_CONFIG_DEFAULT();
    result = esp_bluedroid_init_with_cfg(&bluedroid_config);
    if (result != ESP_OK) return result;
    result = esp_bluedroid_enable();
    if (result != ESP_OK) return result;
    result = initialize_profiles();
    if (result != ESP_OK) return result;
    if (xTaskCreate(reconnect_worker, "s31_bt_reconnect", 4096U, NULL, 8U, NULL) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }

    s31_gateway_set_bt_ready(true);
    ESP_LOGI(TAG, "BR/EDR gateway ready as %s", S31_BT_DEVICE_NAME);
    return ESP_OK;
}

esp_err_t s31_bt_send_avrcp(uint8_t command, uint8_t key_state)
{
    if (!s31_bt_avrcp_command_supported(command) || key_state > BRIDGE_KEY_TAP) {
        return ESP_ERR_INVALID_ARG;
    }
    if (!atomic_load_explicit(&s_avrcp_connected, memory_order_acquire)) {
        atomic_store_explicit(&s_tap_release_pending, false, memory_order_release);
        return ESP_ERR_INVALID_STATE;
    }
    uint8_t label;
    if (key_state == BRIDGE_KEY_TAP) {
        esp_err_t result;
        if (atomic_load_explicit(&s_tap_release_pending, memory_order_acquire)) {
            if ((uint8_t)atomic_load_explicit(&s_tap_command, memory_order_acquire) !=
                command) {
                return ESP_ERR_INVALID_STATE;
            }
            label = (uint8_t)atomic_load_explicit(&s_tap_label, memory_order_acquire);
        } else {
            label = s_next_transaction_label++ & 0x0fU;
            result = esp_avrc_ct_send_passthrough_cmd(
                label, command, BRIDGE_KEY_PRESSED);
            if (result != ESP_OK) {
                return result;
            }
            atomic_store_explicit(&s_tap_command, command, memory_order_relaxed);
            atomic_store_explicit(&s_tap_label, label, memory_order_relaxed);
            atomic_store_explicit(&s_tap_release_pending, true, memory_order_release);
        }
        result = esp_avrc_ct_send_passthrough_cmd(
            label, command, BRIDGE_KEY_RELEASED);
        if (result == ESP_OK) {
            atomic_store_explicit(&s_tap_release_pending, false, memory_order_release);
        }
        return result;
    }
    if (key_state == BRIDGE_KEY_PRESSED) {
        label = s_next_transaction_label++ & 0x0fU;
        s_key_transaction_label[command] = label;
    } else {
        label = s_key_transaction_label[command];
    }
    return esp_avrc_ct_send_passthrough_cmd(label, command, key_state);
}
