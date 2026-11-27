#!/usr/bin/env python3
"""Configure-time patch of ESP-IDF's Bluedroid btc_avrc.c for AVRCP 1.3 TG metadata.

Copies <IDF>/components/bt/host/bluedroid/btc/profile/std/avrc/btc_avrc.c to the
build directory, asserts the exact anchors of the pinned ESP-IDF (v6.0.2), and
applies three minimal hooks. The IDF tree inside the Docker image is never
modified; output/CMakeLists.txt swaps the patched copy into __idf_bt.

  1. cs_rn_allowed_evt 0x2000 -> 0x2026: allow PLAY_STATUS_CHANGE (1),
     TRACK_CHANGE (2) and PLAY_POS_CHANGED (5) next to VOLUME_CHANGE (13).
  2. btc_rc_upstreams_evt(): answer INFORM_DISPLAY_CHARSET (ACCEPT),
     GET_PLAY_STATUS and GET_ELEMENT_ATTR (IMPLEMENTED/STABLE) from callbacks
     the application registers with larkbridge_avrc_tg_set_meta_hooks()
     instead of rejecting them.
  3. btc_avrc_tg_send_rn_rsp(): fill PLAY_STATUS_CHANGE, TRACK_CHANGE and
     PLAY_POS_CHANGED parameters.

Verified against the unpatched send_metamsg_rsp(): ESP_AVRC_RN_RSP_INTERIM is
passed as AVRC_CMD_NOTIF and mapped to ctype AVRC_RSP_INTERIM on the label the
CT registered with; ESP_AVRC_RN_RSP_CHANGED goes out as AVRC_RSP_CHANGED only
while rc_ntf[event_id - 1].registered is set, and that flag is cleared on
CHANGED, so the CT must re-register (AVRCP 1.3 one-shot semantics). No change
is needed there.

usage: patch_btc_avrc.py <original btc_avrc.c> <output patched_btc_avrc.c>
Exit 0 on success; any anchor mismatch exits 1 and names the anchor.
"""
from __future__ import annotations

import sys
from pathlib import Path

HOOK_DECLS = r'''
/* ---- LarkBridge AVRCP 1.3 TG metadata hooks (tools/patch_btc_avrc.py) ---- */
typedef struct {
    /* GetPlayStatus: length/position in ms (0xFFFFFFFF = unknown), AVRC play status. */
    void (*get_play_status)(UINT32 *song_len, UINT32 *song_pos, UINT8 *play_status);
    /* GetElementAttributes: copy attribute attr_id (1..7) into buf, return length. */
    UINT16 (*get_attr)(UINT8 attr_id, UINT8 *buf, UINT16 capacity);
} larkbridge_avrc_tg_meta_hooks_t;

static const larkbridge_avrc_tg_meta_hooks_t *s_lb_meta_hooks;

void larkbridge_avrc_tg_set_meta_hooks(const larkbridge_avrc_tg_meta_hooks_t *hooks)
{
    s_lb_meta_hooks = hooks;
}

#define LB_ATTR_MAX 7
#define LB_ATTR_TEXT_MAX 96
/* Keep one response well inside a single AVCTP fragment chain. */
#define LB_ATTR_TOTAL_MAX 420

static void lb_send_play_status(UINT8 label, UINT8 ctype)
{
    tAVRC_RESPONSE avrc_rsp;
    memset(&avrc_rsp, 0, sizeof(avrc_rsp));
    avrc_rsp.get_play_status.pdu = AVRC_PDU_GET_PLAY_STATUS;
    avrc_rsp.get_play_status.opcode = opcode_from_pdu(AVRC_PDU_GET_PLAY_STATUS);
    avrc_rsp.get_play_status.status = AVRC_STS_NO_ERROR;
    avrc_rsp.get_play_status.song_len = 0xFFFFFFFF;
    avrc_rsp.get_play_status.song_pos = 0xFFFFFFFF;
    avrc_rsp.get_play_status.play_status = AVRC_PLAYSTATE_STOPPED;
    if (s_lb_meta_hooks != NULL && s_lb_meta_hooks->get_play_status != NULL) {
        s_lb_meta_hooks->get_play_status(&avrc_rsp.get_play_status.song_len,
                                         &avrc_rsp.get_play_status.song_pos,
                                         &avrc_rsp.get_play_status.play_status);
    }
    send_metamsg_rsp(btc_rc_cb.rc_handle, label, ctype, &avrc_rsp);
}

static void lb_send_element_attrs(const tAVRC_GET_ELEM_ATTRS_CMD *cmd, UINT8 label, UINT8 ctype)
{
    static UINT8 s_text[LB_ATTR_MAX][LB_ATTR_TEXT_MAX];
    tAVRC_ATTR_ENTRY entries[LB_ATTR_MAX];
    UINT8 ids[LB_ATTR_MAX];
    UINT8 count = 0;
    UINT16 total = 0;
    tAVRC_RESPONSE avrc_rsp;

    if (cmd->num_attr == 0) {
        for (UINT8 id = 1; id <= LB_ATTR_MAX; ++id) {
            ids[count++] = id;
        }
    } else {
        for (UINT8 i = 0; i < cmd->num_attr && i < AVRC_MAX_ELEM_ATTR_SIZE && count < LB_ATTR_MAX; ++i) {
            UINT32 id = cmd->attrs[i];
            BOOLEAN seen = FALSE;
            if (id < 1 || id > LB_ATTR_MAX) {
                continue;
            }
            for (UINT8 j = 0; j < count; ++j) {
                seen = seen || ids[j] == (UINT8)id;
            }
            if (!seen) {
                ids[count++] = (UINT8)id;
            }
        }
    }
    memset(entries, 0, sizeof(entries));
    for (UINT8 i = 0; i < count; ++i) {
        UINT16 length = 0;
        UINT16 room = (UINT16)(LB_ATTR_TOTAL_MAX - total);
        if (room > LB_ATTR_TEXT_MAX) {
            room = LB_ATTR_TEXT_MAX;
        }
        if (s_lb_meta_hooks != NULL && s_lb_meta_hooks->get_attr != NULL && room > 0) {
            length = s_lb_meta_hooks->get_attr(ids[i], s_text[i], room);
        }
        total = (UINT16)(total + length);
        entries[i].attr_id = ids[i];
        entries[i].name.charset_id = AVRC_CHARSET_ID_UTF8;
        entries[i].name.str_len = length;
        entries[i].name.p_str = s_text[i];
    }
    memset(&avrc_rsp, 0, sizeof(avrc_rsp));
    avrc_rsp.get_elem_attrs.pdu = AVRC_PDU_GET_ELEMENT_ATTR;
    avrc_rsp.get_elem_attrs.opcode = opcode_from_pdu(AVRC_PDU_GET_ELEMENT_ATTR);
    avrc_rsp.get_elem_attrs.status = AVRC_STS_NO_ERROR;
    avrc_rsp.get_elem_attrs.num_attr = count;
    avrc_rsp.get_elem_attrs.p_attrs = entries;
    send_metamsg_rsp(btc_rc_cb.rc_handle, label, ctype, &avrc_rsp);
}

static void lb_send_inform_charset(UINT8 label, UINT8 ctype)
{
    tAVRC_RESPONSE avrc_rsp;
    memset(&avrc_rsp, 0, sizeof(avrc_rsp));
    avrc_rsp.inform_charset.pdu = AVRC_PDU_INFORM_DISPLAY_CHARSET;
    avrc_rsp.inform_charset.opcode = opcode_from_pdu(AVRC_PDU_INFORM_DISPLAY_CHARSET);
    avrc_rsp.inform_charset.status = AVRC_STS_NO_ERROR;
    /* A CONTROL command: send_metamsg_rsp maps it to AVRC_RSP_ACCEPT. */
    send_metamsg_rsp(btc_rc_cb.rc_handle, label, ctype, &avrc_rsp);
}
/* ---- end LarkBridge hooks ---- */
'''

PATCHES = [
    (
        "rn_allowed_evt",
        "const static uint16_t cs_rn_allowed_evt = \\\n        0x2000;",
        "const static uint16_t cs_rn_allowed_evt = \\\n        0x2026; /* LarkBridge: +play_status, track_chg, pos_chg */",
    ),
    (
        "upstreams_reject_group",
        "    case AVRC_PDU_GET_PLAY_STATUS:\n"
        "    case AVRC_PDU_GET_ELEMENT_ATTR:\n"
        "    case AVRC_PDU_INFORM_DISPLAY_CHARSET:\n"
        "    //todo: check the valid response for these PDUs\n",
        "    case AVRC_PDU_GET_PLAY_STATUS:\n"
        "        lb_send_play_status(label, ctype);\n"
        "        break;\n"
        "    case AVRC_PDU_GET_ELEMENT_ATTR:\n"
        "        lb_send_element_attrs(&pavrc_cmd->get_elem_attrs, label, ctype);\n"
        "        break;\n"
        "    case AVRC_PDU_INFORM_DISPLAY_CHARSET:\n"
        "        lb_send_inform_charset(label, ctype);\n"
        "        break;\n"
        "    //todo: check the valid response for these PDUs\n",
    ),
    (
        "rn_rsp_cases",
        "    case ESP_AVRC_RN_VOLUME_CHANGE:\n"
        "        avrc_rsp.reg_notif.param.volume = param->volume;\n"
        "        break;\n"
        "    // todo: implement other event notifications\n",
        "    case ESP_AVRC_RN_VOLUME_CHANGE:\n"
        "        avrc_rsp.reg_notif.param.volume = param->volume;\n"
        "        break;\n"
        "    case ESP_AVRC_RN_PLAY_STATUS_CHANGE:\n"
        "        avrc_rsp.reg_notif.param.play_status = param->playback;\n"
        "        break;\n"
        "    case ESP_AVRC_RN_TRACK_CHANGE:\n"
        "        memcpy(avrc_rsp.reg_notif.param.track, param->elm_id, AVRC_UID_SIZE);\n"
        "        break;\n"
        "    case ESP_AVRC_RN_PLAY_POS_CHANGED:\n"
        "        avrc_rsp.reg_notif.param.play_pos = param->play_pos;\n"
        "        break;\n"
        "    // todo: implement other event notifications\n",
    ),
]

# Hooks need opcode_from_pdu, send_metamsg_rsp and btc_rc_cb, which are all
# declared or defined before btc_rc_upstreams_evt(): insert just before it.
HOOK_ANCHOR = "static void btc_rc_upstreams_evt(UINT16 event, tAVRC_COMMAND *pavrc_cmd, UINT8 ctype, UINT8 label)\n{"

VERIFY = [
    # send_metamsg_rsp semantics relied on above (see module docstring).
    "            if (code == AVRC_CMD_NOTIF) {\n                ctype = AVRC_RSP_INTERIM;",
    "            btc_rc_cb.rc_ntf[event_id - 1].registered = FALSE;",
    "((rsp == ESP_AVRC_RN_RSP_INTERIM) ? AVRC_CMD_NOTIF : AVRC_RSP_CHANGED)",
]


def patch(text: str) -> str:
    for snippet in VERIFY:
        if text.count(snippet) != 1:
            raise SystemExit(f"patch_btc_avrc: verify anchor not found exactly once: {snippet[:60]!r}")
    for name, old, new in PATCHES:
        count = text.count(old)
        if count != 1:
            raise SystemExit(f"patch_btc_avrc: anchor '{name}' found {count} times (expected 1)")
        text = text.replace(old, new)
    index = text.find(HOOK_ANCHOR)
    if index < 0 or text.find(HOOK_ANCHOR, index + 1) >= 0:
        raise SystemExit("patch_btc_avrc: anchor 'btc_rc_upstreams_evt definition' not unique")
    return text[:index] + HOOK_DECLS + "\n" + text[index:]


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    source, target = Path(argv[1]), Path(argv[2])
    patched = patch(source.read_text(encoding="utf-8"))
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists() or target.read_text(encoding="utf-8") != patched:
        target.write_text(patched, encoding="utf-8")
    print(f"patch_btc_avrc: {source} -> {target} (3 hooks applied)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
