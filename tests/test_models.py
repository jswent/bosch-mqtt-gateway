from dataclasses import FrozenInstanceError

import pytest

from bosch_mqtt_gateway.models import EntityState, PanelInfo


def test_observations_are_deeply_immutable():
    source = {"alarms": [1, 2], "nested": {"value": 3}}
    state = EntityState("area", "1", "Area", "disarmed", source)
    source["alarms"].append(9)
    assert state.attributes["alarms"] == (1, 2)
    with pytest.raises(TypeError):
        state.attributes["nested"]["value"] = 4
    with pytest.raises(FrozenInstanceError):
        state.name = "mutated"
    with pytest.raises(TypeError):
        EntityState("area", "1", "Area", "disarmed", {"bad": object()})


def test_panel_identity_is_immutable():
    info = PanelInfo("123", "B9512G", "v3.14")
    with pytest.raises(FrozenInstanceError):
        info.serial = "456"
