#pragma once

#include <stdbool.h>
#include <stdint.h>

/* AVRCP tap (press then release) bookkeeping, independent of the Bluetooth stack.
 *
 * A tap arrives from the car with an origin (output session and packet sequence). The output
 * retries a packet with the same origin when the S31 reports failure; a retry only completes
 * the release still owed for that tap. A tap with a new origin always yields a fresh press and
 * release: a stale release from an earlier tap is attempted once and then dropped, so it can
 * neither block a different key nor swallow a new press of the same key (Astra finding 5). */

#define BRIDGE_KEY_STATE_PRESSED 0U
#define BRIDGE_KEY_STATE_RELEASED 1U

typedef enum {
    BRIDGE_TAP_OK = 0,
    BRIDGE_TAP_PRESS_FAILED,
    BRIDGE_TAP_RELEASE_FAILED,
} bridge_tap_result_t;

/* Returns true when the stack accepted the passthrough submission. */
typedef bool (*bridge_tap_send_fn)(void *context, uint8_t label, uint8_t command, uint8_t state);

typedef struct {
    bool release_pending;
    uint8_t command;
    uint8_t label;
    uint32_t origin;
    uint8_t next_label;
    uint32_t stale_releases_sent;
    uint32_t stale_releases_dropped;
} bridge_tap_state_t;

void bridge_tap_init(bridge_tap_state_t *state);
/* Forget any owed release, e.g. after AVRCP disconnects. */
void bridge_tap_reset(bridge_tap_state_t *state);
bridge_tap_result_t bridge_tap_submit(bridge_tap_state_t *state, uint8_t command, uint32_t origin,
                                      bridge_tap_send_fn send, void *context);
/* Origin for a car tap: output session mixed with the 16-bit packet sequence. */
static inline uint32_t bridge_tap_origin(uint32_t session, uint16_t sequence)
{
    return (session << 16) ^ (session >> 16) ^ (uint32_t)sequence;
}
