#ifndef OUTPUT_CONTROL_H
#define OUTPUT_CONTROL_H

#include "bridge_link.h"
#include "esp_err.h"

#include <stdbool.h>
#include <stdint.h>

esp_err_t output_control_start(void);
bool output_control_is_initialized(void);
bool output_control_link_alive(void);
bool output_control_audio_allowed(void);
bool output_control_send_avrcp(uint8_t key_code, bridge_key_state_t key_state);
bool output_control_report_fault(bridge_fault_code_t fault);
void output_control_set_local_ready(uint8_t ready_flags);
void output_control_invalidate_local_audio(void);
void output_control_note_local_audio_started(void);

#endif
