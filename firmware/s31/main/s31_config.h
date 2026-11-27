#pragma once

#include <stdint.h>

enum {
    S31_LARK_USB_VID = 0x3547,
    S31_LARK_USB_PID = 0x0407,

    S31_AUDIO_SAMPLE_RATE_HZ = 48000,
    S31_AUDIO_CHANNELS = 2,
    S31_AUDIO_BITS_PER_SAMPLE = 16,

    S31_I2S_BCLK_GPIO = 47,
    S31_I2S_WS_GPIO = 48,
    S31_I2S_DATA_GPIO = 45,

    S31_LINK_UART_PORT = 1,
    S31_LINK_UART_TX_GPIO = 43,
    S31_LINK_UART_RX_GPIO = 44,
    S31_LINK_UART_BAUD = 115200,

    S31_LINK_HEARTBEAT_PERIOD_MS = 500,
    S31_LINK_TIMEOUT_MS = 1400,

    S31_BT_RECONNECT_INITIAL_MS = 6000,
    S31_BT_RECONNECT_PEER_PROFILE_MS = 2000,
    S31_BT_RECONNECT_DISCONNECT_HOLD_MS = 6000,
    S31_BT_RECONNECT_MAX_MS = 30000,
    S31_BT_RECONNECT_STALL_MS = 10000,
    S31_BT_RECONNECT_RECYCLE_STALLS = 2,
    S31_BT_RECONNECT_RECYCLE_COOLDOWN_MS = 30000,
    /* After a reset that cut a live link, the phone keeps the stale ACL
     * until its link supervision timeout (the Pixel negotiates 8000 slots,
     * 5 s). Paging it before then fails with HCI 0x0B ("connection already
     * exists") and leaves Bluedroid ignoring later connect requests (D4).
     * The reconnect task starts about 0.8 s after boot, so this places the
     * first page about 6 s after reset. */
    S31_BT_RECONNECT_BOOT_HOLD_MS = 5000,
    /* Two user disconnects (HCI 0x13 from the phone) without a sustained
     * connection in between mean "stop paging me" until power cycle. */
    S31_BT_INTENTIONAL_DISCONN_LIMIT = 2,
    S31_BT_INTENTIONAL_DISCONN_RESET_MS = 120000,
    S31_BT_HCI_REMOTE_USER_TERMINATED = 0x13,
};

#define S31_BT_DEVICE_NAME "LarkBridge"
