#include "bridge_tap.h"

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>

#define CHECK(condition)                                                                         \
    do {                                                                                         \
        if (!(condition)) {                                                                      \
            (void)fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #condition);        \
            return 1;                                                                            \
        }                                                                                        \
    } while (0)

typedef struct {
    unsigned count;
    uint8_t command[32];
    uint8_t state[32];
    uint8_t label[32];
    unsigned fail_releases; /* fail the next N release submissions */
    bool fail_presses;
} fake_stack_t;

static bool fake_send(void *context, uint8_t label, uint8_t command, uint8_t state)
{
    fake_stack_t *stack = context;
    if (state == BRIDGE_KEY_STATE_RELEASED && stack->fail_releases != 0U) {
        --stack->fail_releases;
        return false;
    }
    if (state == BRIDGE_KEY_STATE_PRESSED && stack->fail_presses) {
        return false;
    }
    if (stack->count < 32U) {
        stack->command[stack->count] = command;
        stack->state[stack->count] = state;
        stack->label[stack->count] = label;
    }
    ++stack->count;
    return true;
}

enum { STOP = 0x45, PAUSE = 0x46 };

static int test_normal_tap(void)
{
    bridge_tap_state_t tap;
    fake_stack_t stack = {0};
    bridge_tap_init(&tap);
    CHECK(bridge_tap_submit(&tap, STOP, 1U, fake_send, &stack) == BRIDGE_TAP_OK);
    CHECK(stack.count == 2U && stack.state[0] == BRIDGE_KEY_STATE_PRESSED &&
          stack.state[1] == BRIDGE_KEY_STATE_RELEASED && stack.label[0] == stack.label[1]);
    CHECK(!tap.release_pending);
    return 0;
}

static int test_retry_completes_release_only(void)
{
    bridge_tap_state_t tap;
    fake_stack_t stack = {.fail_releases = 3U};
    bridge_tap_init(&tap);
    CHECK(bridge_tap_submit(&tap, STOP, 7U, fake_send, &stack) == BRIDGE_TAP_RELEASE_FAILED);
    CHECK(bridge_tap_submit(&tap, STOP, 7U, fake_send, &stack) == BRIDGE_TAP_RELEASE_FAILED);
    CHECK(bridge_tap_submit(&tap, STOP, 7U, fake_send, &stack) == BRIDGE_TAP_RELEASE_FAILED);
    CHECK(tap.release_pending && stack.count == 1U);
    CHECK(bridge_tap_submit(&tap, STOP, 7U, fake_send, &stack) == BRIDGE_TAP_OK);
    /* Exactly one press and one release reached the phone. */
    CHECK(stack.count == 2U && stack.state[1] == BRIDGE_KEY_STATE_RELEASED);
    CHECK(!tap.release_pending);
    return 0;
}

static int test_different_key_after_exhaustion(void)
{
    bridge_tap_state_t tap;
    fake_stack_t stack = {.fail_releases = 5U};
    bridge_tap_init(&tap);
    for (int attempt = 0; attempt < 5; ++attempt) {
        CHECK(bridge_tap_submit(&tap, STOP, 9U, fake_send, &stack) == BRIDGE_TAP_RELEASE_FAILED);
    }
    /* Release path restored; a new key must not be blocked. */
    CHECK(bridge_tap_submit(&tap, PAUSE, 10U, fake_send, &stack) == BRIDGE_TAP_OK);
    CHECK(stack.count == 4U);
    CHECK(stack.command[1] == STOP && stack.state[1] == BRIDGE_KEY_STATE_RELEASED);
    CHECK(stack.command[2] == PAUSE && stack.state[2] == BRIDGE_KEY_STATE_PRESSED);
    CHECK(stack.command[3] == PAUSE && stack.state[3] == BRIDGE_KEY_STATE_RELEASED);
    CHECK(tap.stale_releases_sent == 1U && !tap.release_pending);
    return 0;
}

static int test_same_key_new_tap_is_not_swallowed(void)
{
    bridge_tap_state_t tap;
    fake_stack_t stack = {.fail_releases = 5U};
    bridge_tap_init(&tap);
    for (int attempt = 0; attempt < 5; ++attempt) {
        (void)bridge_tap_submit(&tap, STOP, 20U, fake_send, &stack);
    }
    CHECK(bridge_tap_submit(&tap, STOP, 21U, fake_send, &stack) == BRIDGE_TAP_OK);
    /* Stale release, then a fresh press and release: two presses in total. */
    unsigned presses = 0U;
    for (unsigned index = 0U; index < stack.count; ++index) {
        presses += stack.state[index] == BRIDGE_KEY_STATE_PRESSED;
    }
    CHECK(presses == 2U && stack.count == 4U);
    CHECK(stack.label[2] != stack.label[0]);
    return 0;
}

static int test_stale_release_dropped_when_still_failing(void)
{
    bridge_tap_state_t tap;
    fake_stack_t stack = {.fail_releases = 6U};
    bridge_tap_init(&tap);
    for (int attempt = 0; attempt < 5; ++attempt) {
        (void)bridge_tap_submit(&tap, STOP, 30U, fake_send, &stack);
    }
    /* The stale release fails once more: drop it and still deliver the new key. */
    CHECK(bridge_tap_submit(&tap, PAUSE, 31U, fake_send, &stack) == BRIDGE_TAP_OK);
    CHECK(tap.stale_releases_dropped == 1U && !tap.release_pending);
    CHECK(stack.command[stack.count - 1U] == PAUSE);
    return 0;
}

static int test_press_failure_and_reset(void)
{
    bridge_tap_state_t tap;
    fake_stack_t stack = {.fail_presses = true};
    bridge_tap_init(&tap);
    CHECK(bridge_tap_submit(&tap, STOP, 40U, fake_send, &stack) == BRIDGE_TAP_PRESS_FAILED);
    CHECK(!tap.release_pending && stack.count == 0U);
    stack.fail_presses = false;
    stack.fail_releases = 1U;
    CHECK(bridge_tap_submit(&tap, STOP, 41U, fake_send, &stack) == BRIDGE_TAP_RELEASE_FAILED);
    bridge_tap_reset(&tap);
    CHECK(!tap.release_pending);
    CHECK(bridge_tap_submit(&tap, STOP, 41U, fake_send, &stack) == BRIDGE_TAP_OK);
    CHECK(stack.count == 3U); /* press, then after reset a fresh press and release */
    return 0;
}

static int test_origin_mixing(void)
{
    CHECK(bridge_tap_origin(0x11223344U, 5U) != bridge_tap_origin(0x11223344U, 6U));
    CHECK(bridge_tap_origin(0x11223344U, 5U) != bridge_tap_origin(0x55667788U, 5U));
    return 0;
}

int main(void)
{
    int failed = test_normal_tap() || test_retry_completes_release_only() ||
                 test_different_key_after_exhaustion() ||
                 test_same_key_new_tap_is_not_swallowed() ||
                 test_stale_release_dropped_when_still_failing() ||
                 test_press_failure_and_reset() || test_origin_mixing();
    if (!failed) {
        (void)puts("bridge_tap_tests: all passed");
    }
    return failed;
}
