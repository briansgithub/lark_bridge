#ifndef OUTPUT_SINK_POLICY_H
#define OUTPUT_SINK_POLICY_H

/* Pure, host-testable sink selection policy for the 32UE (car UX feature 2):
 * a versioned 4-slot most-recently-used list of saved sinks, its NVS blob
 * format and legacy peer_bda migration, the Class-of-Device audio filter,
 * RSSI ranking of pairing candidates, and round-robin reconnect order.
 * No ESP-IDF dependency. */

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define OUTPUT_SINK_ADDR_LEN 6U
#define OUTPUT_SINK_MAX 4U
#define OUTPUT_SINK_NAME_MAX 32U /* bytes incl. terminator */
#define OUTPUT_SINK_BLOB_VERSION 1U
/* [version, count] + MAX * (addr[6] + name[NAME_MAX]) */
#define OUTPUT_SINK_BLOB_SIZE (2U + OUTPUT_SINK_MAX * (OUTPUT_SINK_ADDR_LEN + OUTPUT_SINK_NAME_MAX))
#define OUTPUT_SINK_CANDIDATES_MAX 8U

typedef struct {
    uint8_t addr[OUTPUT_SINK_ADDR_LEN];
    char name[OUTPUT_SINK_NAME_MAX];
} output_sink_t;

typedef struct {
    uint8_t count;
    output_sink_t sinks[OUTPUT_SINK_MAX]; /* slot 0 = most recently used */
} output_sink_list_t;

typedef struct {
    uint8_t addr[OUTPUT_SINK_ADDR_LEN];
    int8_t rssi;
    uint32_t cod;
    bool attempted;
} output_sink_candidate_t;

typedef struct {
    uint8_t count;
    output_sink_candidate_t items[OUTPUT_SINK_CANDIDATES_MAX]; /* strongest RSSI first */
} output_sink_candidates_t;

void output_sink_list_init(output_sink_list_t *list);
/* Index of addr, or -1. */
int output_sink_list_find(const output_sink_list_t *list, const uint8_t addr[OUTPUT_SINK_ADDR_LEN]);
/* Moves (or inserts) addr to slot 0. A full list evicts the least recently used
 * entry. name may be NULL or empty to keep a cached name. Returns true when the
 * list changed (so the caller persists only real changes). */
bool output_sink_list_promote(output_sink_list_t *list, const uint8_t addr[OUTPUT_SINK_ADDR_LEN],
                              const char *name);
bool output_sink_list_remove_index(output_sink_list_t *list, unsigned int index);
bool output_sink_list_remove(output_sink_list_t *list, const uint8_t addr[OUTPUT_SINK_ADDR_LEN]);

/* Blob codec. decode rejects wrong size, unknown version, count > MAX,
 * duplicate or all-zero addresses; names are forced NUL-terminated. */
void output_sink_list_encode(const output_sink_list_t *list, uint8_t blob[OUTPUT_SINK_BLOB_SIZE]);
bool output_sink_list_decode(const uint8_t *blob, size_t length, output_sink_list_t *list);
/* Legacy single-peer (peer_bda) migration: seeds slot 0. Returns true if seeded. */
bool output_sink_list_migrate_legacy(output_sink_list_t *list,
                                     const uint8_t legacy[OUTPUT_SINK_ADDR_LEN]);

/* Class of Device filter for pairing candidates: major class Audio/Video
 * (0x04) and either the Audio or Rendering service bit or an audio minor class
 * (headset, hands-free, loudspeaker, headphones, portable, car audio, HiFi...). */
bool output_sink_cod_is_audio(uint32_t cod);
/* True for names this bridge uses itself ("LarkBridge", "LarkBridge Output"). */
bool output_sink_name_is_self(const char *name);

void output_sink_candidates_init(output_sink_candidates_t *candidates);
/* Adds or refreshes a candidate (keeps the strongest RSSI seen) and keeps the
 * list sorted strongest first. When full, a weaker newcomer is dropped and a
 * stronger one replaces the weakest. Returns true if the list changed. */
bool output_sink_candidates_offer(output_sink_candidates_t *candidates,
                                  const uint8_t addr[OUTPUT_SINK_ADDR_LEN], int8_t rssi,
                                  uint32_t cod);
/* Next not-yet-attempted candidate (marks it attempted), or -1. */
int output_sink_candidates_next(output_sink_candidates_t *candidates);

/* Round-robin reconnect order over count saved sinks, starting at slot 0. */
unsigned int output_sink_next_index(unsigned int cursor, unsigned int count);

/* BOOT button (GPIO0, active low) gestures, sampled every ~100 ms. Actions
 * fire on RELEASE so a line held low by a serial adapter's DTR/RTS auto-reset
 * circuit can never wipe the car bond: a press held past STUCK_MS is ignored
 * until released, and a press already down at boot is ignored. */
#define OUTPUT_BUTTON_PAIR_MS 3000U
#define OUTPUT_BUTTON_WIPE_MS 10000U
#define OUTPUT_BUTTON_STUCK_MS 20000U

typedef enum {
    OUTPUT_BUTTON_NONE = 0,
    OUTPUT_BUTTON_HINT_PAIR, /* crossed 3 s: release now for pairing */
    OUTPUT_BUTTON_HINT_WIPE, /* crossed 10 s: release now to wipe */
    OUTPUT_BUTTON_STUCK,     /* held 20 s: ignored until released */
    OUTPUT_BUTTON_PAIR,
    OUTPUT_BUTTON_WIPE,
} output_button_action_t;

typedef struct {
    bool pressed;
    bool ignore_until_release;
    uint8_t hint;
    uint32_t since_ms;
} output_button_t;

void output_button_init(output_button_t *button, bool pressed_at_boot);
output_button_action_t output_button_update(output_button_t *button, bool pressed,
                                            uint32_t now_ms);

#endif
