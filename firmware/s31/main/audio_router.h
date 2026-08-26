#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "bridge_session.h"
#include "esp_err.h"

typedef struct {
    uint16_t fifo_frames;
    uint32_t underruns;
    uint32_t overruns;
} s31_audio_stats_t;

esp_err_t s31_audio_router_init(void);
void s31_audio_router_set_route(bridge_route_t route);
void s31_audio_router_set_uplink_ready(bool ready);
void s31_audio_router_set_hfp_rate(uint32_t sample_rate_hz);
void s31_audio_router_set_a2dp_rate(uint32_t sample_rate_hz);
size_t s31_audio_router_write_a2dp(const uint8_t *pcm, size_t length);
size_t s31_audio_router_write_hfp_downlink(const uint8_t *pcm, size_t length);
size_t s31_audio_router_write_lark(const uint8_t *pcm, size_t length);
size_t s31_audio_router_read_lark_for_aec(uint8_t *pcm, size_t length);
bool s31_audio_router_read_aec_inputs(uint8_t *mic_48k_stereo, size_t mic_length,
                                      uint8_t *reference_16k_mono,
                                      size_t reference_length);
int32_t s31_audio_router_aec_fill_difference_samples(void);
size_t s31_audio_router_aec_output_bytes(void);
void s31_audio_router_reset_aec_inputs(void);
void s31_audio_router_reset_aec_stream(void);
uint32_t s31_audio_router_aec_generation(void);
/* Processed AEC output is always 16-kHz signed little-endian mono PCM. */
size_t s31_audio_router_write_aec_output(const uint8_t *pcm, size_t length);
uint32_t s31_audio_router_fill_hfp_uplink(uint8_t *pcm, uint32_t length);
void s31_audio_router_get_stats(s31_audio_stats_t *stats);
