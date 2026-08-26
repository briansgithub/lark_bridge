from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_hfp_nodes_are_not_autoconnected() -> None:
    policy = (
        ROOT
        / "pi"
        / "wireplumber"
        / "wireplumber.conf.d"
        / "65-bridge-hfp-no-autolink.conf"
    ).read_text(encoding="utf-8")

    assert 'api.bluez5.profile = "headset-audio-gateway"' in policy
    assert "node.autoconnect = false" in policy
    assert "a2dp-sink" not in policy
    assert "Lark A1" in policy
    assert "FIFINE K054" in policy
