#ifndef FAKE_LARK_USB_DESCRIPTORS_H
#define FAKE_LARK_USB_DESCRIPTORS_H

#include <stdbool.h>
#include <stdint.h>

#define FAKE_LARK_ITF_AC        0U
#define FAKE_LARK_ITF_AS        1U
#define FAKE_LARK_EP_AUDIO_IN   0x87U
#define FAKE_LARK_MAX_PKT_24    300U
#define FAKE_LARK_MAX_PKT_16    200U

typedef enum {
    PROFILE_LARK_A1 = 0,
    PROFILE_WRONG_VID,
    PROFILE_WRONG_RATE,
    PROFILE_MONO_ONLY,
    PROFILE_BIT24_ONLY,
    PROFILE_UAC2_BAD,
} usb_profile_t;

void usb_descriptors_set_profile(usb_profile_t profile);
usb_profile_t usb_descriptors_get_profile(void);
const char *usb_profile_name(usb_profile_t profile);
bool usb_profile_parse(const char *name, usb_profile_t *out);

/* Query active streaming parameters for the selected alternate setting of the current profile. */
void usb_descriptors_alt_params(uint8_t alt, uint8_t *channels, uint8_t *bytes_per_sample,
                                uint32_t *sample_rate_hz, uint16_t *max_packet_size);

#endif
