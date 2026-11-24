#include "s31_telemetry.h"

#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "audio_router.h"
#include "bridge_console.h"
#include "bt_gateway.h"
#include "esp_app_desc.h"
#include "esp_chip_info.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "gateway_state.h"
#include "hal/usb_serial_jtag_ll.h"
#include "link_uart.h"
#include "sdkconfig.h"
#include "uac_lark.h"

static const char *TAG = "s31_telemetry";

#if CONFIG_LARKBRIDGE_TEST_IMAGE
#define IMAGE_KIND BRIDGE_IMAGE_TEST
#else
#define IMAGE_KIND BRIDGE_IMAGE_RELEASE
#endif

#define RECORD_BYTES 1280U

#if CONFIG_LARKBRIDGE_TEST_IMAGE
typedef struct {
    atomic_uint_fast32_t sequence;
    atomic_uint_fast32_t crc;
    atomic_uint_fast32_t rms;
    atomic_uint_fast32_t bytes;
} tap_slot_t;

static tap_slot_t s_taps[S31_TAP_COUNT];
static const char *const TAP_NAMES[S31_TAP_COUNT] = {"i2s_out", "lark_in"};
#endif

static TaskHandle_t s_stats_task;
static TaskHandle_t s_console_task;
static char s_stats_record[RECORD_BYTES];
static char s_console_record[RECORD_BYTES];

static const char *reset_name(esp_reset_reason_t reason)
{
    switch (reason) {
    case ESP_RST_POWERON:
        return "poweron";
    case ESP_RST_EXT:
        return "ext";
    case ESP_RST_SW:
        return "sw";
    case ESP_RST_PANIC:
        return "panic";
    case ESP_RST_INT_WDT:
        return "int_wdt";
    case ESP_RST_TASK_WDT:
        return "task_wdt";
    case ESP_RST_WDT:
        return "wdt";
    case ESP_RST_DEEPSLEEP:
        return "deepsleep";
    case ESP_RST_BROWNOUT:
        return "brownout";
    case ESP_RST_SDIO:
        return "sdio";
    case ESP_RST_USB:
        return "usb";
    case ESP_RST_JTAG:
        return "jtag";
    default:
        return "unknown";
    }
}

static void format_address(char *text, size_t size, const uint8_t address[6])
{
    (void)snprintf(text, size, "%02x:%02x:%02x:%02x:%02x:%02x", address[0], address[1],
                   address[2], address[3], address[4], address[5]);
}

static void emit(char *buffer, bridge_json_t *json)
{
    if (bridge_json_end(json)) {
        printf("%s\n", buffer);
    } else {
        printf("{\"t\":\"error\",\"role\":\"s31\",\"err\":\"record_overflow\"}\n");
    }
    fflush(stdout);
}

static void add_identity(bridge_json_t *json)
{
    const esp_app_desc_t *app = esp_app_get_description();
    bridge_json_str(json, "role", "s31");
    bridge_json_str(json, "version", app->version);
    bridge_json_str(json, "image", bridge_image_kind_name(IMAGE_KIND));
}

static void add_heap(bridge_json_t *json)
{
    bridge_json_object_begin(json, "heap");
    bridge_json_u32(json, "free", (uint32_t)heap_caps_get_free_size(MALLOC_CAP_8BIT));
    bridge_json_u32(json, "min", (uint32_t)heap_caps_get_minimum_free_size(MALLOC_CAP_8BIT));
    bridge_json_u32(json, "largest",
                    (uint32_t)heap_caps_get_largest_free_block(MALLOC_CAP_8BIT));
    bridge_json_object_end(json);
}

static void add_route(bridge_json_t *json, const s31_gateway_snapshot_t *gateway)
{
    bridge_json_object_begin(json, "route");
    bridge_json_u32(json, "desired", (uint32_t)gateway->desired_route);
    bridge_json_u32(json, "authorized", (uint32_t)gateway->authorized_route);
    bridge_json_u32(json, "effective", (uint32_t)gateway->effective_route);
    bridge_json_u32(json, "generation", gateway->route_generation);
    bridge_json_object_end(json);
}

static void add_bt(bridge_json_t *json, bool with_peer)
{
    s31_bt_status_t bt;
    memset(&bt, 0, sizeof(bt));
    s31_bt_get_status(&bt);
    bridge_json_object_begin(json, "bt");
    bridge_json_i32(json, "hfp_state", bt.hfp_service_state);
    bridge_json_i32(json, "a2dp_state", bt.a2dp_connection_state);
    bridge_json_u32(json, "a2dp_rx", bt.a2dp_rx_packets);
    bridge_json_u32(json, "a2dp_bytes", bt.a2dp_rx_bytes);
    bridge_json_bool(json, "hfp_audio", bt.hfp_audio_connected);
    bridge_json_bool(json, "avrcp", bt.avrcp_connected);
    bridge_json_bool(json, "peer_bound", bt.peer_bound);
    if (with_peer && bt.peer_bound) {
        char address[18];
        format_address(address, sizeof(address), bt.peer);
        bridge_json_str(json, "peer", address);
    }
    bridge_json_object_end(json);
}

static void build_stats(bridge_json_t *json, char *buffer, const char *type)
{
    s31_gateway_snapshot_t gateway;
    s31_audio_stats_t audio;
    s31_link_stats_t link;
    s31_uac_stats_t uac;

    memset(&gateway, 0, sizeof(gateway));
    memset(&audio, 0, sizeof(audio));
    memset(&link, 0, sizeof(link));
    memset(&uac, 0, sizeof(uac));
    s31_gateway_get_snapshot(&gateway);
    s31_audio_router_get_stats(&audio);
    s31_link_uart_get_stats(&link);
    s31_uac_lark_get_stats(&uac);

    bridge_json_begin(json, buffer, RECORD_BYTES, type);
    add_identity(json);
    bridge_json_u64(json, "up_ms", (uint64_t)(esp_timer_get_time() / 1000));
    bridge_json_str(json, "reset", reset_name(esp_reset_reason()));
    add_heap(json);
    bridge_json_u32(json, "ready", s31_gateway_ready_flags());
    bridge_json_u32(json, "fault", (uint32_t)gateway.last_fault);
    bridge_json_bool(json, "out_link", gateway.output_link_alive);
    bridge_json_object_begin(json, "session");
    bridge_json_bool(json, "lark", gateway.session.lark_present);
    bridge_json_bool(json, "a2dp", gateway.session.a2dp_up);
    bridge_json_bool(json, "hfp", gateway.session.hfp_up);
    bridge_json_bool(json, "call", gateway.session.call_active);
    bridge_json_bool(json, "aec", gateway.session.aec_ready);
    bridge_json_object_end(json);
    add_route(json, &gateway);
    add_bt(json, false);
    bridge_json_object_begin(json, "i2s");
    bridge_json_u32(json, "fifo", audio.fifo_frames);
    bridge_json_u32(json, "under", audio.underruns);
    bridge_json_u32(json, "over", audio.overruns);
    bridge_json_object_end(json);
    bridge_json_object_begin(json, "uart");
    bridge_json_bool(json, "peer", link.peer_seen);
    bridge_json_bool(json, "hello", link.output_hello_valid);
    bridge_json_u32(json, "proto_err", link.protocol_errors);
    bridge_json_u32(json, "rel_fail", link.reliable_failures);
    bridge_json_u32(json, "out_fifo", link.output_fifo_frames);
    bridge_json_u32(json, "out_fifo_min", link.output_fifo_min_frames);
    bridge_json_u32(json, "out_fifo_max", link.output_fifo_max_frames);
    bridge_json_i32(json, "out_ppm", link.output_correction_ppm);
    bridge_json_object_end(json);
    bridge_json_object_begin(json, "uac");
    bridge_json_bool(json, "started", uac.capture_started);
    bridge_json_u32(json, "rx", uac.rx_packets);
    bridge_json_u32(json, "bytes", uac.rx_bytes);
    bridge_json_u32(json, "read_err", uac.read_errors);
    bridge_json_u32(json, "xfer_err", uac.transfer_errors);
    bridge_json_u32(json, "disc", uac.disconnects);
    bridge_json_u32(json, "opens", uac.opens);
    bridge_json_u32(json, "reopen", uac.reopen_attempts);
    bridge_json_object_end(json);
    bridge_json_object_begin(json, "hwm");
    bridge_json_u32(json, "stats", (uint32_t)uxTaskGetStackHighWaterMark(s_stats_task));
    if (s_console_task != NULL) {
        bridge_json_u32(json, "console", (uint32_t)uxTaskGetStackHighWaterMark(s_console_task));
    }
    bridge_json_object_end(json);
}

/* Identity fields shared by the boot record and the `status` reply, so a host
 * that opened the port after boot still gets the full identity. */
static void add_boot_fields(bridge_json_t *json)
{
    esp_chip_info_t chip;
    uint8_t bt_address[6] = {0};
    char address[18];
    const esp_app_desc_t *app = esp_app_get_description();

    esp_chip_info(&chip);
    bool have_mac = esp_read_mac(bt_address, ESP_MAC_BT) == ESP_OK;
    format_address(address, sizeof(address), bt_address);
    bridge_json_str(json, "project", app->project_name);
    bridge_json_str(json, "idf", esp_get_idf_version());
    bridge_json_u32(json, "model", (uint32_t)chip.model);
    bridge_json_u32(json, "rev", chip.revision);
    bridge_json_u32(json, "cores", chip.cores);
    bridge_json_u32(json, "reset_code", (uint32_t)esp_reset_reason());
    bridge_json_str(json, "bt", have_mac ? address : "unavailable");
#if CONFIG_LARKBRIDGE_S31_ERASE_BONDS_ON_BOOT
    bridge_json_u32(json, "erase_bonds", 1U);
#else
    bridge_json_u32(json, "erase_bonds", 0U);
#endif
#ifdef CONFIG_LARKBRIDGE_S31_EXPERIMENTAL_AEC_UPLINK
    bridge_json_u32(json, "aec_publish", 1U);
#else
    bridge_json_u32(json, "aec_publish", 0U);
#endif
    bridge_json_u32(json, "stats_period_ms", CONFIG_LARKBRIDGE_STATS_PERIOD_MS);
}

/* Runs before the stats task exists, so it may borrow the stats buffer and
 * keep the record off app_main's small stack. */
static void emit_boot_record(void)
{
    char *buffer = s_stats_record;
    bridge_json_t json;

    bridge_json_begin(&json, buffer, RECORD_BYTES, "boot");
    add_identity(&json);
    bridge_json_str(&json, "reset", reset_name(esp_reset_reason()));
    add_boot_fields(&json);
    add_heap(&json);
    emit(buffer, &json);
}

static void stats_task(void *argument)
{
    bridge_json_t json;
    TickType_t wake = xTaskGetTickCount();
    (void)argument;
    for (;;) {
        vTaskDelayUntil(&wake, pdMS_TO_TICKS(CONFIG_LARKBRIDGE_STATS_PERIOD_MS));
        build_stats(&json, s_stats_record, "stats");
        emit(s_stats_record, &json);
    }
}

void s31_telemetry_tap_note(s31_tap_stage_t stage, const void *pcm, size_t bytes)
{
#if CONFIG_LARKBRIDGE_TEST_IMAGE
    if ((unsigned)stage >= S31_TAP_COUNT || pcm == NULL || bytes == 0U) {
        return;
    }
    tap_slot_t *slot = &s_taps[stage];
    uint32_t crc = bridge_crc32(0U, pcm, bytes);
    uint32_t rms = bridge_pcm16_rms((const int16_t *)pcm, bytes / sizeof(int16_t));
    /* Odd sequence = write in progress; readers retry. One writer per stage. */
    atomic_fetch_add_explicit(&slot->sequence, 1U, memory_order_acq_rel);
    atomic_store_explicit(&slot->crc, crc, memory_order_relaxed);
    atomic_store_explicit(&slot->rms, rms, memory_order_relaxed);
    atomic_store_explicit(&slot->bytes, bytes, memory_order_relaxed);
    atomic_fetch_add_explicit(&slot->sequence, 1U, memory_order_acq_rel);
#else
    (void)stage;
    (void)pcm;
    (void)bytes;
#endif
}

static void respond_error(const char *command, const char *error)
{
    bridge_json_t json;
    bridge_json_begin(&json, s_console_record, RECORD_BYTES, "resp");
    bridge_json_str(&json, "role", "s31");
    bridge_json_str(&json, "cmd", command);
    bridge_json_bool(&json, "ok", false);
    bridge_json_str(&json, "err", error);
    emit(s_console_record, &json);
}

static void respond_selftest(void)
{
    bridge_json_t json;
    s31_gateway_snapshot_t gateway;
    s31_bt_status_t bt;
    memset(&gateway, 0, sizeof(gateway));
    memset(&bt, 0, sizeof(bt));
    s31_gateway_get_snapshot(&gateway);
    s31_bt_get_status(&bt);
    bool heap_ok = heap_caps_get_minimum_free_size(MALLOC_CAP_8BIT) >= 32768U;
    bool pass = heap_ok && gateway.i2s_ready && gateway.uart_ready && gateway.bt_stack_ready &&
                bt.hfp_profile_ready && bt.a2dp_profile_ready;
    bridge_json_begin(&json, s_console_record, RECORD_BYTES, "selftest");
    add_identity(&json);
    bridge_json_bool(&json, "pass", pass);
    bridge_json_object_begin(&json, "checks");
    bridge_json_bool(&json, "heap_min_32k", heap_ok);
    bridge_json_bool(&json, "i2s", gateway.i2s_ready);
    bridge_json_bool(&json, "uart", gateway.uart_ready);
    bridge_json_bool(&json, "bt_stack", gateway.bt_stack_ready);
    bridge_json_bool(&json, "hfp_profile", bt.hfp_profile_ready);
    bridge_json_bool(&json, "a2dp_profile", bt.a2dp_profile_ready);
    bridge_json_object_end(&json);
    emit(s_console_record, &json);
}

static void respond_tap(const char *stage_name)
{
#if CONFIG_LARKBRIDGE_TEST_IMAGE
    for (unsigned stage = 0; stage < S31_TAP_COUNT; ++stage) {
        if (strcmp(stage_name, TAP_NAMES[stage]) != 0) {
            continue;
        }
        tap_slot_t *slot = &s_taps[stage];
        uint32_t before;
        uint32_t after;
        uint32_t crc;
        uint32_t rms;
        uint32_t bytes;
        unsigned attempts = 0;
        do {
            before = (uint32_t)atomic_load_explicit(&slot->sequence, memory_order_acquire);
            crc = (uint32_t)atomic_load_explicit(&slot->crc, memory_order_relaxed);
            rms = (uint32_t)atomic_load_explicit(&slot->rms, memory_order_relaxed);
            bytes = (uint32_t)atomic_load_explicit(&slot->bytes, memory_order_relaxed);
            after = (uint32_t)atomic_load_explicit(&slot->sequence, memory_order_acquire);
        } while ((before != after || (before & 1U) != 0U) && ++attempts < 8U);
        bridge_json_t json;
        bridge_json_begin(&json, s_console_record, RECORD_BYTES, "tap");
        add_identity(&json);
        bridge_json_str(&json, "stage", stage_name);
        bridge_json_bool(&json, "ok", before == after && (before & 1U) == 0U);
        bridge_json_u32(&json, "blocks", before / 2U);
        bridge_json_u32(&json, "bytes", bytes);
        bridge_json_str(&json, "fmt", "s16le_stereo_48k");
        bridge_json_u32(&json, "crc", crc);
        bridge_json_u32(&json, "rms", rms);
        emit(s_console_record, &json);
        return;
    }
    respond_error("tap", "unknown_stage");
#else
    (void)stage_name;
    respond_error("tap", "test_image_only");
#endif
}

static void respond_tone(const bridge_console_request_t *request)
{
    uint32_t hz = 0;
    int32_t dbfs = -20;
    if (request->argc == 1U) {
        hz = 0U; /* "tone off" */
    } else if (strcmp(request->argv[0], "i2s_out") != 0) {
        /* Planned stages: usb_in, hfp_in, a2dp_in (hil-rig.md section 6). */
        respond_error("tone", "stage_not_implemented");
        return;
    } else if (!bridge_console_parse_u32(request->argv[1], 20U, 20000U, &hz) ||
               !bridge_console_parse_i32(request->argv[2], -90, 0, &dbfs)) {
        respond_error("tone", "bad_args");
        return;
    }
    if (!s31_audio_router_set_test_tone(hz, dbfs)) {
        respond_error("tone", "test_image_only");
        return;
    }
    bridge_json_t json;
    bridge_json_begin(&json, s_console_record, RECORD_BYTES, "resp");
    add_identity(&json);
    bridge_json_str(&json, "cmd", "tone");
    bridge_json_bool(&json, "ok", true);
    bridge_json_str(&json, "stage", hz == 0U ? "off" : "i2s_out");
    bridge_json_u32(&json, "hz", hz);
    bridge_json_i32(&json, "dbfs", dbfs);
    emit(s_console_record, &json);
}

static void handle_line(const char *line)
{
    bridge_console_request_t request;
    bridge_console_result_t result = bridge_console_parse(line, IMAGE_KIND, &request);
    bridge_json_t json;

    if (result == BRIDGE_CONSOLE_EMPTY) {
        return;
    }
    if (result != BRIDGE_CONSOLE_OK) {
        respond_error(request.command != BRIDGE_CMD_INVALID
                          ? bridge_console_command_name(request.command)
                          : "?",
                      bridge_console_result_name(result));
        return;
    }
    switch (request.command) {
    case BRIDGE_CMD_STATUS:
    case BRIDGE_CMD_STATS:
        build_stats(&json, s_console_record,
                    request.command == BRIDGE_CMD_STATUS ? "status" : "stats");
        if (request.command == BRIDGE_CMD_STATUS) {
            add_boot_fields(&json);
        }
        emit(s_console_record, &json);
        break;
    case BRIDGE_CMD_SELFTEST:
        respond_selftest();
        break;
    case BRIDGE_CMD_PEERS:
        bridge_json_begin(&json, s_console_record, RECORD_BYTES, "peers");
        add_identity(&json);
        add_bt(&json, true);
        emit(s_console_record, &json);
        break;
    case BRIDGE_CMD_ROUTE: {
        s31_gateway_snapshot_t gateway;
        memset(&gateway, 0, sizeof(gateway));
        s31_gateway_get_snapshot(&gateway);
        bridge_json_begin(&json, s_console_record, RECORD_BYTES, "route");
        add_identity(&json);
        add_route(&json, &gateway);
        emit(s_console_record, &json);
        break;
    }
    case BRIDGE_CMD_TONE:
        respond_tone(&request);
        break;
    case BRIDGE_CMD_TAP:
        respond_tap(request.argv[0]);
        break;
    case BRIDGE_CMD_FAULT:
        if (strcmp(request.argv[0], "silence") == 0) {
            s31_audio_router_set_test_tone(0U, 0);
            s31_gateway_force_silence();
            bridge_json_begin(&json, s_console_record, RECORD_BYTES, "resp");
            add_identity(&json);
            bridge_json_str(&json, "cmd", "fault");
            bridge_json_bool(&json, "ok", true);
            bridge_json_str(&json, "kind", "silence");
            emit(s_console_record, &json);
        } else {
            respond_error("fault", "kind_not_implemented");
        }
        break;
    case BRIDGE_CMD_FORGET:
        /* Bond changes stay out of v0: pairing gates use the recovery image. */
        respond_error("forget", "not_implemented");
        break;
    default:
        respond_error("?", "unknown_command");
        break;
    }
}

/* Polls the USB Serial/JTAG RX FIFO directly. The console stays on its
 * non-driver output path, so a host that is not reading can never block a
 * logging task; this task only consumes input bytes. */
static void console_task(void *argument)
{
    char line[BRIDGE_CONSOLE_MAX_LINE + 1U];
    size_t length = 0;
    bool overflow = false;
    (void)argument;
    for (;;) {
        uint8_t chunk[32];
        uint32_t count = 0;
        if (usb_serial_jtag_ll_rxfifo_data_available()) {
            count = usb_serial_jtag_ll_read_rxfifo(chunk, sizeof(chunk));
        }
        if (count == 0U) {
            vTaskDelay(pdMS_TO_TICKS(20U));
            continue;
        }
        for (uint32_t index = 0; index < count; ++index) {
            char character = (char)chunk[index];
            if (character == '\r' || character == '\n') {
                if (overflow) {
                    respond_error("?", "too_long");
                } else if (length > 0U) {
                    line[length] = '\0';
                    handle_line(line);
                }
                length = 0;
                overflow = false;
            } else if (length < BRIDGE_CONSOLE_MAX_LINE) {
                line[length++] = character;
            } else {
                overflow = true;
            }
        }
    }
}

esp_err_t s31_telemetry_start(void)
{
    emit_boot_record();
    if (xTaskCreate(stats_task, "s31_stats", 4096, NULL, 2, &s_stats_task) != pdPASS) {
        ESP_LOGE(TAG, "could not start stats task");
        return ESP_ERR_NO_MEM;
    }
    if (xTaskCreate(console_task, "s31_console", 4096, NULL, 2, &s_console_task) != pdPASS) {
        ESP_LOGE(TAG, "could not start console task");
        return ESP_ERR_NO_MEM;
    }
    ESP_LOGI(TAG, "telemetry started: image=%s stats_period_ms=%d",
             bridge_image_kind_name(IMAGE_KIND), CONFIG_LARKBRIDGE_STATS_PERIOD_MS);
    return ESP_OK;
}
