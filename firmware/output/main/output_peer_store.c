#include "output_peer_store.h"

#include "nvs.h"

#include <stddef.h>
#include <string.h>

#define STORE_NAMESPACE "lb_output"
#define PEER_KEY "peer_bda"
#define LIST_KEY "peer_list"

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

esp_err_t output_peer_store_save_list(const output_sink_list_t *list) {
    nvs_handle_t handle;
    uint8_t blob[OUTPUT_SINK_BLOB_SIZE];
    esp_err_t error;

    if (list == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    output_sink_list_encode(list, blob);
    error = nvs_open(STORE_NAMESPACE, NVS_READWRITE, &handle);
    if (error != ESP_OK) {
        return error;
    }
    error = nvs_set_blob(handle, LIST_KEY, blob, sizeof(blob));
    if (error == ESP_OK) {
        if (list->count > 0U) {
            error = nvs_set_blob(handle, PEER_KEY, list->sinks[0].addr, OUTPUT_PEER_ADDRESS_SIZE);
        } else {
            error = nvs_erase_key(handle, PEER_KEY);
            if (error == ESP_ERR_NVS_NOT_FOUND) {
                error = ESP_OK;
            }
        }
    }
    if (error == ESP_OK) {
        error = nvs_commit(handle);
    }
    nvs_close(handle);
    return error;
}

esp_err_t output_peer_store_load_list(output_sink_list_t *list, bool *migrated) {
    nvs_handle_t handle;
    uint8_t blob[OUTPUT_SINK_BLOB_SIZE];
    size_t length = sizeof(blob);
    esp_err_t error;

    if (list == NULL || migrated == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    *migrated = false;
    output_sink_list_init(list);
    error = nvs_open(STORE_NAMESPACE, NVS_READONLY, &handle);
    if (error == ESP_ERR_NVS_NOT_FOUND) {
        return ESP_OK;
    }
    if (error != ESP_OK) {
        return error;
    }
    error = nvs_get_blob(handle, LIST_KEY, blob, &length);
    nvs_close(handle);
    if (error == ESP_OK) {
        return output_sink_list_decode(blob, length, list) ? ESP_OK : ESP_ERR_INVALID_STATE;
    }
    if (error != ESP_ERR_NVS_NOT_FOUND) {
        return error;
    }

    uint8_t legacy[OUTPUT_PEER_ADDRESS_SIZE];
    bool found = false;
    error = output_peer_store_load(legacy, &found);
    if (error != ESP_OK || !found) {
        return error;
    }
    if (!output_sink_list_migrate_legacy(list, legacy)) {
        return ESP_OK;
    }
    error = output_peer_store_save_list(list);
    *migrated = error == ESP_OK;
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
        error = nvs_erase_key(handle, LIST_KEY);
        if (error == ESP_ERR_NVS_NOT_FOUND) {
            error = ESP_OK;
        }
    }
    if (error == ESP_OK) {
        error = nvs_commit(handle);
    }
    nvs_close(handle);
    return error;
}
