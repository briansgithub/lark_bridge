#ifndef BRIDGE_LINK_TEST_H
#define BRIDGE_LINK_TEST_H

#if CONFIG_LARKBRIDGE_TEST_IMAGE
#include <stdatomic.h>
#include <stdio.h>
#include <string.h>

#include "bridge_console.h"
#include "bridge_link_timing.h"
#include "bridge_protocol.h"

enum { BRIDGE_TEST_DUP = 5, BRIDGE_TEST_MUTE = 6 };

typedef struct {
    atomic_uint request;
    atomic_uint hb_rx, gap, errors, sessions, timeouts, executed, duplicates;
    uint32_t last_hb;
    uint32_t mute_until;
    bool heartbeat_seen;
    bool recover;
} bridge_link_test_t;

static inline bool bridge_link_test_request(bridge_link_test_t *test, const char *kind,
                                            bool allow_duplicate)
{
    static const char *const names[] = {
        "uart_crc", "uart_cobs", "uart_version", "uart_overlong", "uart_dup", "uart_mute"
    };
    for (unsigned i = 0; i < sizeof(names) / sizeof(names[0]); ++i) {
        if (strcmp(kind, names[i]) == 0 && (i + 1 != BRIDGE_TEST_DUP || allow_duplicate)) {
            unsigned idle = 0;
            return atomic_compare_exchange_strong(&test->request, &idle, i + 1);
        }
    }
    return false;
}

static inline bool bridge_link_test_muted(bridge_link_test_t *test, uint32_t now)
{
    if (test->mute_until && (int32_t)(now - test->mute_until) >= 0) {
        test->mute_until = 0;
    }
    return test->mute_until != 0;
}

static inline void bridge_link_test_heartbeat(bridge_link_test_t *test, uint32_t now)
{
    uint32_t gap = now - test->last_hb;
    if (test->heartbeat_seen && gap > test->gap) {
        test->gap = gap;
    }
    test->last_hb = now;
    test->heartbeat_seen = true;
    ++test->hb_rx;
}

static inline void bridge_link_test_session(bridge_link_test_t *test)
{
    ++test->sessions;
    test->heartbeat_seen = false;
    test->gap = 0;
}

static inline void bridge_link_test_timeout(bridge_link_test_t *test, const char *role,
                                            uint32_t age_ms)
{
    ++test->timeouts;
    test->heartbeat_seen = false;
    printf("{\"t\":\"event\",\"role\":\"%s\",\"ev\":\"hb_timeout\","
           "\"hb_age_ms\":%u}\n", role, (unsigned)age_ms);
}

static inline void bridge_link_test_recover(bridge_link_test_t *test, const char *role,
                                            const bridge_packet_t *packet)
{
    if (test->recover) {
        test->recover = false;
        printf("{\"t\":\"event\",\"role\":\"%s\",\"ev\":\"rx_recover\","
               "\"type\":%u,\"seq\":%u}\n", role, packet->type, packet->sequence);
    }
}

static inline void bridge_link_test_timing_json(bridge_json_t *json, const char *name,
                                                const bridge_link_timing_t *timing)
{
    bridge_json_object_begin(json, name);
    bridge_json_u32(json, "count", timing->count);
    bridge_json_u32(json, "ms", timing->last_ms);
    bridge_json_u32(json, "type", timing->last_type);
    bridge_json_u32(json, "seq", timing->last_seq);
    bridge_json_u32(json, "gap_ms", timing->max_gap_ms);
    bridge_json_u32(json, "gap_type", timing->gap_type);
    bridge_json_u32(json, "gap_seq", timing->gap_seq);
    bridge_json_object_end(json);
}

static inline void bridge_link_test_stats(const bridge_link_test_t *test, bridge_json_t *json)
{
    bridge_json_u32(json, "hb_rx", test->hb_rx);
    bridge_json_u32(json, "hb_gap_max_ms", test->gap);
    bridge_json_u32(json, "dec_err", test->errors);
    bridge_json_u32(json, "sessions", test->sessions);
    bridge_json_u32(json, "timeouts", test->timeouts);
    bridge_json_u32(json, "exec", test->executed);
    bridge_json_u32(json, "dup", test->duplicates);
}
#endif
#endif
