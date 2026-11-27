#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "s31_config.h"

typedef enum {
    S31_RECONNECT_NONE = 0,
    S31_RECONNECT_HFP,
    S31_RECONNECT_A2DP,
} s31_reconnect_target_t;

/* Once elapsed, the boot hold stays released across later clock wraps. */
static inline bool s31_reconnect_boot_held(bool *held, uint32_t start_ms, uint32_t now_ms)
{
    *held = *held && now_ms - start_ms < S31_BT_RECONNECT_BOOT_HOLD_MS;
    return *held;
}

static inline uint32_t s31_reconnect_next_backoff(uint32_t backoff_ms)
{
    if (backoff_ms < S31_BT_RECONNECT_INITIAL_MS) {
        return S31_BT_RECONNECT_INITIAL_MS;
    }
    if (backoff_ms >= S31_BT_RECONNECT_MAX_MS / 2U) {
        return S31_BT_RECONNECT_MAX_MS;
    }
    return backoff_ms * 2U;
}

/* Choose at most one profile to connect so outbound pages never overlap or race
 * an in-progress incoming/outgoing connection on the shared BR/EDR ACL. */
static inline s31_reconnect_target_t s31_reconnect_choose_profile(
    bool hfp_up, bool hfp_down, bool a2dp_up, bool a2dp_down, bool any_pending, bool prefer_hfp)
{
    if (any_pending || (hfp_up && a2dp_up)) {
        return S31_RECONNECT_NONE;
    }
    if (hfp_up && a2dp_down) {
        return S31_RECONNECT_A2DP;
    }
    if (a2dp_up && hfp_down) {
        return S31_RECONNECT_HFP;
    }
    if (hfp_down && a2dp_down) {
        return prefer_hfp ? S31_RECONNECT_HFP : S31_RECONNECT_A2DP;
    }
    /* One profile is connecting or disconnecting; wait for it to settle. */
    return S31_RECONNECT_NONE;
}

/* Normal unanswered pages complete in ~5.12 s (Page Timeout). Hitting the 10 s
 * stall guard repeatedly with both profiles down indicates an orphaned ACL in
 * the controller / wedged Bluedroid L2CAP state (D4). */
static inline bool s31_reconnect_should_recycle(unsigned int consecutive_stalls,
                                                bool acl_collision_seen, bool both_down)
{
    if (!both_down) {
        return false;
    }
    return consecutive_stalls >= S31_BT_RECONNECT_RECYCLE_STALLS ||
           (acl_collision_seen && consecutive_stalls >= 1U);
}

static inline bool s31_reconnect_cooldown_elapsed(bool has_recycled, uint32_t last_recycle_ms,
                                                  uint32_t now_ms)
{
    return !has_recycled || (uint32_t)(now_ms - last_recycle_ms) >= S31_BT_RECONNECT_RECYCLE_COOLDOWN_MS;
}

/* Intentional-disconnect suppression (car UX feature 1). RAM only: a power
 * cycle starts clean. One user disconnect still auto-reconnects (H14 single
 * toggle recovery); the second within one connected stretch stops all
 * outbound pages. The phone can still connect to us, which lifts it. */
typedef struct {
    unsigned int strike_count;
    bool suppressed;
    bool connected;
    uint32_t connected_since_ms;
} s31_reconnect_disconn_tracker_t;

typedef enum {
    S31_DISCONN_NOT_COUNTED = 0, /* link loss, local recycle, other reasons */
    S31_DISCONN_STRIKE,          /* counted, reconnect stays enabled */
    S31_DISCONN_SUPPRESSED,      /* this strike reached the limit */
} s31_disconn_outcome_t;

static inline void s31_disconn_tracker_init(s31_reconnect_disconn_tracker_t *tracker)
{
    tracker->strike_count = 0U;
    tracker->suppressed = false;
    tracker->connected = false;
    tracker->connected_since_ms = 0U;
}

/* hci_reason is the raw HCI disconnect reason (0x13 = remote user terminated). */
static inline s31_disconn_outcome_t s31_disconn_tracker_on_disconnect(
    s31_reconnect_disconn_tracker_t *tracker, uint8_t hci_reason)
{
    tracker->connected = false;
    if (hci_reason != (uint8_t)S31_BT_HCI_REMOTE_USER_TERMINATED || tracker->suppressed) {
        return S31_DISCONN_NOT_COUNTED;
    }
    if (tracker->strike_count < (unsigned int)S31_BT_INTENTIONAL_DISCONN_LIMIT) {
        ++tracker->strike_count;
    }
    if (tracker->strike_count >= (unsigned int)S31_BT_INTENTIONAL_DISCONN_LIMIT) {
        tracker->suppressed = true;
        return S31_DISCONN_SUPPRESSED;
    }
    return S31_DISCONN_STRIKE;
}

/* ACL up with the bound phone. While suppressed we never page, so any
 * connection is phone-initiated: that is the escape hatch. Returns true when
 * this connection lifted suppression. The strike count is NOT reset here:
 * disconnect, auto-reconnect 5 s later, disconnect again must reach strike 2. */
static inline bool s31_disconn_tracker_on_connected(s31_reconnect_disconn_tracker_t *tracker,
                                                    uint32_t now_ms)
{
    bool lifted = tracker->suppressed;
    tracker->connected = true;
    tracker->connected_since_ms = now_ms;
    if (lifted) {
        tracker->suppressed = false;
        tracker->strike_count = 0U;
    }
    return lifted;
}

/* Call periodically. A continuous connection of RESET_MS forgives strikes.
 * Returns true when strikes were just cleared. Wrap-safe. */
static inline bool s31_disconn_tracker_tick(s31_reconnect_disconn_tracker_t *tracker,
                                            uint32_t now_ms)
{
    if (!tracker->connected || tracker->strike_count == 0U || tracker->suppressed) {
        return false;
    }
    if ((uint32_t)(now_ms - tracker->connected_since_ms) <
        (uint32_t)S31_BT_INTENTIONAL_DISCONN_RESET_MS) {
        return false;
    }
    tracker->strike_count = 0U;
    return true;
}

static inline bool s31_disconn_tracker_reconnect_allowed(
    const s31_reconnect_disconn_tracker_t *tracker)
{
    return !tracker->suppressed;
}
