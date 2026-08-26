#include "output_peer_store.h"

#include "nvs.h"

#include <stddef.h>

#define STORE_NAMESPACE "lb_output"
#define PEER_KEY "peer_bda"

esp_err_t output_peer_store_load(uint8_t address[OUTPUT_PEER_ADDRESS_SIZE], bool *found) {
    nvs_handle_t handle;
    size_t length = OUTPUT_PEER_ADDRESS_SIZE;
    esp_err_t error;

    if (address == NULL || found == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    *found = false;
    error = nvs_open(STORE_NAMESPACE, NVS_READONLY, &handle);
    if (error == ESP_ERR_NVS_NOT_FOUND) {
        return ESP_OK;
    }
    if (error != ESP_OK) {
        return error;
    }
    error = nvs_get_blob(handle, PEER_KEY, address, &length);
    nvs_close(handle);
    if (error == ESP_ERR_NVS_NOT_FOUND) {
        return ESP_OK;
    }
    if (error != ESP_OK) {
        return error;
    }
    if (length != OUTPUT_PEER_ADDRESS_SIZE) {
        return ESP_ERR_INVALID_SIZE;
    }
    *found = true;
    return ESP_OK;
}

esp_err_t output_peer_store_save(const uint8_t address[OUTPUT_PEER_ADDRESS_SIZE]) {
    nvs_handle_t handle;
    esp_err_t error;

    if (address == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    error = nvs_open(STORE_NAMESPACE, NVS_READWRITE, &handle);
    if (error != ESP_OK) {
        return error;
    }
    error = nvs_set_blob(handle, PEER_KEY, address, OUTPUT_PEER_ADDRESS_SIZE);
    if (error == ESP_OK) {
        error = nvs_commit(handle);
    }
    nvs_close(handle);
    return error;
}

esp_err_t output_peer_store_clear(void) {
    nvs_handle_t handle;
    esp_err_t error = nvs_open(STORE_NAMESPACE, NVS_READWRITE, &handle);

    if (error != ESP_OK) {
        return error;
    }
    error = nvs_erase_key(handle, PEER_KEY);
    if (error == ESP_ERR_NVS_NOT_FOUND) {
        error = ESP_OK;
    }
    if (error == ESP_OK) {
        error = nvs_commit(handle);
    }
    nvs_close(handle);
    return error;
}
