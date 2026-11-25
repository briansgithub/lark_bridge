#ifndef FAKE_LARK_UART_CTL_H
#define FAKE_LARK_UART_CTL_H

#include "audio_gen.h"
#include "usb_descriptors.h"

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef enum {
    CTL_ACTION_NONE = 0,
    CTL_ACTION_UNPLUG,
    CTL_ACTION_CONNECT,
    CTL_ACTION_REPLUG,
} ctl_action_t;

typedef struct {
    bool connected;
    bool streaming;
    uint8_t active_alt;
    uint32_t packets_sent;
    uint32_t replugs;
    usb_profile_t profile;
} ctl_status_t;

typedef struct {
    ctl_action_t action;
    uint32_t replug_ms;
    usb_profile_t new_profile;
    bool profile_changed;
} ctl_result_t;

/* Pure C11 line parser & dispatcher. Writes a newline-terminated reply into `reply`. */
void uart_ctl_handle(const char *line, audio_gen_t *gen, const ctl_status_t *st,
                     char *reply, size_t reply_cap, ctl_result_t *res);

#endif
