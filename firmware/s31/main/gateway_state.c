#include "gateway_state.h"

#include <string.h>

#include "audio_router.h"
#include "freertos/FreeRTOS.h"

static portMUX_TYPE s_lock = portMUX_INITIALIZER_UNLOCKED;
static s31_gateway_snapshot_t s_state;

static bridge_route_t calculate_desired_route(void)
{
    if (!s_state.bt_stack_ready || !s_state.output_link_alive ||
        !s_state.i2s_ready || !s_state.uart_ready) {
        return BRIDGE_ROUTE_SILENCE;
    }
    return s_state.session.route;
}

static void update_route_locked(void)
{
    bridge_route_t desired = calculate_desired_route();
    if (desired != s_state.desired_route) {
        /* Every route identity change consumes the previous authorization.
         * It may become effective again only after a new silence fence. */
        s_state.authorized_route = BRIDGE_ROUTE_SILENCE;
        ++s_state.route_generation;
    }
    s_state.desired_route = desired;
    if (s_state.desired_route == BRIDGE_ROUTE_SILENCE) {
        s_state.authorized_route = BRIDGE_ROUTE_SILENCE;
    }
    s_state.effective_route = s_state.desired_route == s_state.authorized_route
                                  ? s_state.desired_route
                                  : BRIDGE_ROUTE_SILENCE;
    s31_audio_router_set_route(s_state.effective_route);
    s31_audio_router_set_uplink_ready(s_state.session.hfp_uplink_enabled &&
                                      s_state.effective_route == BRIDGE_ROUTE_HFP);
}

void s31_gateway_state_init(void)
{
    taskENTER_CRITICAL(&s_lock);
    memset(&s_state, 0, sizeof(s_state));
    bridge_session_init(&s_state.session);
    s_state.last_fault = BRIDGE_FAULT_AEC_NOT_READY;
    update_route_locked();
    taskEXIT_CRITICAL(&s_lock);
}

void s31_gateway_apply_session_event(bridge_session_event_t event)
{
    if (event == BRIDGE_SESSION_EVENT_LARK_PRESENT ||
        event == BRIDGE_SESSION_EVENT_LARK_LOST) {
        /* A fast USB loss/reconnect must not reuse the old AEC/reference
         * generation even if the external route never left HFP. */
        s31_audio_router_reset_aec_stream();
    }
    taskENTER_CRITICAL(&s_lock);
    bridge_session_apply(&s_state.session, event);
    update_route_locked();
    taskEXIT_CRITICAL(&s_lock);
}

void s31_gateway_set_bt_ready(bool ready)
{
    taskENTER_CRITICAL(&s_lock);
    s_state.bt_stack_ready = ready;
    update_route_locked();
    taskEXIT_CRITICAL(&s_lock);
}

void s31_gateway_set_avrcp_connected(bool connected)
{
    taskENTER_CRITICAL(&s_lock);
    s_state.avrcp_connected = connected;
    taskEXIT_CRITICAL(&s_lock);
}

void s31_gateway_set_output_link(bool alive)
{
    taskENTER_CRITICAL(&s_lock);
    s_state.output_link_alive = alive;
    update_route_locked();
    taskEXIT_CRITICAL(&s_lock);
}

void s31_gateway_set_i2s_ready(bool ready)
{
    taskENTER_CRITICAL(&s_lock);
    s_state.i2s_ready = ready;
    update_route_locked();
    taskEXIT_CRITICAL(&s_lock);
}

void s31_gateway_set_uart_ready(bool ready)
{
    taskENTER_CRITICAL(&s_lock);
    s_state.uart_ready = ready;
    update_route_locked();
    taskEXIT_CRITICAL(&s_lock);
}

void s31_gateway_set_aec_ready(bool ready)
{
    taskENTER_CRITICAL(&s_lock);
    bridge_session_apply(&s_state.session,
                         ready ? BRIDGE_SESSION_EVENT_AEC_READY : BRIDGE_SESSION_EVENT_AEC_LOST);
    if (!ready) {
        s_state.last_fault = BRIDGE_FAULT_AEC_NOT_READY;
    }
    update_route_locked();
    taskEXIT_CRITICAL(&s_lock);
}

void s31_gateway_set_fault(bridge_fault_code_t fault)
{
    taskENTER_CRITICAL(&s_lock);
    s_state.last_fault = fault;
    taskEXIT_CRITICAL(&s_lock);
}

void s31_gateway_force_silence(void)
{
    taskENTER_CRITICAL(&s_lock);
    s_state.authorized_route = BRIDGE_ROUTE_SILENCE;
    update_route_locked();
    taskEXIT_CRITICAL(&s_lock);
}

bool s31_gateway_authorize_route(bridge_route_t route, uint32_t route_generation)
{
    bool accepted;

    taskENTER_CRITICAL(&s_lock);
    accepted = route == s_state.desired_route &&
               route_generation == s_state.route_generation;
    if (accepted) {
        s_state.authorized_route = route;
        update_route_locked();
    }
    taskEXIT_CRITICAL(&s_lock);
    return accepted;
}

void s31_gateway_get_snapshot(s31_gateway_snapshot_t *snapshot)
{
    if (snapshot == NULL) {
        return;
    }
    taskENTER_CRITICAL(&s_lock);
    *snapshot = s_state;
    taskEXIT_CRITICAL(&s_lock);
}

uint8_t s31_gateway_ready_flags(void)
{
    s31_gateway_snapshot_t snapshot;
    uint8_t flags = 0;
    s31_gateway_get_snapshot(&snapshot);
    if (snapshot.bt_stack_ready) flags |= BRIDGE_READY_BT;
    if (snapshot.i2s_ready) flags |= BRIDGE_READY_I2S;
    if (snapshot.uart_ready) flags |= BRIDGE_READY_UART;
    if (snapshot.session.lark_present) flags |= BRIDGE_READY_UAC;
    if (snapshot.session.aec_ready) flags |= BRIDGE_READY_AEC;
    if (snapshot.output_link_alive) flags |= BRIDGE_READY_PEER;
    if (snapshot.bt_stack_ready && snapshot.i2s_ready && snapshot.uart_ready &&
        snapshot.output_link_alive) {
        flags |= BRIDGE_READY_AUDIO;
    }
    return flags;
}
