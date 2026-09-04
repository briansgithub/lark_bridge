from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
POLICY_ROOT = ROOT / "pi" / "wireplumber" / "wireplumber.conf.d"
AUX_NODE = "alsa_output.platform-3f00b840.mailbox.stereo-fallback"


def test_hfp_nodes_are_not_autoconnected() -> None:
    policy = (POLICY_ROOT / "65-bridge-hfp-no-autolink.conf").read_text(encoding="utf-8")

    assert 'api.bluez5.profile = "headset-audio-gateway"' in policy
    assert "node.autoconnect = false" in policy
    assert "a2dp-source" not in policy
    assert "Lark A1" in policy
    assert "FIFINE K053" in policy
    assert "FIFINE K054" in policy


def test_fifine_k053_monitor_output_is_disabled_without_disabling_capture() -> None:
    policy = (
        POLICY_ROOT / "68-bridge-fifine-k053-monitor-output.conf"
    ).read_text(encoding="utf-8")

    assert 'media.class = "Audio/Sink"' in policy
    assert 'alsa.components = "USB0c76:161f"' in policy
    assert "node.disabled = true" in policy
    assert 'media.class = "Audio/Source"' not in policy
    assert "USB0c76:161e" not in policy


def test_phone_media_targets_aux_without_disabling_acquisition() -> None:
    policy = (POLICY_ROOT / "66-bridge-a2dp-source-target.conf").read_text(encoding="utf-8")

    assert 'node.name = "~bluez_input.5C_33_7B_CB_BF_C5.*"' in policy
    assert 'api.bluez5.profile = "a2dp-source"' in policy
    assert 'media.class = "Stream/Output/Audio"' in policy
    assert f'target.object = "{AUX_NODE}"' in policy
    assert "node.autoconnect" not in policy


def test_no_phone_media_rule_disables_transport_acquisition() -> None:
    for path in POLICY_ROOT.glob("*.conf"):
        policy = path.read_text(encoding="utf-8")
        if 'api.bluez5.profile = "a2dp-source"' in policy:
            assert "node.autoconnect = false" not in policy, path.name


def test_phone_media_role_is_advertised_without_removing_existing_roles() -> None:
    policy = (POLICY_ROOT / "50-bridge-bluez.conf").read_text(encoding="utf-8")
    roles = next(line for line in policy.splitlines() if line.strip().startswith("bluez5.roles"))

    for role in ("a2dp_source", "a2dp_sink", "hfp_hf", "hsp_hs"):
        assert role in roles


def test_fixed_aux_sink_has_measured_media_headroom() -> None:
    policy = (POLICY_ROOT / "67-bridge-aux-headroom.conf").read_text(encoding="utf-8")

    assert f'node.name = "{AUX_NODE}"' in policy
    assert "api.alsa.headroom = 960" in policy
    assert "period-size" not in policy
    assert "node.latency" not in policy
