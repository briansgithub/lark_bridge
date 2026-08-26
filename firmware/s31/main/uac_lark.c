#include "uac_lark.h"

#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>

#include "audio_router.h"
#include "bridge_link.h"
#include "bridge_session.h"
#include "esp_intr_alloc.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "gateway_state.h"
#include "s31_config.h"
#include "usb/uac_host.h"
#include "usb/usb_host.h"

#define UAC_EVENT_QUEUE_LENGTH 16U
#define UAC_DRIVER_BUFFER_BYTES (24U * 1024U)
#define UAC_DRIVER_THRESHOLD_BYTES 1920U
#define UAC_READ_BYTES 3840U
#define UAC_REOPEN_DELAY_MS 1000U
#define UAC_REOPEN_MAX_ATTEMPTS 5U

typedef enum {
    UAC_QUEUE_DRIVER_EVENT,
    UAC_QUEUE_DEVICE_EVENT,
} uac_queue_event_kind_t;

typedef struct {
    uac_queue_event_kind_t kind;
    union {
        struct {
            uint8_t address;
            uint8_t interface_number;
            uac_host_driver_event_t event;
        } driver;
        struct {
            uac_host_device_handle_t handle;
            uac_host_device_event_t event;
        } device;
    } data;
} uac_queue_event_t;

static const char *TAG = "s31_uac";
static QueueHandle_t s_events;
static uac_host_device_handle_t s_lark_handle;
static bool s_lark_started;
static bool s_reopen_pending;
static uint8_t s_lark_address;
static uint8_t s_lark_interface;
static uint8_t s_reopen_attempts;
static TickType_t s_reopen_at;

static void post_driver_event(uint8_t address, uint8_t interface_number,
                              const uac_host_driver_event_t event, void *argument)
{
    (void)argument;
    const uac_queue_event_t message = {
        .kind = UAC_QUEUE_DRIVER_EVENT,
        .data.driver = {
            .address = address,
            .interface_number = interface_number,
            .event = event,
        },
    };
    if (xQueueSend(s_events, &message, 0) != pdTRUE) {
        ESP_EARLY_LOGE(TAG, "UAC driver event queue full");
    }
}

static void post_device_event(uac_host_device_handle_t handle,
                              const uac_host_device_event_t event, void *argument)
{
    (void)argument;
    const uac_queue_event_t message = {
        .kind = UAC_QUEUE_DEVICE_EVENT,
        .data.device = {
            .handle = handle,
            .event = event,
        },
    };
    if (xQueueSend(s_events, &message, 0) != pdTRUE) {
        ESP_EARLY_LOGE(TAG, "UAC device event queue full");
    }
}

static bool alt_supports_48k_s16_stereo(const uac_host_dev_alt_param_t *alternate)
{
    if (alternate->format != 1U || alternate->channels != S31_AUDIO_CHANNELS ||
        alternate->subframe_size != sizeof(int16_t) ||
        alternate->bit_resolution != S31_AUDIO_BITS_PER_SAMPLE) {
        return false;
    }
    if (alternate->sample_freq_type == 0U) {
        return alternate->sample_freq_lower <= S31_AUDIO_SAMPLE_RATE_HZ &&
               alternate->sample_freq_upper >= S31_AUDIO_SAMPLE_RATE_HZ;
    }
    uint8_t count = alternate->sample_freq_type;
    if (count > UAC_FREQ_NUM_MAX) {
        count = UAC_FREQ_NUM_MAX;
    }
    for (uint8_t index = 0; index < count; ++index) {
        if (alternate->sample_freq[index] == S31_AUDIO_SAMPLE_RATE_HZ) {
            return true;
        }
    }
    return false;
}

static bool device_has_required_alternate(uac_host_device_handle_t handle,
                                          const uac_host_dev_info_t *information)
{
    for (uint8_t alternate_number = 1U;
         alternate_number <= information->iface_alt_num; ++alternate_number) {
        uac_host_dev_alt_param_t alternate;
        if (uac_host_get_device_alt_param(handle, alternate_number, &alternate) == ESP_OK &&
            alt_supports_48k_s16_stereo(&alternate)) {
            return true;
        }
    }
    return false;
}

static void close_lark(uac_host_device_handle_t handle)
{
    if (handle == NULL) {
        return;
    }
    if (handle == s_lark_handle && s_lark_started) {
        s_lark_started = false;
        esp_err_t stop_result = uac_host_device_stop(handle);
        if (stop_result != ESP_OK) {
            ESP_LOGW(TAG, "UAC stop returned %s", esp_err_to_name(stop_result));
        }
    }
    if (handle == s_lark_handle) {
        s_lark_handle = NULL;
        s31_gateway_apply_session_event(BRIDGE_SESSION_EVENT_LARK_LOST);
    }
    esp_err_t result = uac_host_device_close(handle);
    if (result != ESP_OK) {
        ESP_LOGW(TAG, "UAC close returned %s", esp_err_to_name(result));
    }
}

static bool open_lark(uint8_t address, uint8_t interface_number)
{
    if (s_lark_handle != NULL) {
        ESP_LOGW(TAG, "Ignoring a second USB microphone interface");
        return false;
    }
    const uac_host_device_config_t device_config = {
        .addr = address,
        .iface_num = interface_number,
        .buffer_size = UAC_DRIVER_BUFFER_BYTES,
        .buffer_threshold = UAC_DRIVER_THRESHOLD_BYTES,
        .callback = post_device_event,
        .callback_arg = NULL,
    };
    uac_host_device_handle_t handle = NULL;
    esp_err_t result = uac_host_device_open(&device_config, &handle);
    if (result != ESP_OK) {
        ESP_LOGE(TAG, "Could not open UAC RX interface: %s", esp_err_to_name(result));
        return false;
    }
    uac_host_dev_info_t information;
    result = uac_host_get_device_info(handle, &information);
    if (result != ESP_OK) {
        ESP_LOGE(TAG, "Could not identify UAC device: %s", esp_err_to_name(result));
        close_lark(handle);
        return false;
    }
    /* Preserve the complete component-level descriptor/alternate report in
     * the arrival log before applying the production identity filter. */
    result = uac_host_printf_device_param(handle);
    if (result != ESP_OK) {
        ESP_LOGW(TAG, "could not print UAC descriptor report: %s",
                 esp_err_to_name(result));
    }
    if (information.VID != S31_LARK_USB_VID || information.PID != S31_LARK_USB_PID) {
        ESP_LOGI(TAG, "Ignoring UAC microphone %04x:%04x", information.VID, information.PID);
        close_lark(handle);
        return false;
    }
    if (!device_has_required_alternate(handle, &information)) {
        ESP_LOGE(TAG, "LARK %04x:%04x lacks required 48 kHz/S16/stereo alternate",
                 information.VID, information.PID);
        s31_gateway_set_fault(BRIDGE_FAULT_UAC_LOST);
        close_lark(handle);
        return false;
    }
    const uac_host_stream_config_t stream_config = {
        .channels = S31_AUDIO_CHANNELS,
        .bit_resolution = S31_AUDIO_BITS_PER_SAMPLE,
        .sample_freq = S31_AUDIO_SAMPLE_RATE_HZ,
        .flags = 0,
    };
    s_lark_handle = handle;
    s_lark_started = false;
    result = uac_host_device_start(handle, &stream_config);
    if (result != ESP_OK) {
        ESP_LOGE(TAG, "Could not start LARK capture: %s", esp_err_to_name(result));
        s31_gateway_set_fault(BRIDGE_FAULT_UAC_LOST);
        close_lark(handle);
        return false;
    }
    s_lark_started = true;
    s_lark_address = address;
    s_lark_interface = interface_number;
    s_reopen_pending = false;
    s_reopen_attempts = 0U;
    s31_gateway_apply_session_event(BRIDGE_SESSION_EVENT_LARK_PRESENT);
    ESP_LOGI(TAG, "LARK %04x:%04x capture ready at 48 kHz/S16/stereo",
             information.VID, information.PID);
    return true;
}

static void schedule_reopen(void)
{
    if (s_reopen_attempts >= UAC_REOPEN_MAX_ATTEMPTS) {
        s_reopen_pending = false;
        ESP_LOGE(TAG, "LARK automatic recovery exhausted; waiting for USB re-enumeration");
        return;
    }
    s_reopen_pending = true;
    s_reopen_at = xTaskGetTickCount() + pdMS_TO_TICKS(UAC_REOPEN_DELAY_MS);
}

static void handle_device_event(const uac_queue_event_t *message)
{
    uac_host_device_handle_t handle = message->data.device.handle;
    if (handle != s_lark_handle) {
        return;
    }
    switch (message->data.device.event) {
    case UAC_HOST_DEVICE_EVENT_RX_DONE:
        if (handle == s_lark_handle) {
            uint8_t capture[UAC_READ_BYTES];
            uint32_t bytes_read = 0;
            esp_err_t result = uac_host_device_read(handle, capture, sizeof(capture),
                                                    &bytes_read, 0);
            if (result == ESP_OK && bytes_read != 0U) {
                s31_audio_router_write_lark(capture, bytes_read);
            } else if (result != ESP_OK) {
                ESP_LOGE(TAG, "LARK capture read failed: %s", esp_err_to_name(result));
                s31_gateway_set_fault(BRIDGE_FAULT_UAC_LOST);
                close_lark(handle);
                schedule_reopen();
            }
        }
        break;
    case UAC_HOST_DEVICE_EVENT_TRANSFER_ERROR:
        ESP_LOGE(TAG, "LARK UAC transfer error");
        s31_gateway_set_fault(BRIDGE_FAULT_UAC_LOST);
        close_lark(handle);
        schedule_reopen();
        break;
    case UAC_HOST_DRIVER_EVENT_DISCONNECTED:
        ESP_LOGW(TAG, "USB audio device disconnected");
        close_lark(handle);
        break;
    case UAC_HOST_DEVICE_EVENT_TX_DONE:
    default:
        break;
    }
}

static void uac_task(void *argument)
{
    ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
    const uac_host_driver_config_t driver_config = {
        .create_background_task = true,
        .task_priority = 8,
        .stack_size = 4096,
        .core_id = tskNO_AFFINITY,
        .callback = post_driver_event,
        .callback_arg = NULL,
    };
    esp_err_t result = uac_host_install(&driver_config);
    if (result != ESP_OK) {
        ESP_LOGE(TAG, "UAC class driver install failed: %s", esp_err_to_name(result));
        s31_gateway_set_fault(BRIDGE_FAULT_UAC_LOST);
        vTaskDelete(NULL);
        return;
    }
    ESP_LOGI(TAG, "UAC class driver installed");
    uac_queue_event_t message;
    for (;;) {
        TickType_t wait = portMAX_DELAY;
        if (s_reopen_pending) {
            TickType_t now = xTaskGetTickCount();
            wait = (int32_t)(s_reopen_at - now) > 0 ? s_reopen_at - now : 0U;
        }
        if (xQueueReceive(s_events, &message, wait) != pdTRUE) {
            if (s_reopen_pending) {
                s_reopen_pending = false;
                ++s_reopen_attempts;
                ESP_LOGW(TAG, "retrying LARK UAC capture (%u/%u)", s_reopen_attempts,
                         UAC_REOPEN_MAX_ATTEMPTS);
                if (!open_lark(s_lark_address, s_lark_interface)) {
                    schedule_reopen();
                }
            }
            continue;
        }
        if (message.kind == UAC_QUEUE_DRIVER_EVENT &&
            message.data.driver.event == UAC_HOST_DRIVER_EVENT_RX_CONNECTED) {
            if (s_lark_handle == NULL) {
                s_reopen_pending = false;
                s_reopen_attempts = 0U;
                (void)open_lark(message.data.driver.address,
                                message.data.driver.interface_number);
            }
        } else if (message.kind == UAC_QUEUE_DEVICE_EVENT) {
            handle_device_event(&message);
        }
    }
}

static void usb_host_task(void *argument)
{
    TaskHandle_t uac_task_handle = (TaskHandle_t)argument;
    const usb_host_config_t host_config = {
        .skip_phy_setup = false,
        .intr_flags = ESP_INTR_FLAG_LOWMED,
    };
    esp_err_t result = usb_host_install(&host_config);
    if (result != ESP_OK) {
        ESP_LOGE(TAG, "USB host install failed: %s", esp_err_to_name(result));
        s31_gateway_set_fault(BRIDGE_FAULT_UAC_LOST);
        vTaskDelete(NULL);
        return;
    }
    ESP_LOGI(TAG, "USB host installed");
    xTaskNotifyGive(uac_task_handle);
    for (;;) {
        uint32_t event_flags = 0;
        result = usb_host_lib_handle_events(portMAX_DELAY, &event_flags);
        if (result != ESP_OK) {
            ESP_LOGE(TAG, "USB host event loop failed: %s", esp_err_to_name(result));
            s31_gateway_set_fault(BRIDGE_FAULT_UAC_LOST);
            vTaskDelay(pdMS_TO_TICKS(100));
        }
    }
}

esp_err_t s31_uac_lark_init(void)
{
    s_events = xQueueCreate(UAC_EVENT_QUEUE_LENGTH, sizeof(uac_queue_event_t));
    if (s_events == NULL) {
        return ESP_ERR_NO_MEM;
    }
    TaskHandle_t uac_task_handle = NULL;
    if (xTaskCreate(uac_task, "s31_uac", 8192, NULL, 8, &uac_task_handle) != pdPASS) {
        vQueueDelete(s_events);
        s_events = NULL;
        return ESP_ERR_NO_MEM;
    }
    if (xTaskCreate(usb_host_task, "s31_usb", 4096, uac_task_handle, 9, NULL) != pdPASS) {
        vTaskDelete(uac_task_handle);
        vQueueDelete(s_events);
        s_events = NULL;
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}
