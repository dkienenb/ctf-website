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

GRANT_TTL_MS = 5 * 60 * 1000
GUAC_ORIGIN = "/guacamole/"
ZERO_IV = b"\x00" * 16


class GrantError(Exception):
    pass


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


def issue_grant(user, lab, *, vnc_host="host.docker.internal", vnc_port="5900"):
    """A grant valid five minutes, permitting only this lab's attacker desktop."""
    payload = {
        "username": user.discord_id,
        "expires": int(time.time() * 1000) + GRANT_TTL_MS,
        "connections": {
            f"lab-{lab.id}": {
                "id": f"lab-{lab.id}",
                "protocol": "vnc",
                "parameters": {"hostname": vnc_host, "port": vnc_port},
            }
        },
    }
    key = _json_secret()
    data = base64.b64encode(_sign_and_encrypt(json.dumps(payload).encode(), key)).decode()
    return f"{GUAC_ORIGIN}?data={quote(data)}"
