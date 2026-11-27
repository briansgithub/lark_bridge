#ifndef OUTPUT_PEER_STORE_H
#define OUTPUT_PEER_STORE_H

#include "esp_err.h"
#include "output_sink_policy.h"

#include <stdbool.h>
#include <stdint.h>

#define OUTPUT_PEER_ADDRESS_SIZE 6U

/* Legacy single-peer key ("peer_bda"). Kept in sync with MRU slot 0 so an
 * older image flashed later still finds the most recent sink. */
esp_err_t output_peer_store_load(uint8_t address[OUTPUT_PEER_ADDRESS_SIZE], bool *found);

/* Versioned MRU list ("peer_list"). When it is absent, a legacy "peer_bda" is
 * migrated into slot 0 and the list is saved; *migrated reports that. */
esp_err_t output_peer_store_load_list(output_sink_list_t *list, bool *migrated);
esp_err_t output_peer_store_save_list(const output_sink_list_t *list);
/* Erases both keys. */
esp_err_t output_peer_store_clear(void);

#endif
