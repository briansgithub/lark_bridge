#include "output_telemetry.h"

#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "bridge_console.h"
#include "driver/uart.h"
#include "driver/uart_vfs.h"
#include "esp_app_desc.h"
#include "esp_chip_info.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "output_audio.h"
#include "output_bluetooth.h"
#include "output_control.h"
#include "output_peer_store.h"
#include "sdkconfig.h"

static const char *TAG = "output_telemetry";

#if CONFIG_LARKBRIDGE_TEST_IMAGE
#define IMAGE_KIND BRIDGE_IMAGE_TEST
#else
#define IMAGE_KIND BRIDGE_IMAGE_RELEASE
#endif

#define RECORD_BYTES 1536U

#if CONFIG_LARKBRIDGE_TEST_IMAGE
typedef struct {
    atomic_uint_fast32_t sequence;
    atomic_uint_fast32_t crc;
    atomic_uint_fast32_t rms;
    atomic_uint_fast32_t rms_l, rms_r, nz_blocks, nz_frames;
    atomic_uint_fast32_t tone_mhz_l, tone_mhz_r;
    atomic_uint_fast32_t bytes;
} tap_slot_t;

static tap_slot_t s_taps[OUTPUT_TAP_COUNT];
static const char *const TAP_NAMES[OUTPUT_TAP_COUNT] = {"i2s_in", "a2dp_out"};
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

/* ESP-IDF gives each task its own stdout FILE, so newlib's stream lock does
 * not serialize the stats and console tasks. Without this lock a reply could
 * land inside a periodic stats line and neither record would parse. */
static StaticSemaphore_t s_emit_lock_storage;
static SemaphoreHandle_t s_emit_lock;

static void emit(char *buffer, bridge_json_t *json)
{
    bool locked = s_emit_lock != NULL && xSemaphoreTake(s_emit_lock, portMAX_DELAY) == pdTRUE;
    if (bridge_json_end(json)) {
        printf("%s\n", buffer);
    } else {
        printf("{\"t\":\"error\",\"role\":\"output\",\"err\":\"record_overflow\"}\n");
    }
    fflush(stdout);
    if (locked) {
        (void)xSemaphoreGive(s_emit_lock);
    }
}

static void add_identity(bridge_json_t *json)
{
    const esp_app_desc_t *app = esp_app_get_description();
    bridge_json_str(json, "role", "output");
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

static void add_peer(bridge_json_t *json)
{
    uint8_t peer[OUTPUT_PEER_ADDRESS_SIZE] = {0};
    bool found = false;
    esp_err_t result = output_peer_store_load(peer, &found);
    bridge_json_object_begin(json, "bt");
    bridge_json_bool(json, "stack", output_bluetooth_stack_ready());
    bridge_json_bool(json, "connected", output_bluetooth_peer_connected());
    bridge_json_bool(json, "streaming", output_bluetooth_audio_streaming());
    bridge_json_bool(json, "peer_saved", result == ESP_OK && found);
    if (result == ESP_OK && found) {
        char address[18];
        format_address(address, sizeof(address), peer);
        bridge_json_str(json, "peer", address);
    } else if (result != ESP_OK) {
        bridge_json_str(json, "peer_err", esp_err_to_name(result));
    }
    bridge_json_object_end(json);
}

static void build_stats(bridge_json_t *json, char *buffer, const char *type)
{
    output_audio_stats_t audio;
    output_control_stats_t control;

    memset(&audio, 0, sizeof(audio));
    memset(&control, 0, sizeof(control));
    output_audio_get_stats(&audio);
    output_control_get_stats(&control);

    bridge_json_begin(json, buffer, RECORD_BYTES, type);
    add_identity(json);
    bridge_json_u64(json, "up_ms", (uint64_t)(esp_timer_get_time() / 1000));
    bridge_json_str(json, "reset", reset_name(esp_reset_reason()));
    add_heap(json);
    bridge_json_u32(json, "ready", control.local_ready_flags);
    bridge_json_object_begin(json, "bt");
    bridge_json_bool(json, "stack", output_bluetooth_stack_ready());
    bridge_json_bool(json, "connected", output_bluetooth_peer_connected());
    bridge_json_bool(json, "streaming", output_bluetooth_audio_streaming());
    bridge_json_object_end(json);
    bridge_json_bool(json, "audio", output_control_audio_allowed());
    bridge_json_object_begin(json, "uart");
#if CONFIG_LARKBRIDGE_TEST_IMAGE
    output_control_test_stats(json);
#endif
    bridge_json_bool(json, "init", control.initialized);
    bridge_json_bool(json, "alive", control.link_alive);
    bridge_json_bool(json, "hello", control.hello_valid);
    bridge_json_u32(json, "remote_ready", control.remote_ready_flags);
    bridge_json_u32(json, "route", control.route);
    bridge_json_u32(json, "proto_err", control.protocol_errors);
    bridge_json_u32(json, "rel_fail", control.reliable_failures);
    bridge_json_object_end(json);
    bridge_json_object_begin(json, "fifo");
    bridge_json_u32(json, "level", audio.fifo_frames);
    bridge_json_u32(json, "min", audio.fifo_min_frames);
    bridge_json_u32(json, "max", audio.fifo_max_frames);
    bridge_json_i32(json, "ppm", audio.correction_ppm);
    bridge_json_i32(json, "adapt", audio.adaptive_input_delta_frames);
    bridge_json_object_end(json);
    bridge_json_object_begin(json, "a2dp");
    bridge_json_u32(json, "under", audio.underruns);
    bridge_json_u32(json, "over", audio.overruns);
    bridge_json_u32(json, "missing", audio.missing_frames);
    bridge_json_u32(json, "dropped", audio.dropped_frames);
    bridge_json_u32(json, "i2s_err", audio.i2s_read_errors);
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
#if CONFIG_LARKBRIDGE_OUTPUT_ERASE_BONDS_ON_BOOT
    bridge_json_u32(json, "erase_bonds", 1U);
#else
    bridge_json_u32(json, "erase_bonds", 0U);
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

#if CONFIG_LARKBRIDGE_TEST_IMAGE
/* Estimate a periodic test tone from positive zero crossings in one PCM block.
 * Use 48 kHz nominal timing. This does not measure the physical sample clock. */
static uint32_t tone_frequency_mhz(const int16_t *samples, size_t frames, size_t channel,
                                   uint32_t rate_hz)
{
    size_t first = 0U;
    size_t last = 0U;
    uint32_t crossings = 0U;
    for (size_t frame = 1U; frame < frames; ++frame) {
        if (samples[(frame - 1U) * 2U + channel] <= 0 && samples[frame * 2U + channel] > 0) {
            if (crossings == 0U) {
                first = frame;
            }
            last = frame;
            ++crossings;
        }
    }
    return crossings < 2U ? 0U : (uint32_t)(1000ULL * rate_hz * (crossings - 1U) / (last - first));
}
#endif

void output_telemetry_tap_note(output_tap_stage_t stage, const void *pcm, size_t bytes,
                               uint32_t rate_hz)
{
#if CONFIG_LARKBRIDGE_TEST_IMAGE
    if ((unsigned)stage >= OUTPUT_TAP_COUNT || pcm == NULL || bytes == 0U) {
        return;
    }
    tap_slot_t *slot = &s_taps[stage];
    uint32_t crc = bridge_crc32(0U, pcm, bytes);
    uint32_t rms = bridge_pcm16_rms((const int16_t *)pcm, bytes / sizeof(int16_t));
    size_t frames = bytes / (2U * sizeof(int16_t));
    bridge_pcm16_stereo_stats_t stereo;
    bridge_pcm16_stereo_stats(pcm, frames, &stereo);
    uint32_t tone_mhz_l = 0U;
    uint32_t tone_mhz_r = 0U;
    if (stereo.nonzero) {
        tone_mhz_l = tone_frequency_mhz(pcm, frames, 0U, rate_hz);
        tone_mhz_r = tone_frequency_mhz(pcm, frames, 1U, rate_hz);
    }
    /* Odd sequence = write in progress; readers retry. One writer per stage. */
    atomic_fetch_add_explicit(&slot->sequence, 1U, memory_order_acq_rel);
    atomic_store_explicit(&slot->crc, crc, memory_order_relaxed);
    atomic_store_explicit(&slot->rms, rms, memory_order_relaxed);
    atomic_store_explicit(&slot->rms_l, stereo.rms_left, memory_order_relaxed);
    atomic_store_explicit(&slot->rms_r, stereo.rms_right, memory_order_relaxed);
    atomic_store_explicit(&slot->tone_mhz_l, tone_mhz_l, memory_order_relaxed);
    atomic_store_explicit(&slot->tone_mhz_r, tone_mhz_r, memory_order_relaxed);
    atomic_fetch_add_explicit(&slot->nz_frames, stereo.nonzero_frames, memory_order_relaxed);
    if (stereo.nonzero) atomic_fetch_add_explicit(&slot->nz_blocks, 1U, memory_order_relaxed);
    atomic_store_explicit(&slot->bytes, bytes, memory_order_relaxed);
    atomic_fetch_add_explicit(&slot->sequence, 1U, memory_order_acq_rel);
#else
    (void)stage;
    (void)pcm;
    (void)bytes;
    (void)rate_hz;
#endif
}

static void respond_error(const char *command, const char *error)
{
    bridge_json_t json;
    bridge_json_begin(&json, s_console_record, RECORD_BYTES, "resp");
    bridge_json_str(&json, "role", "output");
    bridge_json_str(&json, "cmd", command);
    bridge_json_bool(&json, "ok", false);
    bridge_json_str(&json, "err", error);
    emit(s_console_record, &json);
}

static void respond_selftest(void)
{
    bridge_json_t json;
    output_control_stats_t control;
    memset(&control, 0, sizeof(control));
    output_control_get_stats(&control);
    bool heap_ok = heap_caps_get_minimum_free_size(MALLOC_CAP_8BIT) >= 32768U;
    bool i2s_ok = output_audio_is_initialized();
    bool bt_ok = output_bluetooth_stack_ready();
    bool pass = heap_ok && i2s_ok && control.initialized && bt_ok;
    bridge_json_begin(&json, s_console_record, RECORD_BYTES, "selftest");
    add_identity(&json);
    bridge_json_bool(&json, "pass", pass);
    bridge_json_object_begin(&json, "checks");
    bridge_json_bool(&json, "heap_min_32k", heap_ok);
    bridge_json_bool(&json, "i2s", i2s_ok);
    bridge_json_bool(&json, "uart", control.initialized);
    bridge_json_bool(&json, "bt_stack", bt_ok);
    bridge_json_object_end(&json);
    emit(s_console_record, &json);
}

static void respond_tap(const char *stage_name)
{
#if CONFIG_LARKBRIDGE_TEST_IMAGE
    for (unsigned stage = 0; stage < OUTPUT_TAP_COUNT; ++stage) {
        if (strcmp(stage_name, TAP_NAMES[stage]) != 0) {
            continue;
        }
        tap_slot_t *slot = &s_taps[stage];
        uint32_t before;
        uint32_t after;
        uint32_t crc;
        uint32_t rms, rms_l, rms_r, nz_blocks, nz_frames;
        uint32_t tone_mhz_l, tone_mhz_r;
        uint32_t bytes;
        unsigned attempts = 0;
        do {
            before = (uint32_t)atomic_load_explicit(&slot->sequence, memory_order_acquire);
            crc = (uint32_t)atomic_load_explicit(&slot->crc, memory_order_relaxed);
            rms = (uint32_t)atomic_load_explicit(&slot->rms, memory_order_relaxed);
            rms_l = atomic_load_explicit(&slot->rms_l, memory_order_relaxed);
            rms_r = atomic_load_explicit(&slot->rms_r, memory_order_relaxed);
            nz_frames = atomic_load_explicit(&slot->nz_frames, memory_order_relaxed);
            nz_blocks = atomic_load_explicit(&slot->nz_blocks, memory_order_relaxed);
            bytes = (uint32_t)atomic_load_explicit(&slot->bytes, memory_order_relaxed);
            tone_mhz_l = atomic_load_explicit(&slot->tone_mhz_l, memory_order_relaxed);
            tone_mhz_r = atomic_load_explicit(&slot->tone_mhz_r, memory_order_relaxed);
            after = (uint32_t)atomic_load_explicit(&slot->sequence, memory_order_acquire);
        } while ((before != after || (before & 1U) != 0U) && ++attempts < 8U);
        bridge_json_t json;
        bridge_json_begin(&json, s_console_record, RECORD_BYTES, "tap");
        add_identity(&json);
        bridge_json_str(&json, "stage", stage_name);
        bridge_json_bool(&json, "ok", before == after && (before & 1U) == 0U);
        bridge_json_u32(&json, "blocks", before / 2U);
        bridge_json_u32(&json, "bytes", bytes);
        bridge_json_str(&json, "fmt", "s16le_stereo");
        bridge_json_u32(&json, "crc", crc);
        bridge_json_u32(&json, "rms", rms);
        bridge_json_u32(&json, "rms_l", rms_l);
        bridge_json_u32(&json, "rms_r", rms_r);
        bridge_json_u32(&json, "nz_frames", nz_frames);
        bridge_json_u32(&json, "nz_blocks", nz_blocks);
        bridge_json_u32(&json, "tone_mhz_l", tone_mhz_l);
        bridge_json_u32(&json, "tone_mhz_r", tone_mhz_r);
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
    } else if (strcmp(request->argv[0], "a2dp_out") != 0) {
        /* Planned stage: i2s_in (hil-rig.md section 6). */
        respond_error("tone", "stage_not_implemented");
        return;
    } else if (!bridge_console_parse_u32(request->argv[1], 20U, 20000U, &hz) ||
               !bridge_console_parse_i32(request->argv[2], -90, 0, &dbfs)) {
        respond_error("tone", "bad_args");
        return;
    }
    if (!output_audio_set_test_tone(hz, dbfs)) {
        respond_error("tone", "test_image_only");
        return;
    }
    bridge_json_t json;
    bridge_json_begin(&json, s_console_record, RECORD_BYTES, "resp");
    add_identity(&json);
    bridge_json_str(&json, "cmd", "tone");
    bridge_json_bool(&json, "ok", true);
    bridge_json_str(&json, "stage", hz == 0U ? "off" : "a2dp_out");
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
        add_peer(&json);
        emit(s_console_record, &json);
        break;
    case BRIDGE_CMD_ROUTE: {
        output_control_stats_t control;
        memset(&control, 0, sizeof(control));
        output_control_get_stats(&control);
        bridge_json_begin(&json, s_console_record, RECORD_BYTES, "route");
        add_identity(&json);
        bridge_json_u32(&json, "route", control.route);
        bridge_json_bool(&json, "audio_allowed", output_control_audio_allowed());
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
            output_audio_set_test_tone(0U, 0);
            output_control_invalidate_local_audio();
            bridge_json_begin(&json, s_console_record, RECORD_BYTES, "resp");
            add_identity(&json);
            bridge_json_str(&json, "cmd", "fault");
            bridge_json_bool(&json, "ok", true);
            bridge_json_str(&json, "kind", "silence");
            emit(s_console_record, &json);
            break;
        }
#if CONFIG_LARKBRIDGE_TEST_IMAGE
        bool reboot = strcmp(request.argv[0], "reboot") == 0;
        if (strcmp(request.argv[0], "fifo_window") == 0) {
            output_audio_test_fifo_window();
        } else if (!reboot && !output_control_test_fault(request.argv[0])) {
            respond_error("fault", "unsupported_or_busy");
            break;
        }
        bridge_json_begin(&json, s_console_record, RECORD_BYTES, "resp");
        add_identity(&json);
        bridge_json_str(&json, "cmd", "fault");
        bridge_json_bool(&json, "ok", true);
        bridge_json_str(&json, "kind", request.argv[0]);
        emit(s_console_record, &json);
        if (reboot) {
            vTaskDelay(pdMS_TO_TICKS(150U));
            esp_restart();
        }
#endif
#if !CONFIG_LARKBRIDGE_TEST_IMAGE
        respond_error("fault", "kind_not_implemented");
#endif
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

/* Reads UART0 (the CP2102N console) through the UART driver. UART output
 * always drains at the line rate, so routing stdout through the driver cannot
 * stall logging even with no host attached. */
static void console_task(void *argument)
{
    char line[BRIDGE_CONSOLE_MAX_LINE + 1U];
    size_t length = 0;
    bool overflow = false;
    (void)argument;
    for (;;) {
        uint8_t chunk[32];
        int count = uart_read_bytes(UART_NUM_0, chunk, sizeof(chunk), pdMS_TO_TICKS(100U));
        for (int index = 0; index < count; ++index) {
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

esp_err_t output_telemetry_start(void)
{
    s_emit_lock = xSemaphoreCreateMutexStatic(&s_emit_lock_storage);
    emit_boot_record();
    esp_err_t uart_result = uart_driver_install(UART_NUM_0, 256, 0, 0, NULL, 0);
    if (uart_result == ESP_OK) {
        uart_vfs_dev_use_driver(UART_NUM_0);
    } else {
        ESP_LOGW(TAG, "console input unavailable: %s", esp_err_to_name(uart_result));
    }
    if (xTaskCreate(stats_task, "out_stats", 4096, NULL, 2, &s_stats_task) != pdPASS) {
        ESP_LOGE(TAG, "could not start stats task");
        return ESP_ERR_NO_MEM;
    }
    if (uart_result == ESP_OK &&
        xTaskCreate(console_task, "out_console", 4096, NULL, 2, &s_console_task) != pdPASS) {
        ESP_LOGE(TAG, "could not start console task");
        return ESP_ERR_NO_MEM;
    }
    ESP_LOGI(TAG, "telemetry started: image=%s stats_period_ms=%d",
             bridge_image_kind_name(IMAGE_KIND), CONFIG_LARKBRIDGE_STATS_PERIOD_MS);
    return ESP_OK;
}
