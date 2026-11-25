#include "uac1_driver.h"
#include "usb_descriptors.h"

#include "tusb.h"
#include "device/usbd_pvt.h"

#include <string.h>

static audio_gen_t *s_gen;
static bool s_mounted;
static uint8_t s_active_alt;
static uint8_t s_channels = 2U;
static uint8_t s_bytes_per_sample = 2U;
static uint32_t s_sample_rate_hz = 48000U;
static uint16_t s_max_packet_size = FAKE_LARK_MAX_PKT_16;
static uint32_t s_packets_sent;
static bool s_ep_open;
static uint8_t s_ep_addr;
static uint8_t s_freq_buf[3];
static CFG_TUSB_MEM_ALIGN uint8_t s_iso_buf[FAKE_LARK_MAX_PKT_24];

void uac1_driver_bind_audio(audio_gen_t *gen)
{
    s_gen = gen;
}

void uac1_driver_get_state(uac1_state_t *out)
{
    out->mounted = s_mounted;
    out->streaming = s_mounted && (s_active_alt > 0U) && s_ep_open;
    out->active_alt = s_active_alt;
    out->sample_rate_hz = s_sample_rate_hz;
    out->packets_sent = s_packets_sent;
}

void tud_mount_cb(void)
{
    s_mounted = true;
}

void tud_umount_cb(void)
{
    s_mounted = false;
    s_active_alt = 0U;
    s_ep_open = false;
}

static void queue_next_iso_packet(uint8_t rhport)
{
    if (!s_ep_open || s_active_alt == 0U || s_gen == NULL) {
        return;
    }
    if (usbd_edpt_busy(rhport, s_ep_addr)) {
        return;
    }
    size_t len = audio_gen_fill_ms(s_gen, s_iso_buf, s_max_packet_size,
                                   s_channels, s_bytes_per_sample);
    if (len > 0U) {
        (void)usbd_edpt_xfer(rhport, s_ep_addr, s_iso_buf, (uint16_t)len);
    }
}

static void uac1_init(void)
{
    s_mounted = false;
    s_active_alt = 0U;
    s_ep_open = false;
    s_ep_addr = FAKE_LARK_EP_AUDIO_IN;
}

static void uac1_reset(uint8_t rhport)
{
    (void)rhport;
    s_mounted = false;
    s_active_alt = 0U;
    s_ep_open = false;
}

static uint16_t uac1_open(uint8_t rhport, tusb_desc_interface_t const *itf_desc, uint16_t max_len)
{
    (void)rhport;
    if (itf_desc->bInterfaceClass != TUSB_CLASS_AUDIO) {
        return 0U;
    }
    /* Claim only this interface number (and all of its alternate settings &
     * endpoints) so TinyUSB's process_set_config calls uac1_open() once per
     * bInterfaceNumber and populates itf2drv[0], itf2drv[1], and ep2drv[][]. */
    uint8_t const target_itf = itf_desc->bInterfaceNumber;
    uint8_t const *p = (uint8_t const *)itf_desc;
    uint8_t const *end = p + max_len;
    while (p < end) {
        uint8_t len = p[0];
        uint8_t type = p[1];
        if (len == 0U || p + len > end) {
            break;
        }
        if (type == TUSB_DESC_INTERFACE && p != (uint8_t const *)itf_desc) {
            tusb_desc_interface_t const *next_itf = (tusb_desc_interface_t const *)p;
            if (next_itf->bInterfaceNumber != target_itf) {
                break;
            }
        }
        p += len;
    }
    if (target_itf == FAKE_LARK_ITF_AS) {
        (void)usbd_edpt_iso_alloc(rhport, FAKE_LARK_EP_AUDIO_IN, FAKE_LARK_MAX_PKT_24);
        s_active_alt = 0U;
        s_ep_open = false;
    }
    return (uint16_t)(p - (uint8_t const *)itf_desc);
}

static bool uac1_control_xfer_cb(uint8_t rhport, uint8_t stage,
                                 tusb_control_request_t const *request)
{
    /* Standard SET_INTERFACE / GET_INTERFACE on AS interface (If 1). */
    if (request->bmRequestType_bit.type == TUSB_REQ_TYPE_STANDARD &&
        request->bmRequestType_bit.recipient == TUSB_REQ_RCPT_INTERFACE &&
        request->wIndex == FAKE_LARK_ITF_AS) {
        if (request->bRequest == TUSB_REQ_SET_INTERFACE) {
            if (stage == CONTROL_STAGE_SETUP) {
                uint8_t alt = (uint8_t)request->wValue;
                s_ep_open = false;
                s_active_alt = alt;
                if (alt > 0U) {
                    usb_descriptors_alt_params(alt, &s_channels, &s_bytes_per_sample,
                                               &s_sample_rate_hz, &s_max_packet_size);
                    if (s_gen != NULL) {
                        audio_gen_set_rate(s_gen, s_sample_rate_hz);
                    }
                    tusb_desc_endpoint_t ep_desc = {
                        .bLength = sizeof(tusb_desc_endpoint_t),
                        .bDescriptorType = TUSB_DESC_ENDPOINT,
                        .bEndpointAddress = FAKE_LARK_EP_AUDIO_IN,
                        .bmAttributes = {.xfer = TUSB_XFER_ISOCHRONOUS, .sync = 1, .usage = 0},
                        .wMaxPacketSize = s_max_packet_size,
                        .bInterval = 1,
                    };
                    s_ep_addr = FAKE_LARK_EP_AUDIO_IN;
                    if (usbd_edpt_iso_activate(rhport, &ep_desc)) {
                        s_ep_open = true;
                        queue_next_iso_packet(rhport);
                    }
                }
                return tud_control_status(rhport, request);
            }
            return true;
        }
        if (request->bRequest == TUSB_REQ_GET_INTERFACE) {
            if (stage == CONTROL_STAGE_SETUP) {
                return tud_control_xfer(rhport, request, &s_active_alt, 1U);
            }
            return true;
        }
    }

    /* UAC 1.0 Class Endpoint requests: SET_CUR (0x01) / GET_CUR (0x81) Sampling Freq. */
    if (request->bmRequestType_bit.type == TUSB_REQ_TYPE_CLASS &&
        request->bmRequestType_bit.recipient == TUSB_REQ_RCPT_ENDPOINT &&
        (request->wIndex & 0xFFU) == FAKE_LARK_EP_AUDIO_IN &&
        (request->wValue >> 8) == 0x01U) {
        if (request->bRequest == 0x01U) { /* SET_CUR */
            if (stage == CONTROL_STAGE_SETUP) {
                return tud_control_xfer(rhport, request, s_freq_buf,
                                        request->wLength <= 3U ? request->wLength : 3U);
            }
            if (stage == CONTROL_STAGE_DATA) {
                uint32_t hz = (uint32_t)s_freq_buf[0] |
                              ((uint32_t)s_freq_buf[1] << 8) |
                              ((uint32_t)s_freq_buf[2] << 16);
                if (hz >= 8000U && hz <= 96000U) {
                    s_sample_rate_hz = hz;
                    if (s_gen != NULL) {
                        audio_gen_set_rate(s_gen, hz);
                    }
                }
            }
            return true;
        }
        if (request->bRequest == 0x81U) { /* GET_CUR */
            if (stage == CONTROL_STAGE_SETUP) {
                s_freq_buf[0] = (uint8_t)(s_sample_rate_hz & 0xFFU);
                s_freq_buf[1] = (uint8_t)((s_sample_rate_hz >> 8) & 0xFFU);
                s_freq_buf[2] = (uint8_t)((s_sample_rate_hz >> 16) & 0xFFU);
                return tud_control_xfer(rhport, request, s_freq_buf, 3U);
            }
            return true;
        }
    }

    return false;
}

static bool uac1_xfer_cb(uint8_t rhport, uint8_t ep_addr, xfer_result_t result,
                         uint32_t xferred_bytes)
{
    (void)result;
    (void)xferred_bytes;
    if (ep_addr == s_ep_addr && s_ep_open && s_active_alt > 0U) {
        s_packets_sent++;
        queue_next_iso_packet(rhport);
    }
    return true;
}

static const usbd_class_driver_t k_uac1_driver = {
#if CFG_TUSB_DEBUG >= 2
    .name             = "FAKE_LARK_UAC1",
#endif
    .init             = uac1_init,
    .reset            = uac1_reset,
    .open             = uac1_open,
    .control_xfer_cb  = uac1_control_xfer_cb,
    .xfer_cb          = uac1_xfer_cb,
    .sof              = NULL,
};

usbd_class_driver_t const *usbd_app_driver_get_cb(uint8_t *driver_count)
{
    *driver_count = 1U;
    return &k_uac1_driver;
}
