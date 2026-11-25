#include "audio_gen.h"
#include "uart_ctl.h"

#include <assert.h>
#include <math.h>
#include <stdio.h>
#include <string.h>

/* Stub for host test (no TinyUSB linked). */
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

static void test_tone_and_alignment(void)
{
    audio_gen_t g;
    audio_gen_init(&g);
    uint8_t buf[200];
    double sum_sq = 0.0;
    size_t total_frames = 0;
    for (int ms = 0; ms < 100; ++ms) {
        size_t n = audio_gen_fill_ms(&g, buf, sizeof(buf), 2, 2);
        assert(n == 192U);
        assert((n % 4U) == 0U);
        for (size_t i = 0; i < n; i += 4U) {
            int16_t l = (int16_t)(buf[i] | (buf[i + 1] << 8));
            int16_t r = (int16_t)(buf[i + 2] | (buf[i + 3] << 8));
            assert(l == r); /* real Lark duplicates L == R */
            sum_sq += (double)l * (double)l;
            total_frames++;
        }
    }
    double rms = sqrt(sum_sq / (double)total_frames);
    /* -20 dBFS sine RMS = 32767 * 0.1 / sqrt(2) ~= 2317 */
    assert(rms > 2200.0 && rms < 2450.0);
}

static void test_bresenham_skew(void)
{
    audio_gen_t g;
    audio_gen_init(&g);
    audio_gen_set_skew(&g, 1000); /* +1000 ppm -> +48 frames/sec */
    uint8_t buf[200];
    size_t total_frames = 0;
    for (int ms = 0; ms < 1000; ++ms) {
        size_t n = audio_gen_fill_ms(&g, buf, sizeof(buf), 2, 2);
        assert((n % 4U) == 0U && n <= 200U);
        total_frames += n / 4U;
    }
    assert(total_frames == 48048U);
}

static void test_uart_commands(void)
{
    audio_gen_t g;
    audio_gen_init(&g);
    ctl_status_t st = {.connected = true, .streaming = true, .active_alt = 2,
                       .packets_sent = 42, .replugs = 3, .profile = PROFILE_LARK_A1};
    char reply[192];
    ctl_result_t res;

    uart_ctl_handle("PING", &g, &st, reply, sizeof(reply), &res);
    assert(strcmp(reply, "OK PONG\n") == 0);

    uart_ctl_handle("STATUS", &g, &st, reply, sizeof(reply), &res);
    assert(strstr(reply, "\"profile\":\"lark_a1\"") != NULL);
    assert(strstr(reply, "\"pkts\":42") != NULL);

    uart_ctl_handle("REPLUG 750", &g, &st, reply, sizeof(reply), &res);
    assert(res.action == CTL_ACTION_REPLUG && res.replug_ms == 750U);

    uart_ctl_handle("PLAY speech -18", &g, &st, reply, sizeof(reply), &res);
    assert(g.mode == AUDIO_MODE_SPEECH && g.dbfs == -18);

    uart_ctl_handle("SKEW -500", &g, &st, reply, sizeof(reply), &res);
    assert(g.skew_ppm == -500);

    uart_ctl_handle("PROFILE wrong_vid", &g, &st, reply, sizeof(reply), &res);
    assert(res.profile_changed && res.new_profile == PROFILE_WRONG_VID && res.action == CTL_ACTION_REPLUG);
}

int main(void)
{
    test_tone_and_alignment();
    test_bresenham_skew();
    test_uart_commands();
    printf("fake_lark host tests: ALL PASS\n");
    return 0;
}
