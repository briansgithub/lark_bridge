#include "bridge_meta.h"

#include "bridge_link.h"

#include <string.h>

size_t bridge_utf8_truncate(const char *text, size_t length, size_t max)
{
    if (text == NULL) {
        return 0U;
    }
    if (length <= max) {
        return length;
    }
    size_t cut = max;
    /* text[cut] is the first dropped byte. If it is a continuation byte, the
     * sequence it belongs to started before cut: drop that lead byte too. */
    while (cut > 0U && ((uint8_t)text[cut] & 0xc0U) == 0x80U) {
        --cut;
    }
    return cut;
}

size_t bridge_meta_pack_play_status(uint8_t *output, size_t capacity, uint8_t generation,
                                    uint8_t play_status, uint8_t flags, uint32_t length_ms,
                                    uint32_t position_ms)
{
    if (output == NULL || capacity < BRIDGE_META_PLAY_STATUS_SIZE) {
        return 0U;
    }
    output[0] = BRIDGE_META_KIND_PLAY_STATUS;
    output[1] = generation;
    output[2] = play_status;
    output[3] = flags;
    bridge_put_u32_le(&output[4], length_ms);
    bridge_put_u32_le(&output[8], position_ms);
    return BRIDGE_META_PLAY_STATUS_SIZE;
}

size_t bridge_meta_pack_text(uint8_t *output, size_t capacity, uint8_t generation,
                             uint8_t attr_id, const char *text, size_t length)
{
    if (output == NULL || attr_id < 1U || attr_id > BRIDGE_META_ATTR_COUNT ||
        (text == NULL && length != 0U)) {
        return 0U;
    }
    /* Stop at an embedded NUL so the receiver's strict decode accepts it. */
    if (text != NULL) {
        const char *nul = memchr(text, '\0', length);
        if (nul != NULL) {
            length = (size_t)(nul - text);
        }
    }
    size_t used = bridge_utf8_truncate(text, length, BRIDGE_META_TEXT_MAX);
    if (capacity < BRIDGE_META_TEXT_HEADER_SIZE + used) {
        return 0U;
    }
    output[0] = BRIDGE_META_KIND_TEXT_ATTR;
    output[1] = generation;
    output[2] = attr_id;
    output[3] = (uint8_t)used;
    if (used != 0U) {
        memcpy(&output[BRIDGE_META_TEXT_HEADER_SIZE], text, used);
    }
    return BRIDGE_META_TEXT_HEADER_SIZE + used;
}

size_t bridge_meta_pack_clear(uint8_t *output, size_t capacity, uint8_t generation)
{
    if (output == NULL || capacity < BRIDGE_META_CLEAR_SIZE) {
        return 0U;
    }
    output[0] = BRIDGE_META_KIND_CLEAR;
    output[1] = generation;
    return BRIDGE_META_CLEAR_SIZE;
}

bool bridge_meta_unpack(const uint8_t *input, size_t length, bridge_meta_record_t *record)
{
    if (input == NULL || record == NULL || length < 2U) {
        return false;
    }
    memset(record, 0, sizeof(*record));
    record->kind = input[0];
    record->generation = input[1];
    switch (input[0]) {
    case BRIDGE_META_KIND_PLAY_STATUS:
        if (length != BRIDGE_META_PLAY_STATUS_SIZE) {
            return false;
        }
        record->play_status = input[2];
        record->flags = input[3];
        record->song_length_ms = bridge_get_u32_le(&input[4]);
        record->song_position_ms = bridge_get_u32_le(&input[8]);
        return true;
    case BRIDGE_META_KIND_TEXT_ATTR:
        if (length < BRIDGE_META_TEXT_HEADER_SIZE || input[2] < 1U ||
            input[2] > BRIDGE_META_ATTR_COUNT || input[3] > BRIDGE_META_TEXT_MAX ||
            length != BRIDGE_META_TEXT_HEADER_SIZE + (size_t)input[3] ||
            memchr(&input[BRIDGE_META_TEXT_HEADER_SIZE], '\0', input[3]) != NULL) {
            return false;
        }
        record->attr_id = input[2];
        record->text_length = input[3];
        memcpy(record->text, &input[BRIDGE_META_TEXT_HEADER_SIZE], input[3]);
        record->text[input[3]] = '\0';
        return true;
    case BRIDGE_META_KIND_CLEAR:
        return length == BRIDGE_META_CLEAR_SIZE;
    default:
        return false;
    }
}
