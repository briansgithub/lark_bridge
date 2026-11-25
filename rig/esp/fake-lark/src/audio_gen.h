#ifndef FAKE_LARK_AUDIO_GEN_H
#define FAKE_LARK_AUDIO_GEN_H

#include <stddef.h>
#include <stdint.h>

typedef enum {
    AUDIO_MODE_SILENCE = 0,
    AUDIO_MODE_TONE,
    AUDIO_MODE_NOISE,
    AUDIO_MODE_SPEECH,
} audio_mode_t;

typedef struct {
    audio_mode_t mode;
    uint32_t sample_rate_hz;
    uint32_t tone_hz;
    int32_t dbfs;
    int32_t skew_ppm;
    uint32_t amp_q15;      /* peak amplitude in [0, 32767] */
    uint32_t phase;        /* 32-bit NCO phase accumulator */
    uint32_t phase_inc;
    uint32_t lfsr;         /* deterministic xorshift32 state */
    uint32_t speech_sample;
    int32_t speech_lp1;
    int32_t speech_lp2;
    int64_t skew_accum;    /* Bresenham ppm accumulator */
    uint32_t rate_accum;   /* fractional sample-rate accumulator (e.g. 44.1 kHz) */
} audio_gen_t;

void audio_gen_init(audio_gen_t *g);
void audio_gen_set_mode(audio_gen_t *g, audio_mode_t mode, uint32_t tone_hz, int32_t dbfs);
void audio_gen_set_rate(audio_gen_t *g, uint32_t sample_rate_hz);
void audio_gen_set_skew(audio_gen_t *g, int32_t skew_ppm);
const char *audio_mode_name(audio_mode_t mode);

/* Fills one 1 ms USB isochronous packet into `out` (up to `max_bytes`).
 * `channels` is 1 or 2; `bytes_per_sample` is 2 (S16LE) or 3 (S24LE).
 * Always returns a multiple of (channels * bytes_per_sample) <= max_bytes. */
size_t audio_gen_fill_ms(audio_gen_t *g, uint8_t *out, size_t max_bytes,
                         uint8_t channels, uint8_t bytes_per_sample);

#endif
