#include "output_sbc.h"

#include <stdio.h>

#define CHECK(condition) do { \
    if (!(condition)) { \
        fprintf(stderr, "line %d: %s\n", __LINE__, #condition); \
        return 1; \
    } \
} while (0)

int main(void)
{
    output_sbc_t caps = {
        .samp_freq = OUTPUT_SBC_SF_44K | OUTPUT_SBC_SF_48K,
        .ch_mode = OUTPUT_SBC_CH_MODE_JOINT_STEREO | OUTPUT_SBC_CH_MODE_STEREO |
                   OUTPUT_SBC_CH_MODE_MONO,
        .block_len = OUTPUT_SBC_BLOCK_LEN_16,
        .num_subbands = OUTPUT_SBC_NUM_SUBBANDS_8,
        .alloc_mthd = OUTPUT_SBC_ALLOC_MTHD_LOUDNESS,
        .min_bitpool = 0U,
        .max_bitpool = 250U,
    };
    output_sbc_t selected;
    CHECK(output_sbc_select(&caps, false, &selected));
    CHECK(selected.samp_freq == OUTPUT_SBC_SF_44K);
    CHECK(selected.ch_mode == OUTPUT_SBC_CH_MODE_JOINT_STEREO);
    CHECK(selected.min_bitpool == 2U && selected.max_bitpool == 53U);
    CHECK(output_sbc_select(&caps, true, &selected));
    CHECK(selected.samp_freq == OUTPUT_SBC_SF_48K);
    CHECK(selected.ch_mode == OUTPUT_SBC_CH_MODE_STEREO);
    CHECK(selected.max_bitpool == 35U);

    caps.samp_freq = OUTPUT_SBC_SF_48K;
    caps.ch_mode = OUTPUT_SBC_CH_MODE_MONO;
    caps.max_bitpool = 31U;
    CHECK(output_sbc_select(&caps, false, &selected));
    CHECK(selected.ch_mode == OUTPUT_SBC_CH_MODE_MONO);
    CHECK(selected.samp_freq == OUTPUT_SBC_SF_48K && selected.max_bitpool == 31U);

    caps.samp_freq = OUTPUT_SBC_SF_44K;
    caps.min_bitpool = 35U;
    caps.max_bitpool = 53U;
    CHECK(output_sbc_select(&caps, true, &selected));
    CHECK(selected.samp_freq == OUTPUT_SBC_SF_44K);
    CHECK(selected.min_bitpool == 35U && selected.max_bitpool == 35U);
    caps.min_bitpool = 36U;
    CHECK(output_sbc_select(&caps, true, &selected) && selected.max_bitpool == 53U);
    caps.max_bitpool = 35U;
    CHECK(!output_sbc_select(&caps, false, &selected));
    caps.min_bitpool = 2U;
    caps.samp_freq = 0x4U;
    CHECK(!output_sbc_select(&caps, false, &selected));
    return 0;
}
