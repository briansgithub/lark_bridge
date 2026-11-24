#include "reconnect_timer.h"

#include <stdio.h>

int main(void)
{
    const uint32_t starts[] = {100U, UINT32_MAX - 2000U};
    for (unsigned int i = 0; i < sizeof(starts) / sizeof(starts[0]); ++i) {
        uint32_t start = starts[i];
        bool held = true;
        if (!s31_reconnect_boot_held(&held, start, start) ||
            !s31_reconnect_boot_held(&held, start, start + 1000U) ||
            !s31_reconnect_boot_held(&held, start, start + 4999U) ||
            s31_reconnect_boot_held(&held, start, start + 5000U) ||
            s31_reconnect_boot_held(&held, start, start + 6000U) ||
            s31_reconnect_boot_held(&held, start, start)) {
            fprintf(stderr, "boot hold failed for start tick %u\n", (unsigned int)start);
            return 1;
        }
    }
    return 0;
}
