#include "bridge_session.h"

#include <stddef.h>

static void recalculate(bridge_session_t *session) {
    session->hfp_uplink_enabled =
        session->lark_present && session->hfp_up && session->call_active && session->aec_ready;
    if (session->call_active) {
        /* A call never falls through to media: an unavailable HFP route is silence. */
        session->route = session->hfp_up ? BRIDGE_ROUTE_HFP : BRIDGE_ROUTE_SILENCE;
    } else {
        session->route = session->a2dp_up ? BRIDGE_ROUTE_A2DP : BRIDGE_ROUTE_SILENCE;
    }
}

void bridge_session_init(bridge_session_t *session) {
    if (session == NULL) {
        return;
    }
    session->lark_present = false;
    session->a2dp_up = false;
    session->hfp_up = false;
    session->call_active = false;
    session->aec_ready = false;
    session->hfp_uplink_enabled = false;
    session->route = BRIDGE_ROUTE_SILENCE;
}

void bridge_session_apply(bridge_session_t *session, bridge_session_event_t event) {
    if (session == NULL) {
        return;
    }
    switch (event) {
    case BRIDGE_SESSION_EVENT_LARK_PRESENT:
        session->lark_present = true;
        break;
    case BRIDGE_SESSION_EVENT_LARK_LOST:
        session->lark_present = false;
        break;
    case BRIDGE_SESSION_EVENT_A2DP_UP:
        session->a2dp_up = true;
        break;
    case BRIDGE_SESSION_EVENT_A2DP_DOWN:
        session->a2dp_up = false;
        break;
    case BRIDGE_SESSION_EVENT_HFP_UP:
        session->hfp_up = true;
        break;
    case BRIDGE_SESSION_EVENT_HFP_DOWN:
        session->hfp_up = false;
        break;
    case BRIDGE_SESSION_EVENT_CALL_STARTED:
        session->call_active = true;
        break;
    case BRIDGE_SESSION_EVENT_CALL_ENDED:
        session->call_active = false;
        break;
    case BRIDGE_SESSION_EVENT_AEC_READY:
        session->aec_ready = true;
        break;
    case BRIDGE_SESSION_EVENT_AEC_LOST:
        session->aec_ready = false;
        break;
    default:
        return;
    }
    recalculate(session);
}
