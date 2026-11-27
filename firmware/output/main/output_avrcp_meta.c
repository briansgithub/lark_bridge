#include "output_avrcp_meta.h"

#include "bridge_meta.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "output_meta_cache.h"

#include <stdatomic.h>
#include <string.h>

#define POLL_PERIOD_US 50000U
#define DEFAULT_POS_INTERVAL_MS 2000U
#define MIN_POS_INTERVAL_MS 1000U

/* Defined in the patched Bluedroid btc_avrc.c (tools/patch_btc_avrc.py). */
typedef struct {
    void (*get_play_status)(uint32_t *song_len, uint32_t *song_pos, uint8_t *play_status);
    uint16_t (*get_attr)(uint8_t attr_id, uint8_t *buf, uint16_t capacity);
} larkbridge_avrc_tg_meta_hooks_t;
void larkbridge_avrc_tg_set_meta_hooks(const larkbridge_avrc_tg_meta_hooks_t *hooks);

static const char *TAG = "output_meta";
static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;
static output_meta_cache_t s_cache;
/* Guarded by s_mux: which notifications the car currently has registered. */
static uint16_t s_registered;
static uint32_t s_pos_interval_ms = DEFAULT_POS_INTERVAL_MS;
static uint32_t s_last_pos_ms;
static output_avrcp_meta_stats_t s_stats;
static esp_timer_handle_t s_timer;

static uint32_t now_ms(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000);
}

static void hook_play_status(uint32_t *song_len, uint32_t *song_pos, uint8_t *play_status)
{
    uint32_t now = now_ms();
    portENTER_CRITICAL(&s_mux);
    *song_len = output_meta_cache_length(&s_cache);
    *song_pos = output_meta_cache_position(&s_cache, now);
    *play_status = s_cache.play_status;
    ++s_stats.play_status_queries;
    portEXIT_CRITICAL(&s_mux);
}

static uint16_t hook_attr(uint8_t attr_id, uint8_t *buf, uint16_t capacity)
{
    portENTER_CRITICAL(&s_mux);
    size_t length = output_meta_cache_attr(&s_cache, attr_id, buf, capacity);
    if (attr_id == BRIDGE_META_ATTR_TITLE) {
        ++s_stats.elem_attr_queries;
    }
    portEXIT_CRITICAL(&s_mux);
    return (uint16_t)length;
}

static const larkbridge_avrc_tg_meta_hooks_t s_hooks = {
    .get_play_status = hook_play_status,
    .get_attr = hook_attr,
};

static uint16_t event_bit(uint8_t event_id)
{
    return (uint16_t)(1U << event_id);
}

static void fill_param(uint8_t event_id, esp_avrc_rn_param_t *param, uint32_t now)
{
    memset(param, 0, sizeof(*param));
    portENTER_CRITICAL(&s_mux);
    switch (event_id) {
    case ESP_AVRC_RN_PLAY_STATUS_CHANGE:
        param->playback = (esp_avrc_playback_stat_t)s_cache.play_status;
        break;
    case ESP_AVRC_RN_TRACK_CHANGE: {
        uint64_t uid = s_cache.track_uid;
        for (int i = 7; i >= 0; --i) {
            param->elm_id[i] = (uint8_t)(uid & 0xffU);
            uid >>= 8U;
        }
        break;
    }
    case ESP_AVRC_RN_PLAY_POS_CHANGED:
        param->play_pos = output_meta_cache_position(&s_cache, now);
        break;
    default:
        break;
    }
    portEXIT_CRITICAL(&s_mux);
}

static void send_changed(unsigned int notify)
{
    static const struct {
        unsigned int bit;
        uint8_t event_id;
    } map[] = {
        {OUTPUT_META_NOTIFY_STATUS, ESP_AVRC_RN_PLAY_STATUS_CHANGE},
        {OUTPUT_META_NOTIFY_TRACK, ESP_AVRC_RN_TRACK_CHANGE},
        {OUTPUT_META_NOTIFY_POSITION, ESP_AVRC_RN_PLAY_POS_CHANGED},
    };
    uint32_t now = now_ms();
    for (size_t i = 0U; i < sizeof(map) / sizeof(map[0]); ++i) {
        if ((notify & map[i].bit) == 0U) {
            continue;
        }
        portENTER_CRITICAL(&s_mux);
        bool registered = (s_registered & event_bit(map[i].event_id)) != 0U;
        if (registered) {
            /* AVRCP 1.3: CHANGED is one-shot; the car re-registers. */
            s_registered &= (uint16_t)~event_bit(map[i].event_id);
            if (map[i].event_id == ESP_AVRC_RN_PLAY_POS_CHANGED) {
                s_last_pos_ms = now;
            }
            if (map[i].event_id == ESP_AVRC_RN_TRACK_CHANGE) {
                ++s_stats.track_changes;
            }
        }
        portEXIT_CRITICAL(&s_mux);
        if (!registered) {
            continue;
        }
        esp_avrc_rn_param_t param;
        fill_param(map[i].event_id, &param, now);
        esp_err_t result =
            esp_avrc_tg_send_rn_rsp(map[i].event_id, ESP_AVRC_RN_RSP_CHANGED, &param);
        if (result == ESP_OK) {
            portENTER_CRITICAL(&s_mux);
            ++s_stats.changed_sent;
            portEXIT_CRITICAL(&s_mux);
        }
        ESP_LOGI(TAG, "CHANGED event=0x%02x: %s", map[i].event_id, esp_err_to_name(result));
    }
}

static void poll_timer(void *argument)
{
    (void)argument;
    uint32_t now = now_ms();
    portENTER_CRITICAL(&s_mux);
    unsigned int notify = output_meta_cache_poll(&s_cache, now);
    if ((s_registered & event_bit(ESP_AVRC_RN_PLAY_POS_CHANGED)) != 0U &&
        s_cache.play_status == BRIDGE_META_STATUS_PLAYING &&
        (uint32_t)(now - s_last_pos_ms) >= s_pos_interval_ms) {
        notify |= OUTPUT_META_NOTIFY_POSITION;
    }
    portEXIT_CRITICAL(&s_mux);
    if (notify != 0U) {
        send_changed(notify);
    }
}

esp_err_t output_avrcp_meta_init(void)
{
    output_meta_cache_init(&s_cache);
    memset(&s_stats, 0, sizeof(s_stats));
    s_registered = 0U;
    larkbridge_avrc_tg_set_meta_hooks(&s_hooks);
    const esp_timer_create_args_t args = {
        .callback = poll_timer,
        .name = "avrcp_meta",
    };
    esp_err_t result = esp_timer_create(&args, &s_timer);
    if (result == ESP_OK) {
        result = esp_timer_start_periodic(s_timer, POLL_PERIOD_US);
    }
    return result;
}

void output_avrcp_meta_configure_tg(void)
{
    esp_avrc_rn_evt_cap_mask_t events = {0};
    (void)esp_avrc_rn_evt_bit_mask_operation(ESP_AVRC_BIT_MASK_OP_SET, &events,
                                             ESP_AVRC_RN_PLAY_STATUS_CHANGE);
    (void)esp_avrc_rn_evt_bit_mask_operation(ESP_AVRC_BIT_MASK_OP_SET, &events,
                                             ESP_AVRC_RN_TRACK_CHANGE);
    (void)esp_avrc_rn_evt_bit_mask_operation(ESP_AVRC_BIT_MASK_OP_SET, &events,
                                             ESP_AVRC_RN_PLAY_POS_CHANGED);
    esp_err_t result = esp_avrc_tg_set_rn_evt_cap(&events);
    ESP_LOGI(TAG, "AVRCP TG notification caps 0x%04x: %s", (unsigned int)events.bits,
             esp_err_to_name(result));
}

void output_avrcp_meta_handle_packet(const bridge_packet_t *packet)
{
    bridge_meta_record_t record;
    if (!bridge_meta_unpack(packet->payload, packet->payload_length, &record)) {
        portENTER_CRITICAL(&s_mux);
        ++s_stats.bad_records;
        portEXIT_CRITICAL(&s_mux);
        return;
    }
    uint32_t now = now_ms();
    portENTER_CRITICAL(&s_mux);
    uint8_t before = s_cache.generation;
    bool before_valid = s_cache.generation_valid;
    unsigned int notify = output_meta_cache_apply(&s_cache, &record, now);
    ++s_stats.records;
    s_stats.generation = s_cache.generation;
    s_stats.play_status = s_cache.play_status;
    portEXIT_CRITICAL(&s_mux);
    if (record.kind == BRIDGE_META_KIND_TEXT_ATTR) {
        ESP_LOGI(TAG, "gen=%u attr=%u '%s'", record.generation, record.attr_id, record.text);
    } else if (record.kind == BRIDGE_META_KIND_PLAY_STATUS) {
        ESP_LOGI(TAG, "gen=%u status=%u len=%lu pos=%lu", record.generation, record.play_status,
                 (unsigned long)record.song_length_ms, (unsigned long)record.song_position_ms);
    } else {
        ESP_LOGI(TAG, "metadata cleared by S31");
    }
    if (record.kind != BRIDGE_META_KIND_CLEAR && (!before_valid || before != record.generation)) {
        ESP_LOGI(TAG, "new track generation %u (track-change debounce %u ms)", record.generation,
                 OUTPUT_META_TRACK_DEBOUNCE_MS);
    }
    if (notify != 0U) {
        send_changed(notify);
    }
}

void output_avrcp_meta_clear(const char *why)
{
    portENTER_CRITICAL(&s_mux);
    unsigned int notify = output_meta_cache_clear(&s_cache);
    s_stats.generation = 0U;
    s_stats.play_status = s_cache.play_status;
    portEXIT_CRITICAL(&s_mux);
    if (notify != 0U) {
        ESP_LOGI(TAG, "metadata cleared (%s)", why);
        send_changed(notify);
    }
}

void output_avrcp_meta_on_register(uint8_t event_id, uint32_t event_parameter)
{
    if (event_id != ESP_AVRC_RN_PLAY_STATUS_CHANGE && event_id != ESP_AVRC_RN_TRACK_CHANGE &&
        event_id != ESP_AVRC_RN_PLAY_POS_CHANGED) {
        return;
    }
    uint32_t now = now_ms();
    portENTER_CRITICAL(&s_mux);
    s_registered |= event_bit(event_id);
    if (event_id == ESP_AVRC_RN_PLAY_POS_CHANGED) {
        uint32_t interval = event_parameter * 1000U;
        s_pos_interval_ms = interval < MIN_POS_INTERVAL_MS ? MIN_POS_INTERVAL_MS : interval;
        s_last_pos_ms = now;
    }
    portEXIT_CRITICAL(&s_mux);
    esp_avrc_rn_param_t param;
    fill_param(event_id, &param, now);
    esp_err_t result = esp_avrc_tg_send_rn_rsp(event_id, ESP_AVRC_RN_RSP_INTERIM, &param);
    if (result == ESP_OK) {
        portENTER_CRITICAL(&s_mux);
        ++s_stats.interim_sent;
        portEXIT_CRITICAL(&s_mux);
    }
    ESP_LOGI(TAG, "car registered event=0x%02x param=%lu; INTERIM: %s", event_id,
             (unsigned long)event_parameter, esp_err_to_name(result));
}

void output_avrcp_meta_on_connection(bool connected)
{
    portENTER_CRITICAL(&s_mux);
    s_registered = 0U;
    portEXIT_CRITICAL(&s_mux);
    ESP_LOGI(TAG, "car AVRCP %s", connected ? "connected" : "disconnected");
}

void output_avrcp_meta_get_stats(output_avrcp_meta_stats_t *stats)
{
    portENTER_CRITICAL(&s_mux);
    *stats = s_stats;
    portEXIT_CRITICAL(&s_mux);
}

void output_avrcp_meta_get_title(char *out, size_t capacity)
{
    if (out == NULL || capacity == 0U) {
        return;
    }
    portENTER_CRITICAL(&s_mux);
    size_t length = output_meta_cache_attr(&s_cache, BRIDGE_META_ATTR_TITLE, (uint8_t *)out,
                                           capacity - 1U);
    portEXIT_CRITICAL(&s_mux);
    out[length] = '\0';
}
