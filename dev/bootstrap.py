"""Create disposable local credentials and TLS identities; no production secrets."""

import base64
import hashlib
import os
import secrets
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

ROOT = Path(__file__).resolve().parent / "generated"
ROOT.mkdir(mode=0o700, exist_ok=True)
os.umask(0o077)
if (ROOT / "gateway.toml").exists() and "--renew-certificates" not in sys.argv:
    raise SystemExit(
        "Already bootstrapped; remove dev/generated explicitly to reset disposable credentials."
    )


def put(name, value):
    if (ROOT / name).exists():
        (ROOT / name).chmod(0o600)
    (ROOT / name).write_bytes(value.encode() if isinstance(value, str) else value)


for name in [
    "panel_code",
    "mqtt_gateway_password",
    "mqtt_ha_password",
    "debug_token",
    "ha_password",
]:
    if not (ROOT / name).exists():
        put(name, secrets.token_hex(24))
key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Bosch disposable development CA")])
now = datetime.now(UTC)
ca = (
    x509.CertificateBuilder()
    .subject_name(name)
    .issuer_name(name)
    .public_key(key.public_key())
    .serial_number(x509.random_serial_number())
    .not_valid_before(now - timedelta(minutes=5))
    .not_valid_after(now + timedelta(days=30))
    .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
    .add_extension(
        x509.KeyUsage(
            digital_signature=True,
            content_commitment=False,
            key_encipherment=False,
            data_encipherment=False,
            key_agreement=False,
            key_cert_sign=True,
            crl_sign=True,
            encipher_only=None,
            decipher_only=None,
        ),
        critical=True,
    )
    .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
    .sign(key, hashes.SHA256())
)
put("ca.crt", ca.public_bytes(serialization.Encoding.PEM))
for hostname in ["mqtt", "simulator"]:
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)]))
        .issuer_name(name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(hostname), x509.DNSName("localhost")]),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    put(hostname + ".crt", leaf.public_bytes(serialization.Encoding.PEM))
    put(
        hostname + ".key",
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
# Mosquitto PBKDF2-SHA512 password file format ($7$).
lines = []
for user, filename in [("gateway", "mqtt_gateway_password"), ("homeassistant", "mqtt_ha_password")]:
    salt = secrets.token_bytes(12)
    digest = hashlib.pbkdf2_hmac("sha512", (ROOT / filename).read_bytes(), salt, 101000)
    lines.append(
        f"{user}:$7$101000${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"
    )
put("passwords", "\n".join(lines) + "\n")
put(
    "acl",
    """user gateway
topic write bosch/dev/state/#
topic write bosch/dev/availability
topic write bosch/dev/panel_availability
topic write homeassistant/+/bosch_dev/+/config
user homeassistant
topic read bosch/dev/#
topic read homeassistant/#
topic write homeassistant/status
""",
)
put(
    "mosquitto.conf",
    """listener 8883
allow_anonymous false
password_file /run/secrets/passwords
acl_file /run/secrets/acl
cafile /run/secrets/ca.crt
certfile /run/secrets/mqtt.crt
keyfile /run/secrets/mqtt.key
tls_version tlsv1.2
persistence false
log_dest stdout
""",
)
put(
    "gateway.toml",
    """[gateway]
id = "bosch_dev"
topic_prefix = "bosch/dev"
state_dir = "/var/lib/bosch-mqtt-gateway"
[panel]
host = "simulator"
port = 7700
automation_code_file = "/run/secrets/panel_code"
ca_file = "/run/secrets/ca.crt"
server_name = "simulator"
[mqtt]
host = "mqtt"
port = 8883
username = "gateway"
password_file = "/run/secrets/mqtt_gateway_password"
ca_file = "/run/secrets/ca.crt"
[http]
host = "0.0.0.0"
port = 8080
token_file = "/run/secrets/debug_token"
""",
)
# Docker Desktop bind mounts preserve host permissions but container UID differs.
# Parent directory remains 0700 on host; isolated containers receive read-only mount.
for path in ROOT.iterdir():
    path.chmod(0o600 if path.name.startswith("ha_") else 0o444)
print("Generated disposable development certificates and credentials (valid for 30 days).")
