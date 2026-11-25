#include "audio_gen.h"

#include <string.h>

/* 256-entry quarter/full sine table in Q15 (-32767..32767) generated from sin(2*pi*i/256). */
static const int16_t k_sine_lut[256] = {
       0,    804,   1608,   2410,   3212,   4011,   4808,   5602,
    6393,   7179,   7962,   8739,   9512,  10278,  11039,  11793,
   12539,  13279,  14010,  14732,  15446,  16151,  16846,  17530,
   18204,  18868,  19519,  20159,  20787,  21403,  22005,  22594,
   23170,  23731,  24279,  24811,  25329,  25832,  26319,  26790,
   27245,  27683,  28105,  28510,  28898,  29268,  29621,  29956,
   30273,  30571,  30852,  31113,  31356,  31580,  31785,  31971,
   32137,  32285,  32412,  32521,  32609,  32678,  32728,  32757,
   32767,  32757,  32728,  32678,  32609,  32521,  32412,  32285,
   32137,  31971,  31785,  31580,  31356,  31113,  30852,  30571,
   30273,  29956,  29621,  29268,  28898,  28510,  28105,  27683,
   27245,  26790,  26319,  25832,  25329,  24811,  24279,  23731,
   23170,  22594,  22005,  21403,  20787,  20159,  19519,  18868,
   18204,  17530,  16846,  16151,  15446,  14732,  14010,  13279,
   12539,  11793,  11039,  10278,   9512,   8739,   7962,   7179,
    6393,   5602,   4808,   4011,   3212,   2410,   1608,    804,
       0,   -804,  -1608,  -2410,  -3212,  -4011,  -4808,  -5602,
   -6393,  -7179,  -7962,  -8739,  -9512, -10278, -11039, -11793,
  -12539, -13279, -14010, -14732, -15446, -16151, -16846, -17530,
  -18204, -18868, -19519, -20159, -20787, -21403, -22005, -22594,
  -23170, -23731, -24279, -24811, -25329, -25832, -26319, -26790,
  -27245, -27683, -28105, -28510, -28898, -29268, -29621, -29956,
  -30273, -30571, -30852, -31113, -31356, -31580, -31785, -31971,
  -32137, -32285, -32412, -32521, -32609, -32678, -32728, -32757,
  -32767, -32757, -32728, -32678, -32609, -32521, -32412, -32285,
  -32137, -31971, -31785, -31580, -31356, -31113, -30852, -30571,
  -30273, -29956, -29621, -29268, -28898, -28510, -28105, -27683,
  -27245, -26790, -26319, -25832, -25329, -24811, -24279, -23731,
  -23170, -22594, -22005, -21403, -20787, -20159, -19519, -18868,
  -18204, -17530, -16846, -16151, -15446, -14732, -14010, -13279,
  -12539, -11793, -11039, -10278,  -9512,  -8739,  -7962,  -7179,
   -6393,  -5602,  -4808,  -4011,  -3212,  -2410,  -1608,   -804,
};

/* 10^(dbfs / 20) * 32767 for dbfs in [-90..0], integer lookup (no libm needed on M0+). */
static uint32_t dbfs_to_amp_q15(int32_t dbfs)
{
    /* 1 dB step table for 0..-19 dB in Q15, divided by 10 per -20 dB. */
    static const uint16_t k_db0_19[20] = {
        32767, 29204, 26028, 23197, 20675, 18426, 16422, 14637, 13045, 11626,
        10362,  9235,  8231,  7336,  6538,  5827,  5193,  4628,  4125,  3677,
    };
    if (dbfs >= 0) {
        return 32767U;
    }
    if (dbfs <= -90) {
        return 1U;
    }
    int32_t atten = -dbfs;
    uint32_t base = k_db0_19[atten % 20];
    int32_t decades = atten / 20;
    while (decades-- > 0) {
        base = (base + 5U) / 10U;
    }
    return base > 0U ? base : 1U;
}

static void update_phase_inc(audio_gen_t *g)
{
    uint32_t sr = g->sample_rate_hz > 0U ? g->sample_rate_hz : 48000U;
    uint64_t inc = ((uint64_t)g->tone_hz << 32) / sr;
    g->phase_inc = (uint32_t)inc;
}

void audio_gen_init(audio_gen_t *g)
{
    memset(g, 0, sizeof(*g));
    g->sample_rate_hz = 48000U;
    g->lfsr = 0x5A3C96E1U;
    /* Default on boot: 1 kHz at -20 dBFS (RMS ~2317 > TAP_ACTIVE_RMS 300). */
    audio_gen_set_mode(g, AUDIO_MODE_TONE, 1000U, -20);
}

void audio_gen_set_mode(audio_gen_t *g, audio_mode_t mode, uint32_t tone_hz, int32_t dbfs)
{
    if (tone_hz < 100U) {
        tone_hz = 100U;
    } else if (tone_hz > 20000U) {
        tone_hz = 20000U;
    }
    if (dbfs > 0) {
        dbfs = 0;
    } else if (dbfs < -90) {
        dbfs = -90;
    }
    g->mode = mode;
    g->tone_hz = tone_hz;
    g->dbfs = dbfs;
    g->amp_q15 = dbfs_to_amp_q15(dbfs);
    update_phase_inc(g);
}

void audio_gen_set_rate(audio_gen_t *g, uint32_t sample_rate_hz)
{
    g->sample_rate_hz = sample_rate_hz > 0U ? sample_rate_hz : 48000U;
    g->rate_accum = 0U;
    update_phase_inc(g);
}

void audio_gen_set_skew(audio_gen_t *g, int32_t skew_ppm)
{
    if (skew_ppm > 20000) {
        skew_ppm = 20000;
    } else if (skew_ppm < -20000) {
        skew_ppm = -20000;
    }
    g->skew_ppm = skew_ppm;
    g->skew_accum = 0;
}

const char *audio_mode_name(audio_mode_t mode)
{
    switch (mode) {
    case AUDIO_MODE_SILENCE: return "silence";
    case AUDIO_MODE_TONE:    return "tone";
    case AUDIO_MODE_NOISE:   return "noise";
    case AUDIO_MODE_SPEECH:  return "speech";
    default:                 return "unknown";
    }
}

static inline uint32_t xorshift32(uint32_t *state)
{
    uint32_t x = *state;
    x ^= x << 13;
    x ^= x >> 17;
    x ^= x << 5;
    *state = x;
    return x;
}

static inline int16_t next_sample(audio_gen_t *g)
{
    switch (g->mode) {
    case AUDIO_MODE_SILENCE:
        return 0;

    case AUDIO_MODE_TONE: {
        uint32_t p = g->phase;
        g->phase = p + g->phase_inc;
        uint8_t idx = (uint8_t)(p >> 24);
        int32_t s = k_sine_lut[idx];
        return (int16_t)((s * (int32_t)g->amp_q15) >> 15);
    }

    case AUDIO_MODE_NOISE: {
        int32_t r = (int16_t)(xorshift32(&g->lfsr) >> 16);
        return (int16_t)((r * (int32_t)g->amp_q15) >> 15);
    }

    case AUDIO_MODE_SPEECH: {
        /* Deterministic speech-like bursts: 3 Hz syllable envelope (333 ms period,
         * 60% active) modulating a band-limited LFSR + 440 Hz/1130 Hz vocal carrier. */
        uint32_t pos = g->speech_sample++;
        uint32_t sr = g->sample_rate_hz > 0U ? g->sample_rate_hz : 48000U;
        uint32_t period = sr / 3U; /* ~16000 samples */
        uint32_t phase_in_syl = pos % period;
        uint32_t active = (period * 3U) / 5U;
        if (phase_in_syl >= active) {
            return 0;
        }
        int32_t env_q15 = 32767;
        uint32_t ramp = sr / 100U; /* 10 ms attack/release */
        if (phase_in_syl < ramp) {
            env_q15 = (int32_t)((phase_in_syl * 32767U) / ramp);
        } else if (phase_in_syl > active - ramp) {
            env_q15 = (int32_t)(((active - phase_in_syl) * 32767U) / ramp);
        }
        /* Two-pole low-pass filtered noise (~1.8 kHz) mixed with pitch-modulated voice */
        int32_t white = (int16_t)(xorshift32(&g->lfsr) >> 16);
        g->speech_lp1 += (white - g->speech_lp1) >> 2;
        g->speech_lp2 += (g->speech_lp1 - g->speech_lp2) >> 2;
        uint32_t p = g->phase;
        g->phase = p + (uint32_t)(((uint64_t)(210U + ((pos / period) % 5U) * 37U) << 32) / sr);
        int32_t voiced = k_sine_lut[(uint8_t)(p >> 24)];
        int32_t mix = (voiced / 2) + g->speech_lp2;
        if (mix > 32767) mix = 32767;
        if (mix < -32767) mix = -32767;
        int32_t shaped = (mix * env_q15) >> 15;
        return (int16_t)((shaped * (int32_t)g->amp_q15) >> 15);
    }

    default:
        return 0;
    }
}

size_t audio_gen_fill_ms(audio_gen_t *g, uint8_t *out, size_t max_bytes,
                         uint8_t channels, uint8_t bytes_per_sample)
{
    if (channels < 1U) channels = 1U;
    if (channels > 2U) channels = 2U;
    if (bytes_per_sample != 2U && bytes_per_sample != 3U) bytes_per_sample = 2U;
    const size_t frame_bytes = (size_t)channels * (size_t)bytes_per_sample;
    const size_t max_frames = max_bytes / frame_bytes;
    if (max_frames == 0U) {
        return 0U;
    }

    /* Nominal frames for this 1 ms USB SOF (handles both 48000 and 44100 Hz exactly). */
    g->rate_accum += g->sample_rate_hz;
    int32_t frames = (int32_t)(g->rate_accum / 1000U);
    g->rate_accum %= 1000U;

    /* Apply exact Bresenham ppm skew. */
    if (g->skew_ppm != 0) {
        g->skew_accum += (int64_t)frames * (int64_t)g->skew_ppm;
        while (g->skew_accum >= 1000000LL) {
            g->skew_accum -= 1000000LL;
            frames += 1;
        }
        while (g->skew_accum <= -1000000LL) {
            g->skew_accum += 1000000LL;
            frames -= 1;
        }
    }
    if (frames < 1) frames = 1;
    if ((size_t)frames > max_frames) frames = (int32_t)max_frames;

    uint8_t *dst = out;
    for (int32_t i = 0; i < frames; ++i) {
        int16_t s = next_sample(g);
        for (uint8_t ch = 0U; ch < channels; ++ch) {
            if (bytes_per_sample == 2U) {
                dst[0] = (uint8_t)(s & 0xFF);
                dst[1] = (uint8_t)((s >> 8) & 0xFF);
                dst += 2;
            } else {
                /* S24LE: place 16-bit sample in top 16 bits, low byte 0. */
                dst[0] = 0U;
                dst[1] = (uint8_t)(s & 0xFF);
                dst[2] = (uint8_t)((s >> 8) & 0xFF);
                dst += 3;
            }
        }
    }
    return (size_t)frames * frame_bytes;
}
