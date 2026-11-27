#include "bt_gateway.h"

#include <stdatomic.h>
#include <string.h>

#include "audio_router.h"
#include "bridge_link.h"
#include "bridge_session.h"
#include "bridge_tap.h"
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
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "gateway_state.h"
#include "s31_peer_store.h"
#include "reconnect_timer.h"
#include "s31_config.h"

static const char *TAG = "s31_bt";
static atomic_bool s_hfp_audio_connected;
static atomic_bool s_avrcp_connected;
static atomic_bool s_a2dp_format_supported;
static atomic_bool s_hfp_profile_ready;
static atomic_bool s_a2dp_profile_ready;
static atomic_bool s_tap_release_pending;
static atomic_bool s_acl_exists_collision;
static atomic_int s_hfp_service_state;
static atomic_int s_a2dp_connection_state;
static atomic_uint s_hfp_connection_completions;
static atomic_uint s_a2dp_connection_completions;
static atomic_uint_fast32_t s_a2dp_rx_packets;
static atomic_uint_fast32_t s_a2dp_rx_bytes;
static bool s_call_present;
static bool s_call_setup;
static bool s_call_route_active;
static uint8_t s_next_transaction_label;
static uint8_t s_key_transaction_label[128];
/* Tap bookkeeping (bridge_tap.c) and the passthrough labels, under s_avrcp_lock. */
static bridge_tap_state_t s_tap;
static SemaphoreHandle_t s_avrcp_lock;
/* Set from the AVRCP callback on disconnect; the next submission forgets owed releases. */
static atomic_bool s_tap_reset_requested;
static portMUX_TYPE s_upstream_mux = portMUX_INITIALIZER_UNLOCKED;
static esp_bd_addr_t s_upstream_bda;
static bool s_upstream_bda_valid;
/* Feature 1: intentional-disconnect strikes (RAM only), under s_disconn_mux. */
static portMUX_TYPE s_disconn_mux = portMUX_INITIALIZER_UNLOCKED;
static s31_reconnect_disconn_tracker_t s_disconn;

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

static uint8_t hci_reason_of(esp_bt_status_t status)
{
    /* Bluedroid reports HCI reasons offset by ESP_BT_STATUS_BASE_FOR_HCI_ERR. */
    int value = (int)status;
    if (value >= (int)ESP_BT_STATUS_BASE_FOR_HCI_ERR) {
        value -= (int)ESP_BT_STATUS_BASE_FOR_HCI_ERR;
    }
    return (uint8_t)value;
}

static void disconn_on_acl_up(const uint8_t address[ESP_BD_ADDR_LEN])
{
    if (!upstream_allowed(address)) {
        return;
    }
    portENTER_CRITICAL(&s_disconn_mux);
    bool lifted = s31_disconn_tracker_on_connected(&s_disconn, now_ms());
    unsigned int strikes = s_disconn.strike_count;
    portEXIT_CRITICAL(&s_disconn_mux);
    if (lifted) {
        ESP_LOGI(TAG, "phone-initiated reconnect while suppressed: auto-reconnect re-enabled, "
                      "strikes reset");
    } else if (strikes != 0U) {
        ESP_LOGI(TAG, "phone ACL up with %u/%u intentional-disconnect strikes", strikes,
                 (unsigned int)S31_BT_INTENTIONAL_DISCONN_LIMIT);
    }
}

static void disconn_on_acl_down(const uint8_t address[ESP_BD_ADDR_LEN], esp_bt_status_t reason)
{
    if (!upstream_allowed(address)) {
        return;
    }
    uint8_t hci = hci_reason_of(reason);
    portENTER_CRITICAL(&s_disconn_mux);
    s31_disconn_outcome_t outcome = s31_disconn_tracker_on_disconnect(&s_disconn, hci);
    unsigned int strikes = s_disconn.strike_count;
    portEXIT_CRITICAL(&s_disconn_mux);
    switch (outcome) {
    case S31_DISCONN_STRIKE:
        ESP_LOGI(TAG, "intentional disconnect (reason=0x%02x) strike %u/%u; auto-reconnect stays on",
                 hci, strikes, (unsigned int)S31_BT_INTENTIONAL_DISCONN_LIMIT);
        break;
    case S31_DISCONN_SUPPRESSED:
        ESP_LOGW(TAG, "intentional disconnect (reason=0x%02x) strike %u/%u; reconnect_suppressed=1 "
                      "until power cycle or phone-initiated connect",
                 hci, strikes, (unsigned int)S31_BT_INTENTIONAL_DISCONN_LIMIT);
        break;
    default:
        ESP_LOGI(TAG, "phone ACL down reason=0x%02x (not an intentional-disconnect strike)", hci);
        break;
    }
}

static bool disconn_tick_allows_reconnect(uint32_t current_ms)
{
    portENTER_CRITICAL(&s_disconn_mux);
    bool forgiven = s31_disconn_tracker_tick(&s_disconn, current_ms);
    bool allowed = s31_disconn_tracker_reconnect_allowed(&s_disconn);
    portEXIT_CRITICAL(&s_disconn_mux);
    if (forgiven) {
        ESP_LOGI(TAG, "connection sustained %u ms: intentional-disconnect strikes reset",
                 (unsigned int)S31_BT_INTENTIONAL_DISCONN_RESET_MS);
    }
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
        if (parameter->conn_stat.state == ESP_HF_CLIENT_CONNECTION_STATE_SLC_CONNECTED ||
            parameter->conn_stat.state == ESP_HF_CLIENT_CONNECTION_STATE_DISCONNECTED) {
            atomic_fetch_add_explicit(&s_hfp_connection_completions, 1U, memory_order_release);
        }
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
    atomic_fetch_add_explicit(&s_a2dp_rx_packets, 1U, memory_order_relaxed);
    atomic_fetch_add_explicit(&s_a2dp_rx_bytes, length, memory_order_relaxed);
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
        if (parameter->conn_stat.state == ESP_A2D_CONNECTION_STATE_CONNECTED ||
            parameter->conn_stat.state == ESP_A2D_CONNECTION_STATE_DISCONNECTED) {
            atomic_fetch_add_explicit(&s_a2dp_connection_completions, 1U, memory_order_release);
        }
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
            atomic_store_explicit(&s_tap_reset_requested, true, memory_order_release);
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
    case ESP_BT_GAP_ACL_CONN_CMPL_STAT_EVT:
        if (parameter->acl_conn_cmpl_stat.stat == ESP_BT_STATUS_HCI_CONNECTION_EXISTS ||
            (int)parameter->acl_conn_cmpl_stat.stat == ESP_HCI_ERR_CONN_ALREADY_EXISTS) {
            atomic_store_explicit(&s_acl_exists_collision, true, memory_order_release);
            ESP_LOGW(TAG, "ACL connection collision (0x0b Conn Exists) detected");
        } else if (parameter->acl_conn_cmpl_stat.stat == ESP_BT_STATUS_SUCCESS ||
                   parameter->acl_conn_cmpl_stat.stat == ESP_BT_STATUS_HCI_SUCCESS) {
            atomic_store_explicit(&s_acl_exists_collision, false, memory_order_release);
            disconn_on_acl_up(parameter->acl_conn_cmpl_stat.bda);
        }
        break;
    case ESP_BT_GAP_ACL_DISCONN_CMPL_STAT_EVT:
        disconn_on_acl_down(parameter->acl_disconn_cmpl_stat.bda,
                            parameter->acl_disconn_cmpl_stat.reason);
        break;
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

typedef struct {
    const char *name;
    atomic_uint *completions;
    esp_err_t (*connect)(esp_bd_addr_t);
    esp_err_t (*disconnect)(esp_bd_addr_t);
    unsigned int request_completion;
    uint32_t deadline_ms;
    bool pending;
} reconnect_profile_t;

static bool cancel_stalled_request(reconnect_profile_t *profile, esp_bd_addr_t peer,
                                   bool connected, bool disconnected, uint32_t current_ms)
{
    if (connected ||
        atomic_load_explicit(profile->completions, memory_order_acquire) != profile->request_completion) {
        profile->pending = false;
    }
    if (!profile->pending || (int32_t)(current_ms - profile->deadline_ms) < 0) {
        return false;
    }

    esp_err_t result = profile->disconnect(peer);
    ESP_LOGW(TAG, "%s reconnect stalled after %u ms; cancel request: %s",
             profile->name, S31_BT_RECONNECT_STALL_MS, esp_err_to_name(result));
    /* Give the stack another deadline to settle if it still reports connecting. */
    profile->pending = !disconnected;
    profile->deadline_ms = current_ms + S31_BT_RECONNECT_STALL_MS;
    return true;
}

static bool request_reconnect(reconnect_profile_t *profile, esp_bd_addr_t peer,
                              bool disconnected, uint32_t current_ms)
{
    if (profile->pending || !disconnected) {
        return false;
    }

    profile->request_completion = atomic_load_explicit(profile->completions, memory_order_acquire);
    esp_err_t result = profile->connect(peer);
    profile->pending = result == ESP_OK;
    profile->deadline_ms = current_ms + S31_BT_RECONNECT_STALL_MS;
    ESP_LOGI(TAG, "%s reconnect request: %s", profile->name, esp_err_to_name(result));
    return true;
}

#if CONFIG_LARKBRIDGE_TEST_IMAGE
/* Test-image fault hook to exercise local BT stack recycle on live hardware. */
static atomic_bool s_test_recycle_requested;
#endif

static esp_err_t recycle_bt_stack(void)
{
    ESP_LOGW(TAG, "recycling local BT stack to clear wedged ACL/L2CAP state");
    s31_gateway_set_bt_ready(false);
    atomic_store_explicit(&s_hfp_audio_connected, false, memory_order_release);
    atomic_store_explicit(&s_avrcp_connected, false, memory_order_release);
    atomic_store_explicit(&s_a2dp_format_supported, false, memory_order_release);
    atomic_store_explicit(&s_hfp_profile_ready, false, memory_order_release);
    atomic_store_explicit(&s_a2dp_profile_ready, false, memory_order_release);
    atomic_store_explicit(&s_tap_reset_requested, true, memory_order_release);
    atomic_store_explicit(&s_tap_release_pending, false, memory_order_release);
    atomic_store_explicit(&s_acl_exists_collision, false, memory_order_release);
    atomic_store_explicit(&s_hfp_service_state, ESP_HF_CLIENT_CONNECTION_STATE_DISCONNECTED,
                          memory_order_release);
    atomic_store_explicit(&s_a2dp_connection_state, ESP_A2D_CONNECTION_STATE_DISCONNECTED,
                          memory_order_release);
    s_call_present = false;
    s_call_setup = false;
    update_call_route();
    portENTER_CRITICAL(&s_disconn_mux);
    s_disconn.connected = false; /* a local recycle is never a strike */
    portEXIT_CRITICAL(&s_disconn_mux);
    s31_gateway_set_avrcp_connected(false);
    s31_gateway_apply_session_event(BRIDGE_SESSION_EVENT_HFP_DOWN);
    s31_gateway_apply_session_event(BRIDGE_SESSION_EVENT_A2DP_DOWN);

    /* Per IDF docs, AVRCP must be deinitialized before A2DP. */
    (void)esp_avrc_ct_deinit();
    (void)esp_a2d_sink_deinit();
    (void)esp_hf_client_deinit();
    vTaskDelay(pdMS_TO_TICKS(500U));

    /* Recycle Bluedroid (sends HCI_Reset on re-enable to clear orphaned ACLs)
     * without toggling esp_bt_controller_disable(), which leaves ESP32-S31 rev0
     * libbtbb in an OLC resched-failed state. */
    (void)esp_bluedroid_disable();
    (void)esp_bluedroid_deinit();
    vTaskDelay(pdMS_TO_TICKS(200U));

    esp_bluedroid_config_t bluedroid_config = BT_BLUEDROID_INIT_CONFIG_DEFAULT();
    esp_err_t result = esp_bluedroid_init_with_cfg(&bluedroid_config);
    if (result != ESP_OK) {
        ESP_LOGE(TAG, "BT recycle bluedroid init failed: %s", esp_err_to_name(result));
        s31_gateway_set_fault(BRIDGE_FAULT_BLUETOOTH);
        return result;
    }
    result = esp_bluedroid_enable();
    if (result != ESP_OK) {
        ESP_LOGE(TAG, "BT recycle bluedroid enable failed: %s", esp_err_to_name(result));
        s31_gateway_set_fault(BRIDGE_FAULT_BLUETOOTH);
        return result;
    }
    result = initialize_profiles();
    if (result != ESP_OK) {
        ESP_LOGE(TAG, "BT recycle profile init failed: %s", esp_err_to_name(result));
        s31_gateway_set_fault(BRIDGE_FAULT_BLUETOOTH);
        return result;
    }
    s31_gateway_set_bt_ready(true);
    ESP_LOGI(TAG, "local BT stack recycle complete");
    return ESP_OK;
}

static void reconnect_worker(void *context)
{
    uint32_t started_ms = now_ms();
    uint32_t next_attempt_ms = started_ms + S31_BT_RECONNECT_BOOT_HOLD_MS;
    bool boot_held = true;
    bool was_connected = false;
    bool partial_grace_armed = false;
    bool prefer_hfp = true;
    bool has_recycled = false;
    uint32_t last_recycle_ms = 0U;
    unsigned int consecutive_stalls = 0U;
    uint32_t backoff_ms = S31_BT_RECONNECT_INITIAL_MS;
    reconnect_profile_t hfp = {
        .name = "HFP", .completions = &s_hfp_connection_completions,
        .connect = esp_hf_client_connect, .disconnect = esp_hf_client_disconnect,
    };
    reconnect_profile_t a2dp = {
        .name = "A2DP", .completions = &s_a2dp_connection_completions,
        .connect = esp_a2d_sink_connect, .disconnect = esp_a2d_sink_disconnect,
    };
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

        bool hfp_down = hfp_state == ESP_HF_CLIENT_CONNECTION_STATE_DISCONNECTED;
        bool a2dp_down = a2dp_state == ESP_A2D_CONNECTION_STATE_DISCONNECTED;
        bool both_down = hfp_down && a2dp_down;

        bool force_recycle = false;
#if CONFIG_LARKBRIDGE_TEST_IMAGE
        force_recycle = atomic_exchange_explicit(&s_test_recycle_requested, false,
                                                 memory_order_acq_rel);
#endif
        if (force_recycle) {
            (void)recycle_bt_stack();
            hfp.pending = false;
            a2dp.pending = false;
            was_connected = false;
            partial_grace_armed = false;
            prefer_hfp = true;
            consecutive_stalls = 0U;
            backoff_ms = S31_BT_RECONNECT_INITIAL_MS;
            boot_held = true;
            started_ms = now_ms();
            next_attempt_ms = started_ms + S31_BT_RECONNECT_BOOT_HOLD_MS;
            has_recycled = true;
            last_recycle_ms = started_ms;
            vTaskDelay(pdMS_TO_TICKS(250U));
            continue;
        }

        bool reconnect_allowed = disconn_tick_allows_reconnect(current_ms);
        if (!reconnect_allowed) {
            /* Suppressed: stay connectable (scan mode is untouched) but never page.
             * Forget any in-flight request bookkeeping so resuming starts clean. */
            hfp.pending = false;
            a2dp.pending = false;
            was_connected = false;
            partial_grace_armed = false;
            consecutive_stalls = 0U;
            backoff_ms = S31_BT_RECONNECT_INITIAL_MS;
            next_attempt_ms = current_ms + S31_BT_RECONNECT_DISCONNECT_HOLD_MS;
        } else if (!s31_reconnect_boot_held(&boot_held, started_ms, current_ms) &&
            profiles_ready && upstream_snapshot(peer)) {
            bool hfp_was_pending = hfp.pending;
            bool a2dp_was_pending = a2dp.pending;
            bool cancelled_hfp = cancel_stalled_request(&hfp, peer, hfp_up, hfp_down, current_ms);
            bool cancelled_a2dp =
                cancel_stalled_request(&a2dp, peer, a2dp_up, a2dp_down, current_ms);
            bool hfp_failed_done = hfp_was_pending && !hfp.pending && !hfp_up && !cancelled_hfp;
            bool a2dp_failed_done =
                a2dp_was_pending && !a2dp.pending && !a2dp_up && !cancelled_a2dp;

            if (hfp_up && a2dp_up) {
                was_connected = true;
                partial_grace_armed = false;
                prefer_hfp = true;
                consecutive_stalls = 0U;
                atomic_store_explicit(&s_acl_exists_collision, false, memory_order_release);
                backoff_ms = S31_BT_RECONNECT_INITIAL_MS;
                next_attempt_ms = current_ms + backoff_ms;
            } else if (hfp_up || a2dp_up) {
                was_connected = true;
                consecutive_stalls = 0U;
                atomic_store_explicit(&s_acl_exists_collision, false, memory_order_release);
                if (!partial_grace_armed || cancelled_hfp || cancelled_a2dp ||
                    hfp_failed_done || a2dp_failed_done) {
                    partial_grace_armed = true;
                    next_attempt_ms = current_ms + S31_BT_RECONNECT_PEER_PROFILE_MS;
                }
            } else {
                partial_grace_armed = false;
                if (was_connected && both_down) {
                    /* Hold quietly after a full disconnect so the phone can settle
                     * or reconnect cleanly without colliding with an outbound page. */
                    was_connected = false;
                    prefer_hfp = true;
                    consecutive_stalls = 0U;
                    backoff_ms = S31_BT_RECONNECT_INITIAL_MS;
                    next_attempt_ms = current_ms + S31_BT_RECONNECT_DISCONNECT_HOLD_MS;
                }
                if ((cancelled_hfp || cancelled_a2dp) && both_down) {
                    ++consecutive_stalls;
                    next_attempt_ms = current_ms + backoff_ms;
                    backoff_ms = s31_reconnect_next_backoff(backoff_ms);
                } else if (hfp_failed_done || a2dp_failed_done) {
                    /* Normal page timeout completed: wait a full quiet backoff window
                     * before paging again so incoming phone reconnects have open air. */
                    next_attempt_ms = current_ms + backoff_ms;
                    backoff_ms = s31_reconnect_next_backoff(backoff_ms);
                }

                bool acl_collision =
                    atomic_load_explicit(&s_acl_exists_collision, memory_order_acquire);
                if (s31_reconnect_should_recycle(consecutive_stalls, acl_collision, both_down) &&
                    s31_reconnect_cooldown_elapsed(has_recycled, last_recycle_ms, current_ms)) {
                    (void)recycle_bt_stack();
                    hfp.pending = false;
                    a2dp.pending = false;
                    was_connected = false;
                    partial_grace_armed = false;
                    prefer_hfp = true;
                    consecutive_stalls = 0U;
                    backoff_ms = S31_BT_RECONNECT_INITIAL_MS;
                    boot_held = true;
                    started_ms = now_ms();
                    next_attempt_ms = started_ms + S31_BT_RECONNECT_BOOT_HOLD_MS;
                    has_recycled = true;
                    last_recycle_ms = started_ms;
                    vTaskDelay(pdMS_TO_TICKS(250U));
                    continue;
                }
            }

            if (!(hfp_up && a2dp_up) && (int32_t)(current_ms - next_attempt_ms) >= 0) {
                s31_reconnect_target_t target = s31_reconnect_choose_profile(
                    hfp_up, hfp_down, a2dp_up, a2dp_down, hfp.pending || a2dp.pending, prefer_hfp);
                reconnect_profile_t *profile = NULL;
                bool disconnected = false;
                if (target == S31_RECONNECT_HFP) {
                    profile = &hfp;
                    disconnected = hfp_down;
                } else if (target == S31_RECONNECT_A2DP) {
                    profile = &a2dp;
                    disconnected = a2dp_down;
                }
                if (profile != NULL) {
                    bool requested = request_reconnect(profile, peer, disconnected, current_ms);
                    if (both_down) {
                        prefer_hfp = (target != S31_RECONNECT_HFP);
                    }
                    if (!requested || !profile->pending) {
                        next_attempt_ms = current_ms +
                                          ((hfp_up || a2dp_up) ? S31_BT_RECONNECT_PEER_PROFILE_MS
                                                               : backoff_ms);
                        if (both_down) {
                            backoff_ms = s31_reconnect_next_backoff(backoff_ms);
                        }
                    }
                }
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
    atomic_init(&s_tap_reset_requested, false);
    atomic_init(&s_acl_exists_collision, false);
#if CONFIG_LARKBRIDGE_TEST_IMAGE
    atomic_init(&s_test_recycle_requested, false);
#endif
    bridge_tap_init(&s_tap);
    if (s_avrcp_lock == NULL) {
        s_avrcp_lock = xSemaphoreCreateMutex();
        if (s_avrcp_lock == NULL) {
            return ESP_ERR_NO_MEM;
        }
    }
    atomic_init(&s_hfp_service_state, ESP_HF_CLIENT_CONNECTION_STATE_DISCONNECTED);
    atomic_init(&s_a2dp_connection_state, ESP_A2D_CONNECTION_STATE_DISCONNECTED);
    memset(s_key_transaction_label, 0, sizeof(s_key_transaction_label));
    s_call_present = false;
    s_call_setup = false;
    s_call_route_active = false;
    s31_disconn_tracker_init(&s_disconn);
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

#if CONFIG_LARKBRIDGE_TEST_IMAGE
/* Astra case 5: fail the next N TAP release submissions (test image only). */
static atomic_uint s_test_release_failures;
#endif

static esp_err_t submit_tap_release(uint8_t label, uint8_t command)
{
#if CONFIG_LARKBRIDGE_TEST_IMAGE
    unsigned pending = atomic_load_explicit(&s_test_release_failures, memory_order_acquire);
    while (pending != 0U &&
           !atomic_compare_exchange_weak_explicit(&s_test_release_failures, &pending,
                                                  pending - 1U, memory_order_acq_rel,
                                                  memory_order_acquire)) {
    }
    if (pending != 0U) {
        ESP_LOGW(TAG, "TEST: injected AVRCP release submit failure (cmd=0x%02x, %u left)",
                 command, pending - 1U);
        return ESP_FAIL;
    }
#endif
    return esp_avrc_ct_send_passthrough_cmd(label, command, BRIDGE_KEY_RELEASED);
}

static bool tap_send(void *context, uint8_t label, uint8_t command, uint8_t state)
{
    (void)context;
    esp_err_t result = state == BRIDGE_KEY_STATE_RELEASED
                           ? submit_tap_release(label, command)
                           : esp_avrc_ct_send_passthrough_cmd(label, command, BRIDGE_KEY_PRESSED);
    return result == ESP_OK;
}

esp_err_t s31_bt_send_avrcp(uint8_t command, uint8_t key_state, uint32_t origin)
{
    if (!s31_bt_avrcp_command_supported(command) || key_state > BRIDGE_KEY_TAP) {
        return ESP_ERR_INVALID_ARG;
    }
    if (!atomic_load_explicit(&s_avrcp_connected, memory_order_acquire)) {
        atomic_store_explicit(&s_tap_reset_requested, true, memory_order_release);
        atomic_store_explicit(&s_tap_release_pending, false, memory_order_release);
        return ESP_ERR_INVALID_STATE;
    }
    if (xSemaphoreTake(s_avrcp_lock, pdMS_TO_TICKS(200U)) != pdTRUE) {
        return ESP_ERR_TIMEOUT;
    }
    if (atomic_exchange_explicit(&s_tap_reset_requested, false, memory_order_acq_rel)) {
        bridge_tap_reset(&s_tap);
    }
    esp_err_t result;
    if (key_state == BRIDGE_KEY_TAP) {
        uint32_t stale_sent = s_tap.stale_releases_sent;
        uint32_t stale_dropped = s_tap.stale_releases_dropped;
        s_tap.next_label = s_next_transaction_label;
        bridge_tap_result_t tap = bridge_tap_submit(&s_tap, command, origin, tap_send, NULL);
        s_next_transaction_label = s_tap.next_label;
        atomic_store_explicit(&s_tap_release_pending, s_tap.release_pending,
                              memory_order_release);
        if (s_tap.stale_releases_dropped != stale_dropped) {
            ESP_LOGW(TAG, "dropped a stale AVRCP release (submit failed) for a new key");
        } else if (s_tap.stale_releases_sent != stale_sent) {
            ESP_LOGI(TAG, "released a stale AVRCP key before a new tap");
        }
        result = tap == BRIDGE_TAP_OK ? ESP_OK : ESP_FAIL;
    } else {
        uint8_t label;
        if (key_state == BRIDGE_KEY_PRESSED) {
            label = s_next_transaction_label++ & 0x0fU;
            s_key_transaction_label[command] = label;
        } else {
            label = s_key_transaction_label[command];
        }
        result = esp_avrc_ct_send_passthrough_cmd(label, command, key_state);
    }
    xSemaphoreGive(s_avrcp_lock);
    return result;
}

void s31_bt_get_status(s31_bt_status_t *status)
{
    if (status == NULL) {
        return;
    }
    status->hfp_service_state = atomic_load_explicit(&s_hfp_service_state, memory_order_acquire);
    status->a2dp_connection_state =
        atomic_load_explicit(&s_a2dp_connection_state, memory_order_acquire);
    status->a2dp_rx_packets =
        (uint32_t)atomic_load_explicit(&s_a2dp_rx_packets, memory_order_relaxed);
    status->a2dp_rx_bytes = (uint32_t)atomic_load_explicit(&s_a2dp_rx_bytes, memory_order_relaxed);
    status->hfp_audio_connected = atomic_load(&s_hfp_audio_connected);
    status->avrcp_connected = atomic_load(&s_avrcp_connected);
    status->hfp_profile_ready = atomic_load(&s_hfp_profile_ready);
    status->a2dp_profile_ready = atomic_load(&s_a2dp_profile_ready);
    status->peer_bound = upstream_snapshot(status->peer);
    portENTER_CRITICAL(&s_disconn_mux);
    status->intentional_disconn_count = s_disconn.strike_count;
    status->reconnect_suppressed = s_disconn.suppressed;
    portEXIT_CRITICAL(&s_disconn_mux);
}

#if CONFIG_LARKBRIDGE_TEST_IMAGE
static uint32_t s_test_origin = 0x7e570000U;

bool s31_bt_test_fault(const char *kind)
{
    uint8_t command;
    if (strcmp(kind, "avrcp_relfail") == 0) {
        /* The output retries a reliable AVRCP packet up to five times. */
        atomic_store_explicit(&s_test_release_failures, 5U, memory_order_release);
        ESP_LOGW(TAG, "TEST: next 5 AVRCP release submissions will fail");
        return true;
    } else if (strcmp(kind, "avrcp_next") == 0) {
        /* The next test tap is a new car tap; without this, taps repeat as retries. */
        ++s_test_origin;
        return true;
    } else if (strcmp(kind, "bt_recycle") == 0) {
        atomic_store_explicit(&s_test_recycle_requested, true, memory_order_release);
        ESP_LOGW(TAG, "TEST: requested local BT stack recycle");
        return true;
    } else if (strcmp(kind, "avrcp_tap_stop") == 0) {
        command = ESP_AVRC_PT_CMD_STOP;
    } else if (strcmp(kind, "avrcp_tap_pause") == 0) {
        command = ESP_AVRC_PT_CMD_PAUSE;
    } else {
        return false;
    }
    /* Same entry point the UART AVRCP handler uses for a car tap. */
    esp_err_t result = s31_bt_send_avrcp(command, BRIDGE_KEY_TAP, s_test_origin);
    ESP_LOGI(TAG, "TEST: avrcp tap cmd=0x%02x result=%s release_pending=%d", command,
             esp_err_to_name(result),
             (int)atomic_load_explicit(&s_tap_release_pending, memory_order_acquire));
    return true;
}
#endif
