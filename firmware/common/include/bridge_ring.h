#ifndef BRIDGE_RING_H
#define BRIDGE_RING_H

#include <stdbool.h>
#include <stddef.h>
#include <stdatomic.h>
#include <stdint.h>

/*
 * Allocation-free single-producer/single-consumer rings.  The caller owns the
 * backing storage, which must remain valid for the lifetime of the ring.  One
 * slot is deliberately left unused so full and empty are unambiguous.
 */
typedef struct {
    uint8_t *storage;
    size_t capacity;
    atomic_size_t write_index;
    atomic_size_t read_index;
} bridge_byte_ring_t;

bool bridge_byte_ring_init(bridge_byte_ring_t *ring, uint8_t *storage, size_t capacity);
size_t bridge_byte_ring_capacity(const bridge_byte_ring_t *ring);
size_t bridge_byte_ring_count(const bridge_byte_ring_t *ring);
size_t bridge_byte_ring_space(const bridge_byte_ring_t *ring);
size_t bridge_byte_ring_write(bridge_byte_ring_t *ring, const uint8_t *data, size_t length);
size_t bridge_byte_ring_read(bridge_byte_ring_t *ring, uint8_t *data, size_t length);
bool bridge_byte_ring_push(bridge_byte_ring_t *ring, uint8_t value);
bool bridge_byte_ring_pop(bridge_byte_ring_t *ring, uint8_t *value);

typedef struct {
    uint8_t *storage;
    size_t slot_count;
    size_t frame_size;
    atomic_size_t write_index;
    atomic_size_t read_index;
} bridge_frame_ring_t;

bool bridge_frame_ring_init(
    bridge_frame_ring_t *ring, uint8_t *storage, size_t slot_count, size_t frame_size);
size_t bridge_frame_ring_capacity(const bridge_frame_ring_t *ring);
size_t bridge_frame_ring_count(const bridge_frame_ring_t *ring);
bool bridge_frame_ring_push(bridge_frame_ring_t *ring, const void *frame);
bool bridge_frame_ring_pop(bridge_frame_ring_t *ring, void *frame);

#endif
