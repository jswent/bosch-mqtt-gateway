import json

import pytest
from jinja2 import Environment

from bosch_mqtt_gateway.discovery import FAULTS, DiscoveryBuilder
from bosch_mqtt_gateway.models import EntityState, PanelInfo


def builder():
    return DiscoveryBuilder(
        gateway_id="test", topic_prefix="bosch/test", discovery_prefix="homeassistant"
    )


def observations():
    return (
        EntityState(
            "area",
            "1",
            "House",
            "armed_away",
            {
                "ready_away": True,
                "ready_home": None,
                "faulting_points": 2,
                "alarms_burglary": "alarm",
                "alarms_fire": "no_issues",
                "alarms_gas": "trouble",
            },
        ),
        EntityState("point", "1", "Front door", True, {"raw_status": 1}),
        EntityState("output", "1", "Siren", False),
        EntityState(
            "door", "1", "Entry", "locked", {"locked": True, "secured": False, "cycling": None}
        ),
        *(EntityState("fault", key, name, False) for key, (name, _, _) in FAULTS.items()),
    )


def test_full_native_mapping():
    items = [
        json.loads(value)
        for value in builder().build(PanelInfo("123", "B9512G", "1"), observations()).values()
    ]
    assert len(items) == 24  # 7 area + 1 point + 1 output + 3 door + 12 fault
    alarms = [item for item in items if "command_topic" in item]
    assert len(alarms) == 1
    assert alarms[0]["command_topic"] == "bosch/test/command/area/x31"
    assert alarms[0]["supported_features"] == []
    assert all(item["availability_mode"] == "all" and item["qos"] == 1 for item in items)
    assert all(len(item["availability"]) == 2 for item in items)
    assert sum(item.get("enabled_by_default") is False for item in items) == 9
    assert all("via_device" in item["device"] for item in items if "enabled_by_default" not in item)


def test_templates_preserve_boolean_unknown_enum_and_attributes():
    env = Environment()
    states = observations()
    payloads = {
        builder().state_topic(state): json.loads(builder().state_payload(state)) for state in states
    }
    rendered = {}
    for raw in builder().build(PanelInfo("123", "B9512G", None), states).values():
        config = json.loads(raw)
        value = payloads[config["state_topic"]]
        rendered[config["name"]] = env.from_string(config["value_template"]).render(
            value_json=value
        )
        assert (
            json.loads(env.from_string(config["json_attributes_template"]).render(value_json=value))
            == value["attributes"]
        )
    assert rendered["Area ready to arm away"] == "ON"
    assert rendered["Area ready to arm home"] == "None"
    assert rendered["Faulting points"] == "2"
    assert rendered["Gas alarm issues"] == "trouble"
    assert rendered["Locked"] == "ON"
    assert rendered["Secured"] == "OFF"
    assert rendered["Momentarily unlocked"] == "None"


def test_600_points_and_identity_collisions():
    states = tuple(EntityState("point", str(i), "Same name", bool(i % 2)) for i in range(600))
    configs = builder().build(PanelInfo("abc", "B9512G", None), states)
    assert len(configs) == 601
    assert len({json.loads(raw)["unique_id"] for raw in configs.values()}) == 601
    assert len({builder().state_topic(state) for state in states}) == 600
    adversarial = tuple(
        EntityState("point", key, "Renamed", None) for key in ("a/b", "a_b", "a b", "A_b", "é", "e")
    )
    assert len(builder().build(PanelInfo("abc", "B9512G", None), adversarial)) == 7
    assert set(configs).isdisjoint(builder().build(PanelInfo("ABC", "B9512G", None), states))
    assert configs == builder().build(PanelInfo("abc", "B9512G", None), states)


def test_nested_immutable_attributes_serialize():
    state = EntityState("point", "1", "Point", None, {"data": {"ids": [1, 2]}})
    assert json.loads(builder().state_payload(state)) == {
        "state": None,
        "attributes": {"data": {"ids": [1, 2]}},
    }


def test_duplicate_identity_rejected():
    with pytest.raises(ValueError, match="Duplicate"):
        builder().build(PanelInfo("1", "Panel", None), (observations()[0], observations()[0]))


@pytest.mark.parametrize("gateway_id", ["a/b", "a+", "", "é"])
def test_unsafe_gateway_identifier_rejected(gateway_id):
    with pytest.raises(ValueError):
        DiscoveryBuilder(
            gateway_id=gateway_id, topic_prefix="bosch/test", discovery_prefix="homeassistant"
        )


@pytest.mark.parametrize("state", [None, "unknown"])
def test_unknown_alarm_resets_state(state):
    configs = builder().build(
        PanelInfo("1", "Panel", None), (EntityState("area", "1", "Area", state),)
    )
    alarm = next(
        json.loads(raw) for topic, raw in configs.items() if "/alarm_control_panel/" in topic
    )
    assert (
        Environment().from_string(alarm["value_template"]).render(value_json={"state": state})
        == "None"
    )


def test_root_device_exists_without_faults():
    configs = builder().build(
        PanelInfo("1", "Panel", None), (EntityState("point", "1", "Point", True),)
    )
    items = [json.loads(raw) for raw in configs.values()]
    child = next(item for item in items if "via_device" in item["device"])
    root = next(item for item in items if "via_device" not in item["device"])
    assert child["device"]["via_device"] in root["device"]["identifiers"]
    assert root["state_topic"] == "bosch/test/panel_availability"
