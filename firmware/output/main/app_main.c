#include "bridge_link.h"
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

static esp_err_t initialize_nvs(void) {
#if CONFIG_LARKBRIDGE_OUTPUT_ERASE_BONDS_ON_BOOT
    esp_err_t erase_error = nvs_flash_erase();
    if (erase_error != ESP_OK) {
        return erase_error;
    }
#endif
    esp_err_t error = nvs_flash_init();
    if (error == ESP_ERR_NVS_NO_FREE_PAGES || error == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        error = nvs_flash_erase();
        if (error == ESP_OK) {
            error = nvs_flash_init();
        }
    }
    return error;
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

    for (;;) {
        uint8_t ready = 0U;
        uint32_t current_ms = (uint32_t)(esp_timer_get_time() / 1000);

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
        if (output_bluetooth_audio_streaming() && output_audio_clock_active(current_ms) &&
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
        vTaskDelay(pdMS_TO_TICKS(100U));
    }
}
