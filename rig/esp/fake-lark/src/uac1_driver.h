#ifndef FAKE_LARK_UAC1_DRIVER_H
#define FAKE_LARK_UAC1_DRIVER_H

#include "audio_gen.h"

#include <stdbool.h>
#include <stdint.h>

typedef struct {
    bool mounted;
    bool streaming;
    uint8_t active_alt;
    uint32_t sample_rate_hz;
    uint32_t packets_sent;
} uac1_state_t;

void uac1_driver_bind_audio(audio_gen_t *gen);
void uac1_driver_get_state(uac1_state_t *out);

#endif
