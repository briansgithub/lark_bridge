#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

esp_err_t s31_bt_gateway_init(void);
bool s31_bt_avrcp_command_supported(uint8_t command);
esp_err_t s31_bt_send_avrcp(uint8_t command, uint8_t key_state);
