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
