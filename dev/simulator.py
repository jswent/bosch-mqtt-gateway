"""Synthetic TLS Mode 2 wire server. Never connects to or controls real hardware."""

import argparse
import asyncio
import ssl
import time
from pathlib import Path

from aiohttp import web
from bosch_alarm_mode2.const import CMD


def bitmap(count):
    result = bytearray((count + 7) // 8)
    for i in range(count):
        result[i // 8] |= 1 << (7 - i % 8)
    return bytes(result)


class Simulator:
    def __init__(self, code, points=600):
        self.code = code
        self.points = {i: 3 for i in range(1, points + 1)}
        self.clients = set()
        self.commands = []
        self.server = None

    def response(self, cmd, data, authenticated):
        if cmd == CMD.WHAT_ARE_YOU:
            info = bytearray(56)
            info[0], info[5], info[6] = 0xA7, 3, 0
            info[23 + 13] = 0x04
            info[23 + 5] = 0x08
            info[23 + 16] = 0x20  # subscription format 1
            return bytes(info)
        if cmd == CMD.AUTHENTICATE:
            return bytes([int(data[1:].rstrip(b"\0").decode() == self.code)])
        if not authenticated:
            raise PermissionError
        if cmd == CMD.PRODUCT_SERIAL:
            return (123456789).to_bytes(6, "big")
        if cmd == CMD.REQUEST_PANEL_SYSTEM_STATUS:
            return b"\x03\x0e\0\0\0\0\0"
        if cmd == CMD.REQUEST_CONFIGURED_POINTS:
            return bitmap(len(self.points))
        if cmd == CMD.REQUEST_CONFIGURED_AREAS:
            return bitmap(4)
        if cmd == CMD.REQUEST_CONFIGURED_OUTPUTS:
            return bitmap(8)
        if cmd == CMD.POINT_STATUS:
            return b"".join(
                data[i : i + 2] + bytes([self.points[int.from_bytes(data[i : i + 2], "big")]])
                for i in range(0, len(data), 2)
            )
        if cmd == CMD.AREA_STATUS:
            return b"".join(data[i : i + 2] + b"\x04" for i in range(0, len(data), 2))
        if cmd == CMD.OUTPUT_STATUS:
            return b"\0"
        if cmd in (CMD.REQUEST_RAW_HISTORY_EVENTS, CMD.SET_SUBSCRIPTION):
            return b""
        raise PermissionError

    async def handle(self, reader, writer):
        self.clients.add(writer)
        authenticated = False
        try:
            while True:
                protocol = (await reader.readexactly(1))[0]
                if protocol not in (1, 4):
                    break
                length = int.from_bytes(await reader.readexactly(1 if protocol == 1 else 2), "big")
                request = await reader.readexactly(length)
                cmd, data = request[0], request[1:]
                self.commands.append(cmd)  # opcodes only, never credential payloads
                self.commands = self.commands[-10000:]
                try:
                    payload = self.response(cmd, data, authenticated)
                    if cmd == CMD.AUTHENTICATE:
                        authenticated = payload == b"\1"
                    payload = b"\xfe" + payload
                except (PermissionError, ValueError, KeyError):
                    payload = b"\xfd\x06"
                writer.write(
                    bytes([protocol])
                    + len(payload).to_bytes(1 if protocol == 1 else 2, "big")
                    + payload
                )
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self.clients.discard(writer)
            writer.close()

    async def event(self, request):
        data = await request.json()
        point, status = int(data["point"]), int(data["status"])
        if point not in self.points or status not in (1, 2, 3, 4, 5, 6):
            raise web.HTTPBadRequest()
        self.points[point] = status
        payload = b"\x07\x01" + point.to_bytes(2, "big") + bytes([status])
        timestamp = time.time_ns()
        for writer in tuple(self.clients):
            writer.write(b"\x02" + len(payload).to_bytes(2, "big") + payload)
            await writer.drain()
        return web.json_response({"emitted_ns": timestamp, "point": point, "status": status})

    async def fault(self, request):
        for writer in tuple(self.clients):
            if request.path == "/malformed":
                writer.write(b"\x09\x00\x00")
                await writer.drain()
            else:
                writer.close()
        return web.json_response({"ok": True})

    async def health(self, request):
        return web.json_response(
            {"points": len(self.points), "clients": len(self.clients), "commands": self.commands}
        )

    async def heartbeat(self):
        while True:
            await asyncio.sleep(5)
            for writer in tuple(self.clients):
                writer.write(b"\x02\x00\x02\x00\x00")

    async def run(self, cert, key, host="0.0.0.0", port=7700, control_port=8081):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(cert, key)
        self.server = await asyncio.start_server(self.handle, host, port, ssl=context)
        app = web.Application(client_max_size=4096)
        app.add_routes(
            [
                web.get("/health", self.health),
                web.post("/event", self.event),
                web.post("/disconnect", self.fault),
                web.post("/malformed", self.fault),
            ]
        )
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, host, control_port).start()
        await self.heartbeat()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--secrets", default="/run/secrets")
    args = parser.parse_args()
    path = Path(args.secrets)
    asyncio.run(
        Simulator((path / "panel_code").read_text().strip()).run(
            path / "simulator.crt", path / "simulator.key"
        )
    )
