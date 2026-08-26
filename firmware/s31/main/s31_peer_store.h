#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#define S31_PEER_ADDRESS_SIZE 6U

esp_err_t s31_peer_store_load(uint8_t address[S31_PEER_ADDRESS_SIZE], bool *found);
esp_err_t s31_peer_store_save(const uint8_t address[S31_PEER_ADDRESS_SIZE]);
