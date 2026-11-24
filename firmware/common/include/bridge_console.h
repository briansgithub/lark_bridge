#pragma once

/* Qualification console: line commands and JSON-lines records.
 *
 * Pure C with no ESP-IDF dependency, so the parser, the release/test-image
 * restriction, and the JSON writer are all covered by host tests. Each board
 * owns the transport (USB Serial/JTAG on the S31, UART0 on the output board)
 * and the command handlers. */

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define BRIDGE_CONSOLE_MAX_LINE 96U
#define BRIDGE_CONSOLE_MAX_ARGS 3U
#define BRIDGE_CONSOLE_MAX_ARG 24U

typedef enum {
    BRIDGE_CMD_INVALID = 0,
    BRIDGE_CMD_STATUS,
    BRIDGE_CMD_SELFTEST,
    BRIDGE_CMD_PEERS,
    BRIDGE_CMD_ROUTE,
    BRIDGE_CMD_STATS,
    /* Test-image only from here on. */
    BRIDGE_CMD_TONE,
    BRIDGE_CMD_TAP,
    BRIDGE_CMD_FAULT,
    BRIDGE_CMD_FORGET,
} bridge_console_command_t;

typedef enum {
    BRIDGE_IMAGE_RELEASE = 0,
    BRIDGE_IMAGE_TEST = 1,
} bridge_image_kind_t;

typedef enum {
    BRIDGE_CONSOLE_OK = 0,
    BRIDGE_CONSOLE_EMPTY,
    BRIDGE_CONSOLE_UNKNOWN,
    BRIDGE_CONSOLE_BAD_ARGS,
    BRIDGE_CONSOLE_TOO_LONG,
    BRIDGE_CONSOLE_TEST_ONLY,
} bridge_console_result_t;

typedef struct {
    bridge_console_command_t command;
    size_t argc;
    char argv[BRIDGE_CONSOLE_MAX_ARGS][BRIDGE_CONSOLE_MAX_ARG];
} bridge_console_request_t;

const char *bridge_console_command_name(bridge_console_command_t command);
const char *bridge_console_result_name(bridge_console_result_t result);
const char *bridge_image_kind_name(bridge_image_kind_t kind);
bool bridge_console_command_is_test_only(bridge_console_command_t command);

/* Parses one line (trailing CR/LF ignored). Rejects test-only commands with
 * BRIDGE_CONSOLE_TEST_ONLY unless image == BRIDGE_IMAGE_TEST. */
bridge_console_result_t bridge_console_parse(const char *line, bridge_image_kind_t image,
                                             bridge_console_request_t *request);

/* Strict decimal parsing for command arguments. */
bool bridge_console_parse_u32(const char *text, uint32_t minimum, uint32_t maximum,
                              uint32_t *value);
bool bridge_console_parse_i32(const char *text, int32_t minimum, int32_t maximum,
                              int32_t *value);

/* Bounded JSON object writer. Once the buffer would overflow, the writer stays
 * failed and bridge_json_end() returns false, so a truncated record is never
 * emitted. */
typedef struct {
    char *buffer;
    size_t capacity;
    size_t length;
    bool first;
    bool failed;
} bridge_json_t;

void bridge_json_begin(bridge_json_t *json, char *buffer, size_t capacity, const char *type);
void bridge_json_str(bridge_json_t *json, const char *key, const char *value);
void bridge_json_u32(bridge_json_t *json, const char *key, uint32_t value);
void bridge_json_i32(bridge_json_t *json, const char *key, int32_t value);
void bridge_json_u64(bridge_json_t *json, const char *key, uint64_t value);
void bridge_json_bool(bridge_json_t *json, const char *key, bool value);
/* Opens/closes a nested object value. */
void bridge_json_object_begin(bridge_json_t *json, const char *key);
void bridge_json_object_end(bridge_json_t *json);
bool bridge_json_end(bridge_json_t *json);

/* CRC-32 (IEEE, reflected) and RMS helpers for the `tap` digest. */
uint32_t bridge_crc32(uint32_t crc, const void *data, size_t length);
uint32_t bridge_pcm16_rms(const int16_t *samples, size_t count);

#ifdef __cplusplus
}
#endif
