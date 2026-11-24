#pragma once

#include <stdbool.h>
#include <stdint.h>

/* A2DP SBC codec-information field values. Each mask is local to its field. */
enum {
    OUTPUT_SBC_SF_44K = 0x2,
    OUTPUT_SBC_SF_48K = 0x1,
    OUTPUT_SBC_CH_MODE_MONO = 0x8,
    OUTPUT_SBC_CH_MODE_STEREO = 0x2,
    OUTPUT_SBC_CH_MODE_JOINT_STEREO = 0x1,
    OUTPUT_SBC_BLOCK_LEN_16 = 0x1,
    OUTPUT_SBC_NUM_SUBBANDS_8 = 0x1,
    OUTPUT_SBC_ALLOC_MTHD_LOUDNESS = 0x1,
};

typedef struct {
    uint8_t samp_freq;
    uint8_t ch_mode;
    uint8_t block_len;
    uint8_t num_subbands;
    uint8_t alloc_mthd;
    uint8_t min_bitpool;
    uint8_t max_bitpool;
} output_sbc_t;

static inline bool output_sbc_select(const output_sbc_t *caps, bool fallback,
                                     output_sbc_t *config)
{
    if ((caps->samp_freq & (OUTPUT_SBC_SF_44K | OUTPUT_SBC_SF_48K)) == 0U ||
        (caps->ch_mode & (OUTPUT_SBC_CH_MODE_JOINT_STEREO |
                          OUTPUT_SBC_CH_MODE_STEREO |
                          OUTPUT_SBC_CH_MODE_MONO)) == 0U ||
        (caps->block_len & OUTPUT_SBC_BLOCK_LEN_16) == 0U ||
        (caps->num_subbands & OUTPUT_SBC_NUM_SUBBANDS_8) == 0U ||
        (caps->alloc_mthd & OUTPUT_SBC_ALLOC_MTHD_LOUDNESS) == 0U) {
        return false;
    }
    output_sbc_t selected;
    bool supports_44k = (caps->samp_freq & OUTPUT_SBC_SF_44K) != 0U;
    bool supports_48k = (caps->samp_freq & OUTPUT_SBC_SF_48K) != 0U;
    selected.samp_freq = supports_44k && !(fallback && supports_48k)
                            ? OUTPUT_SBC_SF_44K : OUTPUT_SBC_SF_48K;
    if ((caps->ch_mode & OUTPUT_SBC_CH_MODE_JOINT_STEREO) != 0U &&
        !(fallback && (caps->ch_mode & OUTPUT_SBC_CH_MODE_STEREO) != 0U)) {
        selected.ch_mode = OUTPUT_SBC_CH_MODE_JOINT_STEREO;
    } else if ((caps->ch_mode & OUTPUT_SBC_CH_MODE_STEREO) != 0U) {
        selected.ch_mode = OUTPUT_SBC_CH_MODE_STEREO;
    } else {
        selected.ch_mode = OUTPUT_SBC_CH_MODE_MONO;
    }
    selected.block_len = OUTPUT_SBC_BLOCK_LEN_16;
    selected.num_subbands = OUTPUT_SBC_NUM_SUBBANDS_8;
    selected.alloc_mthd = OUTPUT_SBC_ALLOC_MTHD_LOUDNESS;
    selected.min_bitpool = caps->min_bitpool < 2U ? 2U : caps->min_bitpool;
    uint8_t limit = fallback && selected.min_bitpool <= 35U ? 35U : 53U;
    selected.max_bitpool = caps->max_bitpool > limit ? limit : caps->max_bitpool;
    if (selected.min_bitpool > selected.max_bitpool) {
        return false;
    }
    *config = selected;
    return true;
}

