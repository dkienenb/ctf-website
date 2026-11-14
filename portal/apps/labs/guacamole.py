"""Signed Guacamole grants (handoff §16): short-lived, scoped to exactly one
connection — the player's own active lab attacker desktop.

Uses the Apache guacamole-auth-json extension format: base64url(JSON payload)
+ '.' + base64url(HMAC-SHA256 over the payload), keyed by a 128-bit secret
shared with the Guacamole server via the JSON_SECRET_KEY environment. The
portal never issues anything beyond the active lab's VNC endpoint; players
cannot supply hosts, ports, or protocols.
"""
import base64
import hashlib
import hmac
import json
import time
from urllib.parse import quote

from django.conf import settings

GRANT_TTL_MS = 5 * 60 * 1000
GUAC_ORIGIN = "/guacamole/"


class GrantError(Exception):
    pass


def _json_secret():
    secret = settings.GUACAMOLE_JSON_SECRET
    if not secret:
        raise GrantError("Guacamole JSON secret is not configured.")
    try:
        return bytes.fromhex(secret)
    except ValueError:
        raise GrantError("GUACAMOLE_JSON_SECRET must be 32 hex characters (128 bits).")


def issue_grant(user, lab, *, vnc_host="host.docker.internal", vnc_port="5900"):
    """A grant valid five minutes, permitting only this lab's attacker desktop."""
    payload = {
        "username": user.discord_id,
        "expires": str(int(time.time() * 1000) + GRANT_TTL_MS),
        "connections": {
            f"lab-{lab.id}": {
                "id": f"lab-{lab.id}",
                "protocol": "vnc",
                "parameters": {"hostname": vnc_host, "port": vnc_port},
            }
        },
    }
    secret = _json_secret()
    data = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    signature = base64.urlsafe_b64encode(
        hmac.new(secret, data.encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    token = f"{data}.{signature}"
    return f"{GUAC_ORIGIN}?username={quote(user.discord_id)}&token={quote(token)}"
