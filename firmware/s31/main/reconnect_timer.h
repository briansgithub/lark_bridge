#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "s31_config.h"

/* Once elapsed, the boot hold stays released across later clock wraps. */
static inline bool s31_reconnect_boot_held(bool *held, uint32_t start_ms, uint32_t now_ms)
{
    *held = *held && now_ms - start_ms < S31_BT_RECONNECT_BOOT_HOLD_MS;
    return *held;
}
