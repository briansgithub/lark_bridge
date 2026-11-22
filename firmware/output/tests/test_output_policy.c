#include "output_policy.h"

#include <stdio.h>

#define CHECK(condition)                                                                  \
    do {                                                                                  \
        if (!(condition)) {                                                               \
            (void)fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #condition); \
            return 1;                                                                     \
        }                                                                                 \
    } while (0)

int main(void) {
    output_policy_t policy;
    uint16_t caps = (uint16_t)(BRIDGE_CAP_I2S_TX | BRIDGE_CAP_FAIL_CLOSED |
                               BRIDGE_CAP_AVRCP_CONTROLLER);

    output_policy_init(&policy, 1500U);
    CHECK(!output_policy_audio_allowed(&policy, 0U));
    CHECK(!output_policy_accept_hello(&policy, BRIDGE_ROLE_OUTPUT, caps));
    CHECK(output_policy_accept_hello(&policy, BRIDGE_ROLE_S31_GATEWAY, caps));
    CHECK(!output_policy_accept_heartbeat(&policy, 100U, 99U, 0U));
    CHECK(output_policy_accept_heartbeat(
        &policy, 100U, BRIDGE_ROUTE_A2DP, BRIDGE_READY_I2S | BRIDGE_READY_UART));
    CHECK(!output_policy_audio_allowed(&policy, 100U));
    CHECK(policy.route == BRIDGE_ROUTE_SILENCE);
    CHECK(output_policy_accept_route(&policy, BRIDGE_ROUTE_A2DP));
    CHECK(output_policy_audio_allowed(&policy, 1599U));
    CHECK(!output_policy_tick(&policy, 1599U));
    CHECK(output_policy_tick(&policy, 1600U));
    CHECK(!output_policy_audio_allowed(&policy, 1600U));
    CHECK(policy.route == BRIDGE_ROUTE_A2DP);

    /* A transient timeout must not erase the last reliable route. A fresh,
     * matching heartbeat from the same S31 boot reopens the gate without a
     * route transaction that the already-active S31 fence will not resend. */
    CHECK(output_policy_accept_heartbeat(
        &policy, 1700U, BRIDGE_ROUTE_A2DP, BRIDGE_READY_I2S | BRIDGE_READY_UART));
    CHECK(output_policy_audio_allowed(&policy, 1700U));

    /* uint32_t subtraction makes the heartbeat check wrap-safe. */
    output_policy_init(&policy, 20U);
    CHECK(output_policy_accept_hello(&policy, BRIDGE_ROLE_S31_GATEWAY, caps));
    CHECK(output_policy_accept_heartbeat(
        &policy, UINT32_MAX - 5U, BRIDGE_ROUTE_HFP, BRIDGE_READY_I2S | BRIDGE_READY_UART));
    CHECK(output_policy_accept_route(&policy, BRIDGE_ROUTE_HFP));
    CHECK(output_policy_audio_allowed(&policy, 3U));
    CHECK(output_policy_tick(&policy, 20U));

    (void)puts("output policy tests passed");
    return 0;
}
