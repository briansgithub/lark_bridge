#include "reconnect_timer.h"

#include <stdio.h>

#define CHECK(cond)                                                                                \
    do {                                                                                           \
        if (!(cond)) {                                                                             \
            fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);                        \
            return 1;                                                                              \
        }                                                                                          \
    } while (0)

int main(void)
{
    const uint32_t starts[] = {100U, UINT32_MAX - 2000U};
    for (unsigned int i = 0; i < sizeof(starts) / sizeof(starts[0]); ++i) {
        uint32_t start = starts[i];
        bool held = true;
        if (!s31_reconnect_boot_held(&held, start, start) ||
            !s31_reconnect_boot_held(&held, start, start + 1000U) ||
            !s31_reconnect_boot_held(&held, start, start + 4999U) ||
            s31_reconnect_boot_held(&held, start, start + 5000U) ||
            s31_reconnect_boot_held(&held, start, start + 6000U) ||
            s31_reconnect_boot_held(&held, start, start)) {
            fprintf(stderr, "boot hold failed for start tick %u\n", (unsigned int)start);
            return 1;
        }
    }

    /* Exponential backoff clamps at S31_BT_RECONNECT_MAX_MS. */
    CHECK(s31_reconnect_next_backoff(0U) == S31_BT_RECONNECT_INITIAL_MS);
    CHECK(s31_reconnect_next_backoff(S31_BT_RECONNECT_INITIAL_MS) ==
          S31_BT_RECONNECT_INITIAL_MS * 2U);
    CHECK(s31_reconnect_next_backoff(12000U) == 24000U);
    CHECK(s31_reconnect_next_backoff(24000U) == S31_BT_RECONNECT_MAX_MS);
    CHECK(s31_reconnect_next_backoff(S31_BT_RECONNECT_MAX_MS) == S31_BT_RECONNECT_MAX_MS);

    /* Never issue concurrent connects or page over a connecting profile. */
    CHECK(s31_reconnect_choose_profile(true, false, true, false, false, true) ==
          S31_RECONNECT_NONE);
    CHECK(s31_reconnect_choose_profile(false, true, false, true, true, true) == S31_RECONNECT_NONE);
    CHECK(s31_reconnect_choose_profile(false, true, false, false, false, true) ==
          S31_RECONNECT_NONE);
    CHECK(s31_reconnect_choose_profile(false, false, false, true, false, false) ==
          S31_RECONNECT_NONE);

    /* One profile up -> complete the missing profile over the live ACL. */
    CHECK(s31_reconnect_choose_profile(true, false, false, true, false, true) ==
          S31_RECONNECT_A2DP);
    CHECK(s31_reconnect_choose_profile(false, true, true, false, false, false) ==
          S31_RECONNECT_HFP);

    /* Both down -> alternate single profile pages. */
    CHECK(s31_reconnect_choose_profile(false, true, false, true, false, true) == S31_RECONNECT_HFP);
    CHECK(s31_reconnect_choose_profile(false, true, false, true, false, false) ==
          S31_RECONNECT_A2DP);

    /* Recycle predicate and wrap-safe cooldown. */
    CHECK(!s31_reconnect_should_recycle(0U, false, true));
    CHECK(!s31_reconnect_should_recycle(1U, false, true));
    CHECK(s31_reconnect_should_recycle(1U, true, true));
    CHECK(s31_reconnect_should_recycle(2U, false, true));
    CHECK(!s31_reconnect_should_recycle(2U, true, false));

    CHECK(s31_reconnect_cooldown_elapsed(false, 0U, 0U));
    CHECK(!s31_reconnect_cooldown_elapsed(true, UINT32_MAX - 1000U, 1000U));
    CHECK(!s31_reconnect_cooldown_elapsed(true, UINT32_MAX - 1000U,
                                          S31_BT_RECONNECT_RECYCLE_COOLDOWN_MS - 1002U));
    CHECK(s31_reconnect_cooldown_elapsed(true, UINT32_MAX - 1000U,
                                         S31_BT_RECONNECT_RECYCLE_COOLDOWN_MS - 1001U));
    return 0;
}
