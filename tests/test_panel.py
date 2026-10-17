import asyncio
import hashlib
import logging
from unittest.mock import AsyncMock

import pytest
from bosch_alarm_mode2.const import AREA_STATUS, CMD, PANEL_MODELS, POINT_STATUS
from bosch_alarm_mode2.panel import Area, Output, Point

from bosch_mqtt_gateway.panel import (
    ALLOWED_COMMANDS,
    PanelAdapter,
    PanelError,
    ReadOnlyConnection,
    _ReadOnlyPanel,
)


class Transport:
    def __init__(self):
        self.frames = []
        self.aborted = False

    def write(self, data):
        self.frames.append(data)

    def abort(self):
        self.aborted = True

    def get_extra_info(self, name):
        return self

    def getpeercert(self, binary_form=False):
        return b"test-certificate"


def adapter(**kwargs):
    return PanelAdapter(
        "panel.example",
        7700,
        "secret",
        None,
        certificate_sha256="a" * 64,
        on_state=kwargs.pop("on_state", lambda s: None),
        on_availability=kwargs.pop("on_availability", lambda s: None),
        **kwargs,
    )


@pytest.mark.parametrize(
    "command",
    [
        CMD.AREA_ARM,
        CMD.SET_OUTPUT_STATE,
        CMD.SET_DOOR_STATE,
        CMD.SET_DATE_TIME,
        CMD.REQUEST_PERMISSION_FOR_PANEL_ACTION,
        0,
        255,
    ],
)
async def test_controls_cannot_reach_transport(command):
    connection = ReadOnlyConnection(lambda d: None, lambda: None)
    transport = Transport()
    connection.connection_made(transport)
    with pytest.raises(PermissionError):
        await connection.send_command(command, b"credentials")
    assert transport.frames == []


async def test_sender_has_no_wire_logging_and_supports_fragmented_response(caplog):
    connection = ReadOnlyConnection(lambda d: None, lambda: None)
    transport = Transport()
    connection.connection_made(transport)
    with caplog.at_level(logging.DEBUG):
        task = asyncio.create_task(connection.send_command(CMD.AUTHENTICATE, b"TOPSECRET"))
        await asyncio.sleep(0)
        for byte in b"\x01\x02\xfe\x01":
            connection.data_received(bytes([byte]))
        assert await task == b"\x01"
    assert "TOPSECRET" not in caplog.text
    assert b"TOPSECRET".hex() not in caplog.text


@pytest.mark.parametrize("frame", [b"\xff", b"\x04\xff\xff", b"\x01\x00", b"\x01\x01\xfc"])
async def test_malformed_frames_abort_without_exposing_data(frame):
    connection = ReadOnlyConnection(lambda d: None, lambda: None)
    transport = Transport()
    connection.connection_made(transport)
    connection.data_received(frame)
    assert transport.aborted


async def test_disconnect_resolves_pending_request():
    connection = ReadOnlyConnection(lambda d: None, lambda: None)
    connection.connection_made(Transport())
    task = asyncio.create_task(connection.send_command(CMD.WHAT_ARE_YOU))
    await asyncio.sleep(0)
    connection.connection_lost(None)
    with pytest.raises(PanelError):
        await task


async def test_pin_failure_sends_no_protocol_bytes(monkeypatch):
    transport = Transport()

    async def create(factory, *args, **kwargs):
        connection = factory()
        connection.connection_made(transport)
        return transport, connection

    monkeypatch.setattr(asyncio.get_running_loop(), "create_connection", create)
    panel = adapter()
    with pytest.raises(PanelError, match="pin mismatch"):
        await panel.start()
    assert transport.aborted
    assert transport.frames == []
    assert panel.snapshot() == ()


@pytest.mark.parametrize("serial", [123, 0, None])
async def test_verified_pin_precedes_auth_and_snapshot(monkeypatch, serial):
    transport = Transport()
    observed = []

    async def create(factory, *args, **kwargs):
        connection = factory()
        connection.connection_made(transport)
        return transport, connection

    async def basic(panel):
        observed.append("basic")
        panel.model = PANEL_MODELS[0xA7]

    async def auth(panel):
        observed.append("auth")

    async def load(panel, selector):
        panel.serial_number = serial
        panel.points = {1: Point("Point", POINT_STATUS.NORMAL)}
        panel.areas = {1: Area("Area", AREA_STATUS.DISARMED)}
        panel._supports_status = False

    monkeypatch.setattr(asyncio.get_running_loop(), "create_connection", create)
    monkeypatch.setattr(_ReadOnlyPanel, "_basicinfo", basic)
    monkeypatch.setattr(_ReadOnlyPanel, "_authenticate", auth)
    monkeypatch.setattr(_ReadOnlyPanel, "load", load)
    monkeypatch.setattr(_ReadOnlyPanel, "_load_status", AsyncMock())
    events = []
    panel = PanelAdapter(
        "panel",
        7700,
        "secret",
        None,
        certificate_sha256=hashlib.sha256(b"test-certificate").hexdigest(),
        on_state=lambda s: None,
        on_availability=events.append,
    )
    if not serial:
        with pytest.raises(PanelError, match="serial identity"):
            await panel.start()
        assert events == []
        assert panel.snapshot() == ()
        assert transport.aborted
        return
    await panel.start()
    assert observed == ["basic", "auth"]
    assert panel.info.serial == "123"
    assert len(panel.snapshot()) == 2
    assert events == [True]
    await panel.stop()
    assert panel.snapshot() == ()
    assert events == [True, False]


def test_entity_specific_dedup_and_readiness_unknown():
    states = []
    owner = adapter(on_state=states.append)
    panel = owner._panel = _ReadOnlyPanel(owner)
    panel._supports_status = False
    panel.areas = {1: Area("Area", AREA_STATUS.DISARMED)}
    panel.points = {i: Point(str(i), POINT_STATUS.NORMAL) for i in range(1, 1001)}
    panel.outputs = {1: Output("Output", 0)}
    owner._attach_observers()
    owner._available = True
    owner._refresh_all()
    assert states[0].attributes["ready_away"] is None
    states.clear()
    panel.points[500].status = POINT_STATUS.OPEN[0]
    assert len(states) == 1
    assert states[0].id == "500"
    panel.points[500].status = POINT_STATUS.OPEN[0]
    assert len(states) == 1
    panel._area_ready_consumer(bytearray(b"\x00\x01\x02\x00\x03"))
    assert states[-1].attributes["ready_away"] is True
    assert states[-1].attributes["faulting_points"] == 3
    panel._on_disconnect()
    assert owner.snapshot() == ()
    assert owner._ready_areas == set()


def test_no_public_action_surface_and_explicit_allowlist():
    owner = adapter()
    assert not any(
        hasattr(owner, name)
        for name in (
            "area_disarm",
            "area_arm_all",
            "set_output_active",
            "door_unlock",
            "set_panel_date",
        )
    )
    assert len(ALLOWED_COMMANDS) == 20
    with pytest.raises(ValueError):
        PanelAdapter(
            "host", 7700, "code", None, on_state=lambda s: None, on_availability=lambda s: None
        )


async def test_refresh_event_burst_has_bounded_tasks_and_reruns_latest():
    owner = adapter()
    gate = asyncio.Event()
    calls = 0

    async def refresh():
        nonlocal calls
        calls += 1
        await gate.wait()

    owner._schedule_refresh("outputs", refresh)
    await asyncio.sleep(0)
    for _ in range(10000):
        owner._schedule_refresh("outputs", refresh)
    assert len(owner._tasks) == 1
    gate.set()
    await asyncio.gather(*owner._tasks)
    assert calls == 2
    assert owner._refresh_tasks == {}
    await owner.stop()


async def test_short_subscription_closes_cleanly():
    owner = adapter()
    panel = owner._panel = _ReadOnlyPanel(owner)
    connection = ReadOnlyConnection(panel._on_status_update, panel._on_disconnect)
    transport = Transport()
    connection.connection_made(transport)
    connection.data_received(b"\x02\x00\x01\x07")
    assert transport.aborted


async def test_subscription_header_can_be_split_at_every_byte():
    received = []
    connection = ReadOnlyConnection(received.append, lambda: None)
    transport = Transport()
    connection.connection_made(transport)
    for byte in b"\x02\x00\x02\x00\x00":
        connection.data_received(bytes([byte]))
    assert received == [b"\x00\x00"]
    assert not transport.aborted


async def test_command_timeout_aborts_entire_connection():
    owner = adapter(timeout=0.001)
    panel = owner._panel = _ReadOnlyPanel(owner)
    connection = panel._connection = ReadOnlyConnection(lambda d: None, lambda: None)
    transport = Transport()
    connection.connection_made(transport)
    with pytest.raises(TimeoutError):
        await panel._send_command(CMD.WHAT_ARE_YOU)
    assert transport.aborted
    assert not connection._pending


async def test_alarm_clear_during_inflight_detail_is_reconciled():
    from bosch_alarm_mode2.const import ALARM_MEMORY_PRIORITIES

    owner = adapter()
    panel = owner._panel = _ReadOnlyPanel(owner)
    area = panel.areas[1] = Area("Area", AREA_STATUS.ALL_ARMED[0])
    priority = ALARM_MEMORY_PRIORITIES.BURGLARY_ALARM
    gate = asyncio.Event()

    async def details(priority):
        await gate.wait()
        area._set_alarm(priority, True)

    panel._get_alarms_for_priority = details
    panel._event_summary_consumer(bytearray([priority, 0, 1]))
    await asyncio.sleep(0)
    panel._event_summary_consumer(bytearray([priority, 0, 0]))
    gate.set()
    await asyncio.gather(*owner._tasks)
    assert priority not in area.alarms_ids
    await owner.stop()


@pytest.mark.parametrize(
    "code,response",
    [
        (CMD.PRODUCT_SERIAL, b""),
        (CMD.PRODUCT_SERIAL, b"12345"),
        (CMD.REQUEST_PANEL_SYSTEM_STATUS, b"123456"),
        (CMD.WHAT_ARE_YOU, b"123"),
        (CMD.ALARM_MEMORY_SUMMARY, b"\x00" * 19),
        (CMD.OUTPUT_STATUS, b""),
        (CMD.ALARM_MEMORY_DETAIL, b"1234"),
    ],
)
async def test_truncated_reads_abort_instead_of_becoming_zero(code, response):
    owner = adapter()
    panel = owner._panel = _ReadOnlyPanel(owner)
    connection = panel._connection = ReadOnlyConnection(lambda d: None, lambda: None)
    transport = Transport()
    connection.connection_made(transport)
    task = asyncio.create_task(panel._send_command(code))
    await asyncio.sleep(0)
    connection.data_received(bytes([1, len(response) + 1, 0xFE]) + response)
    with pytest.raises(PanelError, match="Incomplete"):
        await task
    assert transport.aborted


@pytest.mark.parametrize(
    "response",
    [b"", b"\x00\x01", b"\x00\x01\x03", b"\x00\x01\x03\x00\x01\x03", b"\x00\x01\x03\x00\x03\x03"],
)
def test_partial_or_duplicate_point_status_is_rejected(response):
    from bosch_mqtt_gateway.panel import _validate_read_response

    with pytest.raises(PanelError):
        _validate_read_response(CMD.POINT_STATUS, b"\x00\x01\x00\x02", response)


def test_complete_reordered_status_and_empty_terminators_remain_valid():
    from bosch_mqtt_gateway.panel import _validate_read_response

    _validate_read_response(CMD.POINT_STATUS, b"\x00\x01\x00\x02", b"\x00\x02\x02\x00\x01\x03")
    for command in (
        CMD.REQUEST_CONFIGURED_POINTS,
        CMD.POINT_TEXT,
        CMD.ALARM_MEMORY_DETAIL,
        CMD.SET_SUBSCRIPTION,
        CMD.LOGIN_REMOTE_USER,
    ):
        _validate_read_response(command, b"", b"")
