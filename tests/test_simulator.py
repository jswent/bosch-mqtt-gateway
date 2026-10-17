"""Exercise upstream Mode 2 client against actual synthetic TLS wire framing."""

import asyncio
import importlib.util
import ssl
from pathlib import Path

import pytest
from bosch_alarm_mode2 import panel as upstream
from bosch_alarm_mode2.const import CMD

spec = importlib.util.spec_from_file_location(
    "simulator", Path(__file__).parents[1] / "dev/simulator.py"
)
simulator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(simulator)


def test_bitmap_scale_and_action_denial():
    server = simulator.Simulator("synthetic")
    assert len(simulator.bitmap(600)) == 75
    assert sum(x.bit_count() for x in simulator.bitmap(600)) == 600
    for opcode in (CMD.AREA_ARM, CMD.SET_DOOR_STATE, CMD.SET_OUTPUT_STATE, CMD.SET_DATE_TIME):
        with pytest.raises(PermissionError):
            server.response(opcode, b"\0", True)
    with pytest.raises(PermissionError):
        server.response(CMD.POINT_STATUS, b"\0\1", False)


async def test_real_library_tls_600_points(tmp_path, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    client_context = ssl.create_default_context(cafile=str(cert_path))
    client_context.verify_flags &= ~ssl.VERIFY_X509_STRICT  # leaf used as test trust anchor
    monkeypatch.setattr(upstream, "ssl_context", client_context)
    sim = simulator.Simulator("synthetic")
    server = await asyncio.start_server(sim.handle, "127.0.0.1", 0, ssl=server_context)
    panel = upstream.Panel("localhost", server.sockets[0].getsockname()[1], "synthetic", None)
    try:
        await asyncio.wait_for(panel.connect(), 10)
        assert len(panel.points) == 600
        assert panel.serial_number == 123456789
        assert all(p.is_normal() for p in panel.points.values())
        writer = next(iter(sim.clients))
        writer.write(b"\x02\x00\x05\x07\x01\x02\x58\x02")
        await writer.drain()
        async with asyncio.timeout(2):
            while not panel.points[600].is_open():
                await asyncio.sleep(0.01)
    finally:
        await panel.disconnect()
        server.close()
        await server.wait_closed()
        for writer in tuple(sim.clients):
            writer.close()
