#include "s31_peer_store.h"

#include <stddef.h>

#include "nvs.h"

#define STORE_NAMESPACE "lb_gateway"
#define PEER_KEY "pixel_bda"

esp_err_t s31_peer_store_load(uint8_t address[S31_PEER_ADDRESS_SIZE], bool *found)
{
    nvs_handle_t handle;
    size_t length = S31_PEER_ADDRESS_SIZE;
    esp_err_t result;
    if (address == NULL || found == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    *found = false;
    result = nvs_open(STORE_NAMESPACE, NVS_READONLY, &handle);
    if (result == ESP_ERR_NVS_NOT_FOUND) {
        return ESP_OK;
    }
    if (result != ESP_OK) {
        return result;
    }
    result = nvs_get_blob(handle, PEER_KEY, address, &length);
    nvs_close(handle);
    if (result == ESP_ERR_NVS_NOT_FOUND) {
        return ESP_OK;
    }
    if (result != ESP_OK) {
        return result;
    }
    if (length != S31_PEER_ADDRESS_SIZE) {
        return ESP_ERR_INVALID_SIZE;
    }
    *found = true;
    return ESP_OK;
}

esp_err_t s31_peer_store_save(const uint8_t address[S31_PEER_ADDRESS_SIZE])
{
    nvs_handle_t handle;
    esp_err_t result;
    if (address == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    result = nvs_open(STORE_NAMESPACE, NVS_READWRITE, &handle);
    if (result != ESP_OK) {
        return result;
    }
    result = nvs_set_blob(handle, PEER_KEY, address, S31_PEER_ADDRESS_SIZE);
    if (result == ESP_OK) {
        result = nvs_commit(handle);
    }
    nvs_close(handle);
    return result;
}
