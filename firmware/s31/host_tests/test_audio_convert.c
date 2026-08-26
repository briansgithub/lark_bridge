#include "audio_convert.h"

#include <assert.h>
#include <stdint.h>

static void test_expand_16k(void)
{
    const int16_t input[] = {100, -200};
    int16_t output[12] = {0};
    size_t frames = s31_expand_mono_to_48k_stereo(input, 2, 16000, output, 6);
    assert(frames == 6);
    for (size_t frame = 0; frame < 3; ++frame) {
        assert(output[frame * 2] == 100);
        assert(output[frame * 2 + 1] == 100);
    }
    for (size_t frame = 3; frame < 6; ++frame) {
        assert(output[frame * 2] == -200);
        assert(output[frame * 2 + 1] == -200);
    }
}

static void test_downsample_average(void)
{
    const int16_t input[] = {
        100, 300, 200, 400, 300, 500,
        -100, -300, -200, -400, -300, -500,
    };
    int16_t output[2] = {0};
    size_t samples = s31_downsample_48k_stereo_to_mono(input, 6, 16000, output, 2);
    assert(samples == 2);
    assert(output[0] == 300);
    assert(output[1] == -300);
}

static void test_rejects_unsupported_rates(void)
{
    const int16_t input[] = {1, 1};
    int16_t output[8] = {0};
    assert(s31_expand_mono_to_48k_stereo(input, 1, 44100, output, 4) == 0);
    assert(s31_downsample_48k_stereo_to_mono(input, 1, 44100, output, 4) == 0);
}

static void test_expand_reference_8k_to_16k(void)
{
    const int16_t input[] = {100, -200, 300};
    int16_t output[6] = {0};
    assert(s31_expand_mono_8k_to_16k(input, 3U, output, 6U) == 6U);
    assert(output[0] == 100 && output[1] == 100);
    assert(output[2] == -200 && output[3] == -200);
    assert(output[4] == 300 && output[5] == 300);
    assert(s31_expand_mono_8k_to_16k(input, 3U, output, 5U) == 0U);
}

static void test_streaming_44100_to_48000(void)
{
    int16_t first[147U * 2U];
    int16_t second[147U * 2U];
    int16_t output[162U * 2U];
    s31_stereo_resampler_t state;
    for (size_t frame = 0U; frame < 147U; ++frame) {
        first[frame * 2U] = (int16_t)frame;
        first[frame * 2U + 1U] = (int16_t)(-((int16_t)frame));
        second[frame * 2U] = (int16_t)(frame + 147U);
        second[frame * 2U + 1U] = (int16_t)(-((int16_t)(frame + 147U)));
    }
    s31_stereo_resampler_reset(&state);
    size_t first_frames = s31_resample_44100_to_48000_stereo(
        &state, first, 147U, output, 162U);
    size_t second_frames = s31_resample_44100_to_48000_stereo(
        &state, second, 147U, output, 162U);
    /* The causal interpolator has one input-frame startup latency, then keeps
     * the exact 160:147 rational cadence across callback boundaries. */
    assert(first_frames == 159U);
    assert(second_frames == 160U);
    assert(output[0] >= 145 && output[0] <= 147);
    assert(output[1] <= -145 && output[1] >= -147);

    s31_stereo_resampler_reset(&state);
    assert(s31_resample_44100_to_48000_stereo(&state, first, 147U, output, 10U) == 0U);
    assert(!state.have_previous);
}

int main(void)
{
    test_expand_16k();
    test_downsample_average();
    test_rejects_unsupported_rates();
    test_expand_reference_8k_to_16k();
    test_streaming_44100_to_48000();
    return 0;
}
