#include "output_policy.h"

#include <stddef.h>

static bool route_is_valid(uint8_t route) {
    return route == (uint8_t)BRIDGE_ROUTE_SILENCE || route == (uint8_t)BRIDGE_ROUTE_A2DP ||
           route == (uint8_t)BRIDGE_ROUTE_HFP;
}

static bool heartbeat_is_fresh(const output_policy_t *policy, uint32_t now_ms) {
    return policy->heartbeat_seen &&
           (uint32_t)(now_ms - policy->last_heartbeat_ms) < policy->heartbeat_timeout_ms;
}

void output_policy_init(output_policy_t *policy, uint32_t heartbeat_timeout_ms) {
    if (policy == NULL) {
        return;
    }
    policy->hello_valid = false;
    policy->heartbeat_seen = false;
    policy->route = BRIDGE_ROUTE_SILENCE;
    policy->advertised_route = BRIDGE_ROUTE_SILENCE;
    policy->remote_ready_flags = 0U;
    policy->last_heartbeat_ms = 0U;
    policy->heartbeat_timeout_ms = heartbeat_timeout_ms;
}

bool output_policy_accept_hello(output_policy_t *policy, uint8_t role, uint16_t capabilities) {
    const uint16_t required = (uint16_t)(BRIDGE_CAP_I2S_TX | BRIDGE_CAP_FAIL_CLOSED);

    if (policy == NULL || role != (uint8_t)BRIDGE_ROLE_S31_GATEWAY ||
        (capabilities & required) != required) {
        return false;
    }
    policy->hello_valid = true;
    policy->heartbeat_seen = false;
    policy->route = BRIDGE_ROUTE_SILENCE;
    policy->advertised_route = BRIDGE_ROUTE_SILENCE;
    policy->remote_ready_flags = 0U;
    return true;
}

bool output_policy_accept_heartbeat(
    output_policy_t *policy, uint32_t now_ms, uint8_t route, uint8_t ready_flags) {
    if (policy == NULL || !policy->hello_valid || !route_is_valid(route)) {
        return false;
    }
    policy->heartbeat_seen = true;
    policy->last_heartbeat_ms = now_ms;
    policy->advertised_route = (bridge_route_t)route;
    policy->remote_ready_flags = ready_flags;
    return true;
}

bool output_policy_accept_route(output_policy_t *policy, uint8_t route) {
    if (policy == NULL || !policy->hello_valid || !route_is_valid(route)) {
        return false;
    }
    policy->route = (bridge_route_t)route;
    return true;
}

bool output_policy_tick(output_policy_t *policy, uint32_t now_ms) {
    bool timed_out;

    if (policy == NULL) {
        return false;
    }
    timed_out = policy->hello_valid && policy->heartbeat_seen &&
                !heartbeat_is_fresh(policy, now_ms);
    if (timed_out) {
        policy->heartbeat_seen = false;
        policy->remote_ready_flags = 0U;
        policy->advertised_route = BRIDGE_ROUTE_SILENCE;
    }
    return timed_out;
}

bool output_policy_audio_allowed(const output_policy_t *policy, uint32_t now_ms) {
    const uint8_t required = (uint8_t)(BRIDGE_READY_I2S | BRIDGE_READY_UART);

    return policy != NULL && policy->hello_valid && heartbeat_is_fresh(policy, now_ms) &&
           policy->route != BRIDGE_ROUTE_SILENCE &&
           policy->advertised_route == policy->route &&
           (policy->remote_ready_flags & required) == required;
}
