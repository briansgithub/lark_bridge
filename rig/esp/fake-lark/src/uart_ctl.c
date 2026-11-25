#include "uart_ctl.h"

#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void str_upper(char *dst, const char *src, size_t cap)
{
    size_t i = 0;
    while (src[i] && i + 1U < cap) {
        dst[i] = (char)toupper((unsigned char)src[i]);
        ++i;
    }
    dst[i] = '\0';
}

static bool parse_i32(const char *s, int32_t *out)
{
    if (s == NULL || *s == '\0') return false;
    char *end = NULL;
    long v = strtol(s, &end, 10);
    if (end == s || *end != '\0') return false;
    *out = (int32_t)v;
    return true;
}

void uart_ctl_handle(const char *line, audio_gen_t *gen, const ctl_status_t *st,
                     char *reply, size_t reply_cap, ctl_result_t *res)
{
    memset(res, 0, sizeof(*res));
    char buf[128];
    strncpy(buf, line, sizeof(buf) - 1U);
    buf[sizeof(buf) - 1U] = '\0';

    char *tok[4] = {NULL, NULL, NULL, NULL};
    int ntok = 0;
    char *p = buf;
    while (ntok < 4) {
        while (*p && isspace((unsigned char)*p)) ++p;
        if (*p == '\0') break;
        tok[ntok++] = p;
        while (*p && !isspace((unsigned char)*p)) ++p;
        if (*p) *p++ = '\0';
    }
    if (ntok == 0) {
        reply[0] = '\0';
        return;
    }

    char cmd[24];
    str_upper(cmd, tok[0], sizeof(cmd));

    if (strcmp(cmd, "PING") == 0) {
        snprintf(reply, reply_cap, "OK PONG\n");
        return;
    }
    if (strcmp(cmd, "STATUS") == 0) {
        snprintf(reply, reply_cap,
                 "{\"ok\":true,\"connected\":%s,\"streaming\":%s,\"alt\":%u,"
                 "\"profile\":\"%s\",\"mode\":\"%s\",\"hz\":%lu,\"dbfs\":%ld,"
                 "\"skew_ppm\":%ld,\"pkts\":%lu,\"replugs\":%lu}\n",
                 st->connected ? "true" : "false",
                 st->streaming ? "true" : "false",
                 (unsigned)st->active_alt,
                 usb_profile_name(st->profile),
                 audio_mode_name(gen->mode),
                 (unsigned long)gen->tone_hz,
                 (long)gen->dbfs,
                 (long)gen->skew_ppm,
                 (unsigned long)st->packets_sent,
                 (unsigned long)st->replugs);
        return;
    }
    if (strcmp(cmd, "UNPLUG") == 0) {
        res->action = CTL_ACTION_UNPLUG;
        snprintf(reply, reply_cap, "OK UNPLUG\n");
        return;
    }
    if (strcmp(cmd, "CONNECT") == 0) {
        res->action = CTL_ACTION_CONNECT;
        snprintf(reply, reply_cap, "OK CONNECT\n");
        return;
    }
    if (strcmp(cmd, "REPLUG") == 0) {
        int32_t ms = 500;
        if (ntok >= 2 && !parse_i32(tok[1], &ms)) {
            snprintf(reply, reply_cap, "ERR bad_replug_ms\n");
            return;
        }
        if (ms < 50) ms = 50;
        if (ms > 10000) ms = 10000;
        res->action = CTL_ACTION_REPLUG;
        res->replug_ms = (uint32_t)ms;
        snprintf(reply, reply_cap, "OK REPLUG %ld\n", (long)ms);
        return;
    }
    if (strcmp(cmd, "PLAY") == 0 && ntok >= 2) {
        char sub[24];
        str_upper(sub, tok[1], sizeof(sub));
        if (strcmp(sub, "SILENCE") == 0) {
            audio_gen_set_mode(gen, AUDIO_MODE_SILENCE, gen->tone_hz, gen->dbfs);
            snprintf(reply, reply_cap, "OK PLAY silence\n");
            return;
        }
        if (strcmp(sub, "TONE_1K") == 0) {
            audio_gen_set_mode(gen, AUDIO_MODE_TONE, 1000U, -20);
            snprintf(reply, reply_cap, "OK PLAY tone_1k\n");
            return;
        }
        if (strcmp(sub, "TONE") == 0 && ntok >= 4) {
            int32_t hz = 1000, dbfs = -20;
            if (!parse_i32(tok[2], &hz) || !parse_i32(tok[3], &dbfs) || hz < 100 || hz > 20000) {
                snprintf(reply, reply_cap, "ERR bad_tone_args\n");
                return;
            }
            audio_gen_set_mode(gen, AUDIO_MODE_TONE, (uint32_t)hz, dbfs);
            snprintf(reply, reply_cap, "OK PLAY tone %lu %ld\n",
                     (unsigned long)gen->tone_hz, (long)gen->dbfs);
            return;
        }
        if (strcmp(sub, "NOISE") == 0) {
            int32_t dbfs = -20;
            if (ntok >= 3 && !parse_i32(tok[2], &dbfs)) {
                snprintf(reply, reply_cap, "ERR bad_dbfs\n");
                return;
            }
            audio_gen_set_mode(gen, AUDIO_MODE_NOISE, gen->tone_hz, dbfs);
            snprintf(reply, reply_cap, "OK PLAY noise %ld\n", (long)gen->dbfs);
            return;
        }
        if (strcmp(sub, "SPEECH") == 0) {
            int32_t dbfs = -20;
            if (ntok >= 3 && !parse_i32(tok[2], &dbfs)) {
                snprintf(reply, reply_cap, "ERR bad_dbfs\n");
                return;
            }
            audio_gen_set_mode(gen, AUDIO_MODE_SPEECH, gen->tone_hz, dbfs);
            snprintf(reply, reply_cap, "OK PLAY speech %ld\n", (long)gen->dbfs);
            return;
        }
        snprintf(reply, reply_cap, "ERR bad_play_mode\n");
        return;
    }
    if (strcmp(cmd, "SKEW") == 0 && ntok >= 2) {
        int32_t ppm = 0;
        if (!parse_i32(tok[1], &ppm)) {
            snprintf(reply, reply_cap, "ERR bad_skew\n");
            return;
        }
        audio_gen_set_skew(gen, ppm);
        snprintf(reply, reply_cap, "OK SKEW %ld\n", (long)gen->skew_ppm);
        return;
    }
    if (strcmp(cmd, "PROFILE") == 0 && ntok >= 2) {
        usb_profile_t prof;
        if (!usb_profile_parse(tok[1], &prof)) {
            snprintf(reply, reply_cap, "ERR bad_profile\n");
            return;
        }
        res->profile_changed = true;
        res->new_profile = prof;
        res->action = CTL_ACTION_REPLUG;
        res->replug_ms = 300U;
        snprintf(reply, reply_cap, "OK PROFILE %s\n", usb_profile_name(prof));
        return;
    }

    snprintf(reply, reply_cap, "ERR unknown_cmd\n");
}
