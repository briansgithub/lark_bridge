#include "bridge_tap.h"

#include <stddef.h>

void bridge_tap_init(bridge_tap_state_t *state)
{
    if (state == NULL) {
        return;
    }
    *state = (bridge_tap_state_t){0};
}

void bridge_tap_reset(bridge_tap_state_t *state)
{
    if (state != NULL) {
        state->release_pending = false;
    }
}

bridge_tap_result_t bridge_tap_submit(bridge_tap_state_t *state, uint8_t command, uint32_t origin,
                                      bridge_tap_send_fn send, void *context)
{
    if (state->release_pending) {
        if (state->origin == origin && state->command == command) {
            /* Retry of the same tap: only the release is owed. */
            if (!send(context, state->label, state->command, BRIDGE_KEY_STATE_RELEASED)) {
                return BRIDGE_TAP_RELEASE_FAILED;
            }
            state->release_pending = false;
            return BRIDGE_TAP_OK;
        }
        /* A new tap: release the stale key once, then drop it either way. */
        if (send(context, state->label, state->command, BRIDGE_KEY_STATE_RELEASED)) {
            ++state->stale_releases_sent;
        } else {
            ++state->stale_releases_dropped;
        }
        state->release_pending = false;
    }
    uint8_t label = (uint8_t)(state->next_label++ & 0x0fU);
    if (!send(context, label, command, BRIDGE_KEY_STATE_PRESSED)) {
        return BRIDGE_TAP_PRESS_FAILED;
    }
    state->release_pending = true;
    state->command = command;
    state->label = label;
    state->origin = origin;
    if (!send(context, label, command, BRIDGE_KEY_STATE_RELEASED)) {
        return BRIDGE_TAP_RELEASE_FAILED;
    }
    state->release_pending = false;
    return BRIDGE_TAP_OK;
}
