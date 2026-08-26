#ifndef OUTPUT_PEER_STORE_H
#define OUTPUT_PEER_STORE_H

#include "esp_err.h"

#include <stdbool.h>
#include <stdint.h>

#define OUTPUT_PEER_ADDRESS_SIZE 6U

esp_err_t output_peer_store_load(uint8_t address[OUTPUT_PEER_ADDRESS_SIZE], bool *found);
esp_err_t output_peer_store_save(const uint8_t address[OUTPUT_PEER_ADDRESS_SIZE]);
esp_err_t output_peer_store_clear(void);

#endif
