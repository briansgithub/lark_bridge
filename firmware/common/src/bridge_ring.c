#include "bridge_ring.h"

#include <string.h>

static size_t next_index(size_t index, size_t capacity) {
    return (index + 1U) % capacity;
}

bool bridge_byte_ring_init(bridge_byte_ring_t *ring, uint8_t *storage, size_t capacity) {
    if (ring == NULL || storage == NULL || capacity < 2U) {
        return false;
    }
    ring->storage = storage;
    ring->capacity = capacity;
    atomic_init(&ring->write_index, 0U);
    atomic_init(&ring->read_index, 0U);
    return true;
}

size_t bridge_byte_ring_capacity(const bridge_byte_ring_t *ring) {
    return ring == NULL || ring->capacity == 0U ? 0U : ring->capacity - 1U;
}

size_t bridge_byte_ring_count(const bridge_byte_ring_t *ring) {
    size_t write_index;
    size_t read_index;
    if (ring == NULL || ring->capacity == 0U) {
        return 0U;
    }
    write_index = atomic_load_explicit(&ring->write_index, memory_order_acquire);
    read_index = atomic_load_explicit(&ring->read_index, memory_order_acquire);
    if (write_index >= read_index) {
        return write_index - read_index;
    }
    return ring->capacity - read_index + write_index;
}

bool bridge_byte_ring_push(bridge_byte_ring_t *ring, uint8_t value) {
    size_t next;
    if (ring == NULL || ring->storage == NULL || ring->capacity < 2U) {
        return false;
    }
    size_t write_index = atomic_load_explicit(&ring->write_index, memory_order_relaxed);
    next = next_index(write_index, ring->capacity);
    if (next == atomic_load_explicit(&ring->read_index, memory_order_acquire)) {
        return false;
    }
    ring->storage[write_index] = value;
    atomic_store_explicit(&ring->write_index, next, memory_order_release);
    return true;
}

bool bridge_byte_ring_pop(bridge_byte_ring_t *ring, uint8_t *value) {
    size_t read_index;
    if (ring == NULL || value == NULL || ring->storage == NULL || ring->capacity < 2U) {
        return false;
    }
    read_index = atomic_load_explicit(&ring->read_index, memory_order_relaxed);
    if (read_index == atomic_load_explicit(&ring->write_index, memory_order_acquire)) {
        return false;
    }
    *value = ring->storage[read_index];
    atomic_store_explicit(
        &ring->read_index, next_index(read_index, ring->capacity), memory_order_release);
    return true;
}

size_t bridge_byte_ring_write(bridge_byte_ring_t *ring, const uint8_t *data, size_t length) {
    size_t written = 0U;
    if (data == NULL) {
        return 0U;
    }
    while (written < length && bridge_byte_ring_push(ring, data[written])) {
        ++written;
    }
    return written;
}

size_t bridge_byte_ring_read(bridge_byte_ring_t *ring, uint8_t *data, size_t length) {
    size_t read = 0U;
    if (data == NULL) {
        return 0U;
    }
    while (read < length && bridge_byte_ring_pop(ring, &data[read])) {
        ++read;
    }
    return read;
}

bool bridge_frame_ring_init(
    bridge_frame_ring_t *ring, uint8_t *storage, size_t slot_count, size_t frame_size) {
    if (ring == NULL || storage == NULL || slot_count < 2U || frame_size == 0U ||
        frame_size > SIZE_MAX / slot_count) {
        return false;
    }
    ring->storage = storage;
    ring->slot_count = slot_count;
    ring->frame_size = frame_size;
    atomic_init(&ring->write_index, 0U);
    atomic_init(&ring->read_index, 0U);
    return true;
}

size_t bridge_frame_ring_capacity(const bridge_frame_ring_t *ring) {
    return ring == NULL || ring->slot_count == 0U ? 0U : ring->slot_count - 1U;
}

size_t bridge_frame_ring_count(const bridge_frame_ring_t *ring) {
    size_t write_index;
    size_t read_index;
    if (ring == NULL || ring->slot_count == 0U) {
        return 0U;
    }
    write_index = atomic_load_explicit(&ring->write_index, memory_order_acquire);
    read_index = atomic_load_explicit(&ring->read_index, memory_order_acquire);
    if (write_index >= read_index) {
        return write_index - read_index;
    }
    return ring->slot_count - read_index + write_index;
}

bool bridge_frame_ring_push(bridge_frame_ring_t *ring, const void *frame) {
    size_t next;
    if (ring == NULL || frame == NULL || ring->storage == NULL || ring->slot_count < 2U ||
        ring->frame_size == 0U) {
        return false;
    }
    size_t write_index = atomic_load_explicit(&ring->write_index, memory_order_relaxed);
    next = next_index(write_index, ring->slot_count);
    if (next == atomic_load_explicit(&ring->read_index, memory_order_acquire)) {
        return false;
    }
    memcpy(&ring->storage[write_index * ring->frame_size], frame, ring->frame_size);
    atomic_store_explicit(&ring->write_index, next, memory_order_release);
    return true;
}

bool bridge_frame_ring_pop(bridge_frame_ring_t *ring, void *frame) {
    size_t read_index;
    if (ring == NULL || frame == NULL || ring->storage == NULL || ring->slot_count < 2U ||
        ring->frame_size == 0U) {
        return false;
    }
    read_index = atomic_load_explicit(&ring->read_index, memory_order_relaxed);
    if (read_index == atomic_load_explicit(&ring->write_index, memory_order_acquire)) {
        return false;
    }
    memcpy(frame, &ring->storage[read_index * ring->frame_size], ring->frame_size);
    atomic_store_explicit(
        &ring->read_index, next_index(read_index, ring->slot_count), memory_order_release);
    return true;
}
