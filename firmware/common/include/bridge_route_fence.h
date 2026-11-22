#ifndef BRIDGE_ROUTE_FENCE_H
#define BRIDGE_ROUTE_FENCE_H

#include <stdbool.h>

#include "bridge_session.h"

typedef enum {
    BRIDGE_ROUTE_FENCE_UNSYNCED = 0,
    BRIDGE_ROUTE_FENCE_WAIT_SILENCE_ACK,
    BRIDGE_ROUTE_FENCE_DRAINING,
    BRIDGE_ROUTE_FENCE_WAIT_TARGET_ACK,
    BRIDGE_ROUTE_FENCE_ACTIVE,
} bridge_route_fence_phase_t;

typedef enum {
    BRIDGE_ROUTE_FENCE_ACTION_NONE = 0,
    BRIDGE_ROUTE_FENCE_ACTION_SEND_SILENCE,
    BRIDGE_ROUTE_FENCE_ACTION_START_GUARD,
    BRIDGE_ROUTE_FENCE_ACTION_SEND_TARGET,
    BRIDGE_ROUTE_FENCE_ACTION_AUTHORIZE,
    BRIDGE_ROUTE_FENCE_ACTION_FAIL_CLOSED,
} bridge_route_fence_action_t;

typedef struct {
    bridge_route_fence_phase_t phase;
    bridge_route_t target;
} bridge_route_fence_t;

void bridge_route_fence_reset(bridge_route_fence_t *fence);
bridge_route_fence_action_t bridge_route_fence_set_desired(
    bridge_route_fence_t *fence, bridge_route_t desired);
bridge_route_fence_action_t bridge_route_fence_ack(
    bridge_route_fence_t *fence, bridge_route_t acknowledged_route, bool accepted);
bridge_route_fence_action_t bridge_route_fence_guard_elapsed(
    bridge_route_fence_t *fence);

#endif
