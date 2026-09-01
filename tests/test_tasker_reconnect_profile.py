from pathlib import Path
from xml.etree import ElementTree

REPO = Path(__file__).resolve().parents[1]
PROFILE_PATH = REPO / "config" / "tasker" / "LarkBridge_Auto_Reconnect.prf.xml"
DEVICE_NAME = "LarkBridge BT500"
DEVICE_ADDRESS = "A0:AD:9F:73:6C:24"


def _by_name(root: ElementTree.Element, tag: str, name: str) -> ElementTree.Element:
    matches = [item for item in root.findall(tag) if item.findtext("nme") == name]
    assert len(matches) == 1
    return matches[0]


def _conditions(action: ElementTree.Element) -> list[tuple[str, str, str]]:
    return [
        (
            condition.findtext("lhs", ""),
            condition.findtext("op", ""),
            condition.findtext("rhs", ""),
        )
        for condition in action.findall("ConditionList/Condition")
    ]


def test_tasker_fallback_is_delayed_guarded_and_exact_device_only() -> None:
    root = ElementTree.parse(PROFILE_PATH).getroot()
    assert len(root.findall("Profile")) == 3
    assert len(root.findall("Task")) == 3

    profile = _by_name(root, "Profile", "Auto-Reconnect To LarkBridge")
    task = _by_name(root, "Task", "Connect To LarkBridge")

    assert profile.findtext("nme") == "Auto-Reconnect To LarkBridge"
    assert profile.findtext("mid0") == task.findtext("id")

    states = profile.findall("State")
    assert len(states) == 1
    assert states[0].findtext("code") == "3"
    assert states[0].findtext("pin") == "true"
    assert [item.text for item in states[0].findall("Str")] == [
        DEVICE_NAME,
        DEVICE_ADDRESS,
    ]

    actions = task.findall("Action")
    assert [action.findtext("code") for action in actions] == ["30", "340"]
    assert actions[0].find("Int[@sr='arg1']").get("val") == "25"
    assert actions[1].findtext("Str[@sr='arg2']") == DEVICE_NAME
    assert actions[1].find("Int[@sr='arg3']").get("val") == "8"
    assert [item.text for item in actions[1].findall("ConditionList/bool0")] == ["And"]
    assert [item.text for item in actions[1].findall("ConditionList/bool1")] == ["And"]
    assert _conditions(actions[1]) == [
        ("%PACTIVE", "2", "*Auto-Reconnect To LarkBridge*"),
        ("%LB_AUTO", "3", "0"),
        ("%BLUE", "2", "on"),
    ]


def test_bluetooth_off_sets_manual_hold_and_stops_pending_fallback() -> None:
    root = ElementTree.parse(PROFILE_PATH).getroot()
    profile = _by_name(root, "Profile", "LarkBridge Manual Hold - Bluetooth Off")
    task = _by_name(root, "Task", "LarkBridge Hold - Bluetooth Off")

    assert profile.findtext("mid0") == task.findtext("id")
    state = profile.find("State")
    assert state is not None
    assert state.findtext("code") == "2"
    assert state.find("Int[@sr='arg0']").get("val") == "0"

    actions = task.findall("Action")
    assert [action.findtext("code") for action in actions] == ["547", "137"]
    assert actions[0].findtext("Str[@sr='arg0']") == "%LB_AUTO"
    assert actions[0].findtext("Str[@sr='arg1']") == "0"
    assert actions[1].findtext("Str[@sr='arg1']") == "Connect To LarkBridge"


def test_exact_device_events_rearm_or_hold_according_to_user_action() -> None:
    root = ElementTree.parse(PROFILE_PATH).getroot()
    profile = _by_name(root, "Profile", "LarkBridge Connection Policy")
    task = _by_name(root, "Task", "LarkBridge Update Auto-Reconnect")

    assert profile.findtext("mid0") == task.findtext("id")
    event = profile.find("Event")
    assert event is not None
    assert event.findtext("code") == "2080"
    assert event.findtext("Str[@sr='arg1']") == DEVICE_NAME
    assert event.findtext("Str[@sr='arg2']") == DEVICE_ADDRESS

    actions = task.findall("Action")
    assert [action.findtext("code") for action in actions] == ["547", "547", "137"]
    assert actions[0].findtext("Str[@sr='arg0']") == "%LB_AUTO"
    assert actions[0].findtext("Str[@sr='arg1']") == "1"
    assert _conditions(actions[0]) == [("%bt_connected", "2", "true")]

    manual_disconnect_conditions = [
        ("%bt_connected", "2", "false"),
        ("%WIN", "2", f"*{DEVICE_NAME}*"),
    ]
    assert actions[1].findtext("Str[@sr='arg0']") == "%LB_AUTO"
    assert actions[1].findtext("Str[@sr='arg1']") == "0"
    assert _conditions(actions[1]) == manual_disconnect_conditions
    assert actions[2].findtext("Str[@sr='arg1']") == "Connect To LarkBridge"
    assert _conditions(actions[2]) == manual_disconnect_conditions


def test_no_bt_near_or_retry_loop_is_present() -> None:
    root = ElementTree.parse(PROFILE_PATH).getroot()
    assert root.find(".//State[code='4']") is None

    connect_task = _by_name(root, "Task", "Connect To LarkBridge")
    assert (
        len([a for a in connect_task.findall("Action") if a.findtext("code") == "340"])
        == 1
    )
