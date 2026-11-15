"""Signed AND encrypted Guacamole grants (handoff §16): short-lived, scoped to
exactly one connection — the player's own active lab attacker desktop.

Implements the guacamole-auth-json format exactly:
1. JSON payload (username, expires, connections)
2. Sign with HMAC/SHA-256 using the shared 128-bit key; prepend the
   signature to the plaintext JSON
3. Encrypt with AES-128-CBC, IV = all zero bytes, PKCS7 padding
4. Base64-encode; submit to Guacamole as the `data` query parameter

The portal never issues anything beyond the active lab's VNC endpoint;
players cannot supply hosts, ports, or protocols.
"""
import base64
import hashlib
import hmac
import json
import time
from urllib.parse import quote

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from django.conf import settings

from .services import vnc_port_for

GRANT_TTL_MS = 5 * 60 * 1000
GUAC_ORIGIN = "/guacamole/"
ZERO_IV = b"\x00" * 16


class GrantError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


def _json_secret():
    secret = settings.GUACAMOLE_JSON_SECRET
    if not secret:
        raise GrantError("Guacamole JSON secret is not configured.")
    try:
        key = bytes.fromhex(secret)
    except ValueError:
        raise GrantError("GUACAMOLE_JSON_SECRET must be 32 hex characters (128 bits).")
    if len(key) != 16:
        raise GrantError("GUACAMOLE_JSON_SECRET must be exactly 128 bits (32 hex characters).")
    return key


def _sign_and_encrypt(payload_bytes, key):
    signature = hmac.new(key, payload_bytes, hashlib.sha256).digest()
    padder = PKCS7(128).padder()
    padded = padder.update(signature + payload_bytes) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(ZERO_IV)).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def issue_grant(user, lab, *, vnc_port=None):
    """A grant valid five minutes, permitting only this lab's attacker desktop.

    Routes through the guacd instance co-located on the lab's workhorse
    (per-connection guacd parameters), and reaches the guest's VNC over that
    host's loopback. All destinations are server-derived from the lab record."""
    if not lab.host or not lab.host.management_ip:
        raise GrantError("The lab's workhorse has not reported its management address yet.")
    vnc_host = "127.0.0.1"
    vnc_port = str(vnc_port if vnc_port is not None else vnc_port_for(lab))
    payload = {
        "username": user.discord_id,
        "expires": int(time.time() * 1000) + GRANT_TTL_MS,
        "connections": {
            f"lab-{lab.id}": {
                "id": f"lab-{lab.id}",
                "protocol": "vnc",
                "parameters": {
                    "hostname": vnc_host,
                    "port": vnc_port,
                    "guacd-hostname": lab.host.management_ip,
                    "guacd-port": "4822",
                    "guacd-encryption": "",
                },
            }
        },
    }
    key = _json_secret()
    data = base64.b64encode(_sign_and_encrypt(json.dumps(payload).encode(), key)).decode()
    # Deep-link into the client for this connection; the SPA consumes the
    # `data` parameter on any route and resolves the connection across the
    # user's data sources.
    connection_id = f"lab-{lab.id}"
    return f"{GUAC_ORIGIN}?data={quote(data)}#/client/{connection_id}"
