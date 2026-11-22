#include "aec_gate.h"
#include "audio_router.h"
#include "bridge_link.h"
#include "bt_gateway.h"
#include "esp_app_desc.h"
#include "esp_chip_info.h"
#include "esp_err.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_system.h"
#include "gateway_state.h"
#include "link_uart.h"
#include "nvs_flash.h"
#include "uac_lark.h"

static const char *TAG = "s31_main";

static void log_identity_banner(void)
{
    esp_chip_info_t chip;
    uint8_t bt_address[6] = {0};
    const esp_app_desc_t *app = esp_app_get_description();
    esp_chip_info(&chip);
    esp_err_t mac_result = esp_read_mac(bt_address, ESP_MAC_BT);
    ESP_LOGI(TAG,
             "IDENTITY role=s31 project=%s version=%s idf=%s model=%d rev=%u cores=%u reset=%d",
             app->project_name, app->version, esp_get_idf_version(), chip.model,
             chip.revision, chip.cores, esp_reset_reason());
    ESP_LOGI(TAG,
             "IDENTITY bt=%s%02x:%02x:%02x:%02x:%02x:%02x heap8=%u largest8=%u "
             "erase_bonds=%d aec_publish=%d",
             mac_result == ESP_OK ? "" : "UNAVAILABLE/",
             bt_address[0], bt_address[1], bt_address[2], bt_address[3],
             bt_address[4], bt_address[5],
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_8BIT),
             (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_8BIT),
#if CONFIG_LARKBRIDGE_S31_ERASE_BONDS_ON_BOOT
             1,
#else
             0,
#endif
#ifdef CONFIG_LARKBRIDGE_S31_EXPERIMENTAL_AEC_UPLINK
             1
#else
             0
#endif
    );
}

static esp_err_t initialize_nvs(void)
{
#if CONFIG_LARKBRIDGE_S31_ERASE_BONDS_ON_BOOT
    esp_err_t erase_result = nvs_flash_erase();
    if (erase_result != ESP_OK) {
        return erase_result;
    }
#endif
    esp_err_t result = nvs_flash_init();
    if (result == ESP_ERR_NVS_NO_FREE_PAGES || result == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        result = nvs_flash_erase();
        if (result == ESP_OK) {
            result = nvs_flash_init();
        }
    }
    return result;
}

void app_main(void)
{
    esp_err_t result = initialize_nvs();
    if (result != ESP_OK) {
        ESP_LOGE(TAG, "NVS initialization failed: %s", esp_err_to_name(result));
        return;
    }
    log_identity_banner();

    result = s31_audio_router_init();
    s31_gateway_state_init();
    if (result == ESP_OK) {
        s31_gateway_set_i2s_ready(true);
    } else {
        ESP_LOGE(TAG, "audio/I2S initialization failed: %s", esp_err_to_name(result));
        s31_gateway_set_fault(BRIDGE_FAULT_AUDIO_UNDERRUN);
    }

    result = s31_link_uart_init();
    if (result != ESP_OK) {
        ESP_LOGE(TAG, "inter-board UART initialization failed: %s", esp_err_to_name(result));
        s31_gateway_set_fault(BRIDGE_FAULT_PROTOCOL);
    }

    result = s31_aec_gate_init();
    if (result != ESP_OK) {
        ESP_LOGE(TAG, "AEC worker initialization failed: %s", esp_err_to_name(result));
        s31_gateway_set_aec_ready(false);
        s31_gateway_set_fault(BRIDGE_FAULT_AEC_NOT_READY);
    }

    result = s31_uac_lark_init();
    if (result != ESP_OK) {
        ESP_LOGE(TAG, "USB/UAC initialization failed: %s", esp_err_to_name(result));
        s31_gateway_set_fault(BRIDGE_FAULT_UAC_LOST);
    }

    result = s31_bt_gateway_init();
    if (result != ESP_OK) {
        ESP_LOGE(TAG, "Bluetooth gateway initialization failed: %s", esp_err_to_name(result));
        s31_gateway_set_bt_ready(false);
        s31_gateway_set_fault(BRIDGE_FAULT_BLUETOOTH);
    }

    ESP_LOGW(TAG, "pre-hardware firmware started; call uplink remains digitally muted "
                  "unless the explicit experimental AEC qualification option is enabled");
}
