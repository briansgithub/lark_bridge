#include "usb_descriptors.h"

#include "tusb.h"

#include <string.h>

static usb_profile_t s_profile = PROFILE_LARK_A1;

void usb_descriptors_set_profile(usb_profile_t profile)
{
    s_profile = profile;
}

usb_profile_t usb_descriptors_get_profile(void)
{
    return s_profile;
}

const char *usb_profile_name(usb_profile_t profile)
{
    switch (profile) {
    case PROFILE_LARK_A1:    return "lark_a1";
    case PROFILE_WRONG_VID:  return "wrong_vid";
    case PROFILE_WRONG_RATE: return "wrong_rate";
    case PROFILE_MONO_ONLY:  return "mono_only";
    case PROFILE_BIT24_ONLY: return "bit24_only";
    case PROFILE_UAC2_BAD:   return "uac2_bad";
    default:                 return "unknown";
    }
}

bool usb_profile_parse(const char *name, usb_profile_t *out)
{
    if (strcmp(name, "lark_a1") == 0)    { *out = PROFILE_LARK_A1;    return true; }
    if (strcmp(name, "wrong_vid") == 0)  { *out = PROFILE_WRONG_VID;  return true; }
    if (strcmp(name, "wrong_rate") == 0) { *out = PROFILE_WRONG_RATE; return true; }
    if (strcmp(name, "mono_only") == 0)  { *out = PROFILE_MONO_ONLY;  return true; }
    if (strcmp(name, "bit24_only") == 0) { *out = PROFILE_BIT24_ONLY; return true; }
    if (strcmp(name, "uac2_bad") == 0)   { *out = PROFILE_UAC2_BAD;   return true; }
    return false;
}

void usb_descriptors_alt_params(uint8_t alt, uint8_t *channels, uint8_t *bytes_per_sample,
                                uint32_t *sample_rate_hz, uint16_t *max_packet_size)
{
    *channels = 2U;
    *bytes_per_sample = 2U;
    *sample_rate_hz = 48000U;
    *max_packet_size = FAKE_LARK_MAX_PKT_16;

    switch (s_profile) {
    case PROFILE_LARK_A1:
    case PROFILE_WRONG_VID:
    case PROFILE_UAC2_BAD:
        if (alt == 1U) {
            *bytes_per_sample = 3U;
            *max_packet_size = FAKE_LARK_MAX_PKT_24;
        }
        break;
    case PROFILE_WRONG_RATE:
        *sample_rate_hz = 44100U;
        *max_packet_size = 188U;
        break;
    case PROFILE_MONO_ONLY:
        *channels = 1U;
        *max_packet_size = 104U;
        break;
    case PROFILE_BIT24_ONLY:
        *bytes_per_sample = 3U;
        *max_packet_size = FAKE_LARK_MAX_PKT_24;
        break;
    }
}

/* ---------- Device descriptors ---------- */
static const tusb_desc_device_t k_desc_dev_lark = {
    .bLength            = sizeof(tusb_desc_device_t),
    .bDescriptorType    = TUSB_DESC_DEVICE,
    .bcdUSB             = 0x0110,
    .bDeviceClass       = 0x00,
    .bDeviceSubClass    = 0x00,
    .bDeviceProtocol    = 0x00,
    .bMaxPacketSize0    = CFG_TUD_ENDPOINT0_SIZE,
    .idVendor           = 0x3547,
    .idProduct          = 0x0407,
    .bcdDevice          = 0x0100,
    .iManufacturer      = 0x01,
    .iProduct           = 0x02,
    .iSerialNumber      = 0x03,
    .bNumConfigurations = 0x01,
};

static const tusb_desc_device_t k_desc_dev_wrong_vid = {
    .bLength            = sizeof(tusb_desc_device_t),
    .bDescriptorType    = TUSB_DESC_DEVICE,
    .bcdUSB             = 0x0110,
    .bDeviceClass       = 0x00,
    .bDeviceSubClass    = 0x00,
    .bDeviceProtocol    = 0x00,
    .bMaxPacketSize0    = CFG_TUD_ENDPOINT0_SIZE,
    .idVendor           = 0x1234,
    .idProduct          = 0x5678,
    .bcdDevice          = 0x0100,
    .iManufacturer      = 0x01,
    .iProduct           = 0x02,
    .iSerialNumber      = 0x03,
    .bNumConfigurations = 0x01,
};

uint8_t const *tud_descriptor_device_cb(void)
{
    if (s_profile == PROFILE_WRONG_VID) {
        return (uint8_t const *)&k_desc_dev_wrong_vid;
    }
    return (uint8_t const *)&k_desc_dev_lark;
}

/* ---------- Configuration descriptor macros (UAC 1.0) ---------- */
#define U16_LE(x) (uint8_t)((x) & 0xFF), (uint8_t)(((x) >> 8) & 0xFF)
#define U24_LE(x) (uint8_t)((x) & 0xFF), (uint8_t)(((x) >> 8) & 0xFF), (uint8_t)(((x) >> 16) & 0xFF)

/* Common AC (9 + 9 + 12 + 9 = 39 B) + AS Alt 0 (9 B) = 48 B; Config header = 9 B. */
#define UAC1_AC_AND_AS0(bcd_lo, bcd_hi, ch, ch_cfg_lo) \
    /* Interface 0: Audio Control */ \
    9, TUSB_DESC_INTERFACE, FAKE_LARK_ITF_AC, 0, 0, 0x01, 0x01, 0x00, 0, \
    /* CS_INTERFACE: AC Header (wTotalLength = 9 + 12 + 9 = 30) */ \
    9, 0x24, 0x01, (bcd_lo), (bcd_hi), U16_LE(30), 1, FAKE_LARK_ITF_AS, \
    /* CS_INTERFACE: Input Terminal (ID=1, Microphone 0x0201) */ \
    12, 0x24, 0x02, 1, U16_LE(0x0201), 0, (ch), (ch_cfg_lo), 0x00, 0, 0, \
    /* CS_INTERFACE: Output Terminal (ID=2, USB Streaming 0x0101, src=1) */ \
    9, 0x24, 0x03, 2, U16_LE(0x0101), 0, 1, 0, \
    /* Interface 1, Alt 0: Zero-bandwidth Audio Streaming */ \
    9, TUSB_DESC_INTERFACE, FAKE_LARK_ITF_AS, 0, 0, 0x01, 0x02, 0x00, 0

/* One AS streaming alternate: 9 + 7 + 11 + 9 + 7 = 43 B */
#define UAC1_AS_ALT(alt, ch, subf, bits, rate_hz, max_pkt) \
    /* Interface 1, Alt N */ \
    9, TUSB_DESC_INTERFACE, FAKE_LARK_ITF_AS, (alt), 1, 0x01, 0x02, 0x00, 0, \
    /* CS_INTERFACE: AS_GENERAL (terminal=2, delay=1, PCM=0x0001) */ \
    7, 0x24, 0x01, 2, 1, U16_LE(0x0001), \
    /* CS_INTERFACE: FORMAT_TYPE_I (1 discrete freq) */ \
    11, 0x24, 0x02, 0x01, (ch), (subf), (bits), 1, U24_LE(rate_hz), \
    /* Standard Audio ISO IN Endpoint (9 B): EP 0x87, Async ISO, bInterval=1 */ \
    9, TUSB_DESC_ENDPOINT, FAKE_LARK_EP_AUDIO_IN, 0x05, U16_LE(max_pkt), 1, 0, 0, \
    /* CS_ENDPOINT: EP_GENERAL (7 B, SAMPLING_FREQ_CONTROL = 0x01) */ \
    7, 0x25, 0x01, 0x01, 0x00, U16_LE(0)

#define CFG_LEN_2ALT (9 + 48 + 43 + 43) /* 143 B */
#define CFG_LEN_1ALT (9 + 48 + 43)      /* 100 B */

static const uint8_t k_cfg_lark_a1[CFG_LEN_2ALT] = {
    9, TUSB_DESC_CONFIGURATION, U16_LE(CFG_LEN_2ALT), 2, 1, 0, 0x80, 50,
    UAC1_AC_AND_AS0(0x00, 0x01, 2, 0x03),
    UAC1_AS_ALT(1, 2, 3, 24, 48000, FAKE_LARK_MAX_PKT_24),
    UAC1_AS_ALT(2, 2, 2, 16, 48000, FAKE_LARK_MAX_PKT_16),
};

static const uint8_t k_cfg_wrong_rate[CFG_LEN_1ALT] = {
    9, TUSB_DESC_CONFIGURATION, U16_LE(CFG_LEN_1ALT), 2, 1, 0, 0x80, 50,
    UAC1_AC_AND_AS0(0x00, 0x01, 2, 0x03),
    UAC1_AS_ALT(1, 2, 2, 16, 44100, 188),
};

static const uint8_t k_cfg_mono_only[CFG_LEN_1ALT] = {
    9, TUSB_DESC_CONFIGURATION, U16_LE(CFG_LEN_1ALT), 2, 1, 0, 0x80, 50,
    UAC1_AC_AND_AS0(0x00, 0x01, 1, 0x01),
    UAC1_AS_ALT(1, 1, 2, 16, 48000, 104),
};

static const uint8_t k_cfg_bit24_only[CFG_LEN_1ALT] = {
    9, TUSB_DESC_CONFIGURATION, U16_LE(CFG_LEN_1ALT), 2, 1, 0, 0x80, 50,
    UAC1_AC_AND_AS0(0x00, 0x01, 2, 0x03),
    UAC1_AS_ALT(1, 2, 3, 24, 48000, FAKE_LARK_MAX_PKT_24),
};

static const uint8_t k_cfg_uac2_bad[CFG_LEN_2ALT] = {
    9, TUSB_DESC_CONFIGURATION, U16_LE(CFG_LEN_2ALT), 2, 1, 0, 0x80, 50,
    UAC1_AC_AND_AS0(0x00, 0x02, 2, 0x03),
    UAC1_AS_ALT(1, 2, 3, 24, 48000, FAKE_LARK_MAX_PKT_24),
    UAC1_AS_ALT(2, 2, 2, 16, 48000, FAKE_LARK_MAX_PKT_16),
};

uint8_t const *tud_descriptor_configuration_cb(uint8_t index)
{
    (void)index;
    switch (s_profile) {
    case PROFILE_WRONG_RATE: return k_cfg_wrong_rate;
    case PROFILE_MONO_ONLY:  return k_cfg_mono_only;
    case PROFILE_BIT24_ONLY: return k_cfg_bit24_only;
    case PROFILE_UAC2_BAD:   return k_cfg_uac2_bad;
    case PROFILE_LARK_A1:
    case PROFILE_WRONG_VID:
    default:                 return k_cfg_lark_a1;
    }
}

/* ---------- String descriptors ---------- */
static const char *k_strings[] = {
    (const char[]){0x09, 0x04},
    "Shenzhen Hollyland Technology Co.,Ltd",
    "Wireless Microphone",
    "Wireless Microphone",
};

static uint16_t s_str_buf[48];

uint16_t const *tud_descriptor_string_cb(uint8_t index, uint16_t langid)
{
    (void)langid;
    uint8_t chr_count = 0U;
    if (index == 0U) {
        memcpy(&s_str_buf[1], k_strings[0], 2);
        chr_count = 1U;
    } else {
        if (index >= (uint8_t)(sizeof(k_strings) / sizeof(k_strings[0]))) {
            return NULL;
        }
        const char *str = k_strings[index];
        size_t len = strlen(str);
        if (len > 47U) len = 47U;
        for (size_t i = 0; i < len; ++i) {
            s_str_buf[1U + i] = (uint16_t)str[i];
        }
        chr_count = (uint8_t)len;
    }
    s_str_buf[0] = (uint16_t)((TUSB_DESC_STRING << 8) | (2U * chr_count + 2U));
    return s_str_buf;
}
