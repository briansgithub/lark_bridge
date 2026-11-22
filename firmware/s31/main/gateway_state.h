#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "bridge_session.h"
#include "bridge_link.h"
#include "esp_err.h"

typedef struct {
    bridge_session_t session;
    bool bt_stack_ready;
    bool avrcp_connected;
    bool output_link_alive;
    bool i2s_ready;
    bool uart_ready;
    bridge_route_t desired_route;
    bridge_route_t authorized_route;
    bridge_route_t effective_route;
    uint32_t route_generation;
    bridge_fault_code_t last_fault;
} s31_gateway_snapshot_t;

void s31_gateway_state_init(void);
void s31_gateway_apply_session_event(bridge_session_event_t event);
void s31_gateway_set_bt_ready(bool ready);
void s31_gateway_set_avrcp_connected(bool connected);
void s31_gateway_set_output_link(bool alive);
void s31_gateway_set_i2s_ready(bool ready);
void s31_gateway_set_uart_ready(bool ready);
void s31_gateway_set_aec_ready(bool ready);
void s31_gateway_set_fault(bridge_fault_code_t fault);
void s31_gateway_force_silence(void);
bool s31_gateway_authorize_route(bridge_route_t route, uint32_t route_generation);
void s31_gateway_get_snapshot(s31_gateway_snapshot_t *snapshot);
uint8_t s31_gateway_ready_flags(void);
