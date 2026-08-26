#ifndef OUTPUT_POLICY_H
#define OUTPUT_POLICY_H

#include "bridge_link.h"
#include "bridge_session.h"

#include <stdbool.h>
#include <stdint.h>

typedef struct {
    bool hello_valid;
    bool heartbeat_seen;
    bridge_route_t route;
    bridge_route_t advertised_route;
    uint8_t remote_ready_flags;
    uint32_t last_heartbeat_ms;
    uint32_t heartbeat_timeout_ms;
} output_policy_t;

void output_policy_init(output_policy_t *policy, uint32_t heartbeat_timeout_ms);
bool output_policy_accept_hello(output_policy_t *policy, uint8_t role, uint16_t capabilities);
bool output_policy_accept_heartbeat(
    output_policy_t *policy, uint32_t now_ms, uint8_t route, uint8_t ready_flags);
bool output_policy_accept_route(output_policy_t *policy, uint8_t route);
bool output_policy_tick(output_policy_t *policy, uint32_t now_ms);
bool output_policy_audio_allowed(const output_policy_t *policy, uint32_t now_ms);

#endif
