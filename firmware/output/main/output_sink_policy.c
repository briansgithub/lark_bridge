#include "output_sink_policy.h"

#include <string.h>

#define COD_MAJOR(cod) (((cod) >> 8U) & 0x1fU)
#define COD_MINOR(cod) (((cod) >> 2U) & 0x3fU)
#define COD_MAJOR_AV 0x04U
#define COD_SRVC_RENDERING (1UL << 18U)
#define COD_SRVC_AUDIO (1UL << 21U)

static const uint8_t ZERO_ADDR[OUTPUT_SINK_ADDR_LEN] = {0};

static bool addr_equal(const uint8_t *a, const uint8_t *b)
{
    return memcmp(a, b, OUTPUT_SINK_ADDR_LEN) == 0;
}

static void copy_name(char *dest, const char *src)
{
    size_t length = 0U;
    if (src != NULL) {
        while (length < OUTPUT_SINK_NAME_MAX - 1U && src[length] != '\0') {
            ++length;
        }
        memcpy(dest, src, length);
    }
    memset(dest + length, 0, OUTPUT_SINK_NAME_MAX - length);
}

void output_sink_list_init(output_sink_list_t *list)
{
    memset(list, 0, sizeof(*list));
}

int output_sink_list_find(const output_sink_list_t *list, const uint8_t addr[OUTPUT_SINK_ADDR_LEN])
{
    for (unsigned int i = 0U; i < list->count && i < OUTPUT_SINK_MAX; ++i) {
        if (addr_equal(list->sinks[i].addr, addr)) {
            return (int)i;
        }
    }
    return -1;
}

bool output_sink_list_promote(output_sink_list_t *list, const uint8_t addr[OUTPUT_SINK_ADDR_LEN],
                              const char *name)
{
    output_sink_t entry;
    int found = output_sink_list_find(list, addr);
    bool have_name = name != NULL && name[0] != '\0';

    if (addr_equal(addr, ZERO_ADDR)) {
        return false;
    }
    if (found == 0 && (!have_name || strncmp(list->sinks[0].name, name,
                                              OUTPUT_SINK_NAME_MAX - 1U) == 0)) {
        return false;
    }
    if (found >= 0) {
        entry = list->sinks[found];
        memmove(&list->sinks[1], &list->sinks[0], (size_t)found * sizeof(output_sink_t));
    } else {
        memset(&entry, 0, sizeof(entry));
        memcpy(entry.addr, addr, OUTPUT_SINK_ADDR_LEN);
        unsigned int keep = list->count < OUTPUT_SINK_MAX ? list->count : OUTPUT_SINK_MAX - 1U;
        memmove(&list->sinks[1], &list->sinks[0], keep * sizeof(output_sink_t));
        list->count = (uint8_t)(keep + 1U);
    }
    if (have_name) {
        copy_name(entry.name, name);
    }
    list->sinks[0] = entry;
    return true;
}

bool output_sink_list_remove_index(output_sink_list_t *list, unsigned int index)
{
    if (index >= list->count) {
        return false;
    }
    memmove(&list->sinks[index], &list->sinks[index + 1U],
            (size_t)(list->count - index - 1U) * sizeof(output_sink_t));
    --list->count;
    memset(&list->sinks[list->count], 0, sizeof(output_sink_t));
    return true;
}

bool output_sink_list_remove(output_sink_list_t *list, const uint8_t addr[OUTPUT_SINK_ADDR_LEN])
{
    int found = output_sink_list_find(list, addr);
    return found >= 0 && output_sink_list_remove_index(list, (unsigned int)found);
}

void output_sink_list_encode(const output_sink_list_t *list, uint8_t blob[OUTPUT_SINK_BLOB_SIZE])
{
    memset(blob, 0, OUTPUT_SINK_BLOB_SIZE);
    blob[0] = OUTPUT_SINK_BLOB_VERSION;
    blob[1] = list->count > OUTPUT_SINK_MAX ? OUTPUT_SINK_MAX : list->count;
    for (unsigned int i = 0U; i < blob[1]; ++i) {
        uint8_t *slot = blob + 2U + i * (OUTPUT_SINK_ADDR_LEN + OUTPUT_SINK_NAME_MAX);
        memcpy(slot, list->sinks[i].addr, OUTPUT_SINK_ADDR_LEN);
        memcpy(slot + OUTPUT_SINK_ADDR_LEN, list->sinks[i].name, OUTPUT_SINK_NAME_MAX);
        slot[OUTPUT_SINK_ADDR_LEN + OUTPUT_SINK_NAME_MAX - 1U] = '\0';
    }
}

bool output_sink_list_decode(const uint8_t *blob, size_t length, output_sink_list_t *list)
{
    output_sink_list_t decoded;

    if (blob == NULL || length != OUTPUT_SINK_BLOB_SIZE || blob[0] != OUTPUT_SINK_BLOB_VERSION ||
        blob[1] > OUTPUT_SINK_MAX) {
        return false;
    }
    output_sink_list_init(&decoded);
    for (unsigned int i = 0U; i < blob[1]; ++i) {
        const uint8_t *slot = blob + 2U + i * (OUTPUT_SINK_ADDR_LEN + OUTPUT_SINK_NAME_MAX);
        if (addr_equal(slot, ZERO_ADDR) || output_sink_list_find(&decoded, slot) >= 0) {
            return false;
        }
        output_sink_t *sink = &decoded.sinks[decoded.count++];
        memcpy(sink->addr, slot, OUTPUT_SINK_ADDR_LEN);
        memcpy(sink->name, slot + OUTPUT_SINK_ADDR_LEN, OUTPUT_SINK_NAME_MAX);
        sink->name[OUTPUT_SINK_NAME_MAX - 1U] = '\0';
    }
    *list = decoded;
    return true;
}

bool output_sink_list_migrate_legacy(output_sink_list_t *list,
                                     const uint8_t legacy[OUTPUT_SINK_ADDR_LEN])
{
    if (legacy == NULL || addr_equal(legacy, ZERO_ADDR)) {
        return false;
    }
    output_sink_list_init(list);
    return output_sink_list_promote(list, legacy, NULL);
}

bool output_sink_cod_is_audio(uint32_t cod)
{
    if (COD_MAJOR(cod) != COD_MAJOR_AV) {
        return false;
    }
    if ((cod & (COD_SRVC_AUDIO | COD_SRVC_RENDERING)) != 0U) {
        return true;
    }
    switch (COD_MINOR(cod)) {
    case 0x01: /* wearable headset */
    case 0x02: /* hands-free */
    case 0x04: /* microphone */
    case 0x05: /* loudspeaker */
    case 0x06: /* headphones */
    case 0x07: /* portable audio */
    case 0x08: /* car audio */
    case 0x0a: /* HiFi audio */
        return true;
    default:
        return false;
    }
}

bool output_sink_name_is_self(const char *name)
{
    static const char PREFIX[] = "LarkBridge";
    return name != NULL && strncmp(name, PREFIX, sizeof(PREFIX) - 1U) == 0;
}

void output_sink_candidates_init(output_sink_candidates_t *candidates)
{
    memset(candidates, 0, sizeof(*candidates));
}

static void sort_candidates(output_sink_candidates_t *candidates)
{
    /* Insertion sort, strongest first; stable for equal RSSI. */
    for (unsigned int i = 1U; i < candidates->count; ++i) {
        output_sink_candidate_t item = candidates->items[i];
        unsigned int j = i;
        while (j > 0U && candidates->items[j - 1U].rssi < item.rssi) {
            candidates->items[j] = candidates->items[j - 1U];
            --j;
        }
        candidates->items[j] = item;
    }
}

bool output_sink_candidates_offer(output_sink_candidates_t *candidates,
                                  const uint8_t addr[OUTPUT_SINK_ADDR_LEN], int8_t rssi,
                                  uint32_t cod)
{
    for (unsigned int i = 0U; i < candidates->count; ++i) {
        if (addr_equal(candidates->items[i].addr, addr)) {
            if (rssi <= candidates->items[i].rssi) {
                return false;
            }
            candidates->items[i].rssi = rssi;
            candidates->items[i].cod = cod;
            sort_candidates(candidates);
            return true;
        }
    }
    unsigned int slot = candidates->count;
    if (slot >= OUTPUT_SINK_CANDIDATES_MAX) {
        slot = OUTPUT_SINK_CANDIDATES_MAX - 1U;
        if (rssi <= candidates->items[slot].rssi) {
            return false;
        }
    } else {
        ++candidates->count;
    }
    memset(&candidates->items[slot], 0, sizeof(candidates->items[slot]));
    memcpy(candidates->items[slot].addr, addr, OUTPUT_SINK_ADDR_LEN);
    candidates->items[slot].rssi = rssi;
    candidates->items[slot].cod = cod;
    sort_candidates(candidates);
    return true;
}

int output_sink_candidates_next(output_sink_candidates_t *candidates)
{
    for (unsigned int i = 0U; i < candidates->count; ++i) {
        if (!candidates->items[i].attempted) {
            candidates->items[i].attempted = true;
            return (int)i;
        }
    }
    return -1;
}

unsigned int output_sink_next_index(unsigned int cursor, unsigned int count)
{
    if (count == 0U) {
        return 0U;
    }
    return (cursor + 1U) % count;
}

void output_button_init(output_button_t *button, bool pressed_at_boot)
{
    button->pressed = pressed_at_boot;
    button->ignore_until_release = pressed_at_boot;
    button->hint = 0U;
    button->since_ms = 0U;
}

output_button_action_t output_button_update(output_button_t *button, bool pressed,
                                            uint32_t now_ms)
{
    if (pressed && !button->pressed) {
        button->pressed = true;
        button->since_ms = now_ms;
        button->hint = 0U;
        return OUTPUT_BUTTON_NONE;
    }
    if (pressed) {
        uint32_t held = now_ms - button->since_ms;
        if (button->ignore_until_release) {
            return OUTPUT_BUTTON_NONE;
        }
        if (held >= OUTPUT_BUTTON_STUCK_MS) {
            button->ignore_until_release = true;
            return OUTPUT_BUTTON_STUCK;
        }
        if (held >= OUTPUT_BUTTON_WIPE_MS && button->hint < 2U) {
            button->hint = 2U;
            return OUTPUT_BUTTON_HINT_WIPE;
        }
        if (held >= OUTPUT_BUTTON_PAIR_MS && button->hint < 1U) {
            button->hint = 1U;
            return OUTPUT_BUTTON_HINT_PAIR;
        }
        return OUTPUT_BUTTON_NONE;
    }
    if (!button->pressed) {
        return OUTPUT_BUTTON_NONE;
    }
    button->pressed = false;
    if (button->ignore_until_release) {
        button->ignore_until_release = false;
        return OUTPUT_BUTTON_NONE;
    }
    uint32_t held = now_ms - button->since_ms;
    if (held >= OUTPUT_BUTTON_WIPE_MS) {
        return OUTPUT_BUTTON_WIPE;
    }
    if (held >= OUTPUT_BUTTON_PAIR_MS) {
        return OUTPUT_BUTTON_PAIR;
    }
    return OUTPUT_BUTTON_NONE;
}
