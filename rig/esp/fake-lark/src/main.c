#include "audio_gen.h"
#include "uac1_driver.h"
#include "uart_ctl.h"
#include "usb_descriptors.h"

#include "bsp/board.h"
#include "hardware/gpio.h"
#include "hardware/uart.h"
#include "pico/stdlib.h"
#include "tusb.h"

#include <stdbool.h>
#include <stdint.h>
#include <string.h>

#define CTL_UART        uart0
#define CTL_UART_BAUD   115200U
#define CTL_UART_TX_PIN 0U
#define CTL_UART_RX_PIN 1U
#define LED_PIN         25U

static audio_gen_t s_audio;
static uint32_t s_replugs;
static bool s_replug_active;
static absolute_time_t s_reconnect_at;
static char s_pending_reply[192];

static void uart_send_str(const char *s)
{
    while (*s) {
        uart_putc_raw(CTL_UART, *s++);
    }
}

static void init_ctl_uart(void)
{
    uart_init(CTL_UART, CTL_UART_BAUD);
    gpio_set_function(CTL_UART_TX_PIN, GPIO_FUNC_UART);
    gpio_set_function(CTL_UART_RX_PIN, GPIO_FUNC_UART);
    /* Pi BCM14 floats between `pico-ctl send` commands; pull-up keeps RX idle HIGH. */
    gpio_pull_up(CTL_UART_RX_PIN);
    uart_set_format(CTL_UART, 8, 1, UART_PARITY_NONE);
    uart_set_fifo_enabled(CTL_UART, true);
}

int main(void)
{
    board_init();
    gpio_init(LED_PIN);
    gpio_set_dir(LED_PIN, GPIO_OUT);
    gpio_put(LED_PIN, 0);

    audio_gen_init(&s_audio);
    uac1_driver_bind_audio(&s_audio);
    init_ctl_uart();

    tud_init(0);

    char line[128];
    size_t line_len = 0U;

    for (;;) {
        tud_task();

        /* Complete pending non-blocking REPLUG once disconnect window elapses. */
        if (s_replug_active && absolute_time_diff_us(get_absolute_time(), s_reconnect_at) <= 0) {
            s_replug_active = false;
            tud_connect();
            s_replugs++;
            if (s_pending_reply[0] != '\0') {
                uart_send_str(s_pending_reply);
                s_pending_reply[0] = '\0';
            }
        }

        /* Drain control UART without blocking USB. */
        while (uart_is_readable(CTL_UART)) {
            uint32_t dr = uart_get_hw(CTL_UART)->dr;
            /* Drop framing/break/overrun glitches when Pi toggles BCM14 a5 <-> input. */
            if (dr & 0xF00U) {
                continue;
            }
            char ch = (char)(dr & 0xFFU);
            if (ch == '\r' || ch == '\n') {
                if (line_len > 0U) {
                    line[line_len] = '\0';
                    line_len = 0U;

                    uac1_state_t ust;
                    uac1_driver_get_state(&ust);
                    ctl_status_t st = {
                        .connected = ust.mounted && !s_replug_active,
                        .streaming = ust.streaming && !s_replug_active,
                        .active_alt = ust.active_alt,
                        .packets_sent = ust.packets_sent,
                        .replugs = s_replugs,
                        .profile = usb_descriptors_get_profile(),
                    };
                    char reply[192];
                    ctl_result_t res;
                    uart_ctl_handle(line, &s_audio, &st, reply, sizeof(reply), &res);

                    if (res.profile_changed) {
                        usb_descriptors_set_profile(res.new_profile);
                    }
                    if (res.action == CTL_ACTION_UNPLUG) {
                        s_replug_active = false;
                        tud_disconnect();
                        uart_send_str(reply);
                    } else if (res.action == CTL_ACTION_CONNECT) {
                        s_replug_active = false;
                        tud_connect();
                        uart_send_str(reply);
                    } else if (res.action == CTL_ACTION_REPLUG) {
                        tud_disconnect();
                        s_replug_active = true;
                        s_reconnect_at = make_timeout_time_ms(res.replug_ms);
                        strncpy(s_pending_reply, reply, sizeof(s_pending_reply) - 1U);
                        s_pending_reply[sizeof(s_pending_reply) - 1U] = '\0';
                    } else if (reply[0] != '\0') {
                        uart_send_str(reply);
                    }
                }
            } else if ((unsigned char)ch >= 0x20U && (unsigned char)ch < 0x7FU) {
                if (line_len + 1U < sizeof(line)) {
                    line[line_len++] = ch;
                }
            }
        }

        /* Visual LED indicator on GP25: solid ON = streaming, slow blink = mounted, OFF = detached. */
        uac1_state_t ust;
        uac1_driver_get_state(&ust);
        if (ust.streaming && !s_replug_active) {
            gpio_put(LED_PIN, 1);
        } else if (ust.mounted && !s_replug_active) {
            gpio_put(LED_PIN, ((to_ms_since_boot(get_absolute_time()) / 500U) & 1U) != 0U);
        } else {
            gpio_put(LED_PIN, 0);
        }
    }
}
