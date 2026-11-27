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

    /* Feature 1: 2-strike intentional-disconnect suppression. */
    {
        s31_reconnect_disconn_tracker_t t;
        s31_disconn_tracker_init(&t);
        CHECK(s31_disconn_tracker_reconnect_allowed(&t));
        CHECK(!s31_disconn_tracker_on_connected(&t, 1000U));

        /* Link loss / local recycle never count. */
        CHECK(s31_disconn_tracker_on_disconnect(&t, 0x08U) == S31_DISCONN_NOT_COUNTED);
        CHECK(s31_disconn_tracker_on_disconnect(&t, 0x16U) == S31_DISCONN_NOT_COUNTED);
        CHECK(t.strike_count == 0U && s31_disconn_tracker_reconnect_allowed(&t));

        /* Strike 1 keeps reconnect enabled (H14 single toggle recovery). */
        CHECK(!s31_disconn_tracker_on_connected(&t, 2000U));
        CHECK(s31_disconn_tracker_on_disconnect(&t, 0x13U) == S31_DISCONN_STRIKE);
        CHECK(t.strike_count == 1U && s31_disconn_tracker_reconnect_allowed(&t));

        /* Auto-reconnect 5 s later must NOT reset the strike count... */
        CHECK(!s31_disconn_tracker_on_connected(&t, 7000U));
        CHECK(!s31_disconn_tracker_tick(&t, 7000U + 60000U));
        CHECK(t.strike_count == 1U);
        /* ...so the second user disconnect suppresses. */
        CHECK(s31_disconn_tracker_on_disconnect(&t, 0x13U) == S31_DISCONN_SUPPRESSED);
        CHECK(t.suppressed && !s31_disconn_tracker_reconnect_allowed(&t));
        /* Further disconnects while suppressed change nothing. */
        CHECK(s31_disconn_tracker_on_disconnect(&t, 0x13U) == S31_DISCONN_NOT_COUNTED);
        CHECK(t.strike_count == 2U);
        /* Ticks while suppressed never lift it. */
        CHECK(!s31_disconn_tracker_tick(&t, 900000U));
        CHECK(t.suppressed);

        /* Escape hatch: phone-initiated connect lifts suppression and strikes. */
        CHECK(s31_disconn_tracker_on_connected(&t, 1000000U));
        CHECK(!t.suppressed && t.strike_count == 0U && s31_disconn_tracker_reconnect_allowed(&t));
    }
    {
        /* Sustained 120 s connection forgives a strike (boundary, wrap-safe). */
        const uint32_t base = UINT32_MAX - 30000U;
        s31_reconnect_disconn_tracker_t t;
        s31_disconn_tracker_init(&t);
        (void)s31_disconn_tracker_on_connected(&t, base);
        CHECK(s31_disconn_tracker_on_disconnect(&t, 0x13U) == S31_DISCONN_STRIKE);
        (void)s31_disconn_tracker_on_connected(&t, base);
        CHECK(!s31_disconn_tracker_tick(&t, base + S31_BT_INTENTIONAL_DISCONN_RESET_MS - 1U));
        CHECK(t.strike_count == 1U);
        CHECK(s31_disconn_tracker_tick(&t, base + S31_BT_INTENTIONAL_DISCONN_RESET_MS));
        CHECK(t.strike_count == 0U);
        CHECK(!s31_disconn_tracker_tick(&t, base + S31_BT_INTENTIONAL_DISCONN_RESET_MS + 5U));
        /* After forgiveness a single user disconnect is strike 1 again. */
        CHECK(s31_disconn_tracker_on_disconnect(&t, 0x13U) == S31_DISCONN_STRIKE);
        /* A disconnected stretch never counts as sustained. */
        CHECK(!s31_disconn_tracker_tick(&t, base + 10U * S31_BT_INTENTIONAL_DISCONN_RESET_MS));
        CHECK(t.strike_count == 1U);
    }
    return 0;
}
