#ifndef BRIDGE_SESSION_H
#define BRIDGE_SESSION_H

#include <stdbool.h>

typedef enum {
    BRIDGE_ROUTE_SILENCE = 0,
    BRIDGE_ROUTE_A2DP,
    BRIDGE_ROUTE_HFP
} bridge_route_t;

typedef enum {
    BRIDGE_SESSION_EVENT_LARK_PRESENT,
    BRIDGE_SESSION_EVENT_LARK_LOST,
    BRIDGE_SESSION_EVENT_A2DP_UP,
    BRIDGE_SESSION_EVENT_A2DP_DOWN,
    BRIDGE_SESSION_EVENT_HFP_UP,
    BRIDGE_SESSION_EVENT_HFP_DOWN,
    BRIDGE_SESSION_EVENT_CALL_STARTED,
    BRIDGE_SESSION_EVENT_CALL_ENDED,
    BRIDGE_SESSION_EVENT_AEC_READY,
    BRIDGE_SESSION_EVENT_AEC_LOST
} bridge_session_event_t;

typedef struct {
    bool lark_present;
    bool a2dp_up;
    bool hfp_up;
    bool call_active;
    bool aec_ready;
    bool hfp_uplink_enabled;
    bridge_route_t route;
} bridge_session_t;

void bridge_session_init(bridge_session_t *session);
void bridge_session_apply(bridge_session_t *session, bridge_session_event_t event);

#endif
