"""Verified-TLS, read-only boundary around bosch-alarm-mode2 0.4.11.

Upstream decoding and entity discovery are reused, but its insecure dialer,
wire logging, automatic reconnect and control-command sender are never used.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import ssl
from collections.abc import Callable
from datetime import datetime
from typing import Any

from bosch_alarm_mode2.connection import Connection
from bosch_alarm_mode2.const import (
    ALARM_MEMORY_PRIORITIES as PRIORITIES,
)
from bosch_alarm_mode2.const import (
    ALARM_PANEL_FAULTS,
    AREA_STATUS,
    DOOR_STATUS,
    OUTPUT_STATUS,
    POINT_STATUS,
)
from bosch_alarm_mode2.panel import Area, Panel

from .models import EntityState, PanelInfo

# Fixed reviewed numeric values: reads, authentication and event subscription only.
# No arm/disarm, outputs, doors, clock writes or panel-action permission command.
ALLOWED_COMMANDS = frozenset(
    {
        0x01,
        0x06,
        0x3E,
        0x20,
        0x08,
        0x23,
        0x24,
        0x26,
        0x29,
        0x2B,
        0x2C,
        0x2E,
        0x30,
        0x31,
        0x33,
        0x35,
        0x38,
        0x3C,
        0x5F,
        0x4A,
    }
)


class PanelError(RuntimeError):
    """A safe error suitable for operational logs (never contains wire data)."""


class ReadOnlyConnection(Connection):
    """Frame boundary: deny unknown commands and never log frames or credentials."""

    async def send_command(self, code: int, data: bytes = b"") -> bytearray:
        if code not in ALLOWED_COMMANDS:
            raise PermissionError("Panel command denied by read-only policy")
        if not self._transport:
            raise PanelError("Panel connection is closed")
        async with self._command_semaphore:
            length_size = 2 if self.protocol == 4 else 1
            request = (
                bytes([self.protocol])
                + (len(data) + 1).to_bytes(length_size, "big")
                + bytes([code])
                + data
            )
            future = asyncio.get_running_loop().create_future()
            self._pending.append(future)
            self._transport.write(request)
            return await future

    def data_received(self, data: bytes) -> None:
        self._buffer.extend(data)
        try:
            while self._buffer:
                protocol = self._buffer[0]
                if protocol not in (1, 2, 4):
                    raise PanelError("Invalid panel frame")
                header = 2 if protocol == 1 else 3
                if len(self._buffer) < header:
                    return
                size = int.from_bytes(self._buffer[1:header], "big")
                if size < 1 or size > 16384:
                    raise PanelError("Invalid panel frame size")
                if len(self._buffer) < header + size:
                    return
                frame = self._buffer[header : header + size]
                del self._buffer[: header + size]
                if protocol == 2:
                    self._on_status_update(frame)
                else:
                    self._process_response(frame)
        except Exception:
            self.close()

    def _process_response(self, data: bytearray) -> None:
        if not self._pending:
            raise PanelError("Unexpected panel response")
        future = self._pending.popleft()
        if not self._pending:
            self._pending_last_empty = datetime.now()
        if future.done():
            raise PanelError("Expired panel response")
        if data[0] == 0xFC:
            future.set_result(bytearray())
        elif data[0] == 0xFE:
            future.set_result(data[1:])
        else:
            future.set_exception(PanelError("Panel rejected request"))

    def connection_lost(self, exc: Exception | None) -> None:
        self._transport = None
        self._fail_pending()
        self._on_disconnect()

    def _fail_pending(self) -> None:
        while self._pending:
            future = self._pending.popleft()
            if not future.done():
                future.set_exception(PanelError("Panel connection closed"))

    def close(self) -> None:
        self._fail_pending()
        super().close()


class _ReadOnlyPanel(Panel):
    def __init__(self, owner: PanelAdapter) -> None:
        super().__init__(owner._host, owner._port, owner._automation_code, owner._user_code)
        self.owner = owner
        self._alarm_read_lock = asyncio.Lock()
        self._alarm_event_counts: dict[int, int] = {}

    async def _send_command(self, code: int, data: bytes = b"") -> bytearray:
        try:
            async with asyncio.timeout(self.owner._timeout):
                result = await super()._send_command(code, data)
            self._last_msg = datetime.now()
            return result
        except BaseException:
            if self._connection:
                self._connection.close()
            raise

    async def _load_history(self) -> None:
        # History is not an exposed native HA entity and requires extra privileges.
        return

    def _area_on_off_finalizer(self) -> None:
        return

    def _output_status_finalizer(self) -> None:
        self.owner._schedule_refresh("outputs", self._load_output_status)

    def _event_history_finalizer(self) -> None:
        self.owner._schedule_refresh("faults", self._load_faults)

    def _event_summary_consumer(self, data: bytearray) -> int:
        priority = data[0]
        if priority not in PRIORITIES.TEXT:
            raise PanelError("Invalid alarm priority")
        # Reconcile every priority together; a clear event arriving while an
        # older detail read is in flight marks this operation dirty for a reread.
        self._alarm_event_counts[priority] = int.from_bytes(data[1:3], "big")
        self.owner._schedule_refresh("alarms", self._reconcile_alarm_events)
        return 3

    async def _reconcile_alarm_events(self) -> None:
        if self._alarm_summary_supported_format:
            await self._load_alarm_status()
            return
        # Older panels may provide event details without the summary query.
        async with self._alarm_read_lock:
            for priority, count in tuple(self._alarm_event_counts.items()):
                if count:
                    await self._get_alarms_for_priority(priority)
                else:
                    for area in self.areas.values():
                        area._set_alarm(priority, False)

    async def _load_alarm_status(self) -> None:
        async with self._alarm_read_lock:
            await super()._load_alarm_status()

    def _area_ready_consumer(self, data: bytearray) -> int:
        self.owner._ready_areas.add(int.from_bytes(data[:2], "big"))
        return super()._area_ready_consumer(data)

    def _on_disconnect(self) -> None:
        # Invalidate before upstream resets trigger entity observers.
        self.owner._set_available(False)
        super()._on_disconnect()


class PanelAdapter:
    """Read-only observations. Runtime owns retry and reconnect backoff."""

    def __init__(
        self,
        host: str,
        port: int,
        automation_code: str | None,
        user_code: str | None,
        *,
        ca_file: str | None = None,
        server_name: str | None = None,
        certificate_sha256: str | None = None,
        on_state: Callable[[EntityState], None],
        on_availability: Callable[[bool], None],
        timeout: float = 30,
        poll_interval: float = 30,
    ) -> None:
        if not ca_file and not certificate_sha256:
            raise ValueError("Panel TLS requires a CA file or SHA-256 certificate pin")
        pin = certificate_sha256.replace(":", "").lower() if certificate_sha256 else None
        if pin and (len(pin) != 64 or any(c not in "0123456789abcdef" for c in pin)):
            raise ValueError("Panel certificate pin must be SHA-256 hex")
        self._host, self._port = host, port
        self._automation_code, self._user_code = automation_code, user_code
        self._ca_file, self._server_name, self._pin = ca_file, server_name or host, pin
        self._on_state, self._on_availability = on_state, on_availability
        self._timeout, self._poll_interval = timeout, poll_interval
        self._panel: _ReadOnlyPanel | None = None
        self._available = False
        self._info: PanelInfo | None = None
        self._states: dict[tuple[str, str], EntityState] = {}
        self._ready_areas: set[int] = set()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._refresh_tasks: dict[str, asyncio.Task[Any]] = {}
        self._dirty_refresh: set[str] = set()
        # Upstream parser diagnostics can include entity details; disable at source.
        for name in (
            "bosch_alarm_mode2.panel",
            "bosch_alarm_mode2.connection",
            "bosch_alarm_mode2.history",
        ):
            logging.getLogger(name).disabled = True

    @property
    def info(self) -> PanelInfo:
        if self._info is None:
            raise PanelError("Panel identity is not ready")
        return self._info

    def snapshot(self) -> tuple[EntityState, ...]:
        return tuple(self._states.values()) if self._available else ()

    def _set_available(self, available: bool) -> None:
        if self._available != available:
            self._available = available
            if not available:
                self._states.clear()
                self._ready_areas.clear()
            self._on_availability(available)

    async def start(self) -> None:
        await self.stop()
        self._info = None
        self._panel = panel = _ReadOnlyPanel(self)
        context = (
            ssl.create_default_context(cafile=self._ca_file)
            if self._ca_file
            else ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        )
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        if not self._ca_file:
            # Pin is checked immediately after TLS handshake, before ANY Mode2 bytes.
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        transport = None
        try:
            async with asyncio.timeout(self._timeout):
                transport, connection = await asyncio.get_running_loop().create_connection(
                    lambda: ReadOnlyConnection(panel._on_status_update, panel._on_disconnect),
                    self._host,
                    self._port,
                    ssl=context,
                    server_hostname=self._server_name,
                    ssl_handshake_timeout=self._timeout,
                )
                if self._pin:
                    tls = transport.get_extra_info("ssl_object")
                    certificate = tls.getpeercert(binary_form=True) if tls else None
                    if not certificate or not hmac.compare_digest(
                        hashlib.sha256(certificate).hexdigest(), self._pin
                    ):
                        raise PanelError("Panel TLS certificate pin mismatch")
                panel._connection = connection
                await panel._basicinfo()
                await panel._authenticate()
                await panel.load(Panel.LOAD_EXTENDED_INFO | Panel.LOAD_ENTITIES)
                if panel.serial_number is None:
                    raise PanelError("Panel does not provide a serial identity")
                await panel._load_status()
                if panel._set_subscription_supported_format:
                    await panel._subscribe()
                if panel._connection is not connection:
                    raise PanelError("Panel disconnected while loading snapshot")
                self._info = PanelInfo(
                    str(panel.serial_number),
                    panel.model.name,
                    panel.firmware_version,
                    panel.protocol_version,
                )
                self._attach_observers()
                self._available = True
                self._refresh_all()
                self._on_availability(True)
            self._schedule(self._poll())
        except BaseException:
            if transport is not None:
                transport.abort()
            await self.stop()
            raise

    async def stop(self) -> None:
        self._set_available(False)
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._refresh_tasks.clear()
        self._dirty_refresh.clear()
        if self._panel and self._panel._connection:
            self._panel._connection.close()
        self._panel = None

    def _schedule(self, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _schedule_refresh(self, key: str, operation: Callable[[], Any]) -> None:
        if key in self._refresh_tasks:
            self._dirty_refresh.add(key)
            return

        async def refresh() -> None:
            try:
                while True:
                    self._dirty_refresh.discard(key)
                    await operation()
                    if key not in self._dirty_refresh:
                        break
            finally:
                self._refresh_tasks.pop(key, None)
                self._dirty_refresh.discard(key)

        task = asyncio.create_task(refresh())
        self._refresh_tasks[key] = task
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self._set_available(False)
            if self._panel and self._panel._connection:
                self._panel._connection.close()

    async def _poll(self) -> None:
        while self._available and self._panel:
            await asyncio.sleep(self._poll_interval)
            await self._panel._load_status()

    def _emit(self, entity: EntityState) -> None:
        if not self._available:
            return
        key = (entity.kind, entity.id)
        if self._states.get(key) != entity:
            self._states[key] = entity
            self._on_state(entity)

    def _attach_observers(self) -> None:
        assert self._panel
        for kind, entities in (
            ("area", self._panel.areas),
            ("point", self._panel.points),
            ("output", self._panel.outputs),
            ("door", self._panel.doors),
        ):
            for entity_id, entity in entities.items():

                def callback(k=kind, i=entity_id, e=entity):
                    self._emit_entity(k, i, e)

                entity.status_observer.attach(callback)
                if isinstance(entity, Area):
                    entity.ready_observer.attach(callback)
                    entity.alarm_observer.attach(callback)
        self._panel.faults_observer.attach(self._emit_faults)

    def _refresh_all(self) -> None:
        assert self._panel
        for kind, entities in (
            ("area", self._panel.areas),
            ("point", self._panel.points),
            ("output", self._panel.outputs),
            ("door", self._panel.doors),
        ):
            for entity_id, entity in entities.items():
                self._emit_entity(kind, entity_id, entity)
        self._emit_faults()

    def _emit_entity(self, kind: str, entity_id: int, entity: Any) -> None:
        table = {
            "area": AREA_STATUS.TEXT,
            "point": POINT_STATUS.TEXT,
            "output": OUTPUT_STATUS.TEXT,
            "door": DOOR_STATUS.TEXT,
        }[kind]
        attrs: dict[str, Any] = {
            "raw_status": entity.status,
            "status_text": table.get(entity.status, "Unknown"),
        }
        state: str | bool | None
        if kind == "area":
            state = "unknown"
            for predicate, value in (
                (entity.is_triggered, "triggered"),
                (entity.is_disarmed, "disarmed"),
                (entity.is_arming, "arming"),
                (entity.is_pending, "pending"),
                (entity.is_part_armed, "armed_home"),
                (entity.is_all_armed, "armed_away"),
            ):
                if predicate():
                    state = value
                    break
            if entity.status in (0x0A, 0x0B):
                state = "armed_home"
            ready = entity_id in self._ready_areas
            attrs.update(
                ready_away=entity.all_ready if ready else None,
                ready_home=(entity.all_ready or entity.part_ready) if ready else None,
                faulting_points=entity.faults if ready else None,
                alarms=tuple(entity.alarms),
                alarm_ids=tuple(sorted(entity.alarms_ids)),
            )
            for category in ("burglary", "gas", "fire"):
                attrs[f"alarms_{category}"] = next(
                    (
                        severity.lower()
                        for severity in ("SUPERVISORY", "TROUBLE", "ALARM")
                        if getattr(PRIORITIES, f"{category.upper()}_{severity}")
                        in entity.alarms_ids
                    ),
                    "no_issues",
                )
        elif kind == "point":
            state = None if entity.status == POINT_STATUS.UNKNOWN else entity.is_open()
            attrs["normal"] = entity.is_normal()
        elif kind == "output":
            state = None if entity.status == OUTPUT_STATUS.UNKNOWN else entity.is_active()
        else:
            state = None if entity.status == DOOR_STATUS.UNKNOWN else attrs["status_text"]
            attrs.update(
                open=entity.is_open(),
                locked=entity.is_locked(),
                secured=entity.is_secured(),
                cycling=entity.is_cycling(),
            )
        self._emit(EntityState(kind, str(entity_id), entity.name, state, attrs))

    def _emit_faults(self) -> None:
        assert self._panel
        if not self._panel._supports_status:
            return
        for mask, name in ALARM_PANEL_FAULTS.TEXT.items():
            self._emit(EntityState("fault", str(mask), name, mask in self._panel.panel_faults_ids))
