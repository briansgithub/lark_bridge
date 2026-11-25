#include "bridge_console.h"

#include <stdio.h>
#include <string.h>

#define CHECK(condition)                                                                  \
    do {                                                                                  \
        if (!(condition)) {                                                               \
            (void)fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #condition); \
            return 1;                                                                     \
        }                                                                                 \
    } while (0)

static int test_parse_release(void) {
    bridge_console_request_t request;
    CHECK(bridge_console_parse("status\r\n", BRIDGE_IMAGE_RELEASE, &request) == BRIDGE_CONSOLE_OK);
    CHECK(request.command == BRIDGE_CMD_STATUS && request.argc == 0U);
    CHECK(bridge_console_parse("  peers  ", BRIDGE_IMAGE_RELEASE, &request) == BRIDGE_CONSOLE_OK);
    CHECK(request.command == BRIDGE_CMD_PEERS);
    CHECK(bridge_console_parse("", BRIDGE_IMAGE_RELEASE, &request) == BRIDGE_CONSOLE_EMPTY);
    CHECK(bridge_console_parse(" \r\n", BRIDGE_IMAGE_RELEASE, &request) == BRIDGE_CONSOLE_EMPTY);
    CHECK(bridge_console_parse("reboot", BRIDGE_IMAGE_RELEASE, &request) == BRIDGE_CONSOLE_UNKNOWN);
    CHECK(bridge_console_parse("status now", BRIDGE_IMAGE_RELEASE, &request) ==
          BRIDGE_CONSOLE_BAD_ARGS);
    /* Every test hook is refused by a release image, even with valid arguments. */
    CHECK(bridge_console_parse("tone i2s_out 1000 -20", BRIDGE_IMAGE_RELEASE, &request) ==
          BRIDGE_CONSOLE_TEST_ONLY);
    CHECK(bridge_console_parse("tap lark_in", BRIDGE_IMAGE_RELEASE, &request) ==
          BRIDGE_CONSOLE_TEST_ONLY);
    CHECK(bridge_console_parse("fault crc", BRIDGE_IMAGE_RELEASE, &request) ==
          BRIDGE_CONSOLE_TEST_ONLY);
    CHECK(bridge_console_parse("forget pixel", BRIDGE_IMAGE_RELEASE, &request) ==
          BRIDGE_CONSOLE_TEST_ONLY);
    return 0;
}

static int test_parse_test_image(void) {
    bridge_console_request_t request;
    char long_line[BRIDGE_CONSOLE_MAX_LINE + 8U];
    CHECK(bridge_console_parse("tone i2s_out 1000 -20", BRIDGE_IMAGE_TEST, &request) ==
          BRIDGE_CONSOLE_OK);
    CHECK(request.command == BRIDGE_CMD_TONE && request.argc == 3U);
    CHECK(strcmp(request.argv[0], "i2s_out") == 0 && strcmp(request.argv[2], "-20") == 0);
    CHECK(bridge_console_parse("tone off", BRIDGE_IMAGE_TEST, &request) == BRIDGE_CONSOLE_OK);
    CHECK(bridge_console_parse("tone i2s_out", BRIDGE_IMAGE_TEST, &request) ==
          BRIDGE_CONSOLE_BAD_ARGS);
    CHECK(bridge_console_parse("tap", BRIDGE_IMAGE_TEST, &request) == BRIDGE_CONSOLE_BAD_ARGS);
    CHECK(bridge_console_parse("tap a b c d e", BRIDGE_IMAGE_TEST, &request) ==
          BRIDGE_CONSOLE_BAD_ARGS);
    CHECK(bridge_console_parse("tap \x01", BRIDGE_IMAGE_TEST, &request) == BRIDGE_CONSOLE_BAD_ARGS);
    memset(long_line, 'a', sizeof(long_line) - 1U);
    long_line[sizeof(long_line) - 1U] = '\0';
    CHECK(bridge_console_parse(long_line, BRIDGE_IMAGE_TEST, &request) == BRIDGE_CONSOLE_TOO_LONG);
    CHECK(bridge_console_parse("tap aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", BRIDGE_IMAGE_TEST, &request) ==
          BRIDGE_CONSOLE_BAD_ARGS);
    return 0;
}

static int test_numbers(void) {
    uint32_t u = 0;
    int32_t i = 0;
    CHECK(bridge_console_parse_u32("1000", 20U, 20000U, &u) && u == 1000U);
    CHECK(!bridge_console_parse_u32("19", 20U, 20000U, &u));
    CHECK(!bridge_console_parse_u32("20001", 20U, 20000U, &u));
    CHECK(!bridge_console_parse_u32("10a", 0U, 100U, &u));
    CHECK(!bridge_console_parse_u32("", 0U, 100U, &u));
    CHECK(!bridge_console_parse_u32("99999999999999999999", 0U, UINT32_MAX, &u));
    CHECK(bridge_console_parse_i32("-20", -60, 0, &i) && i == -20);
    CHECK(bridge_console_parse_i32("0", -60, 0, &i) && i == 0);
    CHECK(!bridge_console_parse_i32("-61", -60, 0, &i));
    CHECK(!bridge_console_parse_i32("1", -60, 0, &i));
    CHECK(!bridge_console_parse_i32("-", -60, 0, &i));
    return 0;
}

static int test_json(void) {
    char buffer[128];
    char tiny[16];
    bridge_json_t json;
    bridge_json_begin(&json, buffer, sizeof(buffer), "stats");
    bridge_json_u32(&json, "up", 12U);
    bridge_json_i32(&json, "ppm", -7);
    bridge_json_bool(&json, "ok", true);
    bridge_json_str(&json, "s", "a\"b\\c\n");
    bridge_json_object_begin(&json, "heap");
    bridge_json_u32(&json, "free", 5U);
    bridge_json_u32(&json, "min", 4U);
    bridge_json_object_end(&json);
    bridge_json_u64(&json, "big", 5000000000ULL);
    CHECK(bridge_json_end(&json));
    CHECK(strcmp(buffer,
                 "{\"t\":\"stats\",\"up\":12,\"ppm\":-7,\"ok\":true,\"s\":\"a\\\"b\\\\c\\u000a\","
                 "\"heap\":{\"free\":5,\"min\":4},\"big\":5000000000}") == 0);
    bridge_json_begin(&json, tiny, sizeof(tiny), "stats");
    bridge_json_u32(&json, "uptime", 123456U);
    CHECK(!bridge_json_end(&json));
    CHECK(tiny[0] == '\0');
    /* Exactly fitting record. */
    bridge_json_begin(&json, tiny, 11U, "ab");
    CHECK(bridge_json_end(&json));
    CHECK(strcmp(tiny, "{\"t\":\"ab\"}") == 0);
    bridge_json_begin(&json, tiny, 10U, "ab");
    CHECK(!bridge_json_end(&json));
    return 0;
}

static int test_digests(void) {
    const int16_t square[4] = {1000, -1000, 1000, -1000};
    const int16_t full[2] = {32767, -32768};
    CHECK(bridge_crc32(0U, "123456789", 9U) == 0xCBF43926U);
    CHECK(bridge_crc32(bridge_crc32(0U, "1234", 4U), "56789", 5U) == 0xCBF43926U);
    CHECK(bridge_pcm16_rms(square, 4U) == 1000U);
    CHECK(bridge_pcm16_rms(full, 2U) == 32768U || bridge_pcm16_rms(full, 2U) == 32767U);
    CHECK(bridge_pcm16_rms(NULL, 0U) == 0U);
    return 0;
}

static int test_stereo_stats(void) {
    /* Left 1000/-1000, right the H4 marker: inverted at half amplitude. */
    const int16_t marker[8] = {1000, -500, -1000, 500, 1000, -500, -1000, 500};
    const int16_t silence[6] = {0, 0, 0, 0, 0, 0};
    const int16_t one_lsb[4] = {0, 0, 0, 1};
    bridge_pcm16_stereo_stats_t stats;
    bridge_pcm16_stereo_stats(marker, 4U, &stats);
    CHECK(stats.nonzero_frames == 4U);
    CHECK(stats.rms_left == 1000U && stats.rms_right == 500U && stats.nonzero);
    bridge_pcm16_stereo_stats(silence, 3U, &stats);
    CHECK(stats.nonzero_frames == 0U);
    CHECK(stats.rms_left == 0U && stats.rms_right == 0U && !stats.nonzero);
    bridge_pcm16_stereo_stats(one_lsb, 2U, &stats);
    CHECK(stats.nonzero_frames == 1U);
    CHECK(stats.rms_left == 0U && stats.rms_right == 1U && stats.nonzero);
    bridge_pcm16_stereo_stats(NULL, 0U, &stats);
    CHECK(stats.nonzero_frames == 0U);
    CHECK(stats.rms_left == 0U && !stats.nonzero);
    return 0;
}

int main(void) {
    int failures = test_parse_release() + test_parse_test_image() + test_numbers() + test_json() +
                   test_digests() + test_stereo_stats();
    if (failures == 0) {
        (void)printf("bridge_console_tests: PASS\n");
    }
    return failures == 0 ? 0 : 1;
}
