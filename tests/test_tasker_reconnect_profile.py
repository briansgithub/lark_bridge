from pathlib import Path
from xml.etree import ElementTree

REPO = Path(__file__).resolve().parents[1]
PROFILE_PATH = REPO / "config" / "tasker" / "LarkBridge_Auto_Reconnect.prf.xml"


def test_tasker_fallback_is_delayed_guarded_and_exact_device_only() -> None:
    root = ElementTree.parse(PROFILE_PATH).getroot()
    profile = root.find("Profile")
    task = root.find("Task")
    assert profile is not None
    assert task is not None

    assert profile.findtext("nme") == "Auto-Reconnect To LarkBridge"
    assert profile.findtext("mid0") == task.findtext("id")

    states = profile.findall("State")
    assert len(states) == 1
    assert states[0].findtext("code") == "3"
    assert states[0].findtext("pin") == "true"
    assert [item.text for item in states[0].findall("Str")] == [
        "LarkBridge BT500",
        "A0:AD:9F:73:6C:24",
    ]

    actions = task.findall("Action")
    assert [action.findtext("code") for action in actions] == ["30", "340"]
    assert actions[0].find("Int[@sr='arg1']").get("val") == "25"
    assert actions[1].findtext("Str[@sr='arg2']") == "LarkBridge BT500"
    assert actions[1].find("Int[@sr='arg3']").get("val") == "8"

    condition = actions[1].find("ConditionList/Condition")
    assert condition is not None
    assert condition.findtext("lhs") == "%PACTIVE"
    assert condition.findtext("op") == "2"
    assert condition.findtext("rhs") == "*Auto-Reconnect To LarkBridge*"
