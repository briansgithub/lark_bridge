#include "output_sink_policy.h"

#include <stdio.h>
#include <string.h>

#define CHECK(cond)                                                                                \
    do {                                                                                           \
        if (!(cond)) {                                                                             \
            fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);                        \
            return 1;                                                                              \
        }                                                                                          \
    } while (0)

static const uint8_t CRV[6] = {0x0c, 0xd9, 0xc1, 0xca, 0x19, 0xda};
static const uint8_t SPK[6] = {0xc9, 0x5c, 0xfd, 0x6e, 0x28, 0x46};
static const uint8_t RIG[6] = {0xb8, 0x27, 0xeb, 0x43, 0x8d, 0x51};
static const uint8_t BUDS[6] = {0x11, 0x22, 0x33, 0x44, 0x55, 0x66};
static const uint8_t FIFTH[6] = {0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0xff};

static int test_mru(void)
{
    output_sink_list_t list;
    output_sink_list_init(&list);
    CHECK(list.count == 0U && output_sink_list_find(&list, CRV) == -1);

    CHECK(output_sink_list_promote(&list, CRV, "HandsFreeLink"));
    CHECK(!output_sink_list_promote(&list, CRV, "HandsFreeLink")); /* no change, no write */
    CHECK(!output_sink_list_promote(&list, CRV, NULL));
    CHECK(output_sink_list_promote(&list, SPK, "MP43247"));
    CHECK(list.count == 2U && output_sink_list_find(&list, SPK) == 0 &&
          output_sink_list_find(&list, CRV) == 1);

    /* Promote existing: dedupe, keeps cached name when none given. */
    CHECK(output_sink_list_promote(&list, CRV, NULL));
    CHECK(list.count == 2U && output_sink_list_find(&list, CRV) == 0);
    CHECK(strcmp(list.sinks[0].name, "HandsFreeLink") == 0);
    CHECK(strcmp(list.sinks[1].name, "MP43247") == 0);

    /* Fill and evict least recently used. */
    CHECK(output_sink_list_promote(&list, RIG, "larkbridge-rig"));
    CHECK(output_sink_list_promote(&list, BUDS, "Buds"));
    CHECK(list.count == 4U);
    /* order: BUDS RIG CRV SPK */
    CHECK(output_sink_list_find(&list, SPK) == 3);
    CHECK(output_sink_list_promote(&list, FIFTH, ""));
    CHECK(list.count == 4U && output_sink_list_find(&list, SPK) == -1);
    CHECK(output_sink_list_find(&list, FIFTH) == 0 && output_sink_list_find(&list, CRV) == 3);
    /* Promoting the last slot does not evict anything. */
    CHECK(output_sink_list_promote(&list, CRV, NULL));
    CHECK(list.count == 4U && output_sink_list_find(&list, CRV) == 0 &&
          output_sink_list_find(&list, FIFTH) == 1 && output_sink_list_find(&list, RIG) == 3);

    /* Remove. */
    CHECK(output_sink_list_remove(&list, FIFTH));
    CHECK(list.count == 3U && output_sink_list_find(&list, BUDS) == 1);
    CHECK(!output_sink_list_remove(&list, FIFTH));
    CHECK(output_sink_list_remove_index(&list, 2U));
    CHECK(!output_sink_list_remove_index(&list, 2U));
    CHECK(list.count == 2U);

    /* Long names are truncated and terminated. */
    char long_name[64];
    memset(long_name, 'x', sizeof(long_name) - 1U);
    long_name[sizeof(long_name) - 1U] = '\0';
    CHECK(output_sink_list_promote(&list, SPK, long_name));
    CHECK(strlen(list.sinks[0].name) == OUTPUT_SINK_NAME_MAX - 1U);

    /* The zero address is never stored. */
    static const uint8_t zero[6] = {0};
    CHECK(!output_sink_list_promote(&list, zero, "x"));
    return 0;
}

static int test_blob_and_migration(void)
{
    output_sink_list_t list;
    output_sink_list_t back;
    uint8_t blob[OUTPUT_SINK_BLOB_SIZE];

    /* Legacy peer_bda (Brian's Honda CR-V) becomes slot 0. */
    CHECK(output_sink_list_migrate_legacy(&list, CRV));
    CHECK(list.count == 1U && output_sink_list_find(&list, CRV) == 0);
    static const uint8_t zero[6] = {0};
    CHECK(!output_sink_list_migrate_legacy(&back, zero));
    CHECK(!output_sink_list_migrate_legacy(&back, NULL));

    CHECK(output_sink_list_promote(&list, SPK, "MP43247"));
    output_sink_list_encode(&list, blob);
    CHECK(blob[0] == OUTPUT_SINK_BLOB_VERSION && blob[1] == 2U);
    CHECK(output_sink_list_decode(blob, sizeof(blob), &back));
    CHECK(back.count == 2U && output_sink_list_find(&back, SPK) == 0 &&
          output_sink_list_find(&back, CRV) == 1 && strcmp(back.sinks[0].name, "MP43247") == 0);

    /* Rejections leave the output untouched. */
    output_sink_list_t untouched;
    output_sink_list_init(&untouched);
    CHECK(!output_sink_list_decode(blob, sizeof(blob) - 1U, &untouched));
    uint8_t bad[OUTPUT_SINK_BLOB_SIZE];
    memcpy(bad, blob, sizeof(bad));
    bad[0] = 2U;
    CHECK(!output_sink_list_decode(bad, sizeof(bad), &untouched));
    memcpy(bad, blob, sizeof(bad));
    bad[1] = OUTPUT_SINK_MAX + 1U;
    CHECK(!output_sink_list_decode(bad, sizeof(bad), &untouched));
    memcpy(bad, blob, sizeof(bad));
    memcpy(bad + 2U + (6U + OUTPUT_SINK_NAME_MAX), bad + 2U, 6U); /* duplicate */
    CHECK(!output_sink_list_decode(bad, sizeof(bad), &untouched));
    memcpy(bad, blob, sizeof(bad));
    memset(bad + 2U, 0, 6U);
    CHECK(!output_sink_list_decode(bad, sizeof(bad), &untouched));
    CHECK(untouched.count == 0U);
    /* Unterminated name is forced terminated. */
    memcpy(bad, blob, sizeof(bad));
    memset(bad + 2U + 6U, 'y', OUTPUT_SINK_NAME_MAX);
    CHECK(output_sink_list_decode(bad, sizeof(bad), &back));
    CHECK(strlen(back.sinks[0].name) == OUTPUT_SINK_NAME_MAX - 1U);
    /* Empty list round trip. */
    output_sink_list_init(&list);
    output_sink_list_encode(&list, blob);
    CHECK(output_sink_list_decode(blob, sizeof(blob), &back) && back.count == 0U);
    return 0;
}

static int test_cod(void)
{
    CHECK(output_sink_cod_is_audio(0x340408U)); /* Honda HandsFreeLink */
    CHECK(output_sink_cod_is_audio(0x248404U)); /* earbuds */
    CHECK(output_sink_cod_is_audio(0x240404U)); /* headset */
    CHECK(output_sink_cod_is_audio(0x240414U)); /* loudspeaker */
    CHECK(output_sink_cod_is_audio(0x00240404U)); /* MP43247 as BlueZ reports it */
    CHECK(output_sink_cod_is_audio(0x000420U)); /* car audio minor, no service bits */
    CHECK(!output_sink_cod_is_audio(0x5a020cU)); /* smartphone */
    CHECK(!output_sink_cod_is_audio(0x00440420U & ~0x1f00U)); /* major misc */
    CHECK(!output_sink_cod_is_audio(0x10010cU)); /* computer */
    CHECK(!output_sink_cod_is_audio(0x000434U)); /* AV camcorder, no audio service */
    CHECK(output_sink_cod_is_audio(0x200434U));  /* AV with audio service bit */
    CHECK(!output_sink_cod_is_audio(0U));
    CHECK(output_sink_name_is_self("LarkBridge"));
    CHECK(output_sink_name_is_self("LarkBridge Output"));
    CHECK(!output_sink_name_is_self("HandsFreeLink"));
    CHECK(!output_sink_name_is_self(NULL));
    CHECK(!output_sink_name_is_self("Lark"));
    return 0;
}

static int test_candidates(void)
{
    output_sink_candidates_t c;
    output_sink_candidates_init(&c);
    CHECK(output_sink_candidates_next(&c) == -1);
    CHECK(output_sink_candidates_offer(&c, SPK, -70, 0x240404U));
    CHECK(output_sink_candidates_offer(&c, CRV, -50, 0x340408U));
    CHECK(output_sink_candidates_offer(&c, RIG, -60, 0x240404U));
    CHECK(c.count == 3U && memcmp(c.items[0].addr, CRV, 6) == 0 &&
          memcmp(c.items[1].addr, RIG, 6) == 0 && memcmp(c.items[2].addr, SPK, 6) == 0);
    /* Weaker repeat is ignored; stronger repeat re-sorts, no duplicate. */
    CHECK(!output_sink_candidates_offer(&c, SPK, -80, 0x240404U));
    CHECK(output_sink_candidates_offer(&c, SPK, -40, 0x240404U));
    CHECK(c.count == 3U && memcmp(c.items[0].addr, SPK, 6) == 0);

    /* Fallback order: strongest first, each once. */
    int i0 = output_sink_candidates_next(&c);
    int i1 = output_sink_candidates_next(&c);
    int i2 = output_sink_candidates_next(&c);
    CHECK(i0 == 0 && i1 == 1 && i2 == 2 && output_sink_candidates_next(&c) == -1);

    /* Full list: weaker newcomer dropped, stronger replaces the weakest. */
    output_sink_candidates_init(&c);
    for (unsigned int k = 0U; k < OUTPUT_SINK_CANDIDATES_MAX; ++k) {
        uint8_t a[6] = {1, 2, 3, 4, 5, (uint8_t)k};
        CHECK(output_sink_candidates_offer(&c, a, (int8_t)(-60 - (int)k), 0x240404U));
    }
    uint8_t weak[6] = {9, 9, 9, 9, 9, 1};
    uint8_t strong[6] = {9, 9, 9, 9, 9, 2};
    CHECK(!output_sink_candidates_offer(&c, weak, -99, 0x240404U));
    CHECK(output_sink_candidates_offer(&c, strong, -30, 0x240404U));
    CHECK(c.count == OUTPUT_SINK_CANDIDATES_MAX && memcmp(c.items[0].addr, strong, 6) == 0);
    for (unsigned int k = 1U; k < c.count; ++k) {
        CHECK(c.items[k - 1U].rssi >= c.items[k].rssi);
    }
    return 0;
}

static int test_round_robin(void)
{
    CHECK(output_sink_next_index(0U, 0U) == 0U);
    CHECK(output_sink_next_index(0U, 1U) == 0U);
    CHECK(output_sink_next_index(0U, 3U) == 1U);
    CHECK(output_sink_next_index(1U, 3U) == 2U);
    CHECK(output_sink_next_index(2U, 3U) == 0U);
    CHECK(output_sink_next_index(3U, 4U) == 0U);
    return 0;
}

static output_button_action_t run_hold(output_button_t *b, uint32_t start, uint32_t held_ms,
                                       output_button_action_t *last_hint)
{
    output_button_action_t action = OUTPUT_BUTTON_NONE;
    *last_hint = OUTPUT_BUTTON_NONE;
    for (uint32_t t = 0U; t <= held_ms; t += 100U) {
        action = output_button_update(b, true, start + t);
        if (action != OUTPUT_BUTTON_NONE) {
            *last_hint = action;
        }
    }
    return output_button_update(b, false, start + held_ms + 100U);
}

static int test_button(void)
{
    output_button_t b;
    output_button_action_t hint;
    output_button_init(&b, false);
    CHECK(output_button_update(&b, false, 0U) == OUTPUT_BUTTON_NONE);
    CHECK(run_hold(&b, 1000U, 500U, &hint) == OUTPUT_BUTTON_NONE && hint == OUTPUT_BUTTON_NONE);
    CHECK(run_hold(&b, 5000U, 3000U, &hint) == OUTPUT_BUTTON_PAIR && hint == OUTPUT_BUTTON_HINT_PAIR);
    CHECK(run_hold(&b, 20000U, 9800U, &hint) == OUTPUT_BUTTON_PAIR);
    CHECK(run_hold(&b, 40000U, 10000U, &hint) == OUTPUT_BUTTON_WIPE && hint == OUTPUT_BUTTON_HINT_WIPE);
    CHECK(run_hold(&b, 60000U, 19800U, &hint) == OUTPUT_BUTTON_WIPE);
    /* Stuck low (e.g. DTR auto-reset line): never fires, recovers after release. */
    CHECK(run_hold(&b, 100000U, 60000U, &hint) == OUTPUT_BUTTON_NONE);
    CHECK(run_hold(&b, 200000U, 3500U, &hint) == OUTPUT_BUTTON_PAIR);
    /* Held at boot: ignored until released. */
    output_button_init(&b, true);
    CHECK(output_button_update(&b, true, 15000U) == OUTPUT_BUTTON_NONE);
    CHECK(output_button_update(&b, false, 15100U) == OUTPUT_BUTTON_NONE);
    CHECK(run_hold(&b, 16000U, 4000U, &hint) == OUTPUT_BUTTON_PAIR);
    /* Tick wrap. */
    output_button_init(&b, false);
    CHECK(run_hold(&b, UINT32_MAX - 1000U, 3200U, &hint) == OUTPUT_BUTTON_PAIR);
    return 0;
}

int main(void)
{
    if (test_mru() || test_blob_and_migration() || test_cod() || test_candidates() ||
        test_round_robin() || test_button()) {
        return 1;
    }
    puts("output sink policy tests passed");
    return 0;
}
