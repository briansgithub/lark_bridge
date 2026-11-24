#include "bridge_console.h"

#include <limits.h>
#include <stdio.h>
#include <string.h>

typedef struct {
    const char *name;
    bridge_console_command_t command;
    size_t min_args;
    size_t max_args;
} command_spec_t;

static const command_spec_t COMMANDS[] = {
    {"status", BRIDGE_CMD_STATUS, 0U, 0U},
    {"selftest", BRIDGE_CMD_SELFTEST, 0U, 0U},
    {"peers", BRIDGE_CMD_PEERS, 0U, 0U},
    {"route", BRIDGE_CMD_ROUTE, 0U, 0U},
    {"stats", BRIDGE_CMD_STATS, 0U, 0U},
    /* tone <stage> <hz> <dbfs> ; "tone off" stops every generator. */
    {"tone", BRIDGE_CMD_TONE, 1U, 3U},
    {"tap", BRIDGE_CMD_TAP, 1U, 1U},
    {"fault", BRIDGE_CMD_FAULT, 1U, 1U},
    {"forget", BRIDGE_CMD_FORGET, 1U, 1U},
};

const char *bridge_console_command_name(bridge_console_command_t command)
{
    for (size_t index = 0; index < sizeof(COMMANDS) / sizeof(COMMANDS[0]); ++index) {
        if (COMMANDS[index].command == command) {
            return COMMANDS[index].name;
        }
    }
    return "invalid";
}

const char *bridge_console_result_name(bridge_console_result_t result)
{
    switch (result) {
    case BRIDGE_CONSOLE_OK:
        return "ok";
    case BRIDGE_CONSOLE_EMPTY:
        return "empty";
    case BRIDGE_CONSOLE_UNKNOWN:
        return "unknown_command";
    case BRIDGE_CONSOLE_BAD_ARGS:
        return "bad_args";
    case BRIDGE_CONSOLE_TOO_LONG:
        return "too_long";
    case BRIDGE_CONSOLE_TEST_ONLY:
        return "test_image_only";
    }
    return "invalid";
}

const char *bridge_image_kind_name(bridge_image_kind_t kind)
{
    return kind == BRIDGE_IMAGE_TEST ? "test" : "release";
}

bool bridge_console_command_is_test_only(bridge_console_command_t command)
{
    return command == BRIDGE_CMD_TONE || command == BRIDGE_CMD_TAP ||
           command == BRIDGE_CMD_FAULT || command == BRIDGE_CMD_FORGET;
}

static bool is_space(char value)
{
    return value == ' ' || value == '\t' || value == '\r' || value == '\n';
}

bridge_console_result_t bridge_console_parse(const char *line, bridge_image_kind_t image,
                                             bridge_console_request_t *request)
{
    char words[BRIDGE_CONSOLE_MAX_ARGS + 1U][BRIDGE_CONSOLE_MAX_ARG];
    size_t count = 0;
    size_t length;

    if (request == NULL) {
        return BRIDGE_CONSOLE_BAD_ARGS;
    }
    memset(request, 0, sizeof(*request));
    if (line == NULL) {
        return BRIDGE_CONSOLE_EMPTY;
    }
    for (length = 0; length <= BRIDGE_CONSOLE_MAX_LINE && line[length] != '\0'; ++length) {
    }
    if (length > BRIDGE_CONSOLE_MAX_LINE) {
        return BRIDGE_CONSOLE_TOO_LONG;
    }
    for (size_t index = 0; index < length;) {
        size_t word_length = 0;
        while (index < length && is_space(line[index])) {
            ++index;
        }
        if (index >= length) {
            break;
        }
        if (count > BRIDGE_CONSOLE_MAX_ARGS) {
            return BRIDGE_CONSOLE_BAD_ARGS;
        }
        while (index < length && !is_space(line[index])) {
            unsigned char value = (unsigned char)line[index];
            if (value < 0x21U || value > 0x7eU || word_length + 1U >= BRIDGE_CONSOLE_MAX_ARG) {
                return BRIDGE_CONSOLE_BAD_ARGS;
            }
            words[count][word_length++] = line[index++];
        }
        words[count][word_length] = '\0';
        ++count;
    }
    if (count == 0U) {
        return BRIDGE_CONSOLE_EMPTY;
    }
    for (size_t index = 0; index < sizeof(COMMANDS) / sizeof(COMMANDS[0]); ++index) {
        const command_spec_t *spec = &COMMANDS[index];
        if (strcmp(words[0], spec->name) != 0) {
            continue;
        }
        if (bridge_console_command_is_test_only(spec->command) && image != BRIDGE_IMAGE_TEST) {
            request->command = spec->command;
            return BRIDGE_CONSOLE_TEST_ONLY;
        }
        if (count - 1U < spec->min_args || count - 1U > spec->max_args) {
            return BRIDGE_CONSOLE_BAD_ARGS;
        }
        /* tone takes either "off" or exactly <stage> <hz> <dbfs>. */
        if (spec->command == BRIDGE_CMD_TONE && count != 4U &&
            !(count == 2U && strcmp(words[1], "off") == 0)) {
            return BRIDGE_CONSOLE_BAD_ARGS;
        }
        request->command = spec->command;
        request->argc = count - 1U;
        for (size_t arg = 1; arg < count; ++arg) {
            memcpy(request->argv[arg - 1U], words[arg], BRIDGE_CONSOLE_MAX_ARG);
        }
        return BRIDGE_CONSOLE_OK;
    }
    return BRIDGE_CONSOLE_UNKNOWN;
}

static bool parse_magnitude(const char *text, uint64_t limit, uint64_t *value)
{
    uint64_t result = 0;
    if (text == NULL || *text == '\0') {
        return false;
    }
    for (const char *cursor = text; *cursor != '\0'; ++cursor) {
        if (*cursor < '0' || *cursor > '9') {
            return false;
        }
        result = result * 10U + (uint64_t)(*cursor - '0');
        if (result > limit) {
            return false;
        }
    }
    *value = result;
    return true;
}

bool bridge_console_parse_u32(const char *text, uint32_t minimum, uint32_t maximum,
                              uint32_t *value)
{
    uint64_t parsed = 0;
    if (value == NULL || !parse_magnitude(text, maximum, &parsed) || parsed < minimum) {
        return false;
    }
    *value = (uint32_t)parsed;
    return true;
}

bool bridge_console_parse_i32(const char *text, int32_t minimum, int32_t maximum,
                              int32_t *value)
{
    uint64_t parsed = 0;
    bool negative;
    int64_t result;
    if (value == NULL || text == NULL) {
        return false;
    }
    negative = text[0] == '-';
    if (!parse_magnitude(negative ? text + 1 : text, (uint64_t)INT32_MAX + 1U, &parsed)) {
        return false;
    }
    result = negative ? -(int64_t)parsed : (int64_t)parsed;
    if (result < minimum || result > maximum) {
        return false;
    }
    *value = (int32_t)result;
    return true;
}

static void append(bridge_json_t *json, const char *text, size_t length)
{
    if (json->failed) {
        return;
    }
    /* Keep one byte for the terminator and one for the closing brace. */
    if (json->length + length + 2U > json->capacity) {
        json->failed = true;
        return;
    }
    memcpy(json->buffer + json->length, text, length);
    json->length += length;
    json->buffer[json->length] = '\0';
}

static void append_text(bridge_json_t *json, const char *text)
{
    append(json, text, strlen(text));
}

static void append_string(bridge_json_t *json, const char *value)
{
    append(json, "\"", 1U);
    for (const char *cursor = value != NULL ? value : ""; *cursor != '\0'; ++cursor) {
        unsigned char character = (unsigned char)*cursor;
        char escaped[8];
        if (character == '"' || character == '\\') {
            escaped[0] = '\\';
            escaped[1] = (char)character;
            append(json, escaped, 2U);
        } else if (character < 0x20U || character == 0x7fU) {
            int written = snprintf(escaped, sizeof(escaped), "\\u%04x", character);
            append(json, escaped, (size_t)written);
        } else {
            append(json, (const char *)cursor, 1U);
        }
    }
    append(json, "\"", 1U);
}

static void append_key(bridge_json_t *json, const char *key)
{
    if (!json->first) {
        append(json, ",", 1U);
    }
    json->first = false;
    append_string(json, key);
    append(json, ":", 1U);
}

void bridge_json_begin(bridge_json_t *json, char *buffer, size_t capacity, const char *type)
{
    json->buffer = buffer;
    json->capacity = capacity;
    json->length = 0;
    json->first = true;
    json->failed = buffer == NULL || capacity < 3U;
    if (!json->failed) {
        buffer[0] = '\0';
    }
    append(json, "{", 1U);
    if (type != NULL) {
        bridge_json_str(json, "t", type);
    }
}

void bridge_json_str(bridge_json_t *json, const char *key, const char *value)
{
    append_key(json, key);
    append_string(json, value);
}

void bridge_json_u32(bridge_json_t *json, const char *key, uint32_t value)
{
    bridge_json_u64(json, key, value);
}

void bridge_json_u64(bridge_json_t *json, const char *key, uint64_t value)
{
    char digits[24];
    int written = snprintf(digits, sizeof(digits), "%llu", (unsigned long long)value);
    append_key(json, key);
    append(json, digits, (size_t)written);
}

void bridge_json_i32(bridge_json_t *json, const char *key, int32_t value)
{
    char digits[16];
    int written = snprintf(digits, sizeof(digits), "%ld", (long)value);
    append_key(json, key);
    append(json, digits, (size_t)written);
}

void bridge_json_bool(bridge_json_t *json, const char *key, bool value)
{
    append_key(json, key);
    append_text(json, value ? "true" : "false");
}

void bridge_json_object_begin(bridge_json_t *json, const char *key)
{
    append_key(json, key);
    append(json, "{", 1U);
    json->first = true;
}

void bridge_json_object_end(bridge_json_t *json)
{
    append(json, "}", 1U);
    json->first = false;
}

bool bridge_json_end(bridge_json_t *json)
{
    if (json->failed || json->length + 2U > json->capacity) {
        json->failed = true;
        if (json->buffer != NULL && json->capacity > 0U) {
            json->buffer[0] = '\0';
        }
        return false;
    }
    json->buffer[json->length++] = '}';
    json->buffer[json->length] = '\0';
    return true;
}

uint32_t bridge_crc32(uint32_t crc, const void *data, size_t length)
{
    const uint8_t *bytes = (const uint8_t *)data;
    crc = ~crc;
    for (size_t index = 0; index < length; ++index) {
        crc ^= bytes[index];
        for (unsigned bit = 0; bit < 8U; ++bit) {
            crc = (crc >> 1U) ^ (0xEDB88320U & (uint32_t)-(int32_t)(crc & 1U));
        }
    }
    return ~crc;
}

uint32_t bridge_pcm16_rms(const int16_t *samples, size_t count)
{
    uint64_t sum = 0;
    if (samples == NULL || count == 0U) {
        return 0U;
    }
    for (size_t index = 0; index < count; ++index) {
        int32_t sample = samples[index];
        sum += (uint64_t)(sample * sample);
    }
    uint64_t mean = (sum + count / 2U) / count;
    /* Integer square root, rounded to nearest; mean < 2^31 so 32 bits suffice. */
    uint64_t root = 0;
    for (uint64_t bit = 1ULL << 30U; bit != 0U; bit >>= 2U) {
        if (mean >= root + bit) {
            mean -= root + bit;
            root = (root >> 1U) + bit;
        } else {
            root >>= 1U;
        }
    }
    if (mean > root) {
        ++root;
    }
    return (uint32_t)root;
}
