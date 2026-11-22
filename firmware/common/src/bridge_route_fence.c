#include "bridge_route_fence.h"

#include <stddef.h>

static bool route_valid(bridge_route_t route)
{
    return route == BRIDGE_ROUTE_SILENCE || route == BRIDGE_ROUTE_A2DP ||
           route == BRIDGE_ROUTE_HFP;
}

void bridge_route_fence_reset(bridge_route_fence_t *fence)
{
    if (fence != NULL) {
        fence->phase = BRIDGE_ROUTE_FENCE_UNSYNCED;
        fence->target = BRIDGE_ROUTE_SILENCE;
    }
}

bridge_route_fence_action_t bridge_route_fence_set_desired(
    bridge_route_fence_t *fence, bridge_route_t desired)
{
    if (fence == NULL || !route_valid(desired)) {
        return BRIDGE_ROUTE_FENCE_ACTION_FAIL_CLOSED;
    }
    if (fence->phase == BRIDGE_ROUTE_FENCE_UNSYNCED || fence->target != desired) {
        fence->target = desired;
        fence->phase = BRIDGE_ROUTE_FENCE_WAIT_SILENCE_ACK;
        return BRIDGE_ROUTE_FENCE_ACTION_SEND_SILENCE;
    }
    return BRIDGE_ROUTE_FENCE_ACTION_NONE;
}

bridge_route_fence_action_t bridge_route_fence_ack(
    bridge_route_fence_t *fence, bridge_route_t acknowledged_route, bool accepted)
{
    if (fence == NULL || !accepted) {
        bridge_route_fence_reset(fence);
        return BRIDGE_ROUTE_FENCE_ACTION_FAIL_CLOSED;
    }
    if (fence->phase == BRIDGE_ROUTE_FENCE_WAIT_SILENCE_ACK &&
        acknowledged_route == BRIDGE_ROUTE_SILENCE) {
        if (fence->target == BRIDGE_ROUTE_SILENCE) {
            fence->phase = BRIDGE_ROUTE_FENCE_ACTIVE;
            return BRIDGE_ROUTE_FENCE_ACTION_AUTHORIZE;
        }
        fence->phase = BRIDGE_ROUTE_FENCE_DRAINING;
        return BRIDGE_ROUTE_FENCE_ACTION_START_GUARD;
    }
    if (fence->phase == BRIDGE_ROUTE_FENCE_WAIT_TARGET_ACK &&
        acknowledged_route == fence->target) {
        fence->phase = BRIDGE_ROUTE_FENCE_ACTIVE;
        return BRIDGE_ROUTE_FENCE_ACTION_AUTHORIZE;
    }
    bridge_route_fence_reset(fence);
    return BRIDGE_ROUTE_FENCE_ACTION_FAIL_CLOSED;
}

bridge_route_fence_action_t bridge_route_fence_guard_elapsed(
    bridge_route_fence_t *fence)
{
    if (fence == NULL || fence->phase != BRIDGE_ROUTE_FENCE_DRAINING ||
        fence->target == BRIDGE_ROUTE_SILENCE) {
        bridge_route_fence_reset(fence);
        return BRIDGE_ROUTE_FENCE_ACTION_FAIL_CLOSED;
    }
    fence->phase = BRIDGE_ROUTE_FENCE_WAIT_TARGET_ACK;
    return BRIDGE_ROUTE_FENCE_ACTION_SEND_TARGET;
}
