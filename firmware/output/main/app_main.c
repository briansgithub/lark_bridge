#include "bridge_link.h"
#include "driver/gpio.h"
#include "esp_app_desc.h"
#include "esp_chip_info.h"
#include "esp_err.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"
#include "output_audio.h"
#include "output_bluetooth.h"
#include "output_control.h"
#include "output_sink_policy.h"
#include "output_telemetry.h"

#include <stdint.h>

static const char *TAG = "larkbridge_output";

static void log_identity_banner(void) {
    esp_chip_info_t chip;
    uint8_t bt_address[6] = {0};
    const esp_app_desc_t *app = esp_app_get_description();
    esp_chip_info(&chip);
    esp_err_t mac_result = esp_read_mac(bt_address, ESP_MAC_BT);
    ESP_LOGI(TAG,
             "IDENTITY role=output project=%s version=%s idf=%s model=%d rev=%u cores=%u reset=%d",
             app->project_name, app->version, esp_get_idf_version(), chip.model,
             chip.revision, chip.cores, esp_reset_reason());
    ESP_LOGI(TAG,
             "IDENTITY bt=%s%02x:%02x:%02x:%02x:%02x:%02x heap8=%u largest8=%u "
             "erase_bonds=%d",
             mac_result == ESP_OK ? "" : "UNAVAILABLE/",
             bt_address[0], bt_address[1], bt_address[2], bt_address[3],
             bt_address[4], bt_address[5],
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_8BIT),
             (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_8BIT),
#if CONFIG_LARKBRIDGE_OUTPUT_ERASE_BONDS_ON_BOOT
             1
#else
             0
#endif
    );
}

/* DevKitC BOOT button: GPIO0, active low, external 10k pull-up (internal pull-up
 * enabled as well). No conflict with I2S (26/25/22) or the S31 UART (16/17). */
#define BOOT_BUTTON_GPIO GPIO_NUM_0

static bool boot_button_init(void) {
    const gpio_config_t config = {
        .pin_bit_mask = 1ULL << BOOT_BUTTON_GPIO,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    return gpio_config(&config) == ESP_OK;
}

static void boot_button_poll(output_button_t *button) {
    bool pressed = gpio_get_level(BOOT_BUTTON_GPIO) == 0;
    uint32_t now = (uint32_t)(esp_timer_get_time() / 1000);
    switch (output_button_update(button, pressed, now)) {
    case OUTPUT_BUTTON_HINT_PAIR:
        ESP_LOGI(TAG, "BOOT held 3 s: release now for pairing mode (keep holding to 10 s to wipe)");
        break;
    case OUTPUT_BUTTON_HINT_WIPE:
        ESP_LOGW(TAG, "BOOT held 10 s: release now to WIPE all saved sinks and bonds");
        break;
    case OUTPUT_BUTTON_STUCK:
        ESP_LOGW(TAG, "BOOT held 20 s: treating as stuck (serial DTR/RTS?) and ignoring");
        break;
    case OUTPUT_BUTTON_PAIR:
        ESP_LOGI(TAG, "BOOT button: entering pairing mode");
        (void)output_bluetooth_request_pairing();
        break;
    case OUTPUT_BUTTON_WIPE:
        ESP_LOGW(TAG, "BOOT button: factory wipe of saved sinks, then pairing mode");
        (void)output_bluetooth_request_factory_wipe();
        break;
    default:
        break;
    }
}

#if CONFIG_LARKBRIDGE_OUTPUT_STATUS_LED_GPIO >= 0
#if CONFIG_LARKBRIDGE_OUTPUT_STATUS_LED_GPIO == 0 || CONFIG_LARKBRIDGE_OUTPUT_STATUS_LED_GPIO == 16 ||     CONFIG_LARKBRIDGE_OUTPUT_STATUS_LED_GPIO == 17 || CONFIG_LARKBRIDGE_OUTPUT_STATUS_LED_GPIO == 22 ||     CONFIG_LARKBRIDGE_OUTPUT_STATUS_LED_GPIO == 25 || CONFIG_LARKBRIDGE_OUTPUT_STATUS_LED_GPIO == 26
#error "status LED GPIO conflicts with BOOT, UART or I2S"
#endif
#define STATUS_LED_GPIO ((gpio_num_t)CONFIG_LARKBRIDGE_OUTPUT_STATUS_LED_GPIO)

static bool status_led_init(void) {
    const gpio_config_t config = {
        .pin_bit_mask = 1ULL << STATUS_LED_GPIO,
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    if (gpio_config(&config) != ESP_OK) {
        return false;
    }
    (void)gpio_set_level(STATUS_LED_GPIO, 0);
    return true;
}
#endif

static esp_err_t initialize_nvs(void) {
#if CONFIG_LARKBRIDGE_OUTPUT_ERASE_BONDS_ON_BOOT
    esp_err_t erase_error = nvs_flash_erase();
    if (erase_error != ESP_OK) {
        return erase_error;
    }
#endif
    return nvs_flash_init();
}

void app_main(void) {
    esp_err_t nvs_error;
    esp_err_t audio_error;
    esp_err_t control_error;
    esp_err_t bluetooth_error = ESP_ERR_INVALID_STATE;
    uint8_t previous_ready = UINT8_MAX;

    ESP_LOGI(TAG, "starting ESP32 output controller");
    nvs_error = initialize_nvs();
    if (nvs_error != ESP_OK) {
        ESP_LOGE(TAG, "NVS initialization failed: %s", esp_err_to_name(nvs_error));
    } else {
        log_identity_banner();
    }

    audio_error = output_audio_start();
    if (audio_error != ESP_OK) {
        ESP_LOGE(TAG, "I2S initialization failed: %s", esp_err_to_name(audio_error));
    }
    control_error = output_control_start();
    if (control_error != ESP_OK) {
        ESP_LOGE(TAG, "control link initialization failed: %s", esp_err_to_name(control_error));
    }
    if (nvs_error == ESP_OK) {
        bluetooth_error = output_bluetooth_start();
        if (bluetooth_error != ESP_OK) {
            ESP_LOGE(TAG, "Bluetooth initialization failed: %s", esp_err_to_name(bluetooth_error));
            (void)output_control_report_fault(BRIDGE_FAULT_BLUETOOTH);
        }
    }

    esp_err_t telemetry_error = output_telemetry_start();
    if (telemetry_error != ESP_OK) {
        ESP_LOGE(TAG, "telemetry initialization failed: %s", esp_err_to_name(telemetry_error));
    }

    output_button_t boot_button;
    bool button_ready = boot_button_init();
    output_button_init(&boot_button, button_ready && gpio_get_level(BOOT_BUTTON_GPIO) == 0);
    if (!button_ready) {
        ESP_LOGW(TAG, "BOOT button GPIO unavailable; use the pair console command");
    }
#if CONFIG_LARKBRIDGE_OUTPUT_STATUS_LED_GPIO >= 0
    output_led_t status_led;
    bool led_ready = status_led_init();
    bool led_level = false;
    output_led_init(&status_led, output_bluetooth_wipe_count());
#endif

    for (;;) {
        uint8_t ready = 0U;

        if (button_ready && bluetooth_error == ESP_OK) {
            boot_button_poll(&boot_button);
        }
#if CONFIG_LARKBRIDGE_OUTPUT_STATUS_LED_GPIO >= 0
        if (led_ready) {
            output_bluetooth_sinks_t sinks;
            output_bluetooth_get_sinks(&sinks);
            bool level = output_led_update(&status_led, sinks.pairing, output_bluetooth_wipe_count(),
                                           (uint32_t)(esp_timer_get_time() / 1000));
            if (level != led_level) {
                led_level = level;
                (void)gpio_set_level(STATUS_LED_GPIO, level ? 1 : 0);
            }
        }
#endif

        if (output_control_is_initialized()) {
            ready |= BRIDGE_READY_UART;
        }
        if (output_audio_is_initialized()) {
            ready |= BRIDGE_READY_I2S;
        }
        if (output_bluetooth_stack_ready()) {
            ready |= BRIDGE_READY_BT;
        }
        if (output_bluetooth_peer_connected()) {
            ready |= BRIDGE_READY_PEER;
        }
        if (output_bluetooth_audio_streaming() && output_audio_clock_active() &&
            output_control_link_alive()) {
            ready |= BRIDGE_READY_AUDIO;
        }
        output_control_set_local_ready(ready);
        if (ready != previous_ready) {
            ESP_LOGI(TAG, "readiness=0x%02x%s", ready,
                     (ready & (BRIDGE_READY_UART | BRIDGE_READY_I2S | BRIDGE_READY_BT |
                               BRIDGE_READY_PEER | BRIDGE_READY_AUDIO)) ==
                             (BRIDGE_READY_UART | BRIDGE_READY_I2S | BRIDGE_READY_BT |
                              BRIDGE_READY_PEER | BRIDGE_READY_AUDIO)
                         ? " READY"
                         : " muted");
            previous_ready = ready;
        }
        vTaskDelay(pdMS_TO_TICKS(50U)); /* status LED + BOOT button sampling */
    }
}
