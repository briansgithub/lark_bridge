#ifndef BRIDGE_LINK_TIMING_H
#define BRIDGE_LINK_TIMING_H

#include <stdbool.h>
#include <stdint.h>

/* One writer. Callers synchronize snapshots taken by a telemetry task. */
typedef struct {
    bool seen;
    uint32_t count;
    uint32_t last_ms;
    uint8_t last_type;
    uint16_t last_seq;
    uint32_t max_gap_ms;
    uint8_t gap_type;
    uint16_t gap_seq;
} bridge_link_timing_t;

static inline void bridge_link_timing_note(bridge_link_timing_t *timing, uint32_t now_ms,
                                           uint8_t type, uint16_t sequence)
{
    uint32_t gap = now_ms - timing->last_ms;
    if (timing->seen && gap > timing->max_gap_ms) {
        timing->max_gap_ms = gap;
        timing->gap_type = type;
        timing->gap_seq = sequence;
    }
    timing->seen = true;
    ++timing->count;
    timing->last_ms = now_ms;
    timing->last_type = type;
    timing->last_seq = sequence;
}

#endif
